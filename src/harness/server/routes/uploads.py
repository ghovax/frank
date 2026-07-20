"""Uploads routes."""
from fastapi import APIRouter
from datetime import datetime
from datetime import timezone
from fastapi import File
from fastapi import HTTPException
from fastapi import UploadFile
from harness.core.configuration import harness_home_directory
from pathlib import Path
import asyncio
import hashlib
import mimetypes
from harness.server.models import (
    AttachmentReference,
)
from fastapi.responses import FileResponse
from harness.server import state

router = APIRouter()


@router.get("/a2a/files/{token}")
async def serve_a2a_file(token: str):
    """Stream a file authorized by a signed A2A file URL. The token binds the path, an
    audience, and an expiry, and is single-use — a valid link issued by this server resolves
    exactly once (``consume=True``), so it cannot be replayed."""
    signer = state._file_url_signer
    if signer is None:
        raise HTTPException(status_code=404, detail="File serving is unavailable.")
    file_path = signer.verify(token, consume=True)
    if not file_path or not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="File not found, link expired, or already used.")
    return FileResponse(file_path)

@router.post("/uploads")
async def upload_file(file: UploadFile = File(...)):
    """Store a user-provided file under Daisy's managed home and return generic file
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
    uploads_root = harness_home_directory() / "uploads"
    uploads_root.mkdir(parents=True, exist_ok=True)
    # Stream to a temp file while hashing, then atomically move it to its content-addressed
    # name once the digest is known. A dedup hit (target already present) drops the temp.
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
    """Register a user attachment **in place** — the file is referenced by its real
    local path, never copied into Daisy's home. Attachments are look-only (vision +
    local tool reads), so the original path is all any consumer needs; returns the
    same generic metadata shape as /uploads so the two paths are interchangeable to
    the client. Localhost-only, like the rest of the API."""
    path = Path(reference.path).expanduser()
    try:
        resolved = await asyncio.to_thread(path.resolve, strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="File not found.")
    if not await asyncio.to_thread(resolved.is_file):
        raise HTTPException(status_code=400, detail="Attachment must be a regular file.")
    name = resolved.name
    stat = await asyncio.to_thread(resolved.stat)
    mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    upload_id = f"ref-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    return {
        "upload_id": upload_id,
        "title": name,
        "filename": name,
        "path": str(resolved),
        "mime_type": mime_type,
        "size": stat.st_size,
        # In-place references carry no content digest — nothing is stored or deduped by
        # hash. The field stays present (empty) so the client's Attachment shape is uniform.
        "sha256": "",
    }
