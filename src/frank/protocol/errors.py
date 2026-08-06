"""Turn failures, classified into something a client can act on."""

from __future__ import annotations

from litellm import exceptions as litellm_exceptions

from frank.base.model_errors import CONTEXT_OVERFLOW_CODES, ContextWindowExceeded


def _provider_error_body(error: object) -> dict:
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        return body
    response = getattr(error, "response", None)
    if response is None:
        return {}
    try:
        parsed = response.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _provider_error_code(error: object) -> str:
    code = getattr(error, "code", None)
    if code:
        return str(code)
    body = _provider_error_body(error)
    nested = body.get("error") if isinstance(body.get("error"), dict) else body
    return str(nested.get("code") or "") if isinstance(nested, dict) else ""


def _provider_status_code(error: object) -> int | None:
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _safe_turn_error(error: object, had_images: bool = False) -> dict[str, object]:
    """Classify a turn-level failure without exposing raw provider/tool text."""
    status_code = _provider_status_code(error)
    provider_code = _provider_error_code(error)
    fields: dict[str, object] = {}
    if status_code is not None:
        fields["status"] = status_code

    # Its own failure, ahead of every status-code test, because it is the one the harness caused and the one a user can act on.
    overflow = error if isinstance(error, ContextWindowExceeded) else None
    if (overflow is not None
            or isinstance(error, litellm_exceptions.ContextWindowExceededError)
            or provider_code in CONTEXT_OVERFLOW_CODES):
        window = getattr(overflow, "context_window", 0) or 0
        model = getattr(overflow, "model", "") or ""
        tokens = getattr(overflow, "tokens", None)
        # Said only when it is known, and said as the measurement it is.
        if window and tokens:
            measured = f" — about {tokens:,} tokens against a {window:,}-token window"
        elif window:
            measured = f" ({window:,} tokens" + (f", {model}" if model else "") + ")"
        else:
            measured = ""
        return {
            **fields,
            "code": "context_window_exceeded",
            "title": "Conversation is too long for this model",
            "message": (
                f"The request was larger than this model's context window{measured}. "
                "Compact the conversation, start a new one, or switch to a model with a larger "
                "window. A single tool result — a long file or a screen listing — is the usual "
                "cause."
            ),
        }
    # `provider_code` classifies the failure below (e.g. context-length) but is not part of the wire ErrorEvent — the client keys off `code`/`status`, never the raw provider code.

    # A turn with an image that the provider rejects almost always means the agent model is text-only — the most common, most actionable cause, and one the raw "invalid request" gives no hint of.
    if had_images and (isinstance(error, litellm_exceptions.BadRequestError) or status_code == 400):
        return {
            **fields,
            "code": "image_unsupported",
            "title": "This model can't read images",
            "message": "The agent's model rejected the attached image — it looks like a text-only model. Configure a vision-capable model for this agent and try again.",
        }

    if isinstance(error, litellm_exceptions.RateLimitError) or status_code == 429:
        return {
            **fields,
            "code": "rate_limited",
            "title": "Model rate limit reached",
            "message": "The selected provider is rate limiting requests. Wait a bit or switch to another model.",
        }
    if isinstance(error, litellm_exceptions.AuthenticationError) or status_code in {401, 403}:
        return {
            **fields,
            "code": "authentication_failed",
            "title": "Provider credentials need attention",
            "message": "The selected provider rejected the configured credentials. Check the API key or choose another model.",
        }
    if isinstance(error, (litellm_exceptions.ServiceUnavailableError, litellm_exceptions.InternalServerError)) or status_code in {500, 502, 503, 504}:
        return {
            **fields,
            "code": "provider_unavailable",
            "title": "Model temporarily unavailable",
            "message": "The agent's model provider is temporarily unavailable. Try again in a moment or configure a different model for this agent.",
        }
    if isinstance(error, (litellm_exceptions.APIConnectionError, litellm_exceptions.Timeout, TimeoutError)) or status_code == 408:
        return {
            **fields,
            "code": "connection_failed",
            "title": "Connection interrupted",
            "message": "The model connection dropped before the turn finished. Check the connection and retry.",
        }
    if isinstance(error, litellm_exceptions.BadRequestError) or status_code == 400:
        # The overflow codes are tested above, against every error rather than only against a 400, because a streaming provider reports this failure mid-stream where there is no status code to key off at all.
        return {
            **fields,
            "code": "request_rejected",
            "title": "Model rejected the request",
            "message": "The model could not accept this turn. Adjust the request or switch models.",
        }
    return {
        **fields,
        "code": "turn_failed",
        "title": "Turn could not complete",
        "message": "The turn stopped unexpectedly. The raw details were written to the server log.",
    }
