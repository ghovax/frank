from __future__ import annotations

import logging
from typing import Any

import pytest

import frank.base.models as model_catalog
from frank.base.models import ModelDefinition, available_models, resolve_litellm
from frank.base.providers import PROVIDERS, resolve_api_key


class CatalogResponse:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


@pytest.fixture
def open_code_catalog(monkeypatch: pytest.MonkeyPatch):
    payload = {
        "opencode": {
            "npm": "@ai-sdk/openai-compatible",
            "models": {
                "gpt": {"name": "GPT", "provider": {"npm": "@ai-sdk/openai"}},
                "claude": {"name": "Claude", "provider": {"npm": "@ai-sdk/anthropic"}},
                "gemini": {"name": "Gemini", "provider": {"npm": "@ai-sdk/google"}},
                "deepseek": {"name": "DeepSeek"},
            },
        },
        "opencode-go": {
            "npm": "@ai-sdk/openai-compatible",
            "models": {
                "gpt": {"name": "GPT Go", "provider": {"npm": "@ai-sdk/openai"}},
                "qwen": {"name": "Qwen", "provider": {"npm": "@ai-sdk/anthropic"}},
            },
        },
    }
    monkeypatch.setattr(model_catalog.httpx, "get", lambda *_arguments, **_keywords: CatalogResponse(payload))
    model_catalog.clear_catalogue_cache()
    yield model_catalog.list_models()
    model_catalog.clear_catalogue_cache()


def test_open_code_providers_are_distinct_and_share_one_credential() -> None:
    zen = PROVIDERS["opencode"]
    go = PROVIDERS["opencode_go"]

    assert zen.name == "OpenCode Zen"
    assert zen.default_base_url == "https://opencode.ai/zen/v1"
    assert go.name == "OpenCode Go"
    assert go.default_base_url == "https://opencode.ai/zen/go/v1"
    assert go.credential_identifier == "opencode"
    assert resolve_api_key("opencode_go", {"opencode": "shared-key"}) == "shared-key"


def test_catalog_keeps_gateways_separate_and_preserves_protocols(open_code_catalog: list[ModelDefinition]) -> None:
    by_identifier = {model.identifier: model for model in open_code_catalog}

    assert by_identifier["opencode/gpt"].litellm_prefix == "openai/responses"
    assert by_identifier["opencode/claude"].litellm_prefix == "anthropic"
    assert by_identifier["opencode/gemini"].litellm_prefix == "gemini"
    assert by_identifier["opencode/deepseek"].litellm_prefix == "openai"
    assert by_identifier["opencode_go/gpt"].litellm_prefix == "openai/responses"
    assert by_identifier["opencode_go/qwen"].litellm_prefix == "anthropic"
    assert by_identifier["opencode/gpt"].name == "GPT"
    assert by_identifier["opencode_go/gpt"].name == "GPT Go"


def test_resolver_uses_each_gateway_endpoint_and_model_protocol(open_code_catalog: list[ModelDefinition]) -> None:
    assert open_code_catalog
    configured_keys = {"opencode": "shared-key"}

    assert resolve_litellm("opencode/gpt", configured_keys, {}) == {
        "model": "openai/responses/gpt",
        "api_key": "shared-key",
        "api_base": "https://opencode.ai/zen/v1",
    }
    # Anthropic is the one protocol whose path we finish ourselves: LiteLLM appends
    # `/v1/messages` to a custom base unless it already ends that way, so stopping at the
    # gateway's `/v1` would ask for `…/zen/v1/v1/messages`.
    assert resolve_litellm("opencode/claude", configured_keys, {}) == {
        "model": "anthropic/claude",
        "api_key": "shared-key",
        "api_base": "https://opencode.ai/zen/v1/messages",
    }
    # Gemini keeps the bare gateway base. LiteLLM's `_check_custom_proxy` renders it as
    # `{base}/models/{model}:{generateContent|streamGenerateContent}` and chooses the verb per
    # call, so anything composed here would be composed a second time.
    assert resolve_litellm("opencode/gemini", configured_keys, {}) == {
        "model": "gemini/gemini",
        "api_key": "shared-key",
        "api_base": "https://opencode.ai/zen/v1",
    }
    assert resolve_litellm("opencode_go/qwen", configured_keys, {}) == {
        "model": "anthropic/qwen",
        "api_key": "shared-key",
        "api_base": "https://opencode.ai/zen/go/v1/messages",
    }


def test_a_catalog_model_without_an_override_keeps_its_provider_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-model prefix is an override, and an absent one means "the provider's own".

    Only the multi-protocol gateways set it; every other model in the catalogue leaves it empty.
    Reading it as the answer whenever a catalogue entry existed dropped the prefix altogether, so
    `anthropic/claude-…` resolved to `/claude-…` and routed nowhere — and only once the catalogue
    had been fetched, which is why a cold start looked healthy.
    """
    payload = {"anthropic": {"models": {"claude-sonnet-4-5": {"name": "Claude Sonnet 4.5"}}}}
    monkeypatch.setattr(model_catalog.httpx, "get", lambda *_arguments, **_keywords: CatalogResponse(payload))
    model_catalog.clear_catalogue_cache()
    try:
        catalogued = model_catalog.find_model("anthropic/claude-sonnet-4-5")
        assert catalogued is not None and catalogued.litellm_prefix == ""
        assert resolve_litellm("anthropic/claude-sonnet-4-5", {"anthropic": "key"}, {})["model"] == (
            "anthropic/claude-sonnet-4-5"
        )
    finally:
        model_catalog.clear_catalogue_cache()


def test_one_open_code_key_unlocks_both_catalogs(open_code_catalog: list[ModelDefinition]) -> None:
    available_identifiers = {model.identifier for model in available_models({"opencode": "shared-key"})}

    assert "opencode/gpt" in available_identifiers
    assert "opencode_go/qwen" in available_identifiers


def test_unknown_gateway_protocol_is_omitted(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    payload = {
        "opencode": {
            "npm": "@unsupported/sdk",
            "models": {"unknown": {"name": "Unknown"}},
        },
    }
    monkeypatch.setattr(model_catalog.httpx, "get", lambda *_arguments, **_keywords: CatalogResponse(payload))

    with caplog.at_level(logging.WARNING, logger=model_catalog.__name__):
        models = model_catalog._catalog()

    assert models == []
    assert caplog.messages == [
        "skipping opencode/unknown because its models.dev protocol '@unsupported/sdk' is unsupported"
    ]
