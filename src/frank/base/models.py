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
    """A pickable model: its canonical provider-namespaced id, and the name a person reads."""

    identifier: str
    name: str
    provider: str
    # Capabilities from the catalog, used to gate and annotate the interface.
    attachment: bool = False
    vision: bool = False
    input_modalities: tuple[str, ...] = ()
    # Maximum input context in tokens from the catalog, with 0 meaning unknown.
    context_length: int = 0
    # Release date from the catalog, on which the picker sorts newest-first rather than alphabetically.
    release_date: str = ""
    # A per-model override for gateways that expose several wire protocols.
    litellm_prefix: str = ""


# Map catalog provider ids to our local ones, which differ in case convention.
_GATEWAY_LITELLM_PREFIXES = {
    "@ai-sdk/openai-compatible": "openai",
    "@ai-sdk/openai": "openai/responses",
    "@ai-sdk/anthropic": "anthropic",
    "@ai-sdk/google": "gemini",
}


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
    "azure": "azure",
    "alibaba": "alibaba",
    "vercel": "vercel",
    # Mismatched names
    "amazon-bedrock": "amazon_bedrock",
    "google-vertex": "google_vertex",
    "google-vertex-anthropic": "google_vertex_anthropic",
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
    "opencode-go": "opencode_go",
    "kimi-for-coding": "moonshot",
    "minimax-cn": "minimax",
    "minimax-coding-plan": "minimax",
    "minimax-cn-coding-plan": "minimax",
    "moonshotai-cn": "moonshot",
    "scaleway": "scaleway",
}


def _catalog() -> list[ModelDefinition]:
    """The model catalog from models.dev, best effort so the harness still starts without one."""
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
            # Stripped, because the catalogue is community-maintained and some names carry stray whitespace.
            name = (model_info.get("name", "") or model_id).strip() or model_id
            identifier = f"{local_id}/{model_id}"
            modalities = model_info.get("modalities") or {}
            input_modalities = tuple(
                str(modality) for modality in (modalities.get("input") or []) if modality
            )
            litellm_prefix = ""
            if local_id in {"opencode", "opencode_go"}:
                model_provider = model_info.get("provider") or {}
                sdk_package = model_provider.get("npm") or provider_info.get("npm") or ""
                litellm_prefix = _GATEWAY_LITELLM_PREFIXES.get(str(sdk_package), "")
                if not litellm_prefix:
                    logging.getLogger(__name__).warning(
                        "skipping %s because its models.dev protocol %r is unsupported",
                        identifier,
                        sdk_package,
                    )
                    continue
            models.setdefault(identifier, ModelDefinition(
                identifier=identifier,
                name=name,
                provider=local_id,
                attachment=bool(model_info.get("attachment")),
                vision="image" in input_modalities,
                input_modalities=input_modalities,
                context_length=int((model_info.get("limit") or {}).get("context") or 0),
                release_date=str(model_info.get("release_date") or "").strip(),
                litellm_prefix=litellm_prefix,
            ))
    return list(models.values())


# Which OpenAI models the Codex endpoint serves, as an allow and deny set plus a version rule.
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
    """The `chatgpt` subscription models, filtered from the catalog's OpenAI entries so new ones appear automatically."""
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


# The `cursor` provider contributes nothing here by design, since its models are an account fact.
_catalogue_cache: list[ModelDefinition] | None = None
_catalogue_lock = threading.Lock()


def list_models() -> list[ModelDefinition]:
    """The model catalogue, fetched on first use and cached, as a function because building it blocks on the network."""
    global _catalogue_cache
    if _catalogue_cache is not None:
        return list(_catalogue_cache)
    with _catalogue_lock:
        if _catalogue_cache is None:
            base = _catalog()
            _catalogue_cache = base + _chatgpt_models(base)
        return list(_catalogue_cache)


def clear_catalogue_cache() -> None:
    """Drop the cached catalogue so the next listing refetches."""
    global _catalogue_cache
    _catalogue_cache = None


def find_model(model_identifier: str) -> ModelDefinition | None:
    for model in list_models():
        if model.identifier == model_identifier:
            return model
    return None


def provider_and_suffix(model_identifier: str) -> tuple[str, str] | None:
    """Split a model id into its provider and suffix, on the first slash only."""
    if "/" not in model_identifier:
        return None
    provider_identifier, suffix = model_identifier.split("/", 1)
    return provider_identifier, suffix


def available_models(configured_keys: dict[str, str]) -> list[ModelDefinition]:
    """Catalog entries whose provider has a resolvable credential, excluding the subscription providers."""
    unlocked_providers = {
        provider.identifier
        for provider in PROVIDERS.values()
        if resolve_api_key(provider.identifier, configured_keys)
        # The custom provider has no key of its own; it is selectable on demand.
        or provider.identifier == "custom"
    }
    return [model for model in list_models() if model.provider in unlocked_providers]


def _gateway_api_base(provider_base_url: str, litellm_prefix: str) -> str:
    """The base URL to hand LiteLLM for a gateway serving several wire protocols from one host."""
    if litellm_prefix == "anthropic":
        return f"{provider_base_url.rstrip('/')}/messages"
    return provider_base_url


def resolve_litellm(
    model_identifier: str,
    configured_keys: dict[str, str],
    configured_bases: dict[str, str],
) -> dict[str, str]:
    """Translate a provider-qualified model into LiteLLM call parameters."""
    split = provider_and_suffix(model_identifier)
    if split is None:
        raise ValueError(f"Model id has no provider prefix: {model_identifier!r}")
    provider_identifier, suffix = split
    definition: ProviderDefinition | None = get_provider_definition(provider_identifier)
    if definition is None:
        raise ValueError(f"Unknown provider in model id: {model_identifier!r}")
    catalog_model = find_model(model_identifier)
    # The catalogue's prefix is an override set only for multi-protocol gateways, so an empty one means the provider's own.
    litellm_prefix = (catalog_model.litellm_prefix if catalog_model else "") or definition.litellm_prefix
    provider_base_url = (
        resolve_base_url(provider_identifier, configured_bases)
        if definition.uses_custom_base_url or definition.openai_compatible
        else ""
    )
    return {
        "model": f"{litellm_prefix}/{suffix}",
        "api_key": resolve_api_key(provider_identifier, configured_keys),
        "api_base": (
            _gateway_api_base(provider_base_url, litellm_prefix)
            if definition.uses_custom_base_url else provider_base_url
        ),
    }
