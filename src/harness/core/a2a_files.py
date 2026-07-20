"""A2A ``FilePart`` interchange over Daisy's own HTTP.

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
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx
import jwt

from a2a.types import FilePart, FileWithBytes, FileWithUri, Part

from harness.core.net_trust import UntrustedHostError, pin_to_ip, resolve_public_ips

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
        # Pin the connection to the verified IP so a rebind between the check above and the
        # socket connect cannot swap in a private target — unless an egress proxy is configured,
        # which does its own DNS/connect, so pinning to an IP would be wrong (and the resolve
        # check already ran). No proxy (the desktop app's normal case): pin.
        proxied = bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                       or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy"))
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


_FILE_TOKEN_AUDIENCE = "daisy-a2a-file"


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
        # jti -> expiry of tokens already redeemed, so a token is single-use within its window:
        # once a peer fetches the file, that link is spent and a replay 404s. Pruned by expiry, so
        # the set is bounded by the number of live (unexpired) tokens. (A retry after a dropped
        # connection must re-request a fresh link — acceptable for the one-shot A2A file handoff.)
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

    def sign(self, file_path: str, *, ttl_seconds: int = DEFAULT_URL_TTL_SECONDS) -> str:
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
    # Larger files are served by signed URL — but only from the content-addressed upload
    # store. A file outside it (an in-place-referenced local path) is not URL-served: the
    # signer refuses it, so an arbitrary filesystem path can never be handed to a peer.
    try:
        uri = signer.sign(path, ttl_seconds=ttl_seconds)
    except PathNotServableError:
        return None
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
