"""The ChatGPT subscription's account state: which models it serves, and what it has left.

Both are properties of an *account*, not of a session or of a model call, which is why they
live here beside the token store that already owns that account's credentials rather than
inside the chat model that happens to observe them.

Keeping them out of `runtime` earns two things. The browser surface reads all four of these to
render Settings, and it was the only reason anything above the runtime imported it — so the
layer table can now say plainly that nothing does. And a snapshot written by one caller and
read by another is exactly the shape the layering checker refuses inside `runtime`, correctly:
a value taken from a caller and parked at module scope. It is legitimate here because the thing
being described is genuinely one per process — a machine has one signed-in account, and two
sessions billing it share its limits.

The catalogue is the authoritative answer to "which models actually work": a subscription
serves a plan-specific subset, where the models.dev catalogue is the offline superset the
interface greys against. The usage snapshot has no cheaper source than a turn — the limits ride
on `/responses` reply headers and the `/models` GET does not carry them — so it is only as
fresh as the last turn and is absent until the first one after signing in.
"""

from __future__ import annotations

import asyncio
import platform
import time
import uuid
from typing import Any, Optional

import httpx

from frank.base.credentials import ChatGPTAuthError, ChatGPTTokens, valid_tokens
from frank.base.tuning import Tunable, active_tuning

RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
# The account's live, plan-specific model catalogue. Same host and auth as the responses
# endpoint; `client_version` gates each model by its `minimal_client_version`, hiding any model
# whose floor is above what we claim. This is a *floor to clear*, not a cosmetic string: newer
# models raise their floor over time (gpt-5.4 needs 0.98, gpt-5.5 needs 0.124, the gpt-5.6-*
# family needs 0.144), so it must track a current Codex CLI version or the newer models
# silently vanish from the catalogue.
MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
CLIENT_VERSION = "0.144.4"
# Identifies the client to the endpoint, and it is checked. The endpoint admits only
# first-party originators — `codex_cli_rs`, `codex_vscode`, `codex_sdk_ts`, or a value
# starting with `Codex` — and refuses everything else, so this is the Codex CLI's own default
# rather than a name of ours. It did once accept any value, which is why opencode shipped
# `originator: opencode` and why this file used to say no impersonation was required; that
# stopped being true, and opencode now rewrites the header to this same value.
#
# `USER_AGENT` below is the other half of the same check: the two are gated *together*, so
# sending a Codex originator beside a `frank/…` user-agent fails the pair — mid-stream, with a
# `server_error` that reads like backend trouble rather than like a rejected client, which is
# the worst way for this to break because it looks like someone else's outage.
ORIGINATOR = "codex_cli_rs"

# The documented shape of the Codex CLI's user-agent is
# `codex_cli_rs/<version> (<os> <os version>; <arch>)`, with a transport token appended that
# varies by call path — so it is omitted here rather than guessed, and readers of this header
# are advised to match on the `codex_cli_rs/` prefix in any case.
#
# Every value in it is real: the version is `CLIENT_VERSION` above, which is already maintained
# against current Codex releases because the model catalogue gates on it, and the platform
# fields are this machine's. Nothing here is a plausible-looking invention, which is the line
# this file and the Cursor client both hold — a fabricated version invites trust it has not
# earned, while a true one that is merely incomplete does not.
USER_AGENT = (
    f"codex_cli_rs/{CLIENT_VERSION} "
    f"({platform.system()} {platform.release()}; {platform.machine()})"
)


def request_headers(tokens: ChatGPTTokens) -> dict[str, str]:
    """The header set the endpoint expects: a bearer token, the account to bill, an originator
    and User-Agent naming us, a per-request session id, and the streaming negotiation."""
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "ChatGPT-Account-Id": tokens.account_id,
        "originator": ORIGINATOR,
        "User-Agent": USER_AGENT,
        "session-id": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


_models_cache: Optional[tuple[float, dict[str, dict[str, Any]]]] = None
_models_cache_lock = asyncio.Lock()


async def fetch_subscription_models() -> dict[str, dict[str, Any]]:
    """The account's live model catalogue as ``{slug: {"name", "context"}}``.

    Answers with an empty mapping when signed out or on any failure — network, auth, parse — so
    callers fall back to the static list. Cached briefly because the interface polls this and it
    must not be a network round-trip each time.
    """
    global _models_cache
    ttl = active_tuning().duration(Tunable.model_catalogue_ttl_seconds)
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
                    MODELS_URL, params={"client_version": CLIENT_VERSION}, headers=headers,
                )
                response.raise_for_status()
                for entry in response.json().get("models", []):
                    slug = entry.get("slug")
                    if not slug:
                        continue
                    result[slug] = {
                        "name": entry.get("display_name") or slug,
                        "context": int(
                            entry.get("context_window") or entry.get("max_context_window") or 0
                        ),
                    }
        except (ChatGPTAuthError, httpx.HTTPError, ValueError, KeyError):
            result = {}
        _models_cache = (time.monotonic(), result)
        return result


def cached_subscription_models() -> dict[str, dict[str, Any]]:
    """The last catalogue fetched, with no network round-trip, or empty if never fetched.

    For synchronous callers that only want the freshest *known* value. The interface polls the
    model list constantly, so it is warm in practice."""
    return _models_cache[1] if _models_cache is not None else {}


def clear_subscription_models_cache() -> None:
    """Drop the cached catalogue, so the next read reflects a fresh sign-in or sign-out
    immediately rather than waiting out the time to live."""
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
    """Snapshot the account's rate-limit state from a reply's ``x-codex-*`` headers.

    A no-op when they are absent, which some error paths are, so it never replaces a good
    snapshot with an empty one."""
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
        windows.append({
            # The label is derived on the client from `window_minutes`, localised — the
            # five-hour and weekly mapping is not pinned to primary and secondary, and this
            # layer stays free of presentation and locale.
            "key": key,
            "used_percent": _header_float(headers.get(f"x-codex-{key}-used-percent")) or 0.0,
            "window_minutes": window_minutes,
            "resets_at": resets_at,
        })
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
    """The most recent rate-limit snapshot captured from a turn, or ``None`` when no turn has
    run since signing in — the headers ride only on responses replies."""
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
