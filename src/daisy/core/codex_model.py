"""``ChatCodexModel`` — a LangChain chat model that talks to a ChatGPT subscription.

This is the counterpart to :class:`~daisy.core.litellm_model.ChatLiteLLMModel`
for the experimental ``chatgpt`` provider. It does **not** go through LiteLLM,
because the ChatGPT-subscription route is not the normal OpenAI API: it is Codex's
endpoint ``chatgpt.com/backend-api/codex/responses``, which speaks the OpenAI
*Responses* API (not Chat Completions), is stateless (``store: false`` with the
full history resent every turn), and authenticates with the OAuth token minted for
Codex plus a ``ChatGPT-Account-Id`` header.

It sends daisy's *own* system prompt in the ``instructions`` field like any normal
Responses request — there is no Codex-prompt impersonation. (The widely repeated
claim that the endpoint validates a Codex-specific system prompt does not hold:
opencode's codex plugin sends its own prompt under ``originator: opencode`` and it
works; this mirrors that with ``originator: daisy``.) See
:mod:`daisy.core.chatgpt_oauth` for the auth/token side.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from importlib.metadata import PackageNotFoundError, version as _package_version
from typing import Any, AsyncIterator, Callable, Optional, Sequence, cast

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.content import ContentBlock, ReasoningContentBlock, TextContentBlock
from langchain_core.messages.tool import ToolCallChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from daisy.core.chatgpt_oauth import (
    ChatGPTAuthError,
    ChatGPTTokens,
    load_tokens,
    valid_tokens,
)
from daisy.core.message_content import content_blocks_to_message_content, message_text

RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
# The account's live, plan-specific model catalog. Same host/auth as the responses
# endpoint; `client_version` gates each model by its `minimal_client_version`, hiding
# any model whose floor is above what we claim. This is a *floor to clear*, not a
# cosmetic string: newer models raise their floor over time (gpt-5.4 needs 0.98,
# gpt-5.5 needs 0.124, the gpt-5.6-* family needs 0.144), so this must track a
# current Codex CLI version or the newer models silently vanish from the catalog.
MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
CLIENT_VERSION = "0.144.4"
# Identifies the client to the endpoint. opencode sends its own name here rather
# than the real Codex CLI's — the proof that no exact impersonation is required.
ORIGINATOR = "daisy"

try:
    _DAISY_VERSION = _package_version("daisy")
except PackageNotFoundError:  # not installed as a distribution (editable/source run)
    _DAISY_VERSION = "0"
USER_AGENT = f"daisy/{_DAISY_VERSION}"


class ChatCodexModel(BaseChatModel):
    """A ``BaseChatModel`` backed by the ChatGPT-subscription Codex endpoint.

    Auth is not passed in: the access token and account id are read (and refreshed)
    from the shared token store on every call, so a single sign-in serves every
    agent and a background refresh is transparent to callers. ``context_length`` is
    the cold-start fallback the factory reads from the models.dev catalog, but the
    Codex endpoint enforces a *smaller* budget than models.dev's direct-API metadata
    advertises (e.g. models.dev lists ~1M for gpt-5.5/5.6 while Codex serves 272k),
    so :meth:`context_window` prefers the live per-account value when it is known."""

    model: str
    reasoning_effort: Optional[str] = None
    temperature: float = 0.0
    context_length: int = 0
    # A generous bound so a dead connection cannot hang a turn forever, matching
    # ChatLiteLLMModel; the streaming loop only checks aborts between chunks.
    timeout: Optional[float] = 300.0

    @property
    def _llm_type(self) -> str:
        return "codex"

    def context_window(self) -> int:
        # The live subscription catalog is authoritative for the real Codex budget;
        # fall back to the models.dev-derived value only until that cache is warm.
        live = cached_subscription_models().get(self.model)
        if live and live.get("context"):
            return int(live["context"])
        return self.context_length

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model, "reasoning_effort": self.reasoning_effort}

    # Tool binding — identical surface to ChatLiteLLMModel so the harness binds the
    # same way; the Responses-shaped flattening happens at payload-build time.

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: Optional[str] = None,
        parallel_tool_calls: Optional[bool] = None,
        **kwargs: Any,
    ) -> Runnable:
        formatted_tools = [convert_to_openai_tool(tool) for tool in tools]
        bound: dict[str, Any] = {"tools": formatted_tools}
        if tool_choice is not None:
            bound["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            bound["parallel_tool_calls"] = parallel_tool_calls
        return self.bind(**bound, **kwargs)

    # Request construction.

    @staticmethod
    def _text_of(message: BaseMessage) -> str:
        return message_text(message)

    def _to_responses_input(self, messages: Sequence[BaseMessage]) -> tuple[str, list[dict[str, Any]]]:
        """Translate LangChain messages into a Responses ``(instructions, input)``
        pair. daisy's own system prompt(s) go straight into ``instructions`` (the
        standard Responses home for system text) — no Codex preamble. Tool
        calls/results become top-level ``function_call`` / ``function_call_output``
        items (Responses does not nest them in a message)."""
        system_texts: list[str] = []
        items: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, SystemMessage):
                text = self._text_of(message)
                if text:
                    system_texts.append(text)
                continue
            if isinstance(message, ToolMessage):
                items.append({
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": self._text_of(message),
                })
                continue
            if isinstance(message, AIMessage):
                text = self._text_of(message)
                if text:
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    })
                for call in message.tool_calls or []:
                    arguments = call.get("args")
                    serialized = arguments if isinstance(arguments, str) else json.dumps(arguments)
                    items.append({
                        "type": "function_call",
                        "call_id": call.get("id"),
                        "name": call.get("name"),
                        "arguments": serialized,
                    })
                continue
            # HumanMessage and anything else are user input.
            items.append({
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": self._text_of(message)}],
            })
        instructions = "\n\n".join(text for text in system_texts if text)
        return instructions, items

    @staticmethod
    def _to_responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
        # Chat-Completions tools nest name/description/parameters under "function";
        # the Responses API expects them flattened onto the tool object.
        function = tool.get("function", tool)
        return {
            "type": "function",
            "name": function.get("name"),
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {}),
            "strict": False,
        }

    def _build_payload(self, messages: Sequence[BaseMessage], *, stream: bool, **kwargs: Any) -> dict[str, Any]:
        instructions, input_items = self._to_responses_input(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            # Mandatory: the endpoint rejects requests that omit store or set it true.
            "store": False,
            "stream": stream,
            "parallel_tool_calls": kwargs.get("parallel_tool_calls", True),
        }
        if instructions:
            payload["instructions"] = instructions
        tools = kwargs.get("tools")
        if tools:
            payload["tools"] = [self._to_responses_tool(tool) for tool in tools]
            payload["tool_choice"] = kwargs.get("tool_choice") or "auto"
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort, "summary": "auto"}
        return payload

    @staticmethod
    def _headers_from_tokens(tokens: ChatGPTTokens) -> dict[str, str]:
        # Mirrors the header set opencode's working codex plugin sends: bearer token,
        # the account to bill, an originator/User-Agent naming us, a per-request
        # session id, and the streaming content negotiation.
        return {
            "Authorization": f"Bearer {tokens.access_token}",
            "ChatGPT-Account-Id": tokens.account_id,
            "originator": ORIGINATOR,
            "User-Agent": USER_AGENT,
            "session-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    async def _headers(self) -> dict[str, str]:
        return self._headers_from_tokens(await valid_tokens())

    @staticmethod
    def _http_error(status: int, body: str) -> Exception:
        if status in (401, 403):
            return ChatGPTAuthError(
                "ChatGPT rejected the subscription token (expired, revoked, or plan "
                f"lacks access). Sign in again. Detail: {body[:300]}"
            )
        return RuntimeError(f"ChatGPT Codex endpoint returned {status}: {body[:500]}")

    # SSE event translation. The Responses stream is a sequence of ``data: {json}``
    # lines whose ``type`` field drives the dispatch; we turn each into the same
    # ChatGenerationChunk shape the rest of the harness already merges.

    @classmethod
    def _line_to_chunk(cls, line: str, state: dict[str, Any]) -> Optional[ChatGenerationChunk]:
        if not line or not line.startswith("data:"):
            return None
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        return cls._translate_event(data, state)

    @classmethod
    def _translate_event(cls, data: dict[str, Any], state: dict[str, Any]) -> Optional[ChatGenerationChunk]:
        event_type = data.get("type", "")
        if event_type == "response.output_text.delta":
            return cls._chunk(content_block=cls._text_content_block(data))
        if event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            return cls._chunk(content_block=cls._reasoning_content_block(data))
        if event_type == "response.output_item.added":
            item = data.get("item") or {}
            if item.get("type") == "function_call":
                state["saw_tool_call"] = True
                return cls._chunk(tool_call_chunk={
                    "index": int(data.get("output_index", 0) or 0),
                    "id": item.get("call_id"),
                    "name": item.get("name"),
                    "args": item.get("arguments") or "",
                    "type": "tool_call_chunk",
                })
            return None
        if event_type == "response.function_call_arguments.delta":
            return cls._chunk(tool_call_chunk={
                "index": int(data.get("output_index", 0) or 0),
                "id": None,
                "name": None,
                "args": str(data.get("delta", "")),
                "type": "tool_call_chunk",
            })
        if event_type == "response.completed":
            response = data.get("response") or {}
            usage = cls._usage(response.get("usage"))
            finish = "tool_calls" if state.get("saw_tool_call") else "stop"
            return ChatGenerationChunk(
                message=AIMessageChunk(content="", usage_metadata=usage),
                generation_info={"finish_reason": finish},
            )
        if event_type in ("response.failed", "response.error", "error"):
            response = data.get("response") or {}
            detail = (response.get("error") or data.get("error") or {})
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            raise RuntimeError(f"ChatGPT Codex stream failed: {message or 'unknown error'}")
        return None

    @staticmethod
    def _content_block_index(data: dict[str, Any], block_type: str) -> int:
        output_index = int(data.get("output_index", 0) or 0)
        if block_type == "reasoning":
            content_index = int(data.get("summary_index", data.get("content_index", 0)) or 0)
        else:
            content_index = int(data.get("content_index", 0) or 0)
        coordinate_sum = output_index + content_index
        return coordinate_sum * (coordinate_sum + 1) // 2 + content_index

    @staticmethod
    def _content_block_identifier(data: dict[str, Any]) -> str:
        item_identifier = str(data.get("item_id", ""))
        if item_identifier:
            return item_identifier
        return f"response-output-{int(data.get('output_index', 0) or 0)}"

    @classmethod
    def _text_content_block(cls, data: dict[str, Any]) -> TextContentBlock:
        return TextContentBlock(
            type="text",
            text=str(data.get("delta", "")),
            id=cls._content_block_identifier(data),
            index=cls._content_block_index(data, "text"),
        )

    @classmethod
    def _reasoning_content_block(cls, data: dict[str, Any]) -> ReasoningContentBlock:
        return ReasoningContentBlock(
            type="reasoning",
            reasoning=str(data.get("delta", "")),
            id=cls._content_block_identifier(data),
            index=cls._content_block_index(data, "reasoning"),
        )

    @staticmethod
    def _chunk(
        content_block: ContentBlock | None = None,
        tool_call_chunk: Optional[ToolCallChunk] = None,
    ) -> ChatGenerationChunk:
        content_blocks = [content_block] if content_block is not None else []
        message = AIMessageChunk(
            content=content_blocks_to_message_content(content_blocks),
            tool_call_chunks=[tool_call_chunk] if tool_call_chunk else [],
        )
        return ChatGenerationChunk(message=message)

    @staticmethod
    def _usage(usage: Any) -> Optional[UsageMetadata]:
        if not usage:
            return None
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        if not (input_tokens or output_tokens or total_tokens):
            return None
        metadata: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
        cached = (usage.get("input_tokens_details") or {}).get("cached_tokens")
        if cached:
            metadata["input_token_details"] = {"cache_read": int(cached)}
        reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
        if reasoning:
            metadata["output_token_details"] = {"reasoning": int(reasoning)}
        return cast(UsageMetadata, metadata)

    # Streaming generation (the path the harness actually uses).

    async def _astream(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        payload = self._build_payload(messages, stream=True, **kwargs)
        headers = await self._headers()
        state: dict[str, Any] = {"saw_tool_call": False}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", RESPONSES_URL, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise self._http_error(response.status_code, body)
                _capture_usage_headers(response.headers)
                async for line in response.aiter_lines():
                    chunk = self._line_to_chunk(line, state)
                    if chunk is not None:
                        yield chunk

    @staticmethod
    def _aggregate_to_result(aggregate: Optional[ChatGenerationChunk]) -> ChatResult:
        if aggregate is None:
            return ChatResult(generations=[])
        # The stream only ever yields AIMessageChunk, so narrow from the base type
        # for the tool_calls/usage_metadata accessors below.
        chunk_message = cast(AIMessageChunk, aggregate.message)
        message = AIMessage(
            content=chunk_message.content,
            # AIMessageChunk.tool_calls parses the streamed tool_call_chunks for us.
            tool_calls=list(chunk_message.tool_calls) if chunk_message.tool_calls else None,
            additional_kwargs=chunk_message.additional_kwargs,
            usage_metadata=chunk_message.usage_metadata,
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        aggregate: Optional[ChatGenerationChunk] = None
        async for chunk in self._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            aggregate = chunk if aggregate is None else aggregate + chunk
        return self._aggregate_to_result(aggregate)

    def _generate(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        # BaseChatModel requires a sync path; the harness runs async, so this is a
        # rarely-used fallback that reads the token without the async refresh.
        tokens = load_tokens()
        if tokens is None or tokens.is_expired():
            raise ChatGPTAuthError("Not signed in to ChatGPT (or the session expired).")
        payload = self._build_payload(messages, stream=True, **kwargs)
        headers = self._headers_from_tokens(tokens)
        state: dict[str, Any] = {"saw_tool_call": False}
        aggregate: Optional[ChatGenerationChunk] = None
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", RESPONSES_URL, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    raise self._http_error(response.status_code, response.read().decode("utf-8", "replace"))
                _capture_usage_headers(response.headers)
                for line in response.iter_lines():
                    chunk = self._line_to_chunk(line, state)
                    if chunk is not None:
                        aggregate = chunk if aggregate is None else aggregate + chunk
        return self._aggregate_to_result(aggregate)


# Live per-account model discovery. The subscription serves a plan-specific subset,
# so this is the authoritative "which models actually work" source; the models.dev
# filter is the offline superset the UI greys against.

_MODELS_CACHE_TTL_SECONDS = 60.0
_models_cache: Optional[tuple[float, dict[str, dict[str, Any]]]] = None
_models_cache_lock = asyncio.Lock()


async def fetch_subscription_models() -> dict[str, dict[str, Any]]:
    """The account's live Codex model catalog as ``{slug: {"name", "context"}}``.

    Returns an empty dict when signed out or on any failure (network, auth, parse),
    so callers fall back to the static list. Cached briefly because the ``/models``
    endpoint is polled by the UI and this must not be a network round-trip each time."""
    global _models_cache
    if _models_cache is not None and time.monotonic() - _models_cache[0] < _MODELS_CACHE_TTL_SECONDS:
        return _models_cache[1]
    async with _models_cache_lock:
        if _models_cache is not None and time.monotonic() - _models_cache[0] < _MODELS_CACHE_TTL_SECONDS:
            return _models_cache[1]
        result: dict[str, dict[str, Any]] = {}
        try:
            tokens = await valid_tokens()
            headers = {
                key: value
                for key, value in ChatCodexModel._headers_from_tokens(tokens).items()
                if key != "Accept"  # a plain JSON GET, not an SSE stream
            }
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    MODELS_URL, params={"client_version": CLIENT_VERSION}, headers=headers,
                )
                response.raise_for_status()
                for entry in response.json().get("models", []):
                    slug = entry.get("slug")
                    if not slug:
                        continue
                    result[slug] = {
                        "name": entry.get("display_name") or slug,
                        "context": int(entry.get("context_window") or entry.get("max_context_window") or 0),
                    }
        except (ChatGPTAuthError, httpx.HTTPError, ValueError, KeyError):
            result = {}
        _models_cache = (time.monotonic(), result)
        return result


def cached_subscription_models() -> dict[str, dict[str, Any]]:
    """The last live catalog fetched, without a network round-trip (``{}`` if never
    fetched). For sync callers that only want the freshest *known* value — the UI
    polls ``/models`` constantly, so this is warm in practice."""
    return _models_cache[1] if _models_cache is not None else {}


def clear_subscription_models_cache() -> None:
    """Drop the cached live catalog so the next ``/models`` reflects a fresh sign-in
    or sign-out immediately rather than waiting out the TTL."""
    global _models_cache
    _models_cache = None


# Live per-account usage limits. Every /responses reply carries the account's rate
# limit state in ``x-codex-*`` headers (the short rolling window and the weekly
# window ChatGPT surfaces), so we snapshot the latest as turns run and expose it to
# the UI. There is no cheaper source — the /models GET does not carry these headers
# — so the snapshot is only as fresh as the last turn, and stays None until the
# first turn after sign-in.

_usage_snapshot: Optional[dict[str, Any]] = None


def _header_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _header_int(value: Optional[str]) -> Optional[int]:
    parsed = _header_float(value)
    return int(parsed) if parsed is not None else None


def _header_bool(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("true", "1", "yes")


def _capture_usage_headers(headers: httpx.Headers) -> None:
    """Snapshot the account's rate-limit state from a /responses reply's ``x-codex-*``
    headers. A no-op when they are absent (some error paths omit them), so it never
    clobbers a good snapshot with an empty one."""
    global _usage_snapshot
    if "x-codex-primary-window-minutes" not in headers and "x-codex-plan-type" not in headers:
        return
    now = int(time.time())
    windows: list[dict[str, Any]] = []
    for key in ("primary", "secondary"):
        window_minutes = _header_int(headers.get(f"x-codex-{key}-window-minutes")) or 0
        if window_minutes <= 0:
            continue  # this window is not active for the account right now
        resets_at = _header_int(headers.get(f"x-codex-{key}-reset-at"))
        if resets_at is None:
            after = _header_int(headers.get(f"x-codex-{key}-reset-after-seconds"))
            resets_at = now + after if after is not None else None
        windows.append({
            # The label is derived on the client from window_minutes, localized —
            # the 5h/weekly mapping is not pinned to primary/secondary, and the core
            # stays presentation- and locale-agnostic.
            "key": key,
            "used_percent": _header_float(headers.get(f"x-codex-{key}-used-percent")) or 0.0,
            "window_minutes": window_minutes,
            "resets_at": resets_at,
        })
    _usage_snapshot = {
        "plan_type": headers.get("x-codex-plan-type", ""),
        "active_limit": headers.get("x-codex-active-limit", ""),
        "captured_at": now,
        "credits": {
            "has_credits": _header_bool(headers.get("x-codex-credits-has-credits")),
            "balance": _header_float(headers.get("x-codex-credits-balance")),
            "unlimited": _header_bool(headers.get("x-codex-credits-unlimited")),
        },
        "windows": windows,
    }


def get_usage_snapshot() -> Optional[dict[str, Any]]:
    """The most recent rate-limit snapshot captured from a turn, or None when no
    turn has run since sign-in (the headers only ride on /responses replies)."""
    return _usage_snapshot


def clear_usage_snapshot() -> None:
    """Drop the snapshot on sign-out/sign-in so stale limits don't linger."""
    global _usage_snapshot
    _usage_snapshot = None
