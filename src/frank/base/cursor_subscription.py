"""What a Cursor subscription is, as an account rather than as a conversation.

The sibling of :mod:`frank.base.subscription`, which does the same job for the ChatGPT
plan: the endpoints, the header set every call carries, and the account's live model
catalogue. :mod:`frank.runtime.models.cursor` builds turns on top of this and owns
nothing that is true of the account rather than of a turn.

The split is the same one that module draws, for the same reason. The REST layer needs
the catalogue on every ``/models`` poll, and the REST layer may not import the runtime —
so the catalogue lives here, where anything may import it, instead of behind a deferred
import into the model module.

Two things here are Cursor's and not ours. ``request_headers`` sends the exact set the
``RunSSE`` transport was exercised with rather than a superset, and :func:`_checksum`
computes a value that proves nothing and is sent only because the service refuses a
request without it. Both are documented where they are defined.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from frank.base.cursor_credentials import (
    API_BASE_URL,
    CursorAuthError,
    CursorTokens,
    valid_tokens,
)
from frank.base.tuning import Tunable, active_tuning

RUN_PATH = "/agent.v1.AgentService/RunSSE"
APPEND_PATH = "/aiserver.v1.BidiService/BidiAppend"
# Cursor moves this service between backends. api2 is where it answers today; the two agent hosts are where the plugins have seen it move to, one for privacy mode and one without.
AGENT_PRIVACY_URL = "https://agent.api5.cursor.sh"
AGENT_OPEN_URL = "https://agentn.api5.cursor.sh"
RUN_HOSTS = (API_BASE_URL, AGENT_PRIVACY_URL, AGENT_OPEN_URL)
# The two model endpoints, on different services and knowing different halves of the answer: which models a plan serves, and how large a window each has.
USABLE_MODELS_URL = f"{API_BASE_URL}/agent.v1.AgentService/GetUsableModels"
AVAILABLE_MODELS_URL = f"{API_BASE_URL}/aiserver.v1.AiService/AvailableModels"

# A real Cursor CLI build, and specifically the newest one that any working client is known to send: `cli-2026.01.09-231024f` appears in three files across the two OpenCode plugins that drive this service.
CLIENT_VERSION = "cli-2026.01.09-231024f"
CLIENT_TYPE = "cli"

# gRPC status codes worth naming. 8 is RESOURCE_EXHAUSTED, which on this service means the subscription's included usage is spent; 16 is UNAUTHENTICATED.
STATUS_RESOURCE_EXHAUSTED = 8
STATUS_UNAUTHENTICATED = 16


def machine_time_zone() -> str:
    """The machine's IANA zone name, as Cursor's own client reports it.

    Python has no portable API for this — ``time.tzname`` gives an abbreviation like
    ``CET``, not ``Europe/Rome`` — so ``TZ`` is preferred and the ``/etc/localtime``
    symlink is read next, which is where the name actually lives on Linux and macOS. The
    abbreviation is the last resort rather than the first, because a plausible-looking
    wrong value is worse than a coarse right one."""
    if configured := os.environ.get("TZ", "").strip():
        return configured
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return time.tzname[0] if time.tzname else "UTC"


def _checksum(access_token: str) -> str:
    """The ``x-cursor-checksum`` the service expects alongside the bearer token.

    It is not a signature and proves nothing — it is a client-side obfuscation of a
    half-hour-rounded timestamp plus two truncated digests of the token. It is here
    because Cursor's own client sends it and a request without it is refused; there is
    nothing to verify and nothing to keep secret."""
    slot = int(time.time() // 1800) * 1800
    stamp = (slot * 1000) // 1_000_000
    obfuscated = bytearray(stamp.to_bytes(6, "big"))
    previous = 165
    for index in range(len(obfuscated)):
        obfuscated[index] = ((obfuscated[index] ^ previous) + index) & 0xFF
        previous = obfuscated[index]
    prefix = base64.urlsafe_b64encode(bytes(obfuscated)).rstrip(b"=").decode()
    segments = access_token.split(".")
    payload_digest = hashlib.sha256(segments[1].encode()).hexdigest()[:8] if len(segments) > 1 else "00000000"
    token_digest = hashlib.sha256(access_token.encode()).hexdigest()[:8]
    return f"{prefix}{payload_digest}/{token_digest}"


def request_headers(tokens: CursorTokens, request_id: str) -> dict[str, str]:
    """The header set the ``RunSSE`` + ``BidiAppend`` transport was tested with.

    Which headers to send is not a free choice, because the two transports through this
    service were exercised with different sets. The plugins that hold an HTTP/2 stream open
    for ``Run`` send no checksum, no timezone and no streaming hint; the one that uses this
    transport sends all three. Matching that one is deliberate — a header set assembled by
    taking the union of two tested combinations is a third combination nobody has tried."""
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        # Not Connect's own content type: this service answers 415 to application/connect+proto over HTTP/1.1 and expects the gRPC-web framing.
        "Content-Type": "application/grpc-web+proto",
        "x-cursor-checksum": _checksum(tokens.access_token),
        "x-cursor-client-version": CLIENT_VERSION,
        "x-cursor-client-type": CLIENT_TYPE,
        "x-cursor-timezone": machine_time_zone(),
        # Privacy mode: do not retain this conversation for training.
        "x-ghost-mode": "true",
        # Without this the service may park a reply in the blob store instead of streaming it, and a turn that streams nothing looks like a turn that failed.
        "x-cursor-streaming": "true",
        "x-request-id": request_id,
    }


# Live per-account model discovery.
_models_cache: Optional[tuple[float, dict[str, dict[str, Any]]]] = None
_models_cache_lock = asyncio.Lock()

# The floor for a model the catalog named without stating a window, which is the only case left once both endpoints have been asked.
UNKNOWN_CONTEXT_WINDOW = 200_000

# What the server itself said a model's window was, learned from a checkpoint during a turn and keyed by model id.
_observed_context_windows: dict[str, int] = {}


def record_context_window(model_id: str, maximum_tokens: int) -> None:
    """Remember a window the server reported. Keeps the largest seen for a model: a turn that
    has not yet grown past a smaller budget can be told a smaller one."""
    if maximum_tokens > _observed_context_windows.get(model_id, 0):
        _observed_context_windows[model_id] = maximum_tokens

def observed_context_window(model_id: str) -> int:
    """The largest window the server itself reported for a model, or ``0`` when it never has.

    An accessor rather than a shared dict because the store is this module's, and a model
    reading it is asking a question rather than reaching into state it owns."""
    return _observed_context_windows.get(model_id, 0)


# The model ids Cursor serves are not plain model names: the reasoning effort is part of the id (``claude-4.6-opus-high``, ``gpt-5.4-medium``).
_EFFORT_SUFFIX = re.compile(r"-(high|medium|low|max|xhigh)$")


def _display_name(entry: dict[str, Any], model_id: str) -> str:
    """A model's label, with its effort named when the id carries one.

    Cursor gives the same display name to every effort variant of a model, so a picker
    that trusted it would list "Claude 4.6 Opus" three times with no way to tell which is
    which. The effort is in the id, so it goes in the label."""
    name = model_id
    for key in ("displayName", "displayNameShort", "displayModelId"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    effort = _EFFORT_SUFFIX.search(model_id)
    if effort and effort.group(1) not in name.lower():
        return f"{name} ({effort.group(1)})"
    return name


def _token_limit(value: Any) -> int:
    """A Cursor parameter value as a token count. It states windows the way its interface
    displays them — ``200k``, ``1m``, ``272000`` — so all three have to read."""
    text_value = str(value or "").strip().lower().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([km])?", text_value)
    if match is None:
        return 0
    scale = {"k": 1_000, "m": 1_000_000}.get(match.group(2) or "", 1)
    return round(float(match.group(1)) * scale)


async def _connect_json(url: str, body: dict, tokens: CursorTokens) -> dict:
    """One unary RPC over Connect's JSON encoding, which these two model endpoints accept —
    so discovering models needs no protobuf at all, unlike running a turn."""
    headers = {
        **request_headers(tokens, str(uuid.uuid4())),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "connect-protocol-version": "1",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
    return payload if isinstance(payload, dict) else {}


@dataclass
class _Variant:
    """How the backend routes one model variant: its server-side name, its max-mode flag, and the
    parameter values that pick it out from the others sharing that name."""

    server_model: str
    maximum_mode: bool
    parameters: tuple[tuple[str, str], ...]
    context: int


async def _fetch_variants(tokens: CursorTokens) -> dict[str, _Variant]:
    """Every model variant the account can reach, from ``AvailableModels``.

    This is the only endpoint that states a context window, and the only one that describes how a
    variant is *selected* — both live in the same place, so they are read together. It answers with
    base models rather than the effort-suffixed ids a run request takes, each carrying variants
    whose ``parameterValues`` hold the window and the reasoning effort, so the result is keyed both
    by a variant's own ``legacySlug`` (an exact match for a run id) and by the base name (a prefix
    match). Where variants of one base name disagree about the window, the smallest is kept: a
    window that reads too large overruns the model, while one that reads too small only compacts
    early.

    Nothing is requested by name. ``additionalModelNames`` exists to make the service mention
    models it would otherwise omit, and reaching for it would mean hardcoding model names to
    discover models — which is the thing discovery is for."""
    payload = await _connect_json(
        AVAILABLE_MODELS_URL,
        {"isNightly": False, "excludeMaxNamedModels": True, "additionalModelNames": [],
         "useModelParameters": True, "useReactModelPicker": True},
        tokens,
    )
    variants: dict[str, _Variant] = {}

    def remember(key: str, variant: _Variant) -> None:
        if not key:
            return
        existing = variants.get(key)
        if existing is None:
            variants[key] = variant
        elif variant.context and existing.context and variant.context < existing.context:
            variants[key] = variant

    for entry in payload.get("models") or []:
        if not isinstance(entry, dict) or not (base_name := entry.get("name")):
            continue
        server_model = str(entry.get("serverModelName") or base_name)
        for raw_variant in entry.get("variants") or []:
            if not isinstance(raw_variant, dict):
                continue
            values = {
                str(parameter.get("id")): str(parameter.get("value"))
                for parameter in raw_variant.get("parameterValues") or []
                if isinstance(parameter, dict) and parameter.get("id") is not None
            }
            variant = _Variant(
                server_model=server_model,
                maximum_mode=raw_variant.get("isMaxMode") is True,
                parameters=tuple(sorted(values.items())),
                context=_token_limit(values.get("context")),
            )
            remember(str(raw_variant.get("legacySlug") or ""), variant)
            remember(str(base_name), variant)
    return variants


def _variant_for(model_id: str, variants: dict[str, _Variant]) -> Optional[_Variant]:
    """The variant for a run id: an exact match on a slug, else the longest base name the id
    starts with — ``claude-4.6-opus-high`` resolving through ``claude-4.6-opus``."""
    if (exact := variants.get(model_id)) is not None:
        return exact
    candidates = [name for name in variants if model_id.startswith(name)]
    return variants[max(candidates, key=len)] if candidates else None


async def fetch_subscription_models() -> dict[str, dict[str, Any]]:
    """The account's live Cursor model list as ``{model_id: {"name", "context"}}``.

    Two endpoints, because each knows half of it. ``GetUsableModels`` answers with the ids a
    run request accepts verbatim — it returns the very message type the request echoes back —
    so it owns the list. ``AvailableModels`` is the only one that states context windows, so
    it owns those, and a failure there costs windows rather than the whole catalog.

    Returns an empty dict when signed out or on any failure, which is what greys this
    provider's models in the picker. Cached briefly because the interface polls ``/models``
    and this must not be a round-trip each time."""
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
            listing = await _connect_json(USABLE_MODELS_URL, {}, tokens)
            try:
                variants = await _fetch_variants(tokens)
            except (httpx.HTTPError, ValueError, TypeError):
                variants = {}  # a listing without windows or routing still beats no listing
            for entry in listing.get("models") or []:
                if not isinstance(entry, dict):
                    continue
                model_id = entry.get("modelId") or entry.get("displayModelId")
                if not model_id:
                    continue
                variant = _variant_for(model_id, variants)
                result[model_id] = {
                    "name": _display_name(entry, model_id),
                    "context": variant.context if variant else 0,
                    "variant": None if variant is None else {
                        "server_model": variant.server_model,
                        "maximum_mode": variant.maximum_mode,
                        "parameters": variant.parameters,
                    },
                }
        except (CursorAuthError, httpx.HTTPError, ValueError, KeyError, TypeError):
            result = {}
        _models_cache = (time.monotonic(), result)
        return result


def cached_subscription_models() -> dict[str, dict[str, Any]]:
    """The last live list fetched, without a network round-trip (``{}`` if never
    fetched). For sync callers that only want the freshest *known* value."""
    return _models_cache[1] if _models_cache is not None else {}


def clear_subscription_models_cache() -> None:
    """Drop the cached list so the next ``/models`` reflects a fresh sign-in or sign-out
    immediately rather than waiting out the TTL."""
    global _models_cache
    _models_cache = None
