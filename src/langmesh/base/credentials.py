"""ChatGPT-subscription authentication for the `chatgpt` model provider."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import secrets
import time
import urllib.parse
from dataclasses import asdict, dataclass
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import contextvars
from typing import Any, Optional

import httpx

from langmesh.base.paths import oauth_token_path
from langmesh.base.tuning import Tunable, active_tuning

# Codex's public OAuth client and endpoints, reused so the consent screen mints a subscription-scoped token.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"

# OpenAI registered this redirect for the Codex client; it is not ours to change.
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 1455
REDIRECT_PATH = "/auth/callback"
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}{REDIRECT_PATH}"
SCOPE = "openid profile email offline_access"

PROVIDER = "chatgpt"

# Serializes token refreshes, so concurrent turns noticing an expiry produce one refresh rather than a stampede.
_refresh_lock = asyncio.Lock()


@dataclass
class ChatGPTTokens:
    """The persisted result of a successful sign-in, including the account the calls bill against."""

    access_token: str
    refresh_token: str
    id_token: str
    account_id: str
    email: str
    expires_at: float

    def is_expired(
        self, leeway_seconds: float = active_tuning().duration(Tunable.credential_refresh_leeway)
    ) -> bool:
        return time.time() >= (self.expires_at - leeway_seconds)


def auth_file_path() -> Path:
    return oauth_token_path(PROVIDER)


class FileCredentials:
    """Tokens in a 0600 file under the user's data directory, which is the default store."""

    def load(self) -> Optional[ChatGPTTokens]:
        path = auth_file_path()
        if not path.exists():
            return None
        try:
            return ChatGPTTokens(**json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def save(self, tokens: ChatGPTTokens) -> None:
        path = auth_file_path()
        path.write_text(json.dumps(asdict(tokens), separators=(",", ":")))
        os.chmod(path, 0o600)

    def clear(self) -> None:
        auth_file_path().unlink(missing_ok=True)


# Which store the process uses, as a context variable so two sessions may hold different credentials.
_store: contextvars.ContextVar[Any] = contextvars.ContextVar("langmesh_credentials", default=None)


def set_credentials(store: Any) -> contextvars.Token:
    """Make `store` the credential store for this task. Pair with :func:`reset_credentials`."""
    return _store.set(store)


def reset_credentials(token: contextvars.Token) -> None:
    _store.reset(token)


def credentials() -> Any:
    """The bound credential store, or the file-backed default."""
    return _store.get() or _DEFAULT_STORE


_DEFAULT_STORE = FileCredentials()


def load_tokens() -> Optional[ChatGPTTokens]:
    """Load the stored tokens, or `None` when signed out; synchronous file IO."""
    return credentials().load()


def save_tokens(tokens: ChatGPTTokens) -> None:
    credentials().save(tokens)


def clear_tokens() -> None:
    credentials().clear()


def is_signed_in() -> bool:
    return load_tokens() is not None


# PKCE + JWT helpers.


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _generate_code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying it, reading only the account claims rather than trusting them."""
    try:
        _header, payload, _signature = token.split(".")
        return json.loads(_b64url_decode(payload))
    except (ValueError, TypeError):
        return {}


def _extract_account_id(claims: dict) -> str:
    """Read the account id from OpenAI's namespaced authentication claim."""
    auth = claims.get("https://api.openai.com/auth") or {}
    return str(auth.get("chatgpt_account_id") or "") if isinstance(auth, dict) else ""


def _tokens_from_payload(payload: dict, previous: Optional[ChatGPTTokens] = None) -> ChatGPTTokens:
    """Build a token set from a token-endpoint response, carrying over what a refresh may omit."""
    id_token = payload.get("id_token") or (previous.id_token if previous else "")
    claims = _decode_jwt_claims(id_token) if id_token else {}
    account_id = _extract_account_id(claims) or (previous.account_id if previous else "")
    email = claims.get("email") or (previous.email if previous else "")
    expires_in = float(payload.get("expires_in") or 3600)
    return ChatGPTTokens(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or (previous.refresh_token if previous else ""),
        id_token=id_token,
        account_id=account_id,
        email=email,
        expires_at=time.time() + expires_in,
    )


async def _exchange_code(code: str, code_verifier: str) -> ChatGPTTokens:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": code_verifier,
            },
        )
        response.raise_for_status()
        return _tokens_from_payload(response.json())


async def _refresh(tokens: ChatGPTTokens) -> ChatGPTTokens:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": CLIENT_ID,
                "scope": SCOPE,
            },
        )
        response.raise_for_status()
        return _tokens_from_payload(response.json(), previous=tokens)


async def valid_tokens() -> ChatGPTTokens:
    """A live, non-expired token set, refreshing and re-persisting first when the access token is near expiry."""
    tokens = await asyncio.to_thread(load_tokens)
    if tokens is None:
        raise ChatGPTAuthError(
            "Not signed in to ChatGPT. Run `langmesh auth login`, sign in from Settings, or drive `langmesh.base.credentials.ChatGPTLoginFlow` yourself."
        )
    if not tokens.is_expired():
        return tokens
    if not tokens.refresh_token:
        raise ChatGPTAuthError("ChatGPT session expired and cannot be refreshed. Sign in again.")
    async with _refresh_lock:
        # Re-load inside the lock: another turn may have refreshed while we waited.
        current = await asyncio.to_thread(load_tokens) or tokens
        if not current.is_expired():
            return current
        try:
            refreshed = await _refresh(current)
        except httpx.HTTPError as error:
            raise ChatGPTAuthError(f"Could not refresh the ChatGPT session: {error}") from error
        await asyncio.to_thread(save_tokens, refreshed)
        return refreshed


class ChatGPTAuthError(RuntimeError):
    """Raised when a ChatGPT-subscription call cannot be authenticated."""


# The page shown in the browser after the redirect, kept as a sibling asset rather than inline.
_CALLBACK_PAGE_PATH = Path(__file__).resolve().parent / "assets" / "chatgpt_callback.html"


@lru_cache(maxsize=1)
def _callback_template() -> str:
    return _CALLBACK_PAGE_PATH.read_text()


def _callback_page(message: str) -> str:
    """The little HTML page shown in the browser tab after the OAuth redirect."""
    return _callback_template().replace("{{message}}", html.escape(message))


class ChatGPTLoginFlow:
    """One browser sign-in, whose redirect lands on a loopback server this flow owns for its lifetime."""

    def __init__(self) -> None:
        self._code_verifier = _generate_code_verifier()
        self._state = secrets.token_urlsafe(24)
        self._server: Optional[HTTPServer] = None
        # Written by the callback handler, read by the serve loop (same thread).
        self._captured: dict[str, str] = {}

    @property
    def authorize_url(self) -> str:
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": _code_challenge(self._code_verifier),
            "code_challenge_method": "S256",
            # Codex-specific flags OpenAI's authorize endpoint expects.
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": self._state,
            # Names the client on the consent screen (opencode sends its own name here).
            "originator": "LangMesh",
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    async def start(self) -> None:
        """Bind the loopback callback server, raising when its port is already taken."""
        self._server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), self._build_handler())
        # Bound so the serve loop wakes periodically to honour the overall timeout.
        self._server.timeout = 0.5

    async def wait(self, timeout: float = 300.0) -> ChatGPTTokens:
        """Await the browser redirect, exchange the code, and persist the tokens, always closing the server."""
        assert self._server is not None, "start() must be called before wait()"
        try:
            code = await asyncio.to_thread(self._serve_until_callback, timeout)
            tokens = await _exchange_code(code, self._code_verifier)
            await asyncio.to_thread(save_tokens, tokens)
            return tokens
        finally:
            await self.close()

    def _serve_until_callback(self, timeout: float) -> str:
        """Handle requests on the loopback until the callback arrives, then return the authorization code."""
        assert self._server is not None
        deadline = time.monotonic() + timeout
        while not self._captured:
            if time.monotonic() >= deadline:
                raise ChatGPTAuthError("ChatGPT sign-in timed out. Try again.")
            self._server.handle_request()
        if "code" not in self._captured:
            raise ChatGPTAuthError(self._captured.get("error", "ChatGPT sign-in failed."))
        return self._captured["code"]

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        flow = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (stdlib signature)
                pass  # keep the loopback server off the harness logs

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != REDIRECT_PATH:
                    self._page(404, "Not found")  # stray request (favicon, etc.)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                failure = flow._callback_failure(query)
                if failure is not None:
                    flow._captured["error"] = failure
                    self._page(400, failure)
                    return
                flow._captured["code"] = query["code"][0]
                self._page(
                    200, "Signed in to ChatGPT. You can close this tab and return to LangMesh."
                )

            def _page(self, status: int, message: str) -> None:
                body = _callback_page(message).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return CallbackHandler

    def _callback_failure(self, query: dict[str, list[str]]) -> Optional[str]:
        """Why the callback is unusable, or `None` when it carries a valid, state-matched code."""
        if error := query.get("error", [""])[0]:
            return f"Authorization failed: {error}"
        if query.get("state", [""])[0] != self._state:
            return "State mismatch — sign-in aborted for safety."
        if not query.get("code", [""])[0]:
            return "No authorization code was returned."
        return None

    async def close(self) -> None:
        if self._server is not None:
            await asyncio.to_thread(self._server.server_close)
            self._server = None
