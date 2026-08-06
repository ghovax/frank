"""File interchange over Frank's own HTTP: ingesting an inbound file part, and signing a URL for an outbound one."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
from frank.base import environment_variables
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx
import jwt

from a2a.types import FilePart, FileWithBytes, FileWithUri

from frank.base.net_trust import UntrustedHostError, pin_to_ip, resolve_public_ips
from frank.base.tuning import Tunable, active_tuning

# Ceiling on a single ingested file, so one part cannot exhaust disk.
DEFAULT_MAXIMUM_FILE_BYTES = 50 * 1024 * 1024

# Lifetime of an emitted signed URL, kept short because a stale link can simply be re-issued.


# A small file is emitted inline as bytes, so it reaches the peer even if it cannot fetch back.
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


def attachment_from_path(path: Path | str) -> dict[str, Any]:
    """The attachment record for a local file the user handed over, referenced in place."""
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError(f"{resolved} is not a regular file.")
    name = resolved.name
    return {
        "upload_id": f"ref-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{os.urandom(4).hex()}",
        "title": name,
        "filename": name,
        "path": str(resolved),
        "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "size": resolved.stat().st_size,
        # Referenced in place, so nothing is stored under a digest, though the field stays present and empty.
        "sha256": "",
    }


async def ingest_file_part(
    part: FilePart,
    home_directory: Path,
    *,
    maximum_bytes: int = DEFAULT_MAXIMUM_FILE_BYTES,
    allow_private: bool = False,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[dict[str, Any]]:
    """Materialize an inbound file part into the upload store, or `None` when it is too large or unfetchable."""
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
        try:
            hostname, ips = resolve_public_ips(file.uri, allow_private=allow_private)
        except UntrustedHostError:
            return None
        # Pin the connection to the verified address, so a rebind cannot swap in a private target.
        proxied = bool(os.environ.get(environment_variables.HTTPS_PROXY) or os.environ.get("https_proxy")
                       or os.environ.get(environment_variables.ALL_PROXY) or os.environ.get("all_proxy"))
        if proxied or not ips:
            fetch_url, headers, extensions = file.uri, {}, {}
        else:
            fetch_url, headers, extensions = pin_to_ip(file.uri, ips[0], hostname)
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        try:
            raw = bytearray()
            async with client.stream("GET", fetch_url, headers=headers, extensions=extensions) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > maximum_bytes:
                        return None  # abort mid-stream; never buffer the whole hostile body
        except Exception:
            return None
        finally:
            if owns_client:
                await client.aclose()
        return _attachment(_store_bytes(bytes(raw), suffix, home_directory), name, mime_type, len(raw))

    return None


class PathNotServableError(Exception):
    """A path outside the servable root was handed to the signer."""


# Who a signed link is for, unique to this purpose so a token minted here is accepted nowhere else.
_FILE_TOKEN_AUDIENCE = "urn:frank:a2a:file:v1"


class FileUrlSigner:
    """Mints and verifies short-lived signed URLs, binding the path, the audience and the expiry."""

    def __init__(self, secret: bytes | str, base_url: str, allowed_root: Path | str | None = None, route: str = "/a2a/files"):
        self._secret = secret
        self._base_url = base_url.rstrip("/")
        self._route = route
        self._allowed_root = Path(allowed_root).resolve() if allowed_root is not None else None
        # Redeemed token ids and their expiries, so a link is single-use within its window.
        self._redeemed: dict[str, float] = {}

    def _within_root(self, file_path: str) -> bool:
        if self._allowed_root is None:
            return True
        try:
            Path(file_path).resolve().relative_to(self._allowed_root)
            return True
        except (ValueError, OSError):
            return False

    def can_sign(self, file_path: str) -> bool:
        """Whether ``file_path`` is under the servable root and can be URL-served."""
        return self._within_root(file_path)

    def sign(self, file_path: str, *, ttl_seconds: Optional[int] = None) -> str:
        ttl_seconds = ttl_seconds if ttl_seconds is not None else active_tuning().amount(Tunable.file_url_ttl_seconds)
        if not self._within_root(file_path):
            raise PathNotServableError(f"{file_path!r} is outside the servable file root")
        token = jwt.encode(
            {
                "path": file_path,
                "aud": _FILE_TOKEN_AUDIENCE,
                "jti": os.urandom(8).hex(),
                "exp": int(time.time()) + max(1, ttl_seconds),
            },
            self._secret,
            algorithm="HS256",
        )
        return f"{self._base_url}{self._route}/{quote(token, safe='')}"

    def verify(self, token: str, *, consume: bool = False) -> Optional[str]:
        """The file path a token authorizes, or `None` when it is malformed, expired, out of root or already spent."""
        try:
            payload = jwt.decode(token, self._secret, algorithms=["HS256"], audience=_FILE_TOKEN_AUDIENCE)
        except jwt.InvalidTokenError:
            return None
        path = payload.get("path")
        if not isinstance(path, str) or not self._within_root(path):
            return None
        if consume:
            now = time.time()
            self._redeemed = {jti: exp for jti, exp in self._redeemed.items() if exp > now}
            jti = payload.get("jti")
            expiry = float(payload.get("exp", now))
            if not isinstance(jti, str) or jti in self._redeemed:
                return None
            self._redeemed[jti] = expiry
        return path


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
