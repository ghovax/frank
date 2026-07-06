from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from harness.core.providers import (
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

    ``curated`` is always ``False`` — all models now come from the models.dev
    discovery API, and the field is retained only for backward compatibility with
    the frontend type.
    """

    identifier: str
    name: str
    provider: str
    curated: bool = False
    # Capabilities from the models.dev catalog, used to gate and annotate the UI.
    # ``attachment`` is whether the model accepts file attachments at all; ``vision``
    # is whether image input is supported; ``input_modalities`` is the raw list
    # (``text``, ``image``, ``pdf``, ``audio``, …) for finer-grained display.
    attachment: bool = False
    vision: bool = False
    input_modalities: tuple[str, ...] = ()


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

    Best-effort — returns an empty list when the API is unreachable so the server
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
        # Skip providers not registered in this version of Daisy
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
                curated=False,
                attachment=bool(model_info.get("attachment")),
                vision="image" in input_modalities,
                input_modalities=input_modalities,
            ))
    return list(models.values())


MODELS: list[ModelDefinition] = _catalog()


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
