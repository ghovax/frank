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
    # A "native" provider is not routed through LiteLLM at all — it has its own
    # chat-model implementation and its own (non-API-key) auth. The experimental
    # ``chatgpt`` subscription provider is the only one: it signs in over OAuth and
    # calls Codex's Responses endpoint directly (see harness.core.codex_model), so
    # ``litellm_prefix`` is meaningless for it and credential resolution is bypassed.
    native: bool = False


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
            # Experimental: pay for model calls with a ChatGPT subscription instead of
            # an API key, by impersonating the Codex CLI. Unlocked by an OAuth sign-in
            # (Settings), not a stored key; routed through its own model client.
            identifier="chatgpt",
            name="ChatGPT Subscription Plan",
            litellm_prefix="",
            env_vars=(),
            native=True,
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
            identifier="zai",
            name="Zhipu AI",
            litellm_prefix="zai",
            env_vars=("ZAI_API_KEY",),
        ),
        ProviderDefinition(
            identifier="zai_code",
            name="Zhipu AI Coding Plan",
            litellm_prefix="openai",
            env_vars=("ZAI_API_KEY",),
            default_base_url="https://api.z.ai/api/coding/paas/v4",
            openai_compatible=True,
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
            identifier="meta_llama",
            name="Meta Llama",
            litellm_prefix="meta_llama",
            env_vars=(),
        ),
        ProviderDefinition(
            identifier="ai21",
            name="AI21",
            litellm_prefix="ai21",
            env_vars=("AI21_API_KEY",),
        ),
        ProviderDefinition(
            identifier="cerebras",
            name="Cerebras",
            litellm_prefix="cerebras",
            env_vars=("CEREBRAS_API_KEY",),
        ),
        ProviderDefinition(
            identifier="cohere",
            name="Cohere",
            litellm_prefix="cohere",
            env_vars=("COHERE_API_KEY",),
        ),
        ProviderDefinition(
            identifier="databricks",
            name="Databricks",
            litellm_prefix="databricks",
            env_vars=("DATABRICKS_API_KEY",),
        ),
        ProviderDefinition(
            identifier="deepinfra",
            name="DeepInfra",
            litellm_prefix="deepinfra",
            env_vars=("DEEPINFRA_API_KEY",),
        ),
        ProviderDefinition(
            identifier="fireworks_ai",
            name="Fireworks AI",
            litellm_prefix="fireworks_ai",
            env_vars=("FIREWORKS_AI_API_KEY", "FIREWORKS_API_KEY"),
        ),
        ProviderDefinition(
            identifier="hyperbolic",
            name="Hyperbolic",
            litellm_prefix="hyperbolic",
            env_vars=("HYPERBOLIC_API_KEY",),
        ),
        ProviderDefinition(
            identifier="lambda_ai",
            name="Lambda AI",
            litellm_prefix="lambda_ai",
            env_vars=("LAMBDA_API_KEY",),
        ),
        ProviderDefinition(
            identifier="minimax",
            name="MiniMax",
            litellm_prefix="minimax",
            env_vars=("MINIMAX_API_KEY",),
        ),
        ProviderDefinition(
            identifier="novita",
            name="Novita AI",
            litellm_prefix="novita",
            env_vars=("NOVITA_API_KEY",),
        ),
        ProviderDefinition(
            identifier="perplexity",
            name="Perplexity AI",
            litellm_prefix="perplexity",
            env_vars=("PERPLEXITYAI_API_KEY",),
        ),
        ProviderDefinition(
            identifier="sambanova",
            name="SambaNova",
            litellm_prefix="sambanova",
            env_vars=("SAMBA_NOVA_API_KEY",),
        ),
        ProviderDefinition(
            identifier="together_ai",
            name="Together AI",
            litellm_prefix="together_ai",
            env_vars=("TOGETHERAI_API_KEY",),
        ),
        ProviderDefinition(
            identifier="oci",
            name="OCI",
            litellm_prefix="oci",
            env_vars=("OCI_API_KEY",),
        ),
        ProviderDefinition(
            identifier="friendliai",
            name="FriendliAI",
            litellm_prefix="friendliai",
            env_vars=("FRIENDLI_TOKEN",),
        ),
        ProviderDefinition(
            identifier="github_copilot",
            name="GitHub Copilot",
            litellm_prefix="github_copilot",
            env_vars=("GITHUB_TOKEN",),
        ),
        ProviderDefinition(
            identifier="moonshot",
            name="Moonshot AI",
            litellm_prefix="moonshot",
            env_vars=("MOONSHOT_API_KEY",),
        ),
        ProviderDefinition(
            identifier="nebius",
            name="Nebius AI Studio",
            litellm_prefix="nebius",
            env_vars=("NEBIUS_API_KEY",),
        ),
        ProviderDefinition(
            identifier="nscale",
            name="Nscale",
            litellm_prefix="nscale",
            env_vars=("NSCALE_API_KEY",),
        ),
        ProviderDefinition(
            identifier="ovhcloud",
            name="OVHcloud",
            litellm_prefix="ovhcloud",
            env_vars=("OVHCLOUD_API_KEY",),
        ),
        ProviderDefinition(
            identifier="scaleway",
            name="Scaleway",
            litellm_prefix="openai",
            env_vars=("SCALEWAY_API_KEY",),
            default_base_url="https://api.scaleway.ai/v1",
            openai_compatible=True,
        ),
        ProviderDefinition(
            identifier="volcengine",
            name="Volcengine",
            litellm_prefix="volcengine",
            env_vars=("VOLCENGINE_API_KEY",),
        ),
        ProviderDefinition(
            identifier="cloudflare",
            name="Cloudflare Workers AI",
            litellm_prefix="cloudflare",
            env_vars=("CLOUDFLARE_API_KEY", "CLOUDFLARE_ACCOUNT_ID"),
        ),
        ProviderDefinition(
            identifier="featherless_ai",
            name="Featherless AI",
            litellm_prefix="featherless_ai",
            env_vars=("FEATHERLESS_API_KEY",),
        ),
        ProviderDefinition(
            identifier="inception",
            name="Inception",
            litellm_prefix="inception",
            env_vars=("INCEPTION_API_KEY",),
        ),
        ProviderDefinition(
            identifier="maritalk",
            name="MariTalk",
            litellm_prefix="maritalk",
            env_vars=("MARITALK_API_KEY",),
        ),
        ProviderDefinition(
            identifier="morph",
            name="Morph",
            litellm_prefix="morph",
            env_vars=("MORPH_API_KEY",),
        ),
        ProviderDefinition(
            identifier="wandb",
            name="Weights & Biases",
            litellm_prefix="wandb",
            env_vars=("WANDB_API_KEY",),
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
