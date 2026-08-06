"""``ChatCodexModel`` — a LangChain chat model that talks to a ChatGPT subscription.

This is the counterpart to :class:`~frank.runtime.models.litellm.ChatLiteLLMModel`
for the experimental ``chatgpt`` provider. It does **not** go through LiteLLM,
because the ChatGPT-subscription route is not the normal OpenAI API: it is Codex's
endpoint ``chatgpt.com/backend-api/codex/responses``, which speaks the OpenAI
*Responses* API (not Chat Completions), is stateless (``store: false`` with the
full history resent every turn), and authenticates with the OAuth token minted for
Codex plus a ``ChatGPT-Account-Id`` header.

It sends frank's *own* system prompt in the ``instructions`` field like any normal
Responses request — there is no Codex-prompt impersonation. The widely repeated claim
that the endpoint validates a Codex-specific system prompt does not hold; what it does
check is the client's identity, which is why ``originator`` names the Codex CLI while the
prompt stays ours. See :mod:`frank.base.subscription` for that header set — including why
``User-Agent`` still says frank — and :mod:`frank.base.credentials` for the auth/token side.
"""

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

from frank.base.credentials import (
    ChatGPTAuthError,
    load_tokens,
    valid_tokens,
)
from frank.base.message_content import (
    REASONING_MODEL_KEY,
    carried_reasoning_for,
    content_blocks_to_message_content,
    message_text,
)
from frank.base.model_errors import CONTEXT_OVERFLOW_CODES, ContextWindowExceeded
from frank.runtime.cache_trace import INSTRUCTIONS, ITEM, TOOLS, Piece, RequestTrace, diagnose, trace
from frank.base.serialization import compact, upstream_detail
from frank.base.subscription import (
    RESPONSES_URL,
    cached_subscription_models,
    capture_usage_headers,
    request_headers,
)

# What the Codex endpoint serves when its own catalogue has not been fetched yet. Deliberately the
# conservative figure rather than the advertised one — see :meth:`ChatCodexModel.context_window`.
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
    """A ``BaseChatModel`` backed by the ChatGPT-subscription Codex endpoint.

    Auth is not passed in: the access token and account id are read (and refreshed)
    from the shared token store on every call, so a single sign-in serves every
    agent and a background refresh is transparent to callers. ``context_length`` is
    the cold-start fallback the factory reads from the models.dev catalog, but the
    Codex endpoint enforces a *smaller* budget than models.dev's direct-API metadata
    advertises (e.g. models.dev lists ~1M for gpt-5.5/5.6 while Codex serves 272k),
    so :meth:`context_window` prefers the live per-account value when it is known and
    never reports more than :data:`COLD_START_WINDOW` when it is not."""

    model: str
    reasoning_effort: Optional[str] = None
    temperature: float = 0.0
    context_length: int = 0
    #: The conversation this model serves, sent as ``prompt_cache_key``. See
    #: :meth:`_build_payload`.
    session_id: str = ""
    # A generous bound so a dead connection cannot hang a turn forever, matching
    # ChatLiteLLMModel; the streaming loop only checks aborts between chunks.
    timeout: Optional[float] = 300.0

    #: The last request's segment trace, kept so the next one can be told what moved. Declared
    #: rather than merely assigned: a leading underscore on a Pydantic model is a private
    #: attribute, and one that is not declared cannot be set at all.
    _previous_trace: Optional[RequestTrace] = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "codex"

    def context_window(self) -> int:
        # The live subscription catalog is authoritative for the real Codex budget.
        live = cached_subscription_models().get(self.model)
        if live and live.get("context"):
            return int(live["context"])
        # Until that cache is warm, models.dev's figure is not merely unverified, it is known to
        # be wrong for this endpoint in the one direction that does harm: it describes the direct
        # API, and Codex serves less. Believing it made the interface report a conversation as 3%
        # full while the provider was refusing it for length — and, because every window-scaled
        # budget in `tuning` divides by this number, it also set the cap on tool results four
        # times too high, so the backstop against overlong results was itself sized by the error
        # it was meant to catch. Taking the smaller of the two is the only reading that is wrong
        # in the harmless direction.
        return min(self.context_length, COLD_START_WINDOW) if self.context_length else COLD_START_WINDOW

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
        pair. frank's own system prompt(s) go straight into ``instructions`` (the
        standard Responses home for system text) — no Codex preamble. Tool
        calls/results become top-level ``function_call`` / ``function_call_output``
        items (Responses does not nest them in a message)."""
        instructions = ""
        items: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, SystemMessage):
                text = self._text_of(message)
                # Only the *first* one is `instructions`, and only because that is the static
                # prompt this harness puts at the head of every request. Everything after it
                # stays where it was written, as a developer-role item — which is what the Codex
                # CLI does with every piece of contextual material it sends.
                #
                # Joining them all was a rewrite of the first bytes of the request. Anything
                # later in the list — the observation log, most of all — was lifted out of its
                # position and prepended to the prompt, so a fold aimed at saving context threw
                # away the provider's cache for the entire conversation on its way past.
                if not instructions:
                    instructions = text
                elif text:
                    items.append({
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": text}],
                    })
                continue
            if isinstance(message, ToolMessage):
                items.append({
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": self._text_of(message),
                })
                continue
            if isinstance(message, AIMessage):
                # The model's own prior thinking, handed straight back. It has to come before the
                # calls it produced: the endpoint reads this list as the turn in the order it
                # happened, and reasoning that arrives after the call it explains describes
                # nothing. The blobs are opaque and encrypted — they are not read here, only
                # carried, which is the whole of what `store: false` asks of a client.
                items.extend(carried_reasoning_for(message, self.model).get("reasoning_items") or [])
                text = self._text_of(message)
                if text:
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    })
                for call in message.tool_calls or []:
                    arguments = call.get("args")
                    serialized = arguments if isinstance(arguments, str) else compact(arguments)
                    items.append({
                        "type": "function_call",
                        "call_id": call.get("id"),
                        "name": call.get("name"),
                        "arguments": serialized,
                    })
                continue
            # A reminder is not the user talking, and the Responses API can say so directly: a
            # `developer` item stays exactly where it was written, unlike a system message, which
            # this translation would fold into `instructions` at the front of the request. So the
            # role carries the distinction here and no wrapper is needed — which is also what the
            # Codex CLI does with every piece of contextual material it sends.
            role = "developer" if message.additional_kwargs.get("reminder") else "user"
            items.append({
                "type": "message",
                "role": role,
                "content": [{"type": "input_text", "text": self._text_of(message)}],
            })
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
        # Sent whether or not there are tools, matching the Codex CLI: it is a constant in every
        # request it builds, and a field that appears and disappears is a field that can move the
        # bytes in front of the conversation.
        payload["tool_choice"] = kwargs.get("tool_choice") or "auto"
        tools = kwargs.get("tools")
        if tools:
            payload["tools"] = [self._to_responses_tool(tool) for tool in tools]
        if self.session_id:
            # Which cache to look in. The endpoint routes a cache lookup by hashing the leading
            # couple of hundred tokens of the prefix *together with* this key, and OpenAI's own
            # guidance for GPT-5.6 and later is that it must be set for the reliable matching to
            # apply at all. Sending nothing is why an append-only conversation still missed: 41
            # of 66 calls in one session reported zero cached tokens against a prefix that had
            # not changed. One session is one conversation is one prefix.
            payload["prompt_cache_key"] = self.session_id
        # Both unconditional, as in the Codex CLI. `reasoning` is always present — with a null
        # effort when none is configured — and `include` is a constant there, never gated on
        # anything. Asking for the encrypted reasoning back is what makes `store: false` workable
        # at all: the endpoint keeps nothing between calls, so a model not handed its own prior
        # thinking derives it again at every tool hop, paying for the same reasoning repeatedly
        # and losing the thread that made the last call make sense. The content is opaque here
        # and round-trips through `_to_responses_input` untouched.
        payload["reasoning"] = {
            "effort": self.reasoning_effort or None, "summary": "auto",
        }
        payload["include"] = ["reasoning.encrypted_content"]
        return payload

    async def _headers(self) -> dict[str, str]:
        """The request headers, with a freshly-valid access token.

        No longer static: the headers carry this conversation's id, which the endpoint routes a
        cache lookup by. The token store is still per-account and per-machine, and that is what
        being static used to be justified by — but a request that cannot say which conversation
        it belongs to cannot be sent to where that conversation's prefix already is."""
        return request_headers(await valid_tokens(), self.session_id)

    @staticmethod
    def _http_error(status: int, body: str) -> Exception:
        if status in (401, 403):
            return ChatGPTAuthError(
                "ChatGPT rejected the subscription token (expired, revoked, or plan "
                f"lacks access). Sign in again. Detail: {upstream_detail(body)}"
            )
        # An overlong request is refused before the stream opens as often as during it, and the
        # two paths have to agree — a failure that is specific down one and generic down the other
        # is the same opacity, arrived at by a different route. Read from the body's `code`, like
        # the streaming path, rather than from its prose.
        if status == 400 and _error_code(body) in CONTEXT_OVERFLOW_CODES:
            return ContextWindowExceeded("The request exceeded this model's context window.")
        return RuntimeError(f"ChatGPT Codex endpoint returned {status}: {upstream_detail(body)}")

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
        if event_type == "response.output_item.done":
            # A finished reasoning item, kept so the next call can hand it back (see
            # `_to_responses_input`). Only the encrypted form is worth keeping: the summary is
            # for the reader, and it is the `encrypted_content` the endpoint will accept as the
            # model's own thinking. Riding in `additional_kwargs` means it merges across chunks,
            # survives persistence, and reaches a runtime rebuilt from a checkpoint.
            item = data.get("item") or {}
            if item.get("type") == "reasoning" and item.get("encrypted_content"):
                return cls._chunk(reasoning_item={
                    "type": "reasoning",
                    "id": item.get("id"),
                    "summary": item.get("summary") or [],
                    "encrypted_content": item["encrypted_content"],
                }, model=str(state.get("model") or ""))
            return None
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
            structured = detail if isinstance(detail, dict) else {}
            message = structured.get("message") if structured else str(detail)
            # The failure event carries a machine-readable `code` beside its `message`, and this
            # used to read only the message and raise a bare RuntimeError. Everything downstream
            # then had a sentence and no way to act on it, so a context-window overflow — which
            # the harness has a precise answer for — reached the user as "the turn stopped
            # unexpectedly, see the server log", while the log held the provider saying exactly
            # what was wrong.
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
            # Merging two chunks concatenates the lists under a key, so one item per chunk
            # accumulates into the turn's reasoning in the order it arrived. Deliberately
            # carrying no `index`: that is the key LangChain merges list entries *by*, and these
            # are a sequence to append to, not slots to overwrite.
            #
            # The model rides along because the blob is encrypted *to a model*: a session that
            # changes model between turns must not hand the new one thinking it cannot decrypt.
            additional_kwargs=(
                {"reasoning_items": [reasoning_item], REASONING_MODEL_KEY: model}
                if reasoning_item else {}
            ),
        )
        return ChatGenerationChunk(message=message)


    def _trace_payload(self, payload: dict[str, Any]) -> RequestTrace:
        """Cut the outgoing request into the pieces a prompt cache matches on, in wire order.

        Instructions first, then the tool schemas, then one segment per input item — which is
        exactly how the endpoint reads a prefix, so a segment index here is an offset there.
        """
        pieces = [
            Piece(kind=INSTRUCTIONS, text=payload.get("instructions") or ""),
            Piece(kind=TOOLS, text=compact(payload.get("tools") or [])),
        ]
        for position, item in enumerate(payload.get("input") or []):
            # A Responses item is identified by its type; a message item carries a role as well,
            # and the role is the more telling of the two, so it wins where there is one.
            pieces.append(Piece(kind=ITEM, text=compact(item), position=position,
                                role=str(item.get("role") or item.get("type") or "")))
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
        # Taken before the request and held until the response says what the cache did, because
        # the figure alone cannot say *why*. See `frank.runtime.cache_trace`.
        current_trace = self._trace_payload(payload)
        reported = False
        # Carried so a failure can name the model that refused the request and the window it was
        # measured against — the two things that make "too large" actionable rather than a verdict.
        state: dict[str, Any] = {"saw_tool_call": False, "model": self.model,
                                 "context_window": self.context_window()}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", RESPONSES_URL, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise self._http_error(response.status_code, body)
                capture_usage_headers(response.headers)
                async for line in response.aiter_lines():
                    chunk = self._line_to_chunk(line, state)
                    if chunk is not None:
                        # Attached to the chunk that carries usage, so the diagnosis travels
                        # with the figure it explains and is recorded with it.
                        if chunk.message.usage_metadata and not reported:
                            reported = True
                            chunk.message.additional_kwargs["cache_trace"] = self._cache_diagnosis(current_trace)
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
            # An empty list, never `None`. `AIMessage.tool_calls` is a list with a default,
            # and a default applies to an *omitted* key — passing `None` explicitly fails
            # validation before any caller can look at it. Most turns carry tool calls so
            # this stayed hidden; the one that reliably does not is titling, which is why
            # every conversation was named "Untitled conversation".
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
        # BaseChatModel requires a sync path; the harness runs async, so this is a
        # rarely-used fallback that reads the token without the async refresh.
        tokens = load_tokens()
        if tokens is None or tokens.is_expired():
            raise ChatGPTAuthError("Not signed in to ChatGPT (or the session expired).")
        payload = self._build_payload(messages, stream=True, **kwargs)
        headers = request_headers(tokens, self.session_id)
        # Carried so a failure can name the model that refused the request and the window it was
        # measured against — the two things that make "too large" actionable rather than a verdict.
        state: dict[str, Any] = {"saw_tool_call": False, "model": self.model,
                                 "context_window": self.context_window()}
        aggregate: Optional[ChatGenerationChunk] = None
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", RESPONSES_URL, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    raise self._http_error(response.status_code, response.read().decode("utf-8", "replace"))
                capture_usage_headers(response.headers)
                for line in response.iter_lines():
                    chunk = self._line_to_chunk(line, state)
                    if chunk is not None:
                        aggregate = chunk if aggregate is None else aggregate + chunk
        return self._aggregate_to_result(aggregate)
