"""Artifacts service: helpers split out of the server runtime."""
from harness.server.models import ArtifactAnnotationSaveRequest

from datetime import datetime
from datetime import timezone
from fastapi import HTTPException
from harness.core import artifact_versioning as artifacts
from harness.core.sqlite_lock import sqlite_write_lock
from harness.locations.executor import LocationExecutor
from harness.locations.resolver import LocationAddress
from harness.locations.resolver import executor_for
from typing import cast
import asyncio
import base64
import json
import logging
import posixpath
import uuid
from harness.server import state
from harness.server.database import ArtifactAnnotationRecord, ArtifactFileRecord, ArtifactSurfaceRecord, ArtifactVersionRecord, SessionLifecycleRecord, SessionRecord
from harness.server.services.broadcast import _publish_broadcast
from harness.server.services.locations import _resolve_session_locations


_artifact_logger = logging.getLogger("harness.artifacts")


class _CaptureRequest:
    """One capture unit handed from the agent runtime to the background worker.

    ``mode`` is ``"track"`` to version specific paths (structured writes / ``open_artifact``)
    or ``"recheck"`` to restage only already-tracked files after a ``bash`` call. For
    ``track``, ``changed_absolute_paths`` are the exact files to version and
    ``original_contents`` maps an absolute path to its pre-edit bytes (so a first edit of a
    pre-existing file keeps its original). ``surface`` (open_artifact) may carry no path (an
    external URL) — then only a tab is recorded, no git history."""

    def __init__(
        self, *, context_id: str, location_uri: str, executor: LocationExecutor,
        base_directory: str, changed_absolute_paths: list[str] | None,
        mode: str = "track", original_contents: dict[str, str] | None = None,
        tool_call_id: str = "", message: str = "capture", surface: dict | None = None,
    ):
        self.context_id = context_id
        self.location_uri = location_uri
        self.executor = executor
        self.base_directory = base_directory
        self.changed_absolute_paths = changed_absolute_paths
        self.mode = mode
        self.original_contents = original_contents or {}
        self.tool_call_id = tool_call_id
        self.message = message
        self.surface = surface


def _artifact_maximum_bytes() -> int:
    """The per-file byte cap above which a write is recorded as a placeholder version."""
    workspace = getattr(state._global_configuration, "workspace", None) if state._global_configuration else None
    return int(getattr(workspace, "artifact_maximum_bytes", None) or artifacts.DEFAULT_MAXIMUM_BYTES)


def _capture_artifacts(
    *, context_id: str, location_uri: str, executor: LocationExecutor, base_directory: str,
    changed_absolute_paths: list[str] | None, mode: str = "track",
    original_contents: dict[str, str] | None = None, tool_call_id: str = "", message: str = "capture",
    surface: dict | None = None,
) -> None:
    """The callback injected into the agent runtime, called after a write-ish tool call.
    Non-blocking: build a request, enqueue, and return so the agent's turn never waits on
    git. Takes keyword arguments (not a request object) so the runtime stays decoupled from
    the server's internal request type."""
    if state._capture_queue is None:
        return
    request = _CaptureRequest(
        context_id=context_id, location_uri=location_uri, executor=executor,
        base_directory=base_directory, changed_absolute_paths=changed_absolute_paths,
        mode=mode, original_contents=original_contents,
        tool_call_id=tool_call_id, message=message, surface=surface,
    )
    if state._main_loop is not None and state._main_loop.is_running():
        state._main_loop.call_soon_threadsafe(state._capture_queue.put_nowait, request)
    else:
        try:
            state._capture_queue.put_nowait(request)
        except asyncio.QueueFull:
            _artifact_logger.warning("capture queue full; dropped a capture for %s", context_id)


async def _capture_worker() -> None:
    assert state._capture_queue is not None
    while True:
        request = await state._capture_queue.get()
        try:
            await asyncio.to_thread(_run_capture, request)
        except Exception:
            _artifact_logger.exception("artifact capture failed for %s", request.context_id)
        finally:
            state._capture_queue.task_done()


def _project_id_for_context(context_id: str) -> str:
    if state._session_factory is None:
        return ""
    session = state._session_factory()
    try:
        record = session.query(SessionRecord).filter(SessionRecord.id == context_id).first()
        return cast(str, record.project_id) if record is not None and record.project_id else ""
    finally:
        session.close()


def _within(path: str, base: str) -> bool:
    if not base:
        return False
    path_n, base_n = posixpath.normpath(path), posixpath.normpath(base)
    return path_n == base_n or path_n.startswith(base_n.rstrip("/") + "/")


def _group_work_trees(base_directory: str, changed_absolute_paths: list[str]) -> dict[str, list[str]]:
    """Map each work-tree to the changed rel paths under it. A path inside base_directory
    belongs to base_directory; anything else gets its own work-tree (its parent dir)."""
    groups: dict[str, list[str]] = {}
    for absolute_path in changed_absolute_paths:
        work_tree = base_directory if _within(absolute_path, base_directory) else posixpath.dirname(absolute_path)
        groups.setdefault(work_tree, []).append(posixpath.relpath(absolute_path, work_tree))
    return groups


def _run_capture(request: "_CaptureRequest") -> None:
    """Off-loop: version exactly the files this request names (never a folder survey),
    record the index rows, upsert any surface, and broadcast if anything changed."""
    project_id = _project_id_for_context(request.context_id)
    maximum_bytes = _artifact_maximum_bytes()
    location_home = request.executor.home_directory()
    changed_any = False

    if request.mode == "recheck":
        # After a bash call: restage only already-tracked files in the location's repo.
        work_tree = request.base_directory
        git_directory = artifacts.git_directory_for(location_home, project_id, work_tree)
        try:
            result = artifacts.recheck_tracked(
                request.executor, git_directory, work_tree, request.context_id,
                maximum_bytes=maximum_bytes, message=request.message,
            )
        except artifacts.VersionStoreError:
            _artifact_logger.exception("recheck failed (%s @ %s)", work_tree, request.location_uri)
            result = None
        if result is not None and result.files:
            _record_capture(request, project_id, git_directory, work_tree, result)
            changed_any = True
        if changed_any:
            _publish_broadcast({"type": "artifact_captured", "session_id": request.context_id})
        return

    # mode == "track": version each explicitly named path (grouped by its work-tree).
    for work_tree, relative_paths in _group_work_trees(request.base_directory, request.changed_absolute_paths or []).items():
        git_directory = artifacts.git_directory_for(location_home, project_id, work_tree)
        original_contents = {
            posixpath.relpath(absolute_path, work_tree): content
            for absolute_path, content in request.original_contents.items()
            if content is not None and _within(absolute_path, work_tree)
        }
        try:
            versions = artifacts.track_paths(
                request.executor, git_directory, work_tree, request.context_id, relative_paths,
                original_contents=original_contents or None, maximum_bytes=maximum_bytes, message=request.message,
            )
        except artifacts.VersionStoreError:
            _artifact_logger.exception("track failed (%s @ %s)", work_tree, request.location_uri)
            continue
        for version in versions:
            if version.files:
                _record_capture(request, project_id, git_directory, work_tree, version)
                changed_any = True

    if request.surface is not None:
        _upsert_surface(request, project_id, location_home)
        changed_any = True
    if changed_any:
        _publish_broadcast({"type": "artifact_captured", "session_id": request.context_id})


def _record_capture(request: "_CaptureRequest", project_id: str, git_directory: str, work_tree: str, result: "artifacts.CommitResult") -> None:
    assert state._session_factory is not None
    now = datetime.now(timezone.utc).isoformat()
    version_id = str(uuid.uuid4())
    with sqlite_write_lock():
        session = state._session_factory()
        try:
            session.add(ArtifactVersionRecord(
                id=version_id, context_id=request.context_id, project_id=project_id,
                location_uri=request.location_uri, git_directory=git_directory, work_tree=work_tree,
                branch=artifacts.branch_reference(request.context_id), commit_sha=result.commit_sha,
                sequence=result.sequence, message=request.message,
                tool_call_id=request.tool_call_id, created_at=now,
            ))
            for changed in result.files:
                session.add(ArtifactFileRecord(
                    id=str(uuid.uuid4()), version_id=version_id, context_id=request.context_id,
                    location_uri=request.location_uri, git_directory=git_directory, work_tree=work_tree,
                    commit_sha=result.commit_sha, relative_path=changed.relative_path, absolute_path=changed.absolute_path,
                    blob_sha=changed.blob_sha, change_type=changed.change_type, size=changed.size,
                    is_placeholder=changed.is_placeholder, created_at=now,
                ))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _upsert_surface(request: "_CaptureRequest", project_id: str, location_home: str) -> None:
    """Create/refresh the surface (tab) for an ``open_artifact``. Reuses an existing surface
    for the same ``(context, absolute_path)`` so re-opening a file updates one tab. For an
    external-URL artifact (no ``absolute_path``) there is no git history — only the live source."""
    assert state._session_factory is not None
    surface = request.surface or {}
    absolute_path = surface.get("absolute_path", "")
    git_directory = work_tree = relative_path = latest_commit = latest_blob = ""
    if absolute_path:
        work_tree = request.base_directory if _within(absolute_path, request.base_directory) else posixpath.dirname(absolute_path)
        git_directory = artifacts.git_directory_for(location_home, project_id, work_tree)
        relative_path = posixpath.relpath(absolute_path, work_tree)
        # The tracking capture already ran for this path, so the file's latest version is
        # simply the branch head's blob for it.
        latest_commit = artifacts.resolve_reference(request.executor, git_directory, artifacts.branch_reference(request.context_id))
        if latest_commit:
            latest_blob = artifacts.blob_at(request.executor, git_directory, latest_commit, relative_path)
    now = datetime.now(timezone.utc).isoformat()
    requested_surface_id = surface.get("surface_id") or ""
    with sqlite_write_lock():
        session = state._session_factory()
        try:
            # The agent supplies a stable surface id (derived from the target), so a repeat
            # open of the same file/URL reuses one tab; key purely on that id.
            surface_id = requested_surface_id or f"artifact-{uuid.uuid4().hex[:16]}"
            existing = session.get(ArtifactSurfaceRecord, surface_id)
            if existing is None:
                existing = ArtifactSurfaceRecord(id=surface_id, context_id=request.context_id, created_at=now)
                session.add(existing)
            existing.location_uri = request.location_uri
            existing.git_directory = git_directory
            existing.work_tree = work_tree
            existing.relative_path = relative_path
            existing.absolute_path = absolute_path
            existing.kind = surface.get("kind", "image")
            existing.title = surface.get("title", "")
            existing.source = surface.get("source", "")
            existing.tool_call_id = request.tool_call_id
            existing.latest_commit_sha = latest_commit
            existing.latest_blob_sha = latest_blob
            existing.updated_at = now
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"}


def _kind_for_path(relative_path: str) -> str:
    suffix = posixpath.splitext(relative_path)[1].lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in (".html", ".htm", ".xhtml"):
        return "html"
    return "file"


def _annotation_counts_by_version(database_session, context_id: str) -> dict[str, int]:
    """Pin counts keyed by version id (git commit sha)."""
    counts: dict[str, int] = {}
    for row in (
        database_session.query(ArtifactAnnotationRecord)
        .filter(ArtifactAnnotationRecord.context_id == context_id)
        .all()
    ):
        try:
            pins = json.loads(cast(str, row.annotations) or "[]")
        except json.JSONDecodeError:
            pins = []
        counts[cast(str, row.version_id)] = len(pins) if isinstance(pins, list) else 0
    return counts


def _artifact_index(context_id: str, scope: str = "session") -> list[dict]:
    """The file-history list: one entry per tracked ``(git_directory, relative_path)`` with its latest
    change and version count. ``scope='full'`` widens each file to its whole cross-session
    lineage (same location + relative_path), ``'session'`` shows only this session's versions."""
    if state._session_factory is None:
        return []
    database_session = state._session_factory()
    try:
        keys = {
            (cast(str, row.git_directory), cast(str, row.relative_path))
            for row in database_session.query(ArtifactFileRecord.git_directory, ArtifactFileRecord.relative_path)
            .filter(ArtifactFileRecord.context_id == context_id)
            .distinct()
        }
        surfaces = {
            (cast(str, surface.git_directory), cast(str, surface.relative_path)): surface
            for surface in database_session.query(ArtifactSurfaceRecord)
            .filter(ArtifactSurfaceRecord.context_id == context_id)
            .all()
            if surface.relative_path
        }
        items: list[dict] = []
        for git_directory, relative_path in keys:
            query = database_session.query(ArtifactFileRecord).filter(
                ArtifactFileRecord.git_directory == git_directory, ArtifactFileRecord.relative_path == relative_path
            )
            if scope != "full":
                query = query.filter(ArtifactFileRecord.context_id == context_id)
            rows = query.order_by(ArtifactFileRecord.created_at.asc()).all()
            if not rows:
                continue
            latest = rows[-1]
            surface = surfaces.get((git_directory, relative_path))
            items.append({
                "gitDirectory": git_directory,
                "relativePath": relative_path,
                "absolutePath": cast(str, latest.absolute_path),
                "locationUri": cast(str, latest.location_uri),
                "workTree": cast(str, latest.work_tree),
                "versionCount": len(rows),
                "latestCommit": cast(str, latest.commit_sha),
                "latestBlob": cast(str, latest.blob_sha),
                "latestChange": cast(str, latest.change_type),
                "size": cast(int, latest.size),
                "isPlaceholder": bool(latest.is_placeholder),
                "updatedAt": cast(str, latest.created_at),
                "surfaced": surface is not None,
                "kind": cast(str, surface.kind) if surface is not None else _kind_for_path(relative_path),
                "artifactId": cast(str, surface.id) if surface is not None else "",
                "title": (cast(str, surface.title) if surface is not None else "") or posixpath.basename(relative_path),
            })
        items.sort(key=lambda item: item["updatedAt"], reverse=True)
        return items
    finally:
        database_session.close()


def _artifact_versions(context_id: str, git_directory: str, relative_path: str, scope: str = "session") -> list[dict]:
    """Every captured version of one file, oldest → newest (what the filmstrip walks)."""
    if state._session_factory is None:
        return []
    database_session = state._session_factory()
    try:
        query = database_session.query(ArtifactFileRecord).filter(
            ArtifactFileRecord.git_directory == git_directory, ArtifactFileRecord.relative_path == relative_path
        )
        if scope != "full":
            query = query.filter(ArtifactFileRecord.context_id == context_id)
        rows = query.order_by(ArtifactFileRecord.created_at.asc()).all()
        version_ids = [cast(str, row.version_id) for row in rows]
        versions = {
            cast(str, version.id): version
            for version in database_session.query(ArtifactVersionRecord)
            .filter(ArtifactVersionRecord.id.in_(version_ids))
            .all()
        } if version_ids else {}
        annotation_counts = _annotation_counts_by_version(database_session, context_id)
        payload: list[dict] = []
        for row in rows:
            version = versions.get(cast(str, row.version_id))
            commit_sha = cast(str, row.commit_sha)
            payload.append({
                "versionId": commit_sha,  # the UI identity for a version is the commit sha
                "commitSha": commit_sha,
                "blobSha": cast(str, row.blob_sha),
                "sequence": cast(int, version.sequence) if version is not None else 0,
                "changeType": cast(str, row.change_type),
                "size": cast(int, row.size),
                "isPlaceholder": bool(row.is_placeholder),
                "createdAt": cast(str, row.created_at),
                "message": cast(str, version.message) if version is not None else "",
                "toolCallId": cast(str, version.tool_call_id) if version is not None else "",
                "gitDirectory": git_directory,
                "relativePath": relative_path,
                "locationUri": cast(str, row.location_uri),
                "workTree": cast(str, row.work_tree),
                "annotationCount": annotation_counts.get(commit_sha, 0),
            })
        payload.sort(key=lambda item: (item["sequence"], item["createdAt"]))
        return payload
    finally:
        database_session.close()


def _surface_records(context_id: str) -> list[dict]:
    """The surfaced artifacts (artifacts-panel tabs) for a session."""
    if state._session_factory is None:
        return []
    database_session = state._session_factory()
    try:
        rows = (
            database_session.query(ArtifactSurfaceRecord)
            .filter(ArtifactSurfaceRecord.context_id == context_id)
            .order_by(ArtifactSurfaceRecord.created_at.asc())
            .all()
        )
        return [{
            "artifactId": cast(str, row.id),
            "kind": cast(str, row.kind) or "image",
            "title": cast(str, row.title) or posixpath.basename(cast(str, row.relative_path) or cast(str, row.source)),
            "source": cast(str, row.source),
            "gitDirectory": cast(str, row.git_directory),
            "workTree": cast(str, row.work_tree),
            "relativePath": cast(str, row.relative_path),
            "absolutePath": cast(str, row.absolute_path),
            "locationUri": cast(str, row.location_uri),
            "latestCommit": cast(str, row.latest_commit_sha),
            "latestBlob": cast(str, row.latest_blob_sha),
            "toolCallId": cast(str, row.tool_call_id),
            "createdAt": cast(str, row.created_at),
            "updatedAt": cast(str, row.updated_at),
        } for row in rows]
    finally:
        database_session.close()


def _executor_for_location_uri(context_id: str, location_uri: str) -> "LocationExecutor | None":
    """Resolve the executor for one of a session's locations by URI (for serve/restore),
    rebuilding it from the session's location records so it works after a restart."""
    for entry in (_resolve_session_locations(context_id) or []):
        if entry.get("uri") == location_uri:
            address = LocationAddress(
                kind=entry.get("kind", "local"),
                base_directory=entry.get("base_directory", ""),
                host_alias=entry.get("host_alias", ""),
            )
            return executor_for(address)
    if not location_uri or location_uri.startswith("file://"):
        return executor_for(LocationAddress(kind="local", base_directory="", host_alias=""))
    return None


def _restore_artifact(context_id: str, location_uri: str, git_directory: str, work_tree: str, relative_path: str, commit_sha: str) -> None:
    """Restore ``relative_path`` to ``commit_sha`` (append-only), then re-index the new versions."""
    executor = _executor_for_location_uri(context_id, location_uri)
    if executor is None:
        raise HTTPException(status_code=404, detail="Location is unavailable for restore.")
    maximum_bytes = _artifact_maximum_bytes()
    project_id = _project_id_for_context(context_id)
    versions = artifacts.restore(
        executor, git_directory, work_tree, context_id, relative_path, commit_sha,
        maximum_bytes=maximum_bytes,
    )
    request = _CaptureRequest(
        context_id=context_id, location_uri=location_uri, executor=executor,
        base_directory=work_tree, changed_absolute_paths=[posixpath.join(work_tree, relative_path)],
        message=f"restore {relative_path}",
    )
    for version in versions:
        if version.files:
            _record_capture(request, project_id, git_directory, work_tree, version)
    _publish_broadcast({"type": "artifact_captured", "session_id": context_id})


def _artifact_annotation_payload(row: ArtifactAnnotationRecord, surface: "ArtifactSurfaceRecord | None" = None) -> dict:
    """Annotation record → the ``image`` identity the panel renders pins from. The identity
    is ``(surface_id, version_id=commit sha)``; ``source`` is the live file path (readable
    for the latest version's stamping / read_file)."""
    try:
        annotations = json.loads(cast(str, row.annotations) or "[]")
    except json.JSONDecodeError:
        annotations = []
    surface_id = cast(str, row.surface_id)
    version_id = cast(str, row.version_id)
    title = "Image artifact"
    name = ""
    source = ""
    if surface is not None:
        title = cast(str, surface.title) or title
        name = posixpath.basename(cast(str, surface.relative_path) or "")
        source = cast(str, surface.absolute_path) or ""
    return {
        "image": {
            "key": f"{surface_id}::{version_id}",
            "artifactId": surface_id,
            "versionId": version_id,
            "title": title,
            "name": name,
            "versionSeq": 0,
            "source": source,
        },
        "annotations": annotations if isinstance(annotations, list) else [],
        "updatedAt": cast(str, row.updated_at),
    }


def _artifact_annotation_records(context_id: str) -> list[dict]:
    if state._session_factory is None:
        return []
    database_session = state._session_factory()
    try:
        rows = (
            database_session.query(ArtifactAnnotationRecord)
            .filter(ArtifactAnnotationRecord.context_id == context_id)
            .order_by(ArtifactAnnotationRecord.updated_at.desc())
            .all()
        )
        surfaces = {
            cast(str, surface.id): surface
            for surface in database_session.query(ArtifactSurfaceRecord)
            .filter(ArtifactSurfaceRecord.context_id == context_id)
            .all()
        }
        return [_artifact_annotation_payload(row, surfaces.get(cast(str, row.surface_id))) for row in rows]
    finally:
        database_session.close()


def _save_artifact_annotation_record(context_id: str, request: "ArtifactAnnotationSaveRequest") -> dict:
    if state._session_factory is None:
        raise HTTPException(status_code=503, detail="Database is not ready.")
    surface_id = (request.surface_id or "").strip()
    version_id = (request.version_id or "").strip()
    if not surface_id or not version_id:
        raise HTTPException(status_code=400, detail="Annotation surface_id and version_id are required.")
    updated_at = request.updated_at or datetime.now(timezone.utc).isoformat()
    key = {"context_id": context_id, "surface_id": surface_id, "version_id": version_id}
    with sqlite_write_lock():
        database_session = state._session_factory()
        try:
            if database_session.get(SessionRecord, context_id) is None:
                raise HTTPException(status_code=404, detail="Session not found.")
            if not request.annotations:
                row = database_session.get(ArtifactAnnotationRecord, key)
                if row is not None:
                    database_session.delete(row)
                database_session.commit()
                return {"deleted": True}
            row = ArtifactAnnotationRecord(
                context_id=context_id,
                surface_id=surface_id,
                version_id=version_id,
                annotations=json.dumps(request.annotations),
                updated_at=updated_at,
            )
            database_session.merge(row)
            database_session.commit()
            surface = database_session.get(ArtifactSurfaceRecord, surface_id)
            return _artifact_annotation_payload(row, surface)
        except HTTPException:
            database_session.rollback()
            raise
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _delete_artifact_annotation_record(context_id: str, surface_id: str, version_id: str) -> bool:
    if state._session_factory is None:
        return False
    key = {"context_id": context_id, "surface_id": surface_id, "version_id": version_id}
    with sqlite_write_lock():
        database_session = state._session_factory()
        try:
            row = database_session.get(ArtifactAnnotationRecord, key)
            if row is None:
                return False
            database_session.delete(row)
            database_session.commit()
            return True
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


_ARTIFACT_CONTEXT_PREFIX = "@ctx="


def _decode_artifact_context(segment: str) -> tuple[str, str]:
    """Decode a ``@ctx=`` path segment into ``(session, location_uri)``; ("","") if it
    is malformed (falls back to local-disk serving)."""
    try:
        raw = segment[len(_ARTIFACT_CONTEXT_PREFIX):]
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        data = json.loads(decoded)
        return str(data.get("s", "")), str(data.get("l", ""))
    except (ValueError, TypeError):
        return "", ""


def _prune_session_artifacts(context_id: str) -> None:
    """Retention on session delete: drop this session's branch in every shadow repo it
    touched, then delete its artifact index rows (versions/files/surfaces/annotations) and
    its persisted conversation and lifecycle facts. Runs off the event loop. Best-effort
    per repo — a missing/unreachable location's branch is left, but its DB rows are still
    cleared."""
    if state._session_factory is None:
        return
    database_session = state._session_factory()
    try:
        repositories = {
            (cast(str, location_uri), cast(str, git_directory))
            for location_uri, git_directory in database_session.query(
                ArtifactVersionRecord.location_uri, ArtifactVersionRecord.git_directory
            ).filter(ArtifactVersionRecord.context_id == context_id).distinct()
        }
    finally:
        database_session.close()
    for location_uri, git_directory in repositories:
        executor = _executor_for_location_uri(context_id, location_uri)
        if executor is None:
            continue
        try:
            artifacts.prune_session(executor, git_directory, context_id)
        except Exception:
            _artifact_logger.exception("failed to prune session branch in %s", git_directory)
    with sqlite_write_lock():
        database_session = state._session_factory()
        try:
            for model in (
                ArtifactVersionRecord,
                ArtifactFileRecord,
                ArtifactSurfaceRecord,
                ArtifactAnnotationRecord,
                SessionLifecycleRecord,
            ):
                database_session.query(model).filter(model.context_id == context_id).delete(synchronize_session=False)
            database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()
