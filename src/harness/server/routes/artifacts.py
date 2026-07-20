"""Artifacts routes (split from harness.server.runtime)."""
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from harness.core import artifact_versioning as artifacts
from pathlib import Path
import asyncio
import httpx
import mimetypes
import websockets
from harness.server.models import (
    ArtifactAnnotationSaveRequest,
    ArtifactRestoreRequest,
)
from harness.tools.tools import _inject_artifact_runtime
from harness.server.runtime import (
    _ARTIFACT_CONTEXT_PREFIX,
    _artifact_annotation_records,
    _artifact_index,
    _artifact_versions,
    _decode_artifact_context,
    _delete_artifact_annotation_record,
    _executor_for_location_uri,
    _restore_artifact,
    _save_artifact_annotation_record,
    _surface_records,
)
from harness.server.services.broadcast import _publish_broadcast
from harness.server.services.proxy import _PROXY_DROP_HEADERS, _get_proxy_client, _proxy_forward_headers, _rewrite_proxy_css, _rewrite_proxy_html, _rewrite_proxy_js

router = APIRouter()

@router.get("/sessions/{context_id}/artifacts")
async def get_artifacts(context_id: str, scope: str = "session"):
    """The file-history index for this session: every tracked file with its latest change
    and version count, plus which files are surfaced as artifact tabs. ``scope=full`` widens
    each file to its whole cross-session lineage."""
    return {
        "artifacts": await asyncio.to_thread(_artifact_index, context_id, scope),
        "surfaces": await asyncio.to_thread(_surface_records, context_id),
    }


@router.get("/sessions/{context_id}/artifacts/versions")
async def get_artifact_versions(context_id: str, git_directory: str, relative_path: str, scope: str = "session"):
    """Every captured version of one file (oldest → newest) — what the filmstrip walks."""
    return {"versions": await asyncio.to_thread(_artifact_versions, context_id, git_directory, relative_path, scope)}


@router.get("/sessions/{context_id}/artifacts/diff")
async def get_artifact_diff(context_id: str, git_directory: str, relative_path: str, from_commit: str, to_commit: str, location: str = ""):
    """Unified diff of a file between two versions (commit shas)."""
    def _diff() -> str:
        executor = _executor_for_location_uri(context_id, location)
        if executor is None:
            raise HTTPException(status_code=404, detail="Location is unavailable.")
        return artifacts.diff(executor, git_directory, relative_path, from_commit, to_commit)
    return {"diff": await asyncio.to_thread(_diff)}


@router.post("/sessions/{context_id}/artifacts/restore")
async def restore_artifact(context_id: str, request: "ArtifactRestoreRequest"):
    """Restore a file to a chosen version (append-only — the current on-disk state is
    snapshotted first, then the old bytes are written back and recorded as a new version)."""
    await asyncio.to_thread(
        _restore_artifact, context_id, request.location_uri, request.git_directory,
        request.work_tree, request.relative_path, request.commit_sha,
    )
    return {"status": "restored", "session_id": context_id}


@router.get("/artifact-bytes")
async def artifact_bytes(location: str, git_directory: str, sha: str, download: str = "", session: str = ""):
    """Stream the raw bytes of a version's blob straight from the shadow repo (local or
    remote via ``git cat-file``). Powers image rendering and the Download button. ``sha``
    is a git blob sha; ``download`` (a filename) forces an attachment disposition."""
    def _load() -> bytes:
        executor = _executor_for_location_uri(session, location) if session else _executor_for_location_uri("", location)
        if executor is None:
            raise HTTPException(status_code=404, detail="Location is unavailable.")
        try:
            return artifacts.read_blob(executor, git_directory, sha)
        except OSError as error:
            raise HTTPException(status_code=404, detail=f"Version not found: {error}")
    data = await asyncio.to_thread(_load)
    media_type, _ = mimetypes.guess_type(download or sha)
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{Path(download).name}"'
    return Response(content=data, media_type=media_type or "application/octet-stream", headers=headers)


@router.get("/sessions/{context_id}/artifact-annotations")
async def get_artifact_annotations(context_id: str):
    """Image annotations saved for this session, keyed by (surface, version)."""
    return {"records": await asyncio.to_thread(_artifact_annotation_records, context_id)}


@router.put("/sessions/{context_id}/artifact-annotations")
async def save_artifact_annotations(context_id: str, request: "ArtifactAnnotationSaveRequest"):
    """Create, update, or clear the annotation record for one artifact version."""
    record = await asyncio.to_thread(_save_artifact_annotation_record, context_id, request)
    _publish_broadcast({"type": "artifact_annotations_changed", "session_id": context_id})
    return record


@router.delete("/sessions/{context_id}/artifact-annotations")
async def delete_artifact_annotations(context_id: str, surface_id: str, version_id: str):
    """Delete the annotation record for one artifact version."""
    deleted = await asyncio.to_thread(_delete_artifact_annotation_record, context_id, surface_id, version_id)
    if deleted:
        _publish_broadcast({"type": "artifact_annotations_changed", "session_id": context_id})
    return {"deleted": deleted}


@router.get("/artifact-page/{file_path:path}")
async def artifact_page(file_path: str):
    """Serve a file for an ``open_artifact`` artifact (the UI points a sandboxed iframe
    here). HTML gets the artifact runtime injected so an opened page can self-size,
    report render errors, and be interactive; everything else (images, PDFs, CSS/JS
    assets a page references) is served verbatim so relative links inside an opened page
    resolve. The file may live on a remote location — an ``@ctx=`` first path segment
    names the session + location, and the file (and its assets) are read through that
    location's executor (ssh); without it the file is read off local disk. Opened
    artifacts are live files being iterated on, so everything is served no-store — a
    refresh reads the current bytes, which is what makes a remote preview live rather
    than a snapshot. Localhost-only, like the rest of the API."""
    session = ""
    location = ""
    first, _, rest = file_path.partition("/")
    if first.startswith(_ARTIFACT_CONTEXT_PREFIX):
        session, location = _decode_artifact_context(first)
        file_path = rest
    absolute_path = "/" + file_path.lstrip("/")
    suffix = Path(absolute_path).suffix.lower()
    is_html = suffix in (".html", ".htm", ".xhtml")
    no_store = {"Cache-Control": "no-store"}

    if location:
        # Remote (or an explicitly-addressed) location: read through its executor. Bytes,
        # not a FileResponse — there is no local path to stream.
        executor = _executor_for_location_uri(session, location)
        if executor is None:
            raise HTTPException(status_code=404, detail="Location is unavailable.")

        def _read_remote() -> bytes:
            if not executor.exists(absolute_path) or executor.is_directory(absolute_path):
                raise HTTPException(status_code=404, detail="File not found.")
            return executor.read_bytes(absolute_path)

        try:
            data = await asyncio.to_thread(_read_remote)
        except OSError as error:
            raise HTTPException(status_code=404, detail=f"Could not read file: {error}")
        if is_html:
            return HTMLResponse(_inject_artifact_runtime(data.decode("utf-8", errors="replace")), headers=no_store)
        media_type, _ = mimetypes.guess_type(absolute_path)
        return Response(content=data, media_type=media_type or "application/octet-stream", headers=no_store)

    # Local disk: the common, fast path.
    path = Path(absolute_path).resolve()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    if is_html:
        try:
            markup = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise HTTPException(status_code=400, detail=f"Could not read file: {error}")
        return HTMLResponse(_inject_artifact_runtime(markup), headers=no_store)
    return FileResponse(path, headers=no_store)


@router.websocket("/artifact-proxy-ws")
async def artifact_proxy_ws(client_socket: WebSocket):
    """Bridge a WebSocket opened by a proxied page to its real upstream server. The
    injected runtime rewrites ``new WebSocket(...)`` to point here (same-origin, so
    the browser allows it from the framed page); this relays frames both ways. Text
    and binary frames are forwarded verbatim."""
    target = client_socket.query_params.get("url", "")
    if not target.lower().startswith(("ws://", "wss://")):
        await client_socket.close(code=1008)
        return
    await client_socket.accept()
    try:
        async with websockets.connect(target, open_timeout=20) as upstream_socket:
            async def pump_client_to_upstream() -> None:
                while True:
                    message = await client_socket.receive()
                    if message.get("type") == "websocket.disconnect":
                        await upstream_socket.close()
                        return
                    if message.get("text") is not None:
                        await upstream_socket.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream_socket.send(message["bytes"])

            async def pump_upstream_to_client() -> None:
                async for frame in upstream_socket:
                    if isinstance(frame, (bytes, bytearray)):
                        await client_socket.send_bytes(bytes(frame))
                    else:
                        await client_socket.send_text(frame)

            client_pump = asyncio.create_task(pump_client_to_upstream())
            upstream_pump = asyncio.create_task(pump_upstream_to_client())
            done, pending = await asyncio.wait(
                {client_pump, upstream_pump}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
    except (WebSocketDisconnect, OSError, websockets.WebSocketException, asyncio.TimeoutError):
        # A dropped or refused upstream WebSocket ends the bridge quietly — the page
        # sees a closed socket, exactly as it would talking to the origin directly.
        pass
    finally:
        try:
            await client_socket.close()
        except RuntimeError:
            pass


@router.api_route("/artifact-proxy", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def artifact_proxy(url: str, request: Request):
    """Fetch an external ``http(s)`` URL server-side and re-serve it (and everything
    it links to) from our own origin, rewritten so the framed page behaves as if it
    were same-origin. This is what makes ``open_artifact`` a real mini-browser for
    sites that block direct framing.

    All HTTP methods and request bodies are forwarded, a shared cookie jar keeps
    session/consent state across requests, and HTML/CSS/JS bodies are rewritten so
    links, styles, and ES-module imports keep flowing through the proxy.
    Localhost-only, like the rest of the API."""
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Only http(s) URLs can be opened.")
    body = await request.body()
    try:
        upstream = await _get_proxy_client().request(
            request.method,
            url,
            content=body or None,
            headers=_proxy_forward_headers(request, url),
        )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"Could not load {url}: {error}")

    final_url = str(upstream.url)
    content_type = upstream.headers.get("content-type", "application/octet-stream")
    lowered = content_type.lower()
    status = upstream.status_code
    if "html" in lowered:
        return HTMLResponse(_rewrite_proxy_html(upstream.text, final_url), status_code=status)
    if "css" in lowered:
        return Response(_rewrite_proxy_css(upstream.text, final_url), media_type=content_type, status_code=status)
    if any(token in lowered for token in ("javascript", "ecmascript", "text/jsx")):
        return Response(_rewrite_proxy_js(upstream.text, final_url), media_type=content_type, status_code=status)
    # Images, fonts, JSON, media, … — re-serve verbatim from our origin (httpx has
    # already decoded any transfer encoding), minus the headers that no longer apply.
    safe_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _PROXY_DROP_HEADERS
    }
    return Response(content=upstream.content, media_type=content_type, headers=safe_headers, status_code=status)
