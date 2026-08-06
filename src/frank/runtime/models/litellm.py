from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Optional, Sequence, cast
from uuid import uuid4

import litellm
from langchain_core.language_models.chat_models import BaseChatModel

# LiteLLM validates request parameters against each provider's supported set and raises if an unsupported one is present.
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

from frank.base.serialization import compact
from frank.runtime.cache_trace import ITEM, TOOLS, Piece, RequestTrace, diagnose, trace
from frank.base.message_content import (
    REASONING_MODEL_KEY,
    carried_reasoning_for,
    content_blocks_to_message_content,
    message_reasoning_text,
    message_text,
)

litellm.drop_params = True

#: The one thing worth asking for on a Responses request, and the reason to make the request a Responses one at all: the model's own thinking, encrypted, so the next call can hand it back.
_ENCRYPTED_REASONING = ["reasoning.encrypted_content"]

#: Routes that want `reasoning_content` on every assistant message, present and empty rather than absent.
_ALWAYS_REASONING_ROUTES = ("deepseek",)

#: Providers whose reasoning is only recoverable through the Responses API.
_RESPONSES_ROUTES = ("openai", "azure")


class ChatLiteLLMModel(BaseChatModel):
    """A LangChain ``BaseChatModel`` backed by LiteLLM, the single route to every
    provider (Anthropic, OpenAI, Gemini, Bedrock, the OpenAI-compatible family, and
    any custom OpenAI-compatible server). LiteLLM owns each provider's auth, base
    URL, request format, streaming wire format, and reasoning-token normalization;
    this class only translates between LangChain messages and LiteLLM's
    OpenAI-shaped request/response, so the harness's existing tool-binding, chunk
    merging, and reasoning round-trip keep working unchanged across providers.

    One adapter replaces the former ``ChatOpenAI`` + ``ReasoningChatOpenAI`` pair:
    there is no longer an OpenAI-only path and a separate reasoning subclass.
    LiteLLM normalizes provider reasoning fields on the wire; this adapter immediately
    converts them into LangChain's standard reasoning content blocks, so the rest of
    the harness has one provider-neutral representation.
    """

    model: str
    api_key: Optional[SecretStr] = None
    api_base: Optional[str] = None
    #: The conversation this model serves, sent as the provider's prompt cache key.
    session_id: str = ""
    #: The window models.dev advertises, for the models LiteLLM's own map has never heard of.
    context_length: int = 0
    temperature: float = 0.0
    reasoning_effort: Optional[str] = None
    maximum_tokens: Optional[int] = None
    # A bounded request timeout so a stalled or half-dead provider connection cannot hang a turn forever.
    timeout: Optional[float] = 300.0
    default_headers: dict[str, str] = {}

    #: The last request's segment trace, kept so the next one can be told what moved.
    _previous_trace: Optional[RequestTrace] = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "litellm"

    def context_window(self) -> int:
        """The model's maximum input context in tokens. Zero means genuinely unknown.

        Two sources, because neither covers everything. LiteLLM's map knows the models it routes
        natively and nothing about a gateway's — a model reached through an OpenAI-compatible
        endpoint is just a name to it, so it answered zero, and zero is what blanked the
        composer's context ring on every gateway provider. The models.dev catalogue knows those,
        having been fetched under whichever provider publishes them.

        The smaller wins when both answer. They disagree — 200K against 1M for the same Claude,
        one quoting the standard window and the other a beta — and over-reporting is the harmful
        direction: every window-scaled budget divides by this number, so believing the larger
        figure sizes each cap for room that is not there and reports a conversation as far
        emptier than it is."""
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
        """Whether this model's reasoning is only round-trippable over the Responses API.

        Asked of LiteLLM's own model map rather than guessed from the name, because the map is
        what LiteLLM consults when it decides which endpoint to speak — so the answer here and
        the endpoint actually used cannot drift apart. A model already carrying a `responses/`
        segment says so itself; one whose declared mode is `responses` is routed there without
        being asked. What is left is the case this exists for: a reasoning model on `openai` or
        `azure` whose mode is `chat`, which is where the reasoning was being lost.
        """
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
                "content": message_text(message) if isinstance(message, AIMessage) else message.content,
            }
            if isinstance(message, AIMessage):
                tool_calls = ChatLiteLLMModel._tool_calls_to_openai(message.tool_calls)
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                # Reasoning goes back in exactly one form, the same way prose does.
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
        """Whatever of the provider's own reasoning this message can hand back next time.

        A streamed thinking block arrives twice. First as deltas — a slice of text and no
        signature, which is display material and nothing more; Anthropic rejects an unsigned
        block outright rather than ignoring it. Then, when the signature lands, as one whole
        block carrying the full text *and* the signature. Only the second kind is history, so
        only the second kind is kept: taking both would send the same thinking several times
        over and get the request refused for the partials.
        """
        carried: dict[str, Any] = {}
        blocks = [
            block for block in ChatLiteLLMModel._plain(getattr(source, "thinking_blocks", None))
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

    #: What makes a model want explicit cache breakpoints: being a Claude, wherever it is served from.
    _CACHE_BREAKPOINT_MARKERS = ("claude", "anthropic")

    #: Routes whose models take the same explicit breakpoints regardless of family.
    _CACHE_BREAKPOINT_ROUTES = ("dashscope",)

    #: The one route that must never be marked.
    _GATEWAY_ROUTE = "vercel_ai_gateway"

    #: How many breakpoints to place, and where.
    _LEADING_BREAKPOINTS = 2
    _TRAILING_BREAKPOINTS = 2

    #: GitHub Copilot resells Claude but reads a differently named marker, and LiteLLM has no translation for it — so the plain one would ride all the way to Copilot's endpoint and mean nothing there.
    _CACHE_CONTROL_KEYS = {"github_copilot": "copilot_cache_control"}
    _DEFAULT_CACHE_CONTROL_KEY = "cache_control"

    def _route(self) -> str:
        """The LiteLLM provider this model is addressed through — the first path segment."""
        return self.model.split("/", 1)[0].lower()

    def _cache_control_key(self) -> str:
        return self._CACHE_CONTROL_KEYS.get(self._route(), self._DEFAULT_CACHE_CONTROL_KEY)

    def _apply_cache_breakpoints(
        self, dicts: list[dict[str, Any]], messages: Sequence[BaseMessage] = (),
    ) -> list[dict[str, Any]]:
        """Mark the messages the provider should cache up to.

        Anthropic caches nothing unless asked. There were no breakpoints anywhere in this harness,
        which meant every Claude call re-read and re-billed the whole conversation at full rate —
        a worse deal than the OpenAI path, and invisible, because a request with no cache still
        succeeds and simply costs more.

        The trailing pair skips anything the harness marked ``transient``. A transient note is
        assembled for one request and is gone from the next, so a breakpoint on it can never be
        read back — it would burn one of the four Anthropic allows and return nothing. Skipping
        it puts both trailing breakpoints on conversation that will still be there to match.
        """
        route = self._route()
        if route == self._GATEWAY_ROUTE:
            return dicts
        model = self.model.lower()
        if not (any(marker in model for marker in self._CACHE_BREAKPOINT_MARKERS)
                or route in self._CACHE_BREAKPOINT_ROUTES):
            return dicts
        # `dicts` is built one-for-one from `messages`, in order, so the index is the join.
        transient = {
            index for index, message in enumerate(messages)
            if getattr(message, "additional_kwargs", {}).get("transient")
        }
        durable = [
            entry for index, entry in enumerate(dicts)
            if entry["role"] != "system" and index not in transient
        ]
        # Disjoint by construction — one selects system messages and the other excludes them — so the two never mark the same message, and together they never exceed the four breakpoints Anthropic honours.
        system = [entry for entry in dicts if entry["role"] == "system"][: self._LEADING_BREAKPOINTS]
        for entry in system:
            self._mark_cached(entry, self._cache_control_key())
        # Walk back until the trailing breakpoints are actually *placed*, rather than taking the last two entries and hoping.
        placed = 0
        for entry in reversed(durable):
            if placed >= self._TRAILING_BREAKPOINTS:
                break
            if self._mark_cached(entry, self._cache_control_key()):
                placed += 1
        return dicts

    @staticmethod
    def _mark_cached(entry: dict[str, Any], key: str = "cache_control") -> bool:
        """Put the breakpoint where LiteLLM will find it for this role, and say whether it went.

        A tool result carries it on the message, because that is where LiteLLM reads it when
        building Anthropic's `tool_result` block. Every other role carries it on the last content
        block, which means promoting plain string content to a one-block list — the marker has
        nowhere to live on a bare string.

        Answers ``False`` when there is nothing to attach to, so the caller can try the message
        before instead of quietly losing a breakpoint."""
        if entry["role"] == "tool":
            entry[key] = {"type": "ephemeral"}
            return True
        content = entry.get("content")
        if isinstance(content, str):
            if not content:
                return False  # an empty block is not a cacheable prefix, only a malformed one
            # Annotated rather than inferred: a bare literal reads as `list[dict[str, str]]`, and the marker written below is a dict, so the block being promoted here would type as one that cannot hold it.
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
            # LangChain ToolCall stores the parsed arguments under ``args``; the OpenAI wire format we serialize back to uses ``arguments``.
            arguments = call.get("args")
            serialized = arguments if isinstance(arguments, str) else compact(arguments)
            rendered.append({
                "id": call.get("id"),
                "type": "function",
                "function": {"name": call.get("name"), "arguments": serialized},
            })
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
            # Which cache to look in.
            params["prompt_cache_key"] = self.session_id
        if self._route() == self._GATEWAY_ROUTE:
            # A gateway sits in front of many providers and rewrites the request for whichever one it routes to, so it is the only thing positioned to place the breakpoints — which is why `_apply_cache_breakpoints` leaves its traffic alone.
            params["extra_body"] = {**params.get("extra_body", {}), "gateway": {"caching": "auto"}}
        if self._speaks_responses():
            # Ask for the encrypted reasoning, and decline to have the conversation kept.
            params["extra_body"] = {
                **params.get("extra_body", {}),
                "include": _ENCRYPTED_REASONING,
                "store": False,
            }
        # Caller-supplied kwargs (tools, tool_choice, parallel_tool_calls, stop) override the model defaults so bind_tools() bindings reach LiteLLM.
        params.update({key: value for key, value in kwargs.items() if value is not None})
        return params


    def _trace_request(self, params: dict[str, Any], sent: list[dict[str, Any]]) -> RequestTrace:
        """Cut the outgoing request into the pieces a prompt cache matches on, in wire order.

        Tool schemas first, since every OpenAI-shaped request carries them ahead of the
        conversation, then one segment per message.
        """
        pieces = [Piece(kind=TOOLS, text=compact(params.get("tools") or []))]
        for position, message in enumerate(sent):
            pieces.append(Piece(kind=ITEM, text=compact(message), position=position,
                                role=str(message.get("role") or "")))
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
        # include_usage asks the provider for a trailing usage chunk so the real prompt/completion token counts are reported for the streamed turn (LiteLLM drops this option for providers that don't support it, and normalizes the usage object across providers either way).
        params = self._completion_kwargs(
            stop=stop, stream=True, stream_options={"include_usage": True}, **kwargs,
        )
        # One name for the prose this call produces.
        block = f"litellm-{uuid4().hex}-"
        sent = self._apply_cache_breakpoints(self._messages_to_dicts(messages), messages)
        # Taken before the request, reported once the response says what the cache did — a hit figure on its own cannot say whether we moved the prefix or the provider simply missed.
        current_trace = self._trace_request(params, sent)
        reported = False
        stream = cast(AsyncIterator[Any], await litellm.acompletion(messages=sent, **params))
        async for chunk in stream:
            generation_chunk = self._litellm_chunk_to_generation_chunk(chunk, block)
            if generation_chunk is not None:
                # Attached to the chunk that carries usage, so the diagnosis travels with the figure it explains and is recorded with it.
                if generation_chunk.message.usage_metadata and not reported:
                    reported = True
                    generation_chunk.message.additional_kwargs["cache_trace"] = self._cache_diagnosis(current_trace)
                yield generation_chunk

    @staticmethod
    def _usage_metadata(usage: Any) -> Optional[UsageMetadata]:
        """Normalize a LiteLLM ``Usage`` object into a LangChain ``UsageMetadata``
        dict, so real per-call token counts ride along on the message and merge
        automatically when streamed chunks are combined. Returns ``None`` when the
        response carries no usage."""
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
        prompt_details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else getattr(usage, "prompt_tokens_details", None)
        cache_read = _value(prompt_details, "cached_tokens")
        if cache_read:
            metadata["input_token_details"] = {"cache_read": cache_read}
        completion_details = usage.get("completion_tokens_details") if isinstance(usage, dict) else getattr(usage, "completion_tokens_details", None)
        reasoning = _value(completion_details, "reasoning_tokens")
        if reasoning:
            metadata["output_token_details"] = {"reasoning": reasoning}
        return cast(UsageMetadata, metadata)

    def _litellm_chunk_to_generation_chunk(self, chunk: Any, block: str = "") -> Optional[ChatGenerationChunk]:
        usage_metadata = ChatLiteLLMModel._usage_metadata(getattr(chunk, "usage", None))
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            # The trailing include_usage chunk (and some providers' final chunk) carries usage but no choices — surface an empty message that only transports the token counts so the merged response accumulates them.
            if usage_metadata is not None:
                return ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=usage_metadata))
            return None
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            if usage_metadata is not None:
                return ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=usage_metadata))
            return None
        content = getattr(delta, "content", None) or ""
        tool_call_chunks: list[ToolCallChunk] = []
        for call in getattr(delta, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            tool_call_chunks.append(cast(ToolCallChunk, {
                "index": getattr(call, "index", 0) or 0,
                "id": getattr(call, "id", None),
                "name": getattr(function, "name", None) if function else None,
                # The OpenAI streaming delta exposes the partial JSON as function.arguments; langchain-core's ToolCallChunk stores it under the ``args`` key (not ``arguments``).
                "args": getattr(function, "arguments", None) if function else None,
                "type": "tool_call_chunk",
            }))
        reasoning = getattr(delta, "reasoning_content", None)
        if not reasoning:
            # Some providers nest reasoning under a different attribute.
            reasoning = getattr(delta, "reasoning", None)
        content_blocks = ChatLiteLLMModel._standard_content_blocks(content, reasoning, block)
        message = AIMessageChunk(
            content=content_blocks_to_message_content(content_blocks),
            tool_call_chunks=tool_call_chunks,
            usage_metadata=usage_metadata,
            # Chunk merging concatenates these lists, so the finished message ends up holding every signed block of the turn, in the order the model produced them.
            additional_kwargs=self._reasoning_to_carry(delta),
        )
        finish_reason = getattr(choice, "finish_reason", None)
        generation_info = {"finish_reason": finish_reason} if finish_reason else None
        return ChatGenerationChunk(message=message, generation_info=generation_info)

    @staticmethod
    def _standard_content_blocks(content: Any, reasoning: Any, block: str = "") -> list[ContentBlock]:
        """Name the blocks in one streamed chunk.

        `block` identifies the model call this chunk belongs to, and it is what makes these
        identifiers *identify*. They used to be the position within the chunk, which is 0 for
        every chunk of every call — so a whole session's prose shared one name, and anything
        downstream that trusted the identifier to mean "this block" was reading a constant. The
        Responses API hands out a real per-item id; LiteLLM streams no such thing, so the id is
        minted per stream here and the position distinguishes blocks within a chunk.
        """
        normalized_blocks: list[ContentBlock] = []
        if reasoning:
            normalized_blocks.append(ReasoningContentBlock(
                type="reasoning",
                reasoning=str(reasoning),
                id=f"{block}reasoning",
                index=0,
            ))
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
            tool_calls.append({
                "name": getattr(function, "name", None) if function else None,
                "args": parsed_arguments,
                "id": getattr(call, "id", None),
            })
        message = AIMessage(
            content_blocks=ChatLiteLLMModel._standard_content_blocks(content, reasoning),
            # An empty list, never `None`.
            tool_calls=list(tool_calls or []),
            usage_metadata=ChatLiteLLMModel._usage_metadata(getattr(response, "usage", None)),
            additional_kwargs=self._reasoning_to_carry(message_obj),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])
