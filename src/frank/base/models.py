from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass

import httpx

from frank.base.providers import (
    PROVIDERS,
    ProviderDefinition,
    get_provider_definition,
    resolve_api_key,
    resolve_base_url,
)


@dataclass(frozen=True)
class ModelDefinition:
    """A pickable model. The ``identifier`` is the canonical, provider-namespaced
    technical id a user references the model by (``anthropic/claude-sonnet-4``,
    ``opencode/deepseek-v4-flash``); ``name`` is the user-facing label from the
    models.dev catalog, falling back to the raw model suffix when no display name
    is available (the frontend renders those in monospace).
    """

    identifier: str
    name: str
    provider: str
    # Capabilities from the models.dev catalog, used to gate and annotate the UI.
    # ``attachment`` is whether the model accepts file attachments at all; ``vision``
    # is whether image input is supported; ``input_modalities`` is the raw list
    # (``text``, ``image``, ``pdf``, ``audio``, …) for finer-grained display.
    attachment: bool = False
    vision: bool = False
    input_modalities: tuple[str, ...] = ()
    # Maximum input context in tokens, from the models.dev catalog (0 = unknown).
    # Used for the "how full is the context" gauge; the native chatgpt and cursor
    # providers rely on this since they have no LiteLLM model-info map to consult.
    context_length: int = 0
    # ISO release date (YYYY-MM-DD) from the models.dev catalog, or "" if unknown.
    # The picker sorts newest-first on this instead of alphabetically.
    release_date: str = ""


# Map models.dev provider IDs to our local provider identifiers.
# models.dev uses kebab-case; we use snake_case (or the original provider name).
_MODELS_DEV_PROVIDER_MAP: dict[str, str] = {
    # Direct matches: models.dev kebab-case = our snake_case
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "deepseek": "deepseek",
    "xai": "xai",
    "groq": "groq",
    "mistral": "mistral",
    "openrouter": "openrouter",
    "cohere": "cohere",
    "perplexity": "perplexity",
    "deepinfra": "deepinfra",
    "hyperbolic": "hyperbolic",
    "cerebras": "cerebras",
    "minimax": "minimax",
    "nebius": "nebius",
    "ovhcloud": "ovhcloud",
    "wandb": "wandb",
    "inception": "inception",
    "morph": "morph",
    "oci": "oci",
    "databricks": "databricks",
    "zai": "zai",
    # Mismatched names
    "fireworks-ai": "fireworks_ai",
    "novita-ai": "novita",
    "moonshotai": "moonshot",
    "togetherai": "together_ai",
    "cloudflare-workers-ai": "cloudflare",
    "github-copilot": "github_copilot",
    "zhipuai": "zai",
    "zhipuai-coding-plan": "zai_code",
    "zai-coding-plan": "zai_code",
    "friendli": "friendliai",
    "opencode": "opencode",
    "opencode-go": "opencode",
    "kimi-for-coding": "moonshot",
    "minimax-cn": "minimax",
    "minimax-coding-plan": "minimax",
    "minimax-cn-coding-plan": "minimax",
    "moonshotai-cn": "moonshot",
    "scaleway": "scaleway",
}


def _catalog() -> list[ModelDefinition]:
    """Model catalog from models.dev's open-source API.

    Best-effort — returns an empty list when the API is unreachable so the harness
    can still start without a model catalog.
    """
    MODELS_DEV_URL = "https://models.dev/api.json"
    try:
        response = httpx.get(MODELS_DEV_URL, timeout=5)
        response.raise_for_status()
        raw = response.json()
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not fetch model catalog from %s — no models available",
            MODELS_DEV_URL,
        )
        return []

    models: dict[str, ModelDefinition] = {}
    for models_dev_id, provider_info in raw.items():
        local_id = _MODELS_DEV_PROVIDER_MAP.get(models_dev_id)
        if local_id is None:
            continue
        # Skip providers not registered in this version of Frank
        if get_provider_definition(local_id) is None:
            continue
        for model_id, model_info in provider_info.get("models", {}).items():
            name = model_info.get("name", "") or model_id
            identifier = f"{local_id}/{model_id}"
            modalities = model_info.get("modalities") or {}
            input_modalities = tuple(
                str(modality) for modality in (modalities.get("input") or []) if modality
            )
            # Deduplicate: when multiple models.dev provider IDs map to the same
            # local provider and carry the same model, keep the first occurrence.
            models.setdefault(identifier, ModelDefinition(
                identifier=identifier,
                name=name,
                provider=local_id,
                attachment=bool(model_info.get("attachment")),
                vision="image" in input_modalities,
                input_modalities=input_modalities,
                context_length=int((model_info.get("limit") or {}).get("context") or 0),
                release_date=str(model_info.get("release_date") or ""),
            ))
    return list(models.values())


# Which OpenAI models the ChatGPT-subscription (Codex) endpoint currently serves.
# Ported from opencode's codex plugin (packages/opencode/src/plugin/openai/codex.ts):
# a small allow/deny set plus "GPT version > 5.4". The set moves over time, so we
# keep the *rule* in sync with that reference rather than hand-maintaining a model
# list — every model's name, context window, and modalities still ride in from the
# models.dev catalog automatically, so no metadata is hand-rolled here.
_CODEX_ALLOWED_MODELS = frozenset({"gpt-5.5", "gpt-5.3-codex-spark", "gpt-5.4", "gpt-5.4-mini"})
_CODEX_DISALLOWED_MODELS = frozenset({"gpt-5.5-pro"})


def _codex_eligible(model_suffix: str) -> bool:
    if model_suffix in _CODEX_ALLOWED_MODELS:
        return True
    if model_suffix in _CODEX_DISALLOWED_MODELS:
        return False
    match = re.match(r"^gpt-(\d+\.\d+)", model_suffix)
    return float(match.group(1)) > 5.4 if match else False


def _chatgpt_models(base: list[ModelDefinition]) -> list[ModelDefinition]:
    """The experimental ``chatgpt`` subscription models, derived from the OpenAI
    entries in the live models.dev catalog filtered to the codex-eligible set — so
    new GPT-5.x models appear automatically as models.dev learns of them, with their
    real names and context windows, instead of a stale hand-written list."""
    chatgpt: list[ModelDefinition] = []
    for model in base:
        if model.provider != "openai":
            continue
        suffix = model.identifier.split("/", 1)[1]
        if not _codex_eligible(suffix):
            continue
        chatgpt.append(ModelDefinition(
            identifier=f"chatgpt/{suffix}",
            name=model.name,
            provider="chatgpt",
            attachment=model.attachment,
            vision=model.vision,
            input_modalities=model.input_modalities,
            context_length=model.context_length,
            release_date=model.release_date,
        ))
    return chatgpt


# The ``cursor`` provider contributes nothing here, and that is the design rather than an
# omission. Its models cannot come from models.dev, which has no Cursor provider and could not
# describe one: a Cursor model id carries its reasoning effort (``claude-4.6-opus-high``) and
# names Cursor's own Composer family, neither of which exists in a catalog of direct-API
# models. So the account is asked instead — ``GetUsableModels`` for the ids a plan serves and
# ``AvailableModels`` for their context windows — and the ``/models`` endpoint appends what
# comes back. Until a user signs in there is nothing truthful to list, so nothing is listed;
# the picker shows the provider, its sign-in control, and no models. A hand-written stand-in
# would only be a guess about somebody else's subscription wearing the catalog's clothes.
_catalogue_cache: list[ModelDefinition] | None = None
_catalogue_lock = threading.Lock()


def list_models() -> list[ModelDefinition]:
    """The model catalogue, fetched on first use and then cached for the process.

    This is a function rather than a module-level list for a reason that is not style.
    Building the catalogue performs a blocking HTTP GET to models.dev, and doing that at
    *import* time made every process that imports this module — which is every process
    that imports the runtime — pay a second of startup, depend on a reachable third-party
    host, and silently end up with an empty catalogue when offline.

    It also broke the invariant the prototype rests on. On macOS that fetch spawns two
    persistent native network threads, and a multi-threaded parent cannot legally
    ``fork()``: the child aborts inside the Objective-C runtime with a message that reads
    like a CoreFoundation verdict and is not one. Deferring the fetch is what keeps the
    prototype single-threaded up to the moment it forks, and the fetch then happens in the
    child, where threads are nobody's problem.

    Best-effort by design: an unreachable models.dev yields an empty catalogue rather than
    an exception, and the result — empty or not — is cached, so a failed fetch does not
    retry on every call. :func:`clear_catalogue_cache` is how a caller asks for another try.
    """
    global _catalogue_cache
    if _catalogue_cache is not None:
        return list(_catalogue_cache)
    with _catalogue_lock:
        if _catalogue_cache is None:
            base = _catalog()
            _catalogue_cache = base + _chatgpt_models(base)
        return list(_catalogue_cache)


def clear_catalogue_cache() -> None:
    """Drop the cached catalogue so the next :func:`list_models` refetches.

    For a process that started offline and now has a network, and for the settings surface
    after a provider changes."""
    global _catalogue_cache
    _catalogue_cache = None


def find_model(model_identifier: str) -> ModelDefinition | None:
    for model in list_models():
        if model.identifier == model_identifier:
            return model
    return None


def provider_and_suffix(model_identifier: str) -> tuple[str, str] | None:
    """Split a model id into its provider id and the model suffix (everything after
    the first ``/``). The suffix may itself contain slashes (OpenRouter's
    ``anthropic/claude-sonnet-4``), so only the first slash is significant."""
    if "/" not in model_identifier:
        return None
    provider_identifier, suffix = model_identifier.split("/", 1)
    return provider_identifier, suffix


def available_models(configured_keys: dict[str, str]) -> list[ModelDefinition]:
    """Catalog entries whose provider has a resolvable credential. A provider is
    unlocked by an explicit configured key or any of its env vars. Native providers
    (``chatgpt``, ``cursor``) are excluded here — their availability is resolved
    per-model against the live subscription list by the ``/models`` endpoint, not by a
    key."""
    unlocked_providers = {
        provider.identifier
        for provider in PROVIDERS.values()
        if resolve_api_key(provider.identifier, configured_keys)
        # The custom provider has no key of its own; it is selectable on demand.
        or provider.identifier == "custom"
    }
    return [model for model in list_models() if model.provider in unlocked_providers]


def resolve_litellm(
    model_identifier: str,
    configured_keys: dict[str, str],
    configured_bases: dict[str, str],
) -> dict[str, str]:
    """Translate a catalog model id into the LiteLLM call parameters. Returns
    ``{"model", "api_key", "api_base"}`` where ``model`` is the LiteLLM model
    string (provider prefix + suffix) and ``api_base`` is empty for first-party
    clouds (LiteLLM knows their endpoints). Raises ``ValueError`` for an unknown
    provider so the factory surfaces a clear error rather than a LiteLLM 401."""
    split = provider_and_suffix(model_identifier)
    if split is None:
        raise ValueError(f"Model id has no provider prefix: {model_identifier!r}")
    provider_identifier, suffix = split
    definition: ProviderDefinition | None = get_provider_definition(provider_identifier)
    if definition is None:
        raise ValueError(f"Unknown provider in model id: {model_identifier!r}")
    api_key = resolve_api_key(provider_identifier, configured_keys)
    api_base = ""
    if definition.openai_compatible:
        api_base = resolve_base_url(provider_identifier, configured_bases)
    return {
        "model": f"{definition.litellm_prefix}/{suffix}",
        "api_key": api_key,
        "api_base": api_base,
    }
