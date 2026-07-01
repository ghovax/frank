from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderDefinition:
    """One routable LLM provider.

    The ``litellm_prefix`` is the LiteLLM model prefix (the segment before the
    first ``/`` in the model string LiteLLM receives). OpenAI-compatible servers
    (the OpenCode Go gateway, a user-declared custom server, and the
    OpenAI-compatible SaaS family) all ride the ``"openai"`` prefix with a custom
    ``api_base``; first-party clouds use their native prefix (``anthropic``,
    ``gemini``, …). The env-var list mirrors opencode's provider credential table
    and is consulted, in order, only when no key is stored in configuration.
    """

    identifier: str
    name: str
    litellm_prefix: str
    env_vars: tuple[str, ...] = ()
    default_base_url: str = ""
    openai_compatible: bool = False
    # Whether this provider is surfaced as a pickable source of models in the UI.
    # The opencode gateway and any custom server are; the bare "custom" bucket is
    # addressed by the custom provider instead.
    selectable: bool = True


# The order here is the order models are grouped in the picker. opencode first so
# the out-of-the-box default stays the most prominent, then the first-party clouds,
# then the OpenAI-compatible SaaS family, then the user's own server.
PROVIDERS: dict[str, ProviderDefinition] = {
    provider.identifier: provider
    for provider in [
        ProviderDefinition(
            identifier="opencode",
            name="OpenCode Go",
            litellm_prefix="openai",
            env_vars=("OPENCODE_API_KEY",),
            default_base_url="https://opencode.ai/zen/go/v1",
            openai_compatible=True,
        ),
        ProviderDefinition(
            identifier="anthropic",
            name="Anthropic",
            litellm_prefix="anthropic",
            env_vars=("ANTHROPIC_API_KEY",),
        ),
        ProviderDefinition(
            identifier="openai",
            name="OpenAI",
            litellm_prefix="openai",
            env_vars=("OPENAI_API_KEY",),
        ),
        ProviderDefinition(
            identifier="google",
            name="Google Gemini",
            litellm_prefix="gemini",
            env_vars=("GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_API_KEY"),
        ),
        ProviderDefinition(
            identifier="openrouter",
            name="OpenRouter",
            litellm_prefix="openrouter",
            env_vars=("OPENROUTER_API_KEY",),
        ),
        ProviderDefinition(
            identifier="xai",
            name="xAI",
            litellm_prefix="xai",
            env_vars=("XAI_API_KEY",),
        ),
        ProviderDefinition(
            identifier="deepseek",
            name="DeepSeek",
            litellm_prefix="deepseek",
            env_vars=("DEEPSEEK_API_KEY",),
        ),
        ProviderDefinition(
            identifier="groq",
            name="Groq",
            litellm_prefix="groq",
            env_vars=("GROQ_API_KEY",),
        ),
        ProviderDefinition(
            identifier="mistral",
            name="Mistral",
            litellm_prefix="mistral",
            env_vars=("MISTRAL_API_KEY",),
        ),
        ProviderDefinition(
            identifier="custom",
            name="Custom (OpenAI-compatible)",
            litellm_prefix="openai",
            env_vars=(),
            openai_compatible=True,
        ),
    ]
}


def get_provider_definition(provider_identifier: str) -> ProviderDefinition | None:
    return PROVIDERS.get(provider_identifier)


def resolve_api_key(
    provider_identifier: str,
    configured_keys: dict[str, str],
) -> str:
    """Resolve a provider's API key: an explicit configured value wins, then the
    provider's env vars in order. Returns an empty string when nothing is set, so
    ``available_models`` can treat a provider as locked until credentials arrive."""
    configured = configured_keys.get(provider_identifier, "")
    if configured:
        return configured
    definition = PROVIDERS.get(provider_identifier)
    if definition is None:
        return ""
    for env_var in definition.env_vars:
        value = os.environ.get(env_var, "")
        if value:
            return value
    return ""


def resolve_base_url(
    provider_identifier: str,
    configured_bases: dict[str, str],
) -> str:
    """Resolve a provider's base URL: an explicit configured value wins, then the
    provider's default. Only meaningful for OpenAI-compatible providers; the
    first-party clouds ignore it (LiteLLM knows their endpoints)."""
    configured = configured_bases.get(provider_identifier, "")
    if configured:
        return configured
    definition = PROVIDERS.get(provider_identifier)
    return definition.default_base_url if definition is not None else ""
