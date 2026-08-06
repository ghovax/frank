from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderDefinition:
    """One routable LLM provider."""

    identifier: str
    name: str
    litellm_prefix: str
    env_vars: tuple[str, ...] = ()
    default_base_url: str = ""
    openai_compatible: bool = False
    uses_custom_base_url: bool = False
    credential_identifier: str = ""
    # Whether this provider is surfaced as a pickable source of models in the UI.
    selectable: bool = True
    # A "native" provider is not routed through LiteLLM at all — it has its own chat-model implementation and its own (non-API-key) auth.
    native: bool = False


# The order here is the order models are grouped in the picker.
PROVIDERS: dict[str, ProviderDefinition] = {
    provider.identifier: provider
    for provider in [
        ProviderDefinition(
            identifier="opencode",
            name="OpenCode Zen",
            litellm_prefix="openai",
            env_vars=("OPENCODE_API_KEY",),
            default_base_url="https://opencode.ai/zen/v1",
            uses_custom_base_url=True,
        ),
        ProviderDefinition(
            identifier="opencode_go",
            name="OpenCode Go",
            litellm_prefix="openai",
            env_vars=("OPENCODE_API_KEY",),
            default_base_url="https://opencode.ai/zen/go/v1",
            uses_custom_base_url=True,
            credential_identifier="opencode",
        ),
        ProviderDefinition(
            identifier="anthropic",
            name="Anthropic",
            litellm_prefix="anthropic",
            env_vars=("ANTHROPIC_API_KEY",),
        ),
        # The three big clouds' own resale of the frontier models.
        ProviderDefinition(
            identifier="amazon_bedrock",
            name="Amazon Bedrock",
            litellm_prefix="bedrock",
            # Bedrock's own API keys first, then the classic access-key pair.
            env_vars=("AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID"),
        ),
        ProviderDefinition(
            identifier="google_vertex",
            name="Google Vertex AI",
            litellm_prefix="vertex_ai",
            env_vars=("GOOGLE_APPLICATION_CREDENTIALS", "VERTEXAI_PROJECT"),
        ),
        ProviderDefinition(
            identifier="google_vertex_anthropic",
            name="Claude on Vertex AI",
            # The same LiteLLM route: it reads Claude on Vertex from the model id rather than from a separate provider.
            litellm_prefix="vertex_ai",
            env_vars=("GOOGLE_APPLICATION_CREDENTIALS", "VERTEXAI_PROJECT"),
            credential_identifier="google_vertex",
        ),
        ProviderDefinition(
            identifier="azure",
            name="Azure OpenAI",
            litellm_prefix="azure",
            env_vars=("AZURE_API_KEY",),
            # Every Azure account has its own resource host, so there is no default worth registering — the base URL is the deployment.
            uses_custom_base_url=True,
        ),
        ProviderDefinition(
            identifier="alibaba",
            name="Alibaba Model Studio",
            litellm_prefix="dashscope",
            env_vars=("DASHSCOPE_API_KEY",),
        ),
        ProviderDefinition(
            identifier="vercel",
            name="Vercel AI Gateway",
            litellm_prefix="vercel_ai_gateway",
            env_vars=("VERCEL_AI_GATEWAY_API_KEY", "AI_GATEWAY_API_KEY"),
        ),
        ProviderDefinition(
            identifier="openai",
            name="OpenAI",
            litellm_prefix="openai",
            env_vars=("OPENAI_API_KEY",),
        ),
        ProviderDefinition(
            # Experimental: pay for model calls with a ChatGPT subscription instead of an API key, by impersonating the Codex CLI.
            identifier="chatgpt",
            name="ChatGPT Subscription Plan",
            litellm_prefix="",
            env_vars=(),
            native=True,
        ),
        ProviderDefinition(
            # Experimental: pay for model calls with a Cursor subscription instead of an API key, by using the login flow Cursor's own CLI uses.
            identifier="cursor",
            name="Cursor Subscription Plan",
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
    """Resolve a provider key from its shared credential, then its environment."""
    definition = PROVIDERS.get(provider_identifier)
    if definition is None:
        return ""
    credential_identifier = definition.credential_identifier or provider_identifier
    configured = configured_keys.get(credential_identifier, "")
    if configured:
        return configured
    for environment_variable in definition.env_vars:
        value = os.environ.get(environment_variable, "")
        if value:
            return value
    return ""


def resolve_base_url(
    provider_identifier: str,
    configured_bases: dict[str, str],
) -> str:
    """Resolve a provider's explicit base URL or its registered default."""
    configured = configured_bases.get(provider_identifier, "")
    if configured:
        return configured
    definition = PROVIDERS.get(provider_identifier)
    return definition.default_base_url if definition is not None else ""
