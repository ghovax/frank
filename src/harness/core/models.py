from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from harness.core.providers import (
    get_provider_definition,
    resolve_api_key,
    resolve_base_url,
)


@dataclass(frozen=True)
class ModelDefinition:
    """A pickable model. The ``identifier`` is the canonical, provider-namespaced
    technical id a user references the model by (``anthropic/claude-sonnet-4``,
    ``opencode/deepseek-v4-flash``); ``name`` is the user-facing label. Both mirror
    the id/name pair the skills and agents use elsewhere, so the picker, the
    configuration, and the persisted per-session override all speak the same id.

    ``curated`` is ``True`` for entries hand-written in ``models.json`` (which
    carry a human-readable name) and ``False`` for auto-discovered entries (whose
    ``name`` field holds the raw model suffix the frontend renders in monospace).
    """

    identifier: str
    name: str
    provider: str
    curated: bool = False


def _load_catalog() -> list[ModelDefinition]:
    # The catalog lives in a sibling JSON file so adding or trimming a model is a
    # data edit, not a code change. The shape is designed so an automatic
    # discover_models (OpenAI /v1/models, Ollama /api/tags, ...) can append fetched
    # entries to the file later without touching this loader.
    catalog_path = Path(__file__).resolve().parent / "models.json"
    raw_entries = json.loads(catalog_path.read_text())
    return [
        ModelDefinition(
            identifier=entry["id"],
            name=entry["name"],
            provider=entry["provider"],
            curated=True,
        )
        for entry in raw_entries
    ]



def _merged_catalog() -> list[ModelDefinition]:
    # Only models.json — no LiteLLM auto-discovery. The curated file keeps the
    # catalog focused on current, well-known models without old/experimental
    # noise that LiteLLM's indiscriminate catalog pulls in.
    return list(_load_catalog())


MODELS: list[ModelDefinition] = _merged_catalog()


def list_models() -> list[ModelDefinition]:
    return list(MODELS)


def find_model(model_identifier: str) -> ModelDefinition | None:
    for model in MODELS:
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
    unlocked by an explicit configured key or any of its env vars."""
    unlocked_providers = {
        provider.identifier
        for provider in PROVIDERS.values()
        if resolve_api_key(provider.identifier, configured_keys)
        # The custom provider has no key of its own; it is selectable on demand.
        or provider.identifier == "custom"
    }
    return [model for model in MODELS if model.provider in unlocked_providers]


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
