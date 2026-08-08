"""The ChatGPT subscription's account state: which models it serves, and what it has left."""

from __future__ import annotations

import asyncio
import platform
import time
import uuid
from typing import Any, Optional

import httpx

from langmesh.base.credentials import ChatGPTAuthError, ChatGPTTokens, valid_tokens
from langmesh.base.tuning import Tunable, active_tuning

RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
# The account's live, plan-specific model catalogue, gated by the client version we present.
MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
CLIENT_VERSION = "0.144.4"
# Identifies the client to the endpoint, which admits only first-party originators.
ORIGINATOR = "codex_cli_rs"

# The documented user-agent shape, with the varying transport token omitted rather than guessed.
USER_AGENT = f"codex_cli_rs/{CLIENT_VERSION} ({platform.system()} {platform.release()}; {platform.machine()})"


def request_headers(tokens: ChatGPTTokens, session_id: str = "") -> dict[str, str]:
    """The header set the endpoint expects: the token, the account, who we are, and the conversation."""
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "ChatGPT-Account-Id": tokens.account_id,
        "originator": ORIGINATOR,
        "User-Agent": USER_AGENT,
        "session-id": session_id or str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


_models_cache: Optional[tuple[float, dict[str, dict[str, Any]]]] = None
_models_cache_lock = asyncio.Lock()


async def fetch_subscription_models() -> dict[str, dict[str, Any]]:
    """The account's live model catalogue, answering empty on any failure so callers fall back."""
    global _models_cache
    ttl = active_tuning().duration(Tunable.model_catalogue_ttl)
    if _models_cache is not None and time.monotonic() - _models_cache[0] < ttl:
        return _models_cache[1]
    async with _models_cache_lock:
        if _models_cache is not None and time.monotonic() - _models_cache[0] < ttl:
            return _models_cache[1]
        result: dict[str, dict[str, Any]] = {}
        try:
            tokens = await valid_tokens()
            headers = {
                key: value
                for key, value in request_headers(tokens).items()
                if key != "Accept"  # a plain JSON GET, not an SSE stream
            }
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    MODELS_URL,
                    params={"client_version": CLIENT_VERSION},
                    headers=headers,
                )
                response.raise_for_status()
                for entry in response.json().get("models", []):
                    slug = entry.get("slug")
                    if not slug:
                        continue
                    result[slug] = {
                        "name": entry.get("display_name") or slug,
                        "context": int(entry.get("context_window") or 0),
                    }
        except (ChatGPTAuthError, httpx.HTTPError, ValueError, KeyError):
            result = {}
        _models_cache = (time.monotonic(), result)
        return result


def cached_subscription_models() -> dict[str, dict[str, Any]]:
    """The last catalogue fetched, without a network round-trip, for synchronous callers."""
    return _models_cache[1] if _models_cache is not None else {}


def clear_subscription_models_cache() -> None:
    """Drop the cached catalogue so a fresh sign-in or sign-out shows immediately."""
    global _models_cache
    _models_cache = None


_usage_snapshot: Optional[dict[str, Any]] = None


def _header_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _header_int(value: Optional[str]) -> Optional[int]:
    parsed = _header_float(value)
    return int(parsed) if parsed is not None else None


def _header_bool(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("true", "1", "yes")


def capture_usage_headers(headers: httpx.Headers) -> None:
    """Snapshot the account's rate-limit state from a reply's headers, doing nothing when they are absent."""
    global _usage_snapshot
    if "x-codex-primary-window-minutes" not in headers and "x-codex-plan-type" not in headers:
        return
    now = int(time.time())
    windows: list[dict[str, Any]] = []
    for key in ("primary", "secondary"):
        window_minutes = _header_int(headers.get(f"x-codex-{key}-window-minutes")) or 0
        if window_minutes <= 0:
            continue  # this window is not active for the account right now
        resets_at = _header_int(headers.get(f"x-codex-{key}-reset-at"))
        if resets_at is None:
            after = _header_int(headers.get(f"x-codex-{key}-reset-after-seconds"))
            resets_at = now + after if after is not None else None
        windows.append(
            {
                # The label is derived and localised on the client, so this layer stays free of presentation.
                "key": key,
                "used_percent": _header_float(headers.get(f"x-codex-{key}-used-percent")) or 0.0,
                "window_minutes": window_minutes,
                "resets_at": resets_at,
            }
        )
    _usage_snapshot = {
        "plan_type": headers.get("x-codex-plan-type", ""),
        "active_limit": headers.get("x-codex-active-limit", ""),
        "captured_at": now,
        "credits": {
            "has_credits": _header_bool(headers.get("x-codex-credits-has-credits")),
            "balance": _header_float(headers.get("x-codex-credits-balance")),
            "unlimited": _header_bool(headers.get("x-codex-credits-unlimited")),
        },
        "windows": windows,
    }


def get_usage_snapshot() -> Optional[dict[str, Any]]:
    """The most recent rate-limit snapshot, or `None` when no turn has run since signing in."""
    return _usage_snapshot


def set_usage_snapshot(usage: Optional[dict[str, Any]]) -> None:
    """Hold a snapshot captured elsewhere. The daemon calls this with what a worker read."""
    global _usage_snapshot
    _usage_snapshot = usage


def clear_usage_snapshot() -> None:
    """Drop the snapshot on sign-out or sign-in, so stale limits do not linger."""
    global _usage_snapshot
    _usage_snapshot = None


__all__ = [
    "CLIENT_VERSION",
    "MODELS_URL",
    "ORIGINATOR",
    "RESPONSES_URL",
    "USER_AGENT",
    "cached_subscription_models",
    "capture_usage_headers",
    "clear_subscription_models_cache",
    "clear_usage_snapshot",
    "fetch_subscription_models",
    "get_usage_snapshot",
    "request_headers",
]
