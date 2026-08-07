from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Optional, Sequence, cast
from uuid import uuid4

import litellm
from langchain_core.language_models.chat_models import BaseChatModel

# LiteLLM raises on a parameter a provider does not support, so unsupported ones are dropped rather than sent.
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolMessage,
)
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.content import ContentBlock, ReasoningContentBlock
from langchain_core.messages.tool import ToolCallChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr, SecretStr

from langmesh.base.serialization import compact
from langmesh.runtime.cache_trace import ITEM, TOOLS, Piece, RequestTrace, diagnose, trace
from langmesh.base.message_content import (
    REASONING_MODEL_KEY,
    carried_reasoning_for,
    content_blocks_to_message_content,
    message_reasoning_text,
    message_text,
)

litellm.drop_params = True

#: The one thing worth asking a Responses request for: the model's own thinking, encrypted, to hand back next call.
_ENCRYPTED_REASONING = ["reasoning.encrypted_content"]

#: Routes that want `reasoning_content` on every assistant message, present and empty rather than absent.
_ALWAYS_REASONING_ROUTES = ("deepseek",)

#: Providers whose reasoning is only recoverable through the Responses API rather than Chat Completions.
_RESPONSES_ROUTES = ("openai", "azure")


class ChatLiteLLMModel(BaseChatModel):
    """A `BaseChatModel` backed by LiteLLM, the single route to every provider this harness can reach."""

    model: str
    api_key: Optional[SecretStr] = None
    api_base: Optional[str] = None
    #: The conversation this model serves, sent as the provider's prompt cache key.
    session_id: str = ""
    #: The window models.dev advertises, for models LiteLLM's own map has never heard of.
    context_length: int = 0
    temperature: float = 0.0
    reasoning_effort: Optional[str] = None
    maximum_tokens: Optional[int] = None
    # A bounded request timeout, since the streaming loop only checks for aborts between chunks.
    timeout: Optional[float] = 300.0
    default_headers: dict[str, str] = {}

    #: The last request's segment trace, declared rather than merely assigned so Pydantic will hold it.
    _previous_trace: Optional[RequestTrace] = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "litellm"

    def context_window(self) -> int:
        """The model's maximum input context in tokens, from LiteLLM's map or the catalogue, and zero when genuinely unknown."""
        catalogued = max(0, int(self.context_length or 0))
        try:
            info = litellm.get_model_info(self.model)
            live = int(info.get("max_input_tokens") or info.get("max_tokens") or 0)
        except Exception:  # noqa: BLE001 — an unknown id is a custom endpoint, not a failure
            live = 0
        if live and catalogued:
            return min(live, catalogued)
        return live or catalogued

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "api_base": self.api_base,
            "temperature": self.temperature,
            "reasoning_effort": self.reasoning_effort,
        }

    def _speaks_responses(self) -> bool:
        """Whether this model's reasoning only round-trips over the Responses API, asked of LiteLLM's own model map."""
        route, _, remainder = self.model.partition("/")
        if remainder.startswith("responses/"):
            return True
        if route.lower() not in _RESPONSES_ROUTES:
            return False
        try:
            info = litellm.get_model_info(self.model)
        except Exception:  # noqa: BLE001 — an unknown id is a custom endpoint; leave it alone
            return False
        return info.get("mode") == "responses" or bool(info.get("supports_reasoning"))

    def _request_model(self) -> str:
        """The model id to send, which is the Responses one when reasoning depends on it."""
        route, separator, remainder = self.model.partition("/")
        if not self._speaks_responses() or not separator or remainder.startswith("responses/"):
            return self.model
        return f"{route}/responses/{remainder}"

    # Tool binding.

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

    # Message translation between LangChain messages and LiteLLM request dicts.

    def _messages_to_dicts(self, messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
        dicts: list[dict[str, Any]] = []
        for message in messages:
            role = ChatLiteLLMModel._role_for(message)
            entry: dict[str, Any] = {
                "role": role,
                "content": message_text(message)
                if isinstance(message, AIMessage)
                else message.content,
            }
            if isinstance(message, AIMessage):
                tool_calls = ChatLiteLLMModel._tool_calls_to_openai(message.tool_calls)
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                # Reasoning goes back in exactly one form, preferring the provider-native block that carries its own signature.
                carried = carried_reasoning_for(message, self.model)
                entry.update(carried)
                reasoning = message_reasoning_text(message)
                if not carried and (reasoning or self._route() in _ALWAYS_REASONING_ROUTES):
                    entry["reasoning_content"] = reasoning
            elif isinstance(message, ToolMessage):
                entry["tool_call_id"] = message.tool_call_id
            dicts.append(entry)
        return dicts

    def _reasoning_to_carry(self, source: Any) -> dict[str, Any]:
        """Whatever of the provider's own reasoning this message can hand back, taking the signed block over the deltas."""
        carried: dict[str, Any] = {}
        blocks = [
            block
            for block in ChatLiteLLMModel._plain(getattr(source, "thinking_blocks", None))
            if block.get("type") == "redacted_thinking" or block.get("signature")
        ]
        if blocks:
            carried["thinking_blocks"] = blocks
        items = ChatLiteLLMModel._plain(getattr(source, "reasoning_items", None))
        if items:
            carried["reasoning_items"] = items
        if carried:
            carried[REASONING_MODEL_KEY] = self.model
        return carried

    @staticmethod
    def _plain(value: Any) -> list[dict[str, Any]]:
        """A list of provider objects as plain dicts, so it survives being checkpointed to JSON."""
        plain: list[dict[str, Any]] = []
        for entry in value or []:
            if isinstance(entry, dict):
                plain.append(dict(entry))
            elif hasattr(entry, "model_dump"):
                plain.append(entry.model_dump(exclude_none=True))
        return plain

    #: What makes a model want explicit cache breakpoints is being a Claude, wherever it is served from.
    _CACHE_BREAKPOINT_MARKERS = ("claude", "anthropic")

    #: Routes whose models take the same breakpoints regardless of family, like Model Studio's Qwen.
    _CACHE_BREAKPOINT_ROUTES = ("dashscope",)

    #: The one route that must never be marked, because a gateway rewrites the request it is caching.
    _GATEWAY_ROUTE = "vercel_ai_gateway"

    #: How many breakpoints to place and where: two at the unchanging front and two at the moving end.
    _LEADING_BREAKPOINTS = 2
    _TRAILING_BREAKPOINTS = 2

    #: Copilot resells Claude but reads a differently named marker, so the key is named per route rather than assumed.
    _CACHE_CONTROL_KEYS = {"github_copilot": "copilot_cache_control"}
    _DEFAULT_CACHE_CONTROL_KEY = "cache_control"

    def _route(self) -> str:
        """The LiteLLM provider this model is addressed through — the first path segment."""
        return self.model.split("/", 1)[0].lower()

    def _cache_control_key(self) -> str:
        return self._CACHE_CONTROL_KEYS.get(self._route(), self._DEFAULT_CACHE_CONTROL_KEY)

    def _apply_cache_breakpoints(
        self,
        dicts: list[dict[str, Any]],
        messages: Sequence[BaseMessage] = (),
    ) -> list[dict[str, Any]]:
        """Mark the messages the provider should cache up to, since Anthropic caches nothing unless asked."""
        route = self._route()
        if route == self._GATEWAY_ROUTE:
            return dicts
        model = self.model.lower()
        if not (
            any(marker in model for marker in self._CACHE_BREAKPOINT_MARKERS)
            or route in self._CACHE_BREAKPOINT_ROUTES
        ):
            return dicts
        # `dicts` is built one-for-one from `messages`, in order, so the index is the join.
        transient = {
            index
            for index, message in enumerate(messages)
            if getattr(message, "additional_kwargs", {}).get("transient")
        }
        durable = [
            entry
            for index, entry in enumerate(dicts)
            if entry["role"] != "system" and index not in transient
        ]
        # Disjoint by construction, so the two selections never mark the same message or exceed four breakpoints.
        system = [entry for entry in dicts if entry["role"] == "system"][
            : self._LEADING_BREAKPOINTS
        ]
        for entry in system:
            self._mark_cached(entry, self._cache_control_key())
        # Walk back until the trailing breakpoints are actually placed, since not every message can carry one.
        placed = 0
        for entry in reversed(durable):
            if placed >= self._TRAILING_BREAKPOINTS:
                break
            if self._mark_cached(entry, self._cache_control_key()):
                placed += 1
        return dicts

    @staticmethod
    def _mark_cached(entry: dict[str, Any], key: str = "cache_control") -> bool:
        """Put the breakpoint where LiteLLM reads it for this role, and say whether it went."""
        if entry["role"] == "tool":
            entry[key] = {"type": "ephemeral"}
            return True
        content = entry.get("content")
        if isinstance(content, str):
            if not content:
                return False  # an empty block is not a cacheable prefix, only a malformed one
            # Annotated rather than inferred, so the promoted block types as one that can hold the marker written below.
            promoted: list[dict[str, Any]] = [{"type": "text", "text": content}]
            entry["content"] = promoted
        elif not isinstance(content, list) or not content:
            return False
        blocks: list[Any] = entry["content"]
        last = blocks[-1]
        if not isinstance(last, dict):
            return False
        last[key] = {"type": "ephemeral"}
        return True

    @staticmethod
    def _role_for(message: BaseMessage) -> str:
        # LangChain's message types map onto the OpenAI role names LiteLLM expects.
        name = message.__class__.__name__
        if isinstance(message, ToolMessage):
            return "tool"
        return {
            "SystemMessage": "system",
            "HumanMessage": "user",
            "AIMessage": "assistant",
            "AIMessageChunk": "assistant",
            "ToolMessage": "tool",
        }.get(name, "user")

    @staticmethod
    def _tool_calls_to_openai(tool_calls: Sequence[Any]) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        for call in tool_calls:
            # LangChain stores parsed arguments under `args`, while the wire format we serialize back to uses `arguments`.
            arguments = call.get("args")
            serialized = arguments if isinstance(arguments, str) else compact(arguments)
            rendered.append(
                {
                    "id": call.get("id"),
                    "type": "function",
                    "function": {"name": call.get("name"), "arguments": serialized},
                }
            )
        return rendered

    # Shared kwargs assembled for every LiteLLM completion call.

    def _completion_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self._request_model(),
            "temperature": self.temperature,
        }
        if self.api_key is not None:
            unsealed = self.api_key.get_secret_value()
            if unsealed:
                params["api_key"] = unsealed
        if self.api_base:
            params["api_base"] = self.api_base
        if self.reasoning_effort:
            params["reasoning_effort"] = self.reasoning_effort
        if self.maximum_tokens is not None:
            params["max_tokens"] = self.maximum_tokens  # litellm/OpenAI API param name
        if self.timeout is not None:
            params["timeout"] = self.timeout
        if self.default_headers:
            params["extra_headers"] = self.default_headers
        if self.session_id:
            # Which cache to look in: a provider routes the lookup by this key, so requests sharing one land on the same prefix.
            params["prompt_cache_key"] = self.session_id
        if self._route() == self._GATEWAY_ROUTE:
            # A gateway rewrites the request for whichever provider it routes to, so it is the only thing that can place breakpoints.
            params["extra_body"] = {**params.get("extra_body", {}), "gateway": {"caching": "auto"}}
        if self._speaks_responses():
            # Ask for the encrypted reasoning and decline to have the conversation kept, which only make sense together.
            params["extra_body"] = {
                **params.get("extra_body", {}),
                "include": _ENCRYPTED_REASONING,
                "store": False,
            }
        # Caller-supplied arguments override the model defaults, so `bind_tools` bindings reach LiteLLM.
        params.update({key: value for key, value in kwargs.items() if value is not None})
        return params

    def _trace_request(self, params: dict[str, Any], sent: list[dict[str, Any]]) -> RequestTrace:
        """Cut the outgoing request into the pieces a prompt cache matches on, in wire order."""
        pieces = [Piece(kind=TOOLS, text=compact(params.get("tools") or []))]
        for position, message in enumerate(sent):
            pieces.append(
                Piece(
                    kind=ITEM,
                    text=compact(message),
                    position=position,
                    role=str(message.get("role") or ""),
                )
            )
        return trace(pieces)

    def _cache_diagnosis(self, current: RequestTrace) -> dict[str, object]:
        """What this request kept from the last one, and remember it for the next."""
        diagnosis = diagnose(current, self._previous_trace)
        self._previous_trace = current
        return diagnosis

    # Streaming generation.

    async def _astream(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        # `include_usage` asks for a trailing usage chunk, so a streamed turn still reports real token counts.
        params = self._completion_kwargs(
            stop=stop,
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )
        # One name for the prose this call produces, minted here because nothing LiteLLM streams identifies the block.
        block = f"litellm-{uuid4().hex}-"
        sent = self._apply_cache_breakpoints(self._messages_to_dicts(messages), messages)
        # Taken before the request and reported once the response says what the cache did.
        current_trace = self._trace_request(params, sent)
        reported = False
        stream = cast(AsyncIterator[Any], await litellm.acompletion(messages=sent, **params))
        async for chunk in stream:
            generation_chunk = self._litellm_chunk_to_generation_chunk(chunk, block)
            if generation_chunk is not None:
                # Attached to the chunk carrying usage, so the diagnosis travels with the figure it explains.
                if generation_chunk.message.usage_metadata and not reported:
                    reported = True
                    generation_chunk.message.additional_kwargs["cache_trace"] = (
                        self._cache_diagnosis(current_trace)
                    )
                yield generation_chunk

    @staticmethod
    def _usage_metadata(usage: Any) -> Optional[UsageMetadata]:
        """Normalize a LiteLLM usage object into LangChain's, or `None` when the response carries none."""
        if usage is None:
            return None

        def _value(source: Any, key: str) -> int:
            if source is None:
                return 0
            if isinstance(source, dict):
                return int(source.get(key) or 0)
            return int(getattr(source, key, 0) or 0)

        input_tokens = _value(usage, "prompt_tokens")
        output_tokens = _value(usage, "completion_tokens")
        total_tokens = _value(usage, "total_tokens") or (input_tokens + output_tokens)
        if not (input_tokens or output_tokens or total_tokens):
            return None
        metadata: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
        prompt_details = (
            usage.get("prompt_tokens_details")
            if isinstance(usage, dict)
            else getattr(usage, "prompt_tokens_details", None)
        )
        cache_read = _value(prompt_details, "cached_tokens")
        if cache_read:
            metadata["input_token_details"] = {"cache_read": cache_read}
        completion_details = (
            usage.get("completion_tokens_details")
            if isinstance(usage, dict)
            else getattr(usage, "completion_tokens_details", None)
        )
        reasoning = _value(completion_details, "reasoning_tokens")
        if reasoning:
            metadata["output_token_details"] = {"reasoning": reasoning}
        return cast(UsageMetadata, metadata)

    def _litellm_chunk_to_generation_chunk(
        self, chunk: Any, block: str = ""
    ) -> Optional[ChatGenerationChunk]:
        usage_metadata = ChatLiteLLMModel._usage_metadata(getattr(chunk, "usage", None))
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            # A chunk with usage but no choices surfaces an empty message, so the merged response still accumulates the counts.
            if usage_metadata is not None:
                return ChatGenerationChunk(
                    message=AIMessageChunk(content="", usage_metadata=usage_metadata)
                )
            return None
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            if usage_metadata is not None:
                return ChatGenerationChunk(
                    message=AIMessageChunk(content="", usage_metadata=usage_metadata)
                )
            return None
        content = getattr(delta, "content", None) or ""
        tool_call_chunks: list[ToolCallChunk] = []
        for call in getattr(delta, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            tool_call_chunks.append(
                cast(
                    ToolCallChunk,
                    {
                        "index": getattr(call, "index", 0) or 0,
                        "id": getattr(call, "id", None),
                        "name": getattr(function, "name", None) if function else None,
                        # The streaming delta exposes partial JSON as `function.arguments`, which LangChain stores under `args`.
                        "args": getattr(function, "arguments", None) if function else None,
                        "type": "tool_call_chunk",
                    },
                )
            )
        reasoning = getattr(delta, "reasoning_content", None)
        if not reasoning:
            # Some providers nest reasoning under a different attribute.
            reasoning = getattr(delta, "reasoning", None)
        content_blocks = ChatLiteLLMModel._standard_content_blocks(content, reasoning, block)
        message = AIMessageChunk(
            content=content_blocks_to_message_content(content_blocks),
            tool_call_chunks=tool_call_chunks,
            usage_metadata=usage_metadata,
            # Chunk merging concatenates these lists, so the finished message holds every signed block in order.
            additional_kwargs=self._reasoning_to_carry(delta),
        )
        finish_reason = getattr(choice, "finish_reason", None)
        generation_info = {"finish_reason": finish_reason} if finish_reason else None
        return ChatGenerationChunk(message=message, generation_info=generation_info)

    @staticmethod
    def _standard_content_blocks(
        content: Any, reasoning: Any, block: str = ""
    ) -> list[ContentBlock]:
        """Name the blocks in one streamed chunk, keyed by the call so two calls' prose never merges into one block."""
        normalized_blocks: list[ContentBlock] = []
        if reasoning:
            normalized_blocks.append(
                ReasoningContentBlock(
                    type="reasoning",
                    reasoning=str(reasoning),
                    id=f"{block}reasoning",
                    index=0,
                )
            )
        if content:
            source_blocks = AIMessageChunk(content=content).content_blocks
            for position, source_block in enumerate(source_blocks):
                normalized_block: dict[str, Any] = dict(source_block)
                normalized_block["id"] = f"{block}content-{position}"
                normalized_block["index"] = position + 1
                normalized_blocks.append(cast(ContentBlock, normalized_block))
        return normalized_blocks

    # Non-streaming generation.

    async def _agenerate(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        params = self._completion_kwargs(stop=stop, **kwargs)
        response = await litellm.acompletion(
            messages=self._apply_cache_breakpoints(self._messages_to_dicts(messages), messages),
            **params,
        )
        return self._response_to_result(response)

    def _generate(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        params = self._completion_kwargs(stop=stop, **kwargs)
        response = litellm.completion(
            messages=self._apply_cache_breakpoints(self._messages_to_dicts(messages), messages),
            **params,
        )
        return self._response_to_result(response)

    def _response_to_result(self, response: Any) -> ChatResult:
        import json as _json

        choices = getattr(response, "choices", None) or []
        if not choices:
            return ChatResult(generations=[])
        message_obj = getattr(choices[0], "message", None)
        content = getattr(message_obj, "content", None) or ""
        reasoning = getattr(message_obj, "reasoning_content", None)
        tool_calls: list[dict[str, Any]] = []
        for call in getattr(message_obj, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            raw_arguments = getattr(function, "arguments", None) if function else None
            try:
                parsed_arguments = _json.loads(raw_arguments) if raw_arguments else {}
            except (TypeError, ValueError):
                parsed_arguments = raw_arguments
            tool_calls.append(
                {
                    "name": getattr(function, "name", None) if function else None,
                    "args": parsed_arguments,
                    "id": getattr(call, "id", None),
                }
            )
        message = AIMessage(
            content_blocks=ChatLiteLLMModel._standard_content_blocks(content, reasoning),
            # An empty list rather than `None`, because a default applies to an omitted key and an explicit `None` fails validation.
            tool_calls=list(tool_calls or []),
            usage_metadata=ChatLiteLLMModel._usage_metadata(getattr(response, "usage", None)),
            additional_kwargs=self._reasoning_to_carry(message_obj),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])
