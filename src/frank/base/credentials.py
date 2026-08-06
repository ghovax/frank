"""ChatGPT-subscription authentication for the ``chatgpt`` model provider.

This is the *experimental*, unofficial route that lets a ChatGPT Plus/Pro/Team
subscription pay for model calls instead of an API key. There is no official
OpenAI API for it — the only way is to present ourselves to OpenAI's OAuth as the
Codex CLI (its public ``client_id``) and then call the Codex-only endpoint
``chatgpt.com/backend-api/codex/responses`` (see :mod:`frank.runtime.models.codex`).

Consequences, kept deliberately visible:
  * It rides on Codex's public client id and the account-scoped tokens Codex
    itself mints. OpenAI can invalidate this pattern at any time (Anthropic banned
    the equivalent for Claude in Feb 2026); treat it as fragile, not stable.
  * The tokens here are password-equivalent. They are stored in the OAuth token
    directory as ``oauths/chatgpt.json`` (mode 0600), deliberately **outside**
    ``configuration.yaml`` — the config file is watched/digest-synced and would
    thrash on every silent token refresh, and secrets do not belong in the synced
    single-source-of-truth.

The OAuth flow is the standard authorization-code + PKCE dance. Because OpenAI
registered Codex's ``redirect_uri`` as ``http://localhost:1455/auth/callback``, we
must catch the browser redirect on that exact loopback address — so a short-lived
loopback server is spun up for the duration of one sign-in rather than reusing the
Frank server's own port.
"""

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

from frank.base.paths import oauth_token_path
from frank.base.tuning import Tunable, active_tuning

# Codex's public OAuth client and endpoints. The client id is not a secret — it is
# the same value the Codex CLI ships — and reusing it is exactly what makes the
# consent screen mint a ChatGPT-subscription-scoped token for us.
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

# Serializes token refreshes: many concurrent turns can each notice an expiring
# token at once, and we want exactly one refresh + write, not a stampede.
_refresh_lock = asyncio.Lock()


@dataclass
class ChatGPTTokens:
    """The persisted result of a successful sign-in. ``account_id`` is the
    ChatGPT account the calls bill against (sent as the ``ChatGPT-Account-Id``
    header); ``expires_at`` is epoch seconds for the access token."""

    access_token: str
    refresh_token: str
    id_token: str
    account_id: str
    email: str
    expires_at: float

    def is_expired(self, leeway_seconds: float = active_tuning().duration(Tunable.credential_refresh_leeway_seconds)) -> bool:
        return time.time() >= (self.expires_at - leeway_seconds)


def auth_file_path() -> Path:
    return oauth_token_path(PROVIDER)


class FileCredentials:
    """Tokens in a `0600` file under the user's data directory — the default store.

    Password-equivalent material, so the mode is not decoration. This is the right store for a
    person's machine and the wrong one for a program that keeps its secrets elsewhere, which is
    why it is now one implementation of :class:`~frank.base.ports.Credentials` rather than the
    only possibility."""

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


# Which store the process uses. A `ContextVar` rather than a module global for the reason every
# other per-session value in this tree is one: two sessions in one interpreter may legitimately
# hold different credentials, and the last caller must not win.
_store: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "frank_credentials", default=None
)


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
    """Load the stored tokens, or ``None`` when signed out. Synchronous file IO —
    call via ``asyncio.to_thread`` from the event loop."""
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
    """Decode a JWT payload without verifying the signature — we only read the
    account id / email claims OpenAI put there, we are not trusting them for auth."""
    try:
        _header, payload, _signature = token.split(".")
        return json.loads(_b64url_decode(payload))
    except (ValueError, TypeError):
        return {}


def _extract_account_id(claims: dict) -> str:
    """The ChatGPT account id lives under OpenAI's namespaced auth claim; fall back
    to a couple of shapes seen in the wild."""
    auth = claims.get("https://api.openai.com/auth") or {}
    if isinstance(auth, dict):
        account_id = auth.get("chatgpt_account_id") or auth.get("account_id")
        if account_id:
            return str(account_id)
    return str(claims.get("chatgpt_account_id") or "")


def _tokens_from_payload(payload: dict, previous: Optional[ChatGPTTokens] = None) -> ChatGPTTokens:
    """Build a :class:`ChatGPTTokens` from a token-endpoint response, carrying over
    fields a refresh response may omit (a refresh often returns no new id/refresh
    token)."""
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
        response = await client.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": code_verifier,
        })
        response.raise_for_status()
        return _tokens_from_payload(response.json())


async def _refresh(tokens: ChatGPTTokens) -> ChatGPTTokens:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": CLIENT_ID,
            "scope": SCOPE,
        })
        response.raise_for_status()
        return _tokens_from_payload(response.json(), previous=tokens)


async def valid_tokens() -> ChatGPTTokens:
    """Return a live, non-expired token set, refreshing and re-persisting first when
    the access token is at or near expiry. Raises ``ChatGPTAuthError`` when signed
    out or when a refresh fails (e.g. the subscription was revoked)."""
    tokens = await asyncio.to_thread(load_tokens)
    if tokens is None:
        raise ChatGPTAuthError(
            "Not signed in to ChatGPT. Run `frank auth login`, sign in from Settings, or "
            "drive `frank.base.credentials.ChatGPTLoginFlow` yourself."
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


# The HTML shown in the browser tab after the OAuth redirect, kept in a sibling
# asset (with a {{message}} placeholder) rather than inline in the code.
_CALLBACK_PAGE_PATH = Path(__file__).resolve().parent / "assets" / "chatgpt_callback.html"


@lru_cache(maxsize=1)
def _callback_template() -> str:
    return _CALLBACK_PAGE_PATH.read_text()


def _callback_page(message: str) -> str:
    """The little HTML page shown in the browser tab after the OAuth redirect."""
    return _callback_template().replace("{{message}}", html.escape(message))


class ChatGPTLoginFlow:
    """One browser sign-in. ``authorize_url`` is opened by the client; the redirect
    lands on a loopback server this flow owns for its lifetime, which exchanges the
    code, persists the tokens, and completes :meth:`wait`."""

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
            "originator": "Frank",
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    async def start(self) -> None:
        """Bind the loopback callback server. Raises ``OSError`` if port 1455 is
        already taken (e.g. a concurrent Codex or Frank sign-in)."""
        self._server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), self._build_handler())
        # Bound so the serve loop wakes periodically to honour the overall timeout.
        self._server.timeout = 0.5

    async def wait(self, timeout: float = 300.0) -> ChatGPTTokens:
        """Await the browser redirect, exchange the code, persist and return the
        tokens. The blocking one-shot HTTP server runs off the event loop; the token
        exchange runs on it. Always closes the loopback server afterwards."""
        assert self._server is not None, "start() must be called before wait()"
        try:
            code = await asyncio.to_thread(self._serve_until_callback, timeout)
            tokens = await _exchange_code(code, self._code_verifier)
            await asyncio.to_thread(save_tokens, tokens)
            return tokens
        finally:
            await self.close()

    def _serve_until_callback(self, timeout: float) -> str:
        """Handle requests on the loopback until the OAuth callback arrives (ignoring
        stray hits like ``/favicon.ico``), then return the authorization code. Runs in
        a worker thread; raises :class:`ChatGPTAuthError` on timeout or a bad callback."""
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
                self._page(200, "Signed in to ChatGPT. You can close this tab and return to Frank.")

            def _page(self, status: int, message: str) -> None:
                body = _callback_page(message).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return CallbackHandler

    def _callback_failure(self, query: dict[str, list[str]]) -> Optional[str]:
        """A human-readable reason the callback is unusable, or ``None`` when it
        carries a valid, state-matched authorization code."""
        if (error := query.get("error", [""])[0]):
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
