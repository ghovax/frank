"""A2A ``FilePart`` interchange over Frank's own HTTP.

Ingest materializes an inbound ``FilePart`` (base64 bytes, or a URI) into the
content-addressed upload store and returns the attachment dict the harness already
understands. Emit turns a stored file into a ``FilePart{FileWithUri}`` whose URI is a
short-lived JWT-signed link to the file-serving endpoint, so bytes are served on demand
and the link cannot be altered or outlive its window.
"""

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

# Ceiling on a single ingested file so a hostile or buggy peer cannot exhaust disk with one part; larger files are refused.
DEFAULT_MAXIMUM_FILE_BYTES = 50 * 1024 * 1024

# Lifetime of an emitted signed file URL.


# A file at or below this size is emitted inline as bytes rather than a URL, so a small attachment reaches the peer even if it cannot fetch back from this server.
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
    """The attachment record for a local file the user handed over, referenced in place.

    One implementation for the two front doors. The HTTP route serves the desktop app; the
    library calls it directly, because a program embedding this harness has no HTTP to post
    to and should not have to invent the record's shape to attach a file. Two spellings of one
    record is how the two drift, and the model reads whichever it is given.

    Raises ``FileNotFoundError`` when the path is not a regular file, which is the honest
    answer for something the caller named.
    """
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
        # Referenced in place, so there is nothing stored under a digest.
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
    """Materialize an inbound ``FilePart`` into the upload store and return its attachment
    dict, or ``None`` if it is too large or unfetchable. Bytes are decoded; a URI is fetched
    over http(s) only, and only after its host passes the anti-SSRF trust guard — a peer
    cannot make the server fetch an internal/loopback address. The body is streamed against
    the size ceiling and aborted the moment it is exceeded, so a hostile multi-GB response
    cannot exhaust memory before the cap is seen."""
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
        # Pin the connection to the verified IP so a rebind between the check above and the socket connect cannot swap in a private target — unless an egress proxy is configured, which does its own DNS/connect, so pinning to an IP would be wrong (and the resolve check already ran).
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
    """A path outside the servable root was handed to the signer — it will not be minted
    into a fetchable URL."""


# Who a signed file link is for.
_FILE_TOKEN_AUDIENCE = "urn:frank:a2a:file:v1"


class FileUrlSigner:
    """Mints and verifies short-lived signed URLs for the file-serving endpoint. The JWT
    binds the absolute file path, an audience, and an expiry, so a link cannot be altered to
    reach a different file or outlive its window. Signing is *scoped*: only paths under the
    servable root (the content-addressed upload store) can be minted into a URL, so an
    arbitrary absolute path (``/etc/passwd``, a user's in-place-referenced local file) can
    never be handed out — even though egress consent already gates the send, the signer
    imposes the boundary structurally. ``verify`` re-checks the root, so a token can never
    authorize a path outside it regardless of how it was produced."""

    def __init__(self, secret: bytes | str, base_url: str, allowed_root: Path | str | None = None, route: str = "/a2a/files"):
        self._secret = secret
        self._base_url = base_url.rstrip("/")
        self._route = route
        self._allowed_root = Path(allowed_root).resolve() if allowed_root is not None else None
        # jti -> expiry of tokens already redeemed, so a token is single-use within its window: once a peer fetches the file, that link is spent and a replay 404s.
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
        """The file path a token authorizes, or ``None`` if it is malformed, tampered, expired,
        wrong-audience, names a path outside the servable root, or (when ``consume``) has already
        been redeemed. ``consume=True`` marks the token spent, making the link single-use — the
        file-serving route passes it so a signed URL cannot be replayed."""
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
