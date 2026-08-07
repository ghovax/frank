"""Uploads routes."""

from __future__ import annotations
from fastapi import APIRouter, File, HTTPException, UploadFile
from datetime import datetime, timezone
from langmesh.base.paths import uploads_directory
from pathlib import Path
import asyncio
import hashlib
from langmesh.protocol.dtos import (
    AttachmentReference,
)
from langmesh.protocol.files import attachment_from_path
from fastapi.responses import FileResponse
from langmesh.commons import state

router = APIRouter()


@router.get("/a2a/files/{token}")
async def serve_a2a_file(token: str):
    """Stream a file authorized by a signed URL that binds path, audience and expiry, and resolves once."""
    signer = state.file_url_signer
    if signer is None:
        raise HTTPException(status_code=404, detail="File serving is unavailable.")
    file_path = signer.verify(token, consume=True)
    if not file_path or not Path(file_path).exists():
        raise HTTPException(
            status_code=404, detail="File not found, link expired, or already used."
        )
    return FileResponse(file_path)


@router.post("/uploads")
async def upload_file(file: UploadFile = File(...)):
    """Store a user file under LangMesh's managed home, content-addressed, and return its generic metadata."""
    raw_name = Path(file.filename or "upload").name
    suffix = Path(raw_name).suffix  # preserved so the stored file keeps a usable extension
    upload_id = f"upload-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    uploads_root = uploads_directory()
    uploads_root.mkdir(parents=True, exist_ok=True)
    # Stream to a temp file while hashing, then move it atomically once the digest is known.
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
    """Register an attachment in place: dragging a file names that file, where it lives, not a copy."""
    # One builder, shared with `langmesh.Session`, so the two front doors cannot describe a file differently.
    try:
        return await asyncio.to_thread(attachment_from_path, reference.path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found, or not a regular file.")
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Attachment could not be read.")


@router.get("/files/{file_path:path}")
async def serve_local_file(file_path: str):
    """Serve a file from local disk for the interface to display: the bytes, a guessed type, and nothing else."""
    path = Path("/" + file_path.lstrip("/")).resolve()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(path, headers={"Cache-Control": "no-store"})
