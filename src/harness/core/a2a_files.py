"""A2A ``FilePart`` interchange over Daisy's own HTTP.

Ingest materializes an inbound ``FilePart`` (base64 bytes, or a URI) into the
content-addressed upload store and returns the attachment dict the harness already
understands. Emit turns a stored file into a ``FilePart{FileWithUri}`` whose URI is a
short-lived JWT-signed link to the file-serving endpoint, so bytes are served on demand
and the link cannot be altered or outlive its window.
"""

import base64
import hashlib
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx
import jwt

from a2a.types import FilePart, FileWithBytes, FileWithUri, Part

# Ceiling on a single ingested file so a hostile or buggy peer cannot exhaust disk with one
# part; larger files are refused.
DEFAULT_MAXIMUM_FILE_BYTES = 50 * 1024 * 1024

# Lifetime of an emitted signed file URL. Short, because a peer fetches a referenced file
# promptly; a stale link 404s and can be re-issued.
DEFAULT_URL_TTL_SECONDS = 600

# A file at or below this size is emitted inline as bytes rather than a URL, so a small
# attachment reaches the peer even if it cannot fetch back from this server.
DEFAULT_INLINE_MAXIMUM_BYTES = 256 * 1024


def _uploads_root(home_directory: Path) -> Path:
    root = home_directory / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _store_bytes(raw: bytes, suffix: str, home_directory: Path) -> Path:
    """Content-address ``raw`` into the upload store (dedup by sha256), returning its path."""
    target = _uploads_root(home_directory) / f"{hashlib.sha256(raw).hexdigest()}{suffix}"
    if not target.exists():
        target.write_bytes(raw)
    return target


def _attachment(path: Path, name: str, mime_type: str, size: int) -> dict[str, Any]:
    return {
        "path": str(path),
        "title": name,
        "filename": name,
        "mime_type": mime_type or "application/octet-stream",
        "size": size,
        "sha256": path.stem,
    }


async def ingest_file_part(
    part: FilePart,
    home_directory: Path,
    *,
    maximum_bytes: int = DEFAULT_MAXIMUM_FILE_BYTES,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[dict[str, Any]]:
    """Materialize an inbound ``FilePart`` into the upload store and return its attachment
    dict, or ``None`` if it is too large or unfetchable. Bytes are decoded; a URI is fetched
    over http(s) only."""
    file = part.root.file if hasattr(part, "root") else part.file
    name = file.name or "file"
    suffix = Path(name).suffix
    mime_type = file.mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"

    if isinstance(file, FileWithBytes):
        try:
            raw = base64.b64decode(file.bytes)
        except Exception:
            return None
        if len(raw) > maximum_bytes:
            return None
        return _attachment(_store_bytes(raw, suffix, home_directory), name, mime_type, len(raw))

    if isinstance(file, FileWithUri):
        if urlparse(file.uri).scheme not in {"http", "https"}:
            return None
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        try:
            response = await client.get(file.uri)
            response.raise_for_status()
            raw = response.content
        except Exception:
            return None
        finally:
            if owns_client:
                await client.aclose()
        if len(raw) > maximum_bytes:
            return None
        return _attachment(_store_bytes(raw, suffix, home_directory), name, mime_type, len(raw))

    return None


class FileUrlSigner:
    """Mints and verifies short-lived signed URLs for the file-serving endpoint. The JWT
    binds the absolute file path and an expiry, so a link cannot be altered to reach a
    different file or outlive its window."""

    def __init__(self, secret: bytes | str, base_url: str, route: str = "/a2a/files"):
        self._secret = secret
        self._base_url = base_url.rstrip("/")
        self._route = route

    def sign(self, file_path: str, *, ttl_seconds: int = DEFAULT_URL_TTL_SECONDS) -> str:
        token = jwt.encode(
            {"path": file_path, "exp": int(time.time()) + max(1, ttl_seconds)},
            self._secret,
            algorithm="HS256",
        )
        return f"{self._base_url}{self._route}/{quote(token, safe='')}"

    def verify(self, token: str) -> Optional[str]:
        """The file path a token authorizes, or ``None`` if it is malformed, tampered, or
        expired."""
        try:
            payload = jwt.decode(token, self._secret, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            return None
        path = payload.get("path")
        return path if isinstance(path, str) else None


def build_file_part(
    attachment: dict[str, Any],
    signer: FileUrlSigner,
    *,
    ttl_seconds: int = DEFAULT_URL_TTL_SECONDS,
    inline_maximum_bytes: int = DEFAULT_INLINE_MAXIMUM_BYTES,
) -> Optional[Part]:
    """Turn a stored attachment into a ``FilePart``, or ``None`` if the file has no readable
    path. A small file is inlined as ``FileWithBytes``; a larger one is a ``FileWithUri``
    with a signed URL the peer fetches on demand."""
    path = str(attachment.get("path") or "")
    if not path:
        return None
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError:
        return None
    name = str(attachment.get("filename") or attachment.get("title") or file_path.name)
    mime_type = str(attachment.get("mime_type") or "application/octet-stream")
    if size <= inline_maximum_bytes:
        try:
            encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
        except OSError:
            return None
        return Part(root=FilePart(file=FileWithBytes(bytes=encoded, name=name, mime_type=mime_type)))
    uri = signer.sign(path, ttl_seconds=ttl_seconds)
    return Part(root=FilePart(file=FileWithUri(uri=uri, name=name, mime_type=mime_type)))


def load_or_create_secret(home_directory: Path) -> bytes:
    """A stable per-install signing secret, persisted so signed links survive a restart."""
    path = home_directory / "a2a_file_secret"
    if path.exists() and path.read_bytes():
        return path.read_bytes()
    secret = os.urandom(32)
    path.write_bytes(secret)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret
