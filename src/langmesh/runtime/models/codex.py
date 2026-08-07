"""A LangChain chat model backed by a ChatGPT subscription, speaking the Responses API directly."""

from __future__ import annotations

import json
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
from pydantic import PrivateAttr

from langmesh.base.credentials import (
    ChatGPTAuthError,
    load_tokens,
    valid_tokens,
)
from langmesh.base.message_content import (
    REASONING_MODEL_KEY,
    carried_reasoning_for,
    content_blocks_to_message_content,
    message_text,
)
from langmesh.base.model_errors import CONTEXT_OVERFLOW_CODES, ContextWindowExceeded
from langmesh.runtime.cache_trace import (
    INSTRUCTIONS,
    ITEM,
    TOOLS,
    Piece,
    RequestTrace,
    diagnose,
    trace,
)
from langmesh.base.serialization import compact, upstream_detail
from langmesh.base.subscription import (
    RESPONSES_URL,
    cached_subscription_models,
    capture_usage_headers,
    request_headers,
)

# What the endpoint serves before its catalogue is fetched, deliberately the conservative figure.
COLD_START_WINDOW = 272_000


def _error_code(body: str) -> str:
    """The machine-readable ``code`` out of an error body, or ``""``. Never its prose."""
    try:
        parsed = json.loads(body)
    except Exception:
        return ""
    if not isinstance(parsed, dict):
        return ""
    detail = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
    return str(detail.get("code") or "") if isinstance(detail, dict) else ""


class ChatCodexModel(BaseChatModel):
    """A `BaseChatModel` backed by the Codex endpoint, reading its token from the shared store on every call."""

    model: str
    reasoning_effort: Optional[str] = None
    temperature: float = 0.0
    context_length: int = 0
    #: The conversation this model serves, sent as `prompt_cache_key`.
    session_id: str = ""
    # A generous bound so a dead connection cannot hang a turn forever, since aborts are only checked between chunks.
    timeout: Optional[float] = 300.0

    #: The last request's segment trace, declared rather than merely assigned so Pydantic will hold it.
    _previous_trace: Optional[RequestTrace] = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "codex"

    def context_window(self) -> int:
        # The live subscription catalog is authoritative for the real Codex budget.
        live = cached_subscription_models().get(self.model)
        if live and live.get("context"):
            return int(live["context"])
        # Until the catalogue is warm, models.dev's figure is wrong in the direction that does harm, since Codex serves less.
        return (
            min(self.context_length, COLD_START_WINDOW)
            if self.context_length
            else COLD_START_WINDOW
        )

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model, "reasoning_effort": self.reasoning_effort}

    # The same tool-binding surface as the LiteLLM client; the Responses-shaped flattening happens at payload-build time.

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

    def _to_responses_input(
        self, messages: Sequence[BaseMessage]
    ) -> tuple[str, list[dict[str, Any]]]:
        """Translate LangChain messages into a Responses `(instructions, input)` pair."""
        instructions = ""
        items: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, SystemMessage):
                text = self._text_of(message)
                # Only the first system message becomes `instructions`; everything after it stays put as a developer item.
                if not instructions:
                    instructions = text
                elif text:
                    items.append(
                        {
                            "type": "message",
                            "role": "developer",
                            "content": [{"type": "input_text", "text": text}],
                        }
                    )
                continue
            if isinstance(message, ToolMessage):
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": self._text_of(message),
                    }
                )
                continue
            if isinstance(message, AIMessage):
                # The model's own prior thinking, handed back before the calls it produced, since the endpoint reads this in order.
                items.extend(
                    carried_reasoning_for(message, self.model).get("reasoning_items") or []
                )
                text = self._text_of(message)
                if text:
                    items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    )
                for call in message.tool_calls or []:
                    arguments = call.get("args")
                    serialized = arguments if isinstance(arguments, str) else compact(arguments)
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id"),
                            "name": call.get("name"),
                            "arguments": serialized,
                        }
                    )
                continue
            # A reminder is not the user talking, and a `developer` item stays where it was written.
            role = "developer" if message.additional_kwargs.get("reminder") else "user"
            items.append(
                {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text", "text": self._text_of(message)}],
                }
            )
        return instructions, items

    @staticmethod
    def _to_responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
        # Chat-Completions nests name and parameters under `function`, while the Responses API expects them flattened.
        function = tool.get("function", tool)
        return {
            "type": "function",
            "name": function.get("name"),
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {}),
            "strict": False,
        }

    def _build_payload(
        self, messages: Sequence[BaseMessage], *, stream: bool, **kwargs: Any
    ) -> dict[str, Any]:
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
        # Sent whether or not there are tools, matching the Codex client, since a field that comes and goes moves the prefix.
        payload["tool_choice"] = kwargs.get("tool_choice") or "auto"
        tools = kwargs.get("tools")
        if tools:
            payload["tools"] = [self._to_responses_tool(tool) for tool in tools]
        if self.session_id:
            # Which cache to look in, since the endpoint routes a lookup by hashing the prefix together with this key.
            payload["prompt_cache_key"] = self.session_id
        # Both unconditional, as in the Codex client: asking for the encrypted reasoning is what makes `store: false` workable.
        payload["reasoning"] = {
            "effort": self.reasoning_effort or None,
            "summary": "auto",
        }
        payload["include"] = ["reasoning.encrypted_content"]
        return payload

    async def _headers(self) -> dict[str, str]:
        """The request headers, with a freshly-valid access token and this conversation's id."""
        return request_headers(await valid_tokens(), self.session_id)

    @staticmethod
    def _http_error(status: int, body: str) -> Exception:
        if status in (401, 403):
            return ChatGPTAuthError(
                "ChatGPT rejected the subscription token (expired, revoked, or plan "
                f"lacks access). Sign in again. Detail: {upstream_detail(body)}"
            )
        # An overlong request is refused before the stream opens as often as during it, so both paths report it the same way.
        if status == 400 and _error_code(body) in CONTEXT_OVERFLOW_CODES:
            return ContextWindowExceeded("The request exceeded this model's context window.")
        return RuntimeError(f"ChatGPT Codex endpoint returned {status}: {upstream_detail(body)}")

    # The Responses stream is `data:` lines whose `type` drives the dispatch into the harness's own chunk shape.

    @classmethod
    def _line_to_chunk(cls, line: str, state: dict[str, Any]) -> Optional[ChatGenerationChunk]:
        if not line or not line.startswith("data:"):
            return None
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        return cls._translate_event(data, state)

    @classmethod
    def _translate_event(
        cls, data: dict[str, Any], state: dict[str, Any]
    ) -> Optional[ChatGenerationChunk]:
        event_type = data.get("type", "")
        if event_type == "response.output_text.delta":
            return cls._chunk(content_block=cls._text_content_block(data))
        if event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            return cls._chunk(content_block=cls._reasoning_content_block(data))
        if event_type == "response.output_item.done":
            # A finished reasoning item, kept in its encrypted form because that is what the endpoint accepts back.
            item = data.get("item") or {}
            if item.get("type") == "reasoning" and item.get("encrypted_content"):
                return cls._chunk(
                    reasoning_item={
                        "type": "reasoning",
                        "id": item.get("id"),
                        "summary": item.get("summary") or [],
                        "encrypted_content": item["encrypted_content"],
                    },
                    model=str(state.get("model") or ""),
                )
            return None
        if event_type == "response.output_item.added":
            item = data.get("item") or {}
            if item.get("type") == "function_call":
                state["saw_tool_call"] = True
                return cls._chunk(
                    tool_call_chunk={
                        "index": int(data.get("output_index", 0) or 0),
                        "id": item.get("call_id"),
                        "name": item.get("name"),
                        "args": item.get("arguments") or "",
                        "type": "tool_call_chunk",
                    }
                )
            return None
        if event_type == "response.function_call_arguments.delta":
            return cls._chunk(
                tool_call_chunk={
                    "index": int(data.get("output_index", 0) or 0),
                    "id": None,
                    "name": None,
                    "args": str(data.get("delta", "")),
                    "type": "tool_call_chunk",
                }
            )
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
            detail = response.get("error") or data.get("error") or {}
            structured = detail if isinstance(detail, dict) else {}
            message = structured.get("message") if structured else str(detail)
            # The failure event carries a machine-readable code beside its message, so an overflow is raised as one.
            code = str(structured.get("code") or "")
            if code in CONTEXT_OVERFLOW_CODES:
                raise ContextWindowExceeded(
                    message or "The request exceeded this model's context window.",
                    model=str(state.get("model") or ""),
                    context_window=int(state.get("context_window") or 0),
                )
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
        reasoning_item: Optional[dict[str, Any]] = None,
        model: str = "",
    ) -> ChatGenerationChunk:
        content_blocks = [content_block] if content_block is not None else []
        message = AIMessageChunk(
            content=content_blocks_to_message_content(content_blocks),
            tool_call_chunks=[tool_call_chunk] if tool_call_chunk else [],
            # One item per chunk accumulates into the turn's reasoning in order, carrying no `index` so the list is not merged by it.
            additional_kwargs=(
                {"reasoning_items": [reasoning_item], REASONING_MODEL_KEY: model}
                if reasoning_item
                else {}
            ),
        )
        return ChatGenerationChunk(message=message)

    def _trace_payload(self, payload: dict[str, Any]) -> RequestTrace:
        """Cut the outgoing request into the pieces a prompt cache matches on, in the order the endpoint reads them."""
        pieces = [
            Piece(kind=INSTRUCTIONS, text=payload.get("instructions") or ""),
            Piece(kind=TOOLS, text=compact(payload.get("tools") or [])),
        ]
        for position, item in enumerate(payload.get("input") or []):
            # A Responses item is identified by its type, and by its role where it has one, which is the more telling of the two.
            pieces.append(
                Piece(
                    kind=ITEM,
                    text=compact(item),
                    position=position,
                    role=str(item.get("role") or item.get("type") or ""),
                )
            )
        return trace(pieces)

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

    def _cache_diagnosis(self, current: RequestTrace) -> dict[str, object]:
        """What this request kept from the last one, and remember it for the next."""
        diagnosis = diagnose(current, self._previous_trace)
        self._previous_trace = current
        return diagnosis

    async def _astream(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        payload = self._build_payload(messages, stream=True, **kwargs)
        headers = await self._headers()
        # Taken before the request and held until the response says what the cache did.
        current_trace = self._trace_payload(payload)
        reported = False
        # Carried so a failure can name the model that refused the request and the window it was measured against.
        state: dict[str, Any] = {
            "saw_tool_call": False,
            "model": self.model,
            "context_window": self.context_window(),
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", RESPONSES_URL, json=payload, headers=headers
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise self._http_error(response.status_code, body)
                capture_usage_headers(response.headers)
                async for line in response.aiter_lines():
                    chunk = self._line_to_chunk(line, state)
                    if chunk is not None:
                        # Attached to the chunk carrying usage, so the diagnosis travels with the figure it explains.
                        if chunk.message.usage_metadata and not reported:
                            reported = True
                            chunk.message.additional_kwargs["cache_trace"] = self._cache_diagnosis(
                                current_trace
                            )
                        yield chunk

    @staticmethod
    def _aggregate_to_result(aggregate: Optional[ChatGenerationChunk]) -> ChatResult:
        if aggregate is None:
            return ChatResult(generations=[])
        # The stream only ever yields `AIMessageChunk`, so narrow from the base type for the accessors below.
        chunk_message = cast(AIMessageChunk, aggregate.message)
        message = AIMessage(
            content=chunk_message.content,
            # An empty list rather than `None`, because a default applies to an omitted key and an explicit `None` fails validation.
            tool_calls=list(chunk_message.tool_calls or []),
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
        # `BaseChatModel` requires a synchronous path, so this fallback reads the token without the async refresh.
        tokens = load_tokens()
        if tokens is None or tokens.is_expired():
            raise ChatGPTAuthError("Not signed in to ChatGPT (or the session expired).")
        payload = self._build_payload(messages, stream=True, **kwargs)
        headers = request_headers(tokens, self.session_id)
        # Carried so a failure can name the model that refused the request and the window it was measured against.
        state: dict[str, Any] = {
            "saw_tool_call": False,
            "model": self.model,
            "context_window": self.context_window(),
        }
        aggregate: Optional[ChatGenerationChunk] = None
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", RESPONSES_URL, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    raise self._http_error(
                        response.status_code, response.read().decode("utf-8", "replace")
                    )
                capture_usage_headers(response.headers)
                for line in response.iter_lines():
                    chunk = self._line_to_chunk(line, state)
                    if chunk is not None:
                        aggregate = chunk if aggregate is None else aggregate + chunk
        return self._aggregate_to_result(aggregate)
