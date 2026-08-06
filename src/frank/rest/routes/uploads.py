"""Uploads routes."""

from __future__ import annotations
from fastapi import APIRouter, File, HTTPException, UploadFile
from datetime import datetime, timezone
from frank.base.paths import uploads_directory
from pathlib import Path
import asyncio
import hashlib
from frank.protocol.dtos import (
    AttachmentReference,
)
from frank.protocol.files import attachment_from_path
from fastapi.responses import FileResponse
from frank.hub import state

router = APIRouter()


@router.get("/a2a/files/{token}")
async def serve_a2a_file(token: str):
    """Stream a file authorized by a signed A2A file URL. The token binds the path, an
    audience, and an expiry, and is single-use — a valid link issued by this server resolves
    exactly once (``consume=True``), so it cannot be replayed."""
    signer = state.file_url_signer
    if signer is None:
        raise HTTPException(status_code=404, detail="File serving is unavailable.")
    file_path = signer.verify(token, consume=True)
    if not file_path or not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="File not found, link expired, or already used.")
    return FileResponse(file_path)

@router.post("/uploads")
async def upload_file(file: UploadFile = File(...)):
    """Store a user-provided file under Frank's managed home and return generic file
    metadata (path, name, mime, size, digest). This is a core, feature-agnostic
    attachment mechanism — it knows nothing about any particular skill's data model.

    Storage is content-addressed: the file lands at ``uploads/<sha256><ext>`` so that
    re-uploading identical bytes reuses the one stored file (dedup), and garbage
    collection can reason about a file purely by digest. ``upload_id`` stays a unique
    per-upload handle (the client keys pending attachments by it); the ``path`` is the
    shared content-addressed file."""
    raw_name = Path(file.filename or "upload").name
    suffix = Path(raw_name).suffix  # preserved so the stored file keeps a usable extension
    upload_id = f"upload-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    uploads_root = uploads_directory()
    uploads_root.mkdir(parents=True, exist_ok=True)
    # Stream to a temp file while hashing, then atomically move it to its content-addressed name once the digest is known.
    incoming_path = uploads_root / f".incoming-{upload_id}"
    digest = hashlib.sha256()
    size = 0
    try:
        with incoming_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
                handle.write(chunk)
    finally:
        await file.close()
    sha256 = digest.hexdigest()
    target_path = uploads_root / f"{sha256}{suffix}"
    if target_path.exists():
        incoming_path.unlink(missing_ok=True)
    else:
        incoming_path.replace(target_path)
    mime_type = file.content_type or "application/octet-stream"
    return {
        "upload_id": upload_id,
        "title": raw_name,
        "filename": raw_name,
        "path": str(target_path),
        "mime_type": mime_type,
        "size": size,
        "sha256": sha256,
    }


@router.post("/attachments/reference")
async def reference_attachment(reference: AttachmentReference):
    """Register a user attachment **in place** — the file is referenced by its real local
    path, never copied into Frank's home.

    A copy would be the wrong shape for what the user did. Dragging a file into the composer
    names *that* file, where it lives; it does not ask for a duplicate under a digest the
    person has never seen, and it does not ask for a snapshot that stops tracking a file they
    may still be editing.

    The file being somewhere the sandbox denies — `~/Downloads`, most of the time — is
    handled where it belongs, on the confinement: the session gains a read allowance for that
    one exact file. See :meth:`frank.base.confinement.Profile.with_attachments`.

    Returns the same metadata shape as /uploads so the two paths are interchangeable to the
    client. Localhost-only, like the rest of the API."""
    # One builder, shared with `frank.Session`.
    try:
        return await asyncio.to_thread(attachment_from_path, reference.path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found, or not a regular file.")
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Attachment could not be read.")


@router.get("/files/{file_path:path}")
async def serve_local_file(file_path: str):
    """Serve a file from local disk for the interface to display.

    This is what puts an image or a PDF the user attached in front of them: the composer
    and the transcript point an `<img>` or a PDF view here. It is deliberately plain — the
    bytes, their guessed media type, and nothing injected.

    Served no-store because an attachment is a live file the agent may still be editing, so
    a refresh should show the current bytes rather than a cached snapshot. Localhost-only,
    like the rest of this surface.
    """
    path = Path("/" + file_path.lstrip("/")).resolve()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(path, headers={"Cache-Control": "no-store"})
