"""Cursor-subscription authentication for the ``cursor`` model provider.

The sibling of :mod:`daisy.base.credentials`, for the other subscription that can pay
for model calls instead of an API key. Same bargain, different mechanics, and the
mechanics are the whole reason this is a separate module rather than a second branch
in that one.

ChatGPT's route is ordinary OAuth: an authorization endpoint, a PKCE code, a loopback
redirect that hands the code back, a token exchange. Cursor's is not. Cursor's CLI
opens ``cursor.com/loginDeepControl`` with a PKCE challenge and a client-chosen
``uuid``, and then *polls* ``api2.cursor.sh/auth/poll`` with the ``uuid`` and the
verifier until the browser side completes and the poll answers with the token pair.
There is no redirect and therefore nothing to catch: no loopback server, no port to
collide over, no callback page. The browser never talks to us at all.

That difference is worth stating plainly because it is the one thing that makes this
easier than the ChatGPT flow rather than harder — a sign-in cannot fail with "port
1455 is in use", because there is no port.

Everything else is deliberately the same shape as the ChatGPT module, so the two read
as one idea with two backends: tokens live in the OAuth token directory as
``oauths/cursor.json`` (mode 0600), outside ``configuration.yaml`` because that file is
digest-synced and would thrash on every silent refresh; a single lock serialises refreshes
so concurrent turns cannot stampede; and :func:`valid_tokens` is the only way the rest of
the harness gets a token, so a refresh is invisible to callers.

Consequences, kept visible as they are for ChatGPT:
  * This rides on Cursor's own CLI login path. There is no published API for using a
    Cursor subscription from another client, so Cursor can invalidate the pattern at
    any time. Treat it as fragile, not stable.
  * The tokens are password-equivalent — they authorise spending against the account.
  * The access token is a short-lived JWT; the refresh token is the long-lived one.
    Cursor's refresh endpoint sometimes echoes an opaque API key back in the
    ``refreshToken`` field, which would clobber the real one, so a returned refresh is
    only adopted when it actually looks like a JWT.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import uuid as uuid_module
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_delay,
    wait_exponential,
)

from daisy.base.paths import oauth_token_path
from daisy.base.tuning import Tunable, active_tuning

# Cursor's own login surface and API host. Neither is a secret — they are the addresses
# the Cursor CLI itself uses — and using them is exactly what makes the browser mint a
# subscription-scoped token pair for us.
LOGIN_URL = "https://cursor.com/loginDeepControl"
API_BASE_URL = "https://api2.cursor.sh"
POLL_URL = f"{API_BASE_URL}/auth/poll"
# Not a typo and not an API-key feature: this endpoint is what Cursor's CLI posts a
# refresh token to in order to mint a fresh access token.
REFRESH_URL = f"{API_BASE_URL}/auth/exchange_user_api_key"

PROVIDER = "cursor"

# Serializes token refreshes: many concurrent turns can each notice an expiring token
# at once, and we want exactly one refresh + write, not a stampede.
_refresh_lock = asyncio.Lock()


class CursorAuthError(RuntimeError):
    """Raised when a Cursor-subscription call cannot be authenticated."""


class _SignInPending(Exception):
    """The browser has not finished yet — the poll's normal answer for most of its window.

    A distinct type because it is what the retry below retries *on*. Everything else that can
    go wrong falls into one of two other kinds: a transport failure, which is also worth
    retrying because a person mid-sign-in can lose their connection and get it back, and a
    refusal from Cursor, which is not, because asking a second time will not change the
    answer. Distinguishing by kind is why there is no "give up after N consecutive errors"
    tally here: a broken flow fails on the first refusal rather than on the third."""


@dataclass
class CursorTokens:
    """The persisted result of a successful sign-in.

    ``account`` is whatever the access token names its subject as (an email when the
    claim carries one, otherwise the raw ``sub``) and exists only so the interface can
    say *which* account is signed in. ``expires_at`` is epoch seconds for the access
    token, read from the JWT's own ``exp``."""

    access_token: str
    refresh_token: str
    account: str
    expires_at: float

    def is_expired(self, leeway_seconds: float = active_tuning().duration(Tunable.credential_refresh_leeway_seconds)) -> bool:
        return time.time() >= (self.expires_at - leeway_seconds)


def auth_file_path() -> Path:
    return oauth_token_path(PROVIDER)


def load_tokens() -> Optional[CursorTokens]:
    """Load the stored tokens, or ``None`` when signed out. Synchronous file IO — call
    via ``asyncio.to_thread`` from the event loop."""
    path = auth_file_path()
    if not path.exists():
        return None
    try:
        return CursorTokens(**json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def save_tokens(tokens: CursorTokens) -> None:
    """Persist tokens with owner-only permissions (they are password-equivalent)."""
    path = auth_file_path()
    path.write_text(json.dumps(asdict(tokens), separators=(",", ":")))
    os.chmod(path, 0o600)


def clear_tokens() -> None:
    auth_file_path().unlink(missing_ok=True)


def is_signed_in() -> bool:
    return load_tokens() is not None


# PKCE + JWT helpers. The verifier is 32 random bytes base64url-encoded and the
# challenge its SHA-256, which is what Cursor's login page expects to see.

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _generate_verifier() -> str:
    return _b64url(secrets.token_bytes(32))


def _challenge_for(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def _decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying the signature — we read only the expiry
    and the subject for display, and trust neither for authorization."""
    try:
        _header, payload, _signature = token.split(".")
        claims = json.loads(_b64url_decode(payload))
        return claims if isinstance(claims, dict) else {}
    except (ValueError, TypeError):
        return {}


def _looks_like_jwt(value: object) -> bool:
    """Whether a value is plausibly a JWT (three non-empty dot-separated segments).

    Cursor's refresh endpoint sometimes returns an opaque API key in the
    ``refreshToken`` field. Adopting that would overwrite the long-lived OAuth refresh
    token and break every later refresh, so a replacement has to look the part."""
    if not isinstance(value, str):
        return False
    segments = value.split(".")
    return len(segments) == 3 and all(segments)


def _expiry_of(access_token: str) -> float:
    """The access token's own expiry, or an hour out when the token is not a readable
    JWT — a short guess is safer than a long one, because guessing long means calls
    fail before a refresh is attempted."""
    exp = _decode_jwt_claims(access_token).get("exp")
    return float(exp) if isinstance(exp, (int, float)) else time.time() + 3600.0


def _account_of(access_token: str) -> str:
    claims = _decode_jwt_claims(access_token)
    for claim in ("email", "sub"):
        value = claims.get(claim)
        if isinstance(value, str) and value:
            return value
    return ""


def _tokens_from_payload(payload: dict, previous: Optional[CursorTokens] = None) -> CursorTokens:
    """Build a :class:`CursorTokens` from a poll or refresh response, carrying the
    previous refresh token over when the response does not return a usable one."""
    access_token = payload.get("accessToken") or ""
    if not access_token:
        raise CursorAuthError("Cursor returned no access token.")
    returned_refresh = payload.get("refreshToken")
    refresh_token = (
        returned_refresh if _looks_like_jwt(returned_refresh)
        else (previous.refresh_token if previous else str(returned_refresh or ""))
    )
    return CursorTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        account=_account_of(access_token) or (previous.account if previous else ""),
        expires_at=_expiry_of(access_token),
    )


async def _refresh(tokens: CursorTokens) -> CursorTokens:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            REFRESH_URL,
            headers={
                "Authorization": f"Bearer {tokens.refresh_token}",
                "Content-Type": "application/json",
            },
            content="{}",
        )
        response.raise_for_status()
        return _tokens_from_payload(response.json(), previous=tokens)


async def valid_tokens() -> CursorTokens:
    """Return a live, non-expired token set, refreshing and re-persisting first when the
    access token is at or near expiry. Raises :class:`CursorAuthError` when signed out or
    when a refresh fails (e.g. the subscription lapsed)."""
    tokens = await asyncio.to_thread(load_tokens)
    if tokens is None:
        raise CursorAuthError("Not signed in to Cursor. Sign in from Settings to use this provider.")
    if not tokens.is_expired():
        return tokens
    if not tokens.refresh_token:
        raise CursorAuthError("Cursor session expired and cannot be refreshed. Sign in again.")
    async with _refresh_lock:
        # Re-load inside the lock: another turn may have refreshed while we waited.
        current = await asyncio.to_thread(load_tokens) or tokens
        if not current.is_expired():
            return current
        try:
            refreshed = await _refresh(current)
        except httpx.HTTPError as error:
            raise CursorAuthError(f"Could not refresh the Cursor session: {error}") from error
        await asyncio.to_thread(save_tokens, refreshed)
        return refreshed


class CursorLoginFlow:
    """One browser sign-in.

    ``authorize_url`` is opened by the client; :meth:`wait` polls Cursor until the
    browser side completes, then persists the token pair. There is no callback server
    because Cursor's flow has no redirect — the ``uuid`` minted here is the only thing
    tying our poll to that browser tab, and the verifier is what proves the poll is
    coming from whoever started the flow.

    :meth:`close` makes an in-flight :meth:`wait` give up, so a superseded or
    cancelled sign-in stops polling instead of lingering for its full window."""

    def __init__(self) -> None:
        self._verifier = _generate_verifier()
        self._uuid = str(uuid_module.uuid4())
        self._cancelled = False

    @property
    def authorize_url(self) -> str:
        parameters = {
            "challenge": _challenge_for(self._verifier),
            "uuid": self._uuid,
            "mode": "login",
            # Names the shape of the client Cursor is handing a token to. "cli" is the
            # non-editor flow — the one that answers over /auth/poll rather than a
            # redirect — which is the flow this is.
            "redirectTarget": "cli",
        }
        return f"{LOGIN_URL}?{urllib.parse.urlencode(parameters)}"

    async def _ask(self, client: httpx.AsyncClient) -> CursorTokens:
        """One ask of whether the browser has finished.

        Raises :class:`_SignInPending` while it has not, ``httpx.HTTPError`` when the ask itself
        did not get through, and :class:`CursorAuthError` when Cursor refuses — which the retry
        does not retry, so a refusal surfaces at once instead of after the whole window."""
        if self._cancelled:
            raise CursorAuthError("Cursor sign-in was cancelled.")
        response = await client.get(POLL_URL, params={"uuid": self._uuid, "verifier": self._verifier})
        if response.status_code == 404:
            raise _SignInPending
        if not response.is_success:
            raise CursorAuthError(f"Cursor refused the sign-in poll (HTTP {response.status_code}).")
        tokens = _tokens_from_payload(response.json())
        await asyncio.to_thread(save_tokens, tokens)
        return tokens

    async def wait(self) -> CursorTokens:
        """Wait for the browser sign-in to complete, then persist and return the tokens.

        The waiting is tenacity's: a pause that widens from the configured interval up to its
        ceiling, and a deadline that ends the flow. What is left here is which outcomes are worth
        asking again about — and the pacing is a policy setting like every other duration in the
        harness rather than a constant sitting next to the code that reads it."""
        tuning = active_tuning()
        tokens: Optional[CursorTokens] = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception_type((_SignInPending, httpx.HTTPError)),
                    wait=wait_exponential(
                        multiplier=tuning.duration(Tunable.oauth_poll_interval_seconds),
                        max=tuning.duration(Tunable.oauth_poll_ceiling_seconds),
                    ),
                    stop=stop_after_delay(tuning.duration(Tunable.oauth_poll_give_up_seconds)),
                ):
                    with attempt:
                        tokens = await self._ask(client)
        except RetryError as error:
            raise CursorAuthError("Cursor sign-in timed out. Try again.") from error
        assert tokens is not None, "the retry either produces tokens or raises"
        return tokens

    async def close(self) -> None:
        self._cancelled = True
