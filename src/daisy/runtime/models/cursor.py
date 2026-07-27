"""``ChatCursorModel`` — a LangChain chat model that talks to a Cursor subscription.

The third of the three model clients, and the odd one out. :class:`ChatLiteLLMModel`
speaks to providers that offer a chat API. :class:`ChatCodexModel` speaks to a provider
that offers a chat API and pretends it does not. Cursor offers no chat API at all: what
it exposes to its own CLI is ``agent.v1.AgentService``, a Connect-RPC service whose unit
of work is not a completion but a *turn of an agent* — one that streams text, asks the
client to run tools, reads and writes a blob store, and expects to be told when each of
those finished.

So the work here is not translation but reduction: taking a protocol built to drive an
agent and using it to answer one question, which is what every other model in Daisy
answers — given these messages and these tools, what does the model say next.

## Two shapes that do not match, and which one gives way

Cursor's turn is stateful. A conversation is a server-side identity, tool results are
pushed into a stream that stays open across them, and the model continues inside that
same stream. Daisy's turn is stateless: the harness owns the transcript, resends all of
it every time, and expects one assistant reply per call — the same contract that made
``store: false`` the right choice for the Codex client.

Daisy's shape wins, and it wins for a reason rather than for convenience. The harness
compacts history, rewrites it, and replays sessions across process restarts; a
conversation whose real state lived on Cursor's side would drift from the transcript
Daisy believes in, and the transcript is the thing users read and sessions resume from.
So every call here opens a fresh run: the whole conversation is rendered into the turn's
user message, the tools are re-declared, and when the model calls one, this stream ends
and the tool call is handed back. Daisy runs the tool, appends the result, and calls
again — where the next run sees it as history.

The cost is real and worth naming: no server-side prompt caching, and history the model
reads as a rendered transcript rather than as structured turns. What it buys is that
there is exactly one place a conversation lives.

## The stream, and why it takes two requests

``RunSSE`` is server-streaming: it opens a stream named by a request id and sends
``AgentServerMessage``s down it. It does not carry the turn. The turn goes up separately,
through unary ``BidiAppend`` calls addressed to that same request id — which is also how
tool results, blob answers and rejections go up while the stream is running. So a turn is
one long-lived download plus a series of short uploads, and the download has to be
opened first or the uploads have nowhere to land.

## What the agent asks for that Daisy will not do

Cursor's agent has built-in tools — shell, read, write, grep — and it reaches for them
regardless of what the client offered, because its toolset is decided server-side. Daisy
declines every one, using the protocol's own rejection variants (see
:data:`~daisy.runtime.models.cursor_wire.REJECTABLE_EXECS`). Running them would execute
commands and edit files from inside a model client, outside the permission mode, the
confinement, and the session the harness attributes work to. A model client is not a
place from which to touch the machine.

See :mod:`daisy.base.cursor_credentials` for the OAuth side and
:mod:`daisy.runtime.models.cursor_wire` for the protocol itself.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import platform
import re
import time
import uuid
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

from daisy.base.cursor_credentials import (
    API_BASE_URL,
    CursorAuthError,
    CursorTokens,
    valid_tokens,
)
from daisy.base.message_content import content_blocks_to_message_content, message_text
from daisy.base.serialization import compact
from daisy.base.tuning import Tunable, active_tuning
from daisy.runtime.models import cursor_wire as wire

RUN_URL = f"{API_BASE_URL}/agent.v1.AgentService/RunSSE"
APPEND_URL = f"{API_BASE_URL}/aiserver.v1.BidiService/BidiAppend"
# The two model endpoints, on different services and knowing different halves of the answer:
# which models a plan serves, and how large a window each has.
USABLE_MODELS_URL = f"{API_BASE_URL}/agent.v1.AgentService/GetUsableModels"
AVAILABLE_MODELS_URL = f"{API_BASE_URL}/aiserver.v1.AiService/AvailableModels"

# A real Cursor CLI build, and specifically the newest one that any working client is known
# to send: `cli-2026.01.09-231024f` appears in three files across the two OpenCode plugins
# that drive this service. It is not invented and not derived from today's date — an
# authentic-looking version nobody has exercised is worse than an obviously old one, because
# it invites trust it has not earned.
#
# Unlike the Codex client, where `client_version` is a documented floor that hides models
# below it, nothing in Cursor's descriptor gates anything on this. What is known is only that
# the service wants the header: one plugin ships `cli-unknown` as its fallback and works, so
# the value appears to be far less load-bearing than its Codex counterpart. Treat it as a
# value to keep roughly current, not as a floor to clear.
CLIENT_VERSION = "cli-2026.01.09-231024f"
CLIENT_TYPE = "cli"

# gRPC status codes worth naming. 8 is RESOURCE_EXHAUSTED, which on this service means
# the subscription's included usage is spent; 16 is UNAUTHENTICATED.
_STATUS_RESOURCE_EXHAUSTED = 8
_STATUS_UNAUTHENTICATED = 16

# What the model is told about the tools it has been handed. Cursor puts this in
# ``McpInstructions``, alongside the tool definitions themselves.
_TOOL_INSTRUCTIONS = (
    "These are the tools of the Daisy harness you are answering inside. Use them for "
    "everything you need to do: they are the only ones whose results reach you."
)
_BUILTIN_REFUSAL = (
    "Daisy declines this built-in tool. Use the tools provided to you under the "
    "\"daisy\" server instead — they are the only ones this client can run."
)


class ChatCursorModel(BaseChatModel):
    """A ``BaseChatModel`` backed by a Cursor subscription's agent service.

    Auth is not passed in: the token is read (and refreshed) from the shared store on
    every call, so a single sign-in serves every agent and a background refresh is
    invisible here. ``workspace`` is the directory the turn claims to be running in —
    Cursor's ``RequestContext`` wants one, and the agent's own file tools are declined
    anyway, so it is context rather than capability."""

    model: str
    workspace: str = ""
    context_length: int = 0
    # A generous bound so a dead connection cannot hang a turn forever, matching the
    # other two clients; the streaming loop only checks aborts between frames.
    timeout: Optional[float] = 300.0

    @property
    def _llm_type(self) -> str:
        return "cursor"

    def context_window(self) -> int:
        """The model's context window, preferring the most authoritative source that has one.

        A turn that has run beats everything: the server states the window it is filling in
        each checkpoint, for this account and this model. Before that there is the window the
        model catalog reported at discovery. Only when neither exists — a catalog entry that
        arrived without one — is there a constant, and it is a floor rather than a guess at
        the model's family."""
        if observed := _observed_context_windows.get(self.model):
            return observed
        discovered = cached_subscription_models().get(self.model) or {}
        return int(discovered.get("context") or 0) or self.context_length or UNKNOWN_CONTEXT_WINDOW

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model}

    # Tool binding — the same surface as the other two clients, so the harness binds
    # identically; the Cursor-shaped translation happens at request-build time.

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: Optional[str] = None,
        parallel_tool_calls: Optional[bool] = None,
        **kwargs: Any,
    ) -> Runnable:
        # tool_choice and parallel_tool_calls have no counterpart in Cursor's protocol —
        # the agent decides both — so they are accepted and dropped rather than faked.
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    # Turning the harness's messages into one Cursor turn.

    @staticmethod
    def _system_prompt(messages: Sequence[BaseMessage]) -> str:
        return "\n\n".join(
            text for text in (message_text(message) for message in messages
                              if isinstance(message, SystemMessage)) if text
        )

    def _render(self, messages: Sequence[BaseMessage]) -> str:
        """The conversation as the single user message Cursor's turn accepts.

        A first turn is just its text — no scaffolding around a single question. Once
        there is history it is labelled, because the model has to be able to tell its own
        previous output and a tool's result apart from what the user said."""
        conversation = [message for message in messages if not isinstance(message, SystemMessage)]
        if len(conversation) == 1 and not isinstance(conversation[0], (AIMessage, ToolMessage)):
            return message_text(conversation[0])
        blocks: list[str] = []
        for message in conversation:
            if isinstance(message, ToolMessage):
                blocks.append(f"## Tool result: {message.name or 'tool'}\n{message_text(message)}")
                continue
            if isinstance(message, AIMessage):
                body = message_text(message)
                if body:
                    blocks.append(f"## Assistant\n{body}")
                for call in message.tool_calls or []:
                    arguments = call.get("args")
                    rendered = arguments if isinstance(arguments, str) else compact(arguments)
                    blocks.append(f"## Assistant tool call: {call.get('name')}\n{rendered}")
                continue
            blocks.append(f"## User\n{message_text(message)}")
        return self._preamble(any(isinstance(m, ToolMessage) for m in conversation)) + "\n\n" + "\n\n".join(blocks)

    @staticmethod
    def _preamble(carries_tool_results: bool) -> str:
        """What to say about the transcript that follows.

        The second sentence is not decoration. Cursor's agent is built to keep working, and a
        transcript ending in a tool result reaches it as a *new* turn rather than as the
        continuation of one — the protocol has no way to hand back a structured tool result
        outside the stream that asked for it, so a completed call is only ever prose by the
        time the model reads it. Without being told, an agent that reads "I ran `ls`, here is
        the output" can reasonably decide to run `ls`. The maintained OpenCode plugin says the
        same thing in its own recovery prompts, which is where the wording comes from."""
        lines = ["The conversation so far, oldest first. Continue it: answer the last message, "
                 "calling the tools provided when you need them."]
        if carries_tool_results:
            lines.append(
                "Every tool call shown below has already run and its result is included. Do not "
                "run those again unless something has changed — continue from what they returned."
            )
        return "\n".join(lines)

    def _environment(self) -> bytes:
        """The ``RequestContextEnv`` a turn describes itself with — where the client
        believes it is running, and what it is running on."""
        workspace = self.workspace or os.getcwd()
        return wire.request_context_env(
            workspace=workspace,
            shell=os.environ.get("SHELL", "/bin/sh"),
            os_version=f"{platform.system().lower()} {platform.release()}",
            time_zone=_time_zone(),
        )

    def _build_turn(
        self, messages: Sequence[BaseMessage], tools: list[dict[str, Any]]
    ) -> tuple[bytes, dict[bytes, bytes]]:
        """One serialized ``AgentClientMessage``, plus the blobs it refers to.

        The system prompt does not travel inline. Cursor's conversation state names its
        root prompt by blob id and then *asks* for the blob over the KV channel while the
        turn runs, so it is hashed here and handed over when requested."""
        blobs: dict[bytes, bytes] = {}
        root_prompt_ids: list[bytes] = []
        if system_prompt := self._system_prompt(messages):
            payload = json.dumps({"role": "system", "content": system_prompt}).encode("utf-8")
            blob_id = hashlib.sha256(payload).digest()
            blobs[blob_id] = payload
            root_prompt_ids.append(blob_id)

        message_body = wire.user_message(self._render(messages), str(uuid.uuid4()))
        # Cursor keys a user message's blob by its own serialized bytes rather than a
        # hash of them, so a request for it can be answered from the same store.
        blobs[message_body] = message_body

        workspace = self.workspace or os.getcwd()
        environment = self._environment()
        tool_definitions = [
            wire.mcp_tool_definition(
                name=(function := tool.get("function", tool)).get("name", ""),
                description=function.get("description", "") or "",
                schema=function.get("parameters") or {"type": "object", "properties": {}},
            )
            for tool in tools
        ]
        action = wire.blob(
            1,  # ConversationAction.user_message_action
            wire.blob(1, message_body)
            + wire.blob(2, wire.request_context(environment, tool_definitions, _TOOL_INSTRUCTIONS)),
        )
        run_request = wire.agent_run_request(
            state=wire.conversation_state(root_prompt_ids),
            action=action,
            model=wire.model_details(self.model),
            tools=tool_definitions,
            conversation_id=str(uuid.uuid4()),
            file_system_options=(
                wire.mcp_file_system_options(workspace, _TOOL_INSTRUCTIONS) if tool_definitions else b""
            ),
        )
        return wire.client_message_run(run_request), blobs

    # Transport.

    @staticmethod
    def _headers(tokens: CursorTokens, request_id: str) -> dict[str, str]:
        """The header set the ``RunSSE`` + ``BidiAppend`` transport was tested with.

        Which headers to send is not a free choice, because the two transports through this
        service were exercised with different sets. The plugins that hold an HTTP/2 stream open
        for ``Run`` send no checksum, no timezone and no streaming hint; the one that uses this
        transport sends all three. Matching that one is deliberate — a header set assembled by
        taking the union of two tested combinations is a third combination nobody has tried."""
        return {
            "Authorization": f"Bearer {tokens.access_token}",
            # Not Connect's own content type: this service answers 415 to
            # application/connect+proto over HTTP/1.1 and expects the gRPC-web framing.
            "Content-Type": "application/grpc-web+proto",
            "x-cursor-checksum": _checksum(tokens.access_token),
            "x-cursor-client-version": CLIENT_VERSION,
            "x-cursor-client-type": CLIENT_TYPE,
            "x-cursor-timezone": _time_zone(),
            # Privacy mode: do not retain this conversation for training.
            "x-ghost-mode": "true",
            # Without this the service may park a reply in the blob store instead of
            # streaming it, and a turn that streams nothing looks like a turn that failed.
            "x-cursor-streaming": "true",
            "x-request-id": request_id,
        }

    @staticmethod
    def _auth_error(status: int, detail: str) -> Exception:
        if status in (401, 403):
            return CursorAuthError(
                "Cursor rejected the subscription token (expired, revoked, or the plan "
                f"lacks access). Sign in again. Detail: {detail[:300]}"
            )
        return RuntimeError(f"Cursor agent service returned {status}: {detail[:500]}")

    @staticmethod
    def _stream_error(status: int, message: str) -> Exception:
        if status == _STATUS_RESOURCE_EXHAUSTED:
            return RuntimeError("Cursor reports this subscription's usage limit is reached.")
        if status == _STATUS_UNAUTHENTICATED:
            return CursorAuthError("Cursor rejected the subscription token. Sign in again.")
        return RuntimeError(f"Cursor agent stream failed (grpc-status {status}): {message or 'no detail'}")

    async def _append(
        self, client: httpx.AsyncClient, tokens: CursorTokens, request_id: str,
        sequence: int, payload: bytes,
    ) -> None:
        """Push one ``AgentClientMessage`` into the open run."""
        response = await client.post(
            APPEND_URL,
            content=wire.frame(wire.bidi_append_request(request_id, sequence, payload)),
            headers=self._headers(tokens, request_id),
        )
        if response.status_code >= 400:
            raise self._auth_error(response.status_code, response.text)

    # Streaming generation (the path the harness actually uses).

    async def _astream(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        tokens = await valid_tokens()
        request_id = str(uuid.uuid4())
        turn, blobs = self._build_turn(messages, kwargs.get("tools") or [])
        headers = self._headers(tokens, request_id)

        async with httpx.AsyncClient(timeout=self.timeout, http2=False) as client:
            request = client.build_request(
                "POST", RUN_URL, content=wire.frame(wire.bidi_request_id(request_id)), headers=headers,
            )
            # The run has to be opened before the turn is pushed into it, but awaiting the
            # response headers first would deadlock against a server that has nothing to
            # send until the turn arrives. So the open is started, the turn is pushed, and
            # only then are the headers awaited.
            opening = asyncio.create_task(client.send(request, stream=True))
            sequence = 0
            try:
                await self._append(client, tokens, request_id, sequence, turn)
                sequence += 1
            except BaseException:
                # The turn never made it up, so the run it would have driven is dead;
                # close it rather than leaving an open stream behind the raised error.
                opening.cancel()
                with contextlib.suppress(BaseException):
                    await (await opening).aclose()
                raise
            response = await opening
            try:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")
                    raise self._auth_error(response.status_code, detail)
                async for chunk in self._read(client, tokens, request_id, sequence, response, blobs):
                    yield chunk
            finally:
                await response.aclose()

    async def _read(
        self,
        client: httpx.AsyncClient,
        tokens: CursorTokens,
        request_id: str,
        sequence: int,
        response: httpx.Response,
        blobs: dict[bytes, bytes],
    ) -> AsyncIterator[ChatGenerationChunk]:
        """Read the run to its end, answering what the server asks along the way.

        Ends on the first tool call the model makes (handed back for Daisy to run), on
        ``turn_ended``, or on a non-zero trailer status. Anything the server asks for in
        between — a blob, a built-in tool — is answered inline so the turn keeps moving."""
        deframer = wire.Deframer()
        # The two counts come from different places and mean different things. Generated tokens
        # arrive as deltas and are summed; the prompt size is the conversation's context fill,
        # reported whole in each checkpoint, so the largest seen in the turn is the one to keep —
        # an early checkpoint would otherwise pin the meter below the real figure.
        output_tokens = 0
        input_tokens = 0
        # A tool call is announced once (``tool_call_started``) and then asked for once
        # (an mcp exec request), and it is the exec request this hands back, because that
        # is the one carrying the arguments the server committed to. The announcement is
        # kept anyway: if a turn ends having announced a call that was never asked for,
        # dropping it would lose the model's move silently, and a turn that says nothing
        # is indistinguishable from a turn that failed.
        announced: Optional[wire.ToolCall] = None
        async for data in response.aiter_bytes():
            for flags, payload in deframer.feed(data):
                if flags & wire.TRAILER_FLAG:
                    status, detail = wire.parse_trailer(payload)
                    if status != 0:
                        raise self._stream_error(status, detail)
                    continue
                message = wire.parse_server_message(payload)
                output_tokens += message.output_token_delta
                if (details := message.token_details) is not None:
                    input_tokens = max(input_tokens, details.used_tokens)
                    _record_context_window(self.model, details.maximum_tokens)
                announced = message.tool_call or announced
                if message.text_delta:
                    yield _chunk(content_block=_text_block(message.text_delta, request_id))
                if message.thinking_delta:
                    yield _chunk(content_block=_reasoning_block(message.thinking_delta, request_id))
                if message.blob_request is not None:
                    sequence = await self._answer_blob(
                        client, tokens, request_id, sequence, message.blob_request, blobs,
                    )
                if (request := message.exec_request) is not None:
                    if request.tool_call is not None:
                        # The model called one of Daisy's tools. The harness runs it, so
                        # this run is over: hand the call back and stop reading.
                        yield _tool_call_chunk(request.tool_call)
                        yield _final_chunk("tool_calls", input_tokens, output_tokens)
                        return
                    sequence = await self._decline(client, tokens, request_id, sequence, request)
                if message.turn_ended:
                    async for chunk in self._close_turn(announced, input_tokens, output_tokens):
                        yield chunk
                    return
        # The stream closed without a turn_ended, which happens on a clean server-side
        # end as readily as on a truncation. Report what arrived rather than raising.
        async for chunk in self._close_turn(announced, input_tokens, output_tokens):
            yield chunk

    @staticmethod
    async def _close_turn(
        announced: Optional[wire.ToolCall], input_tokens: int, output_tokens: int
    ) -> AsyncIterator[ChatGenerationChunk]:
        """The turn's ending, with a tool call the server announced but never asked for."""
        if announced is not None:
            yield _tool_call_chunk(announced)
            yield _final_chunk("tool_calls", input_tokens, output_tokens)
            return
        yield _final_chunk("stop", input_tokens, output_tokens)

    async def _answer_blob(
        self,
        client: httpx.AsyncClient,
        tokens: CursorTokens,
        request_id: str,
        sequence: int,
        request: wire.BlobRequest,
        blobs: dict[bytes, bytes],
    ) -> int:
        """Serve the conversation's blob store: hand over a blob, or take one to hold.

        A read we cannot satisfy is answered empty rather than left hanging — an
        unanswered KV request stalls the whole run."""
        if request.is_read:
            body = blobs.get(request.blob_id, b"")
            await self._append(
                client, tokens, request_id, sequence,
                wire.client_message_kv(request.kv_id, 2, wire.blob(1, body)),  # get_blob_result
            )
        else:
            blobs[request.blob_id] = request.blob_data or b""
            await self._append(
                client, tokens, request_id, sequence,
                wire.client_message_kv(request.kv_id, 3, b""),  # set_blob_result, no error
            )
        return sequence + 1

    async def _decline(
        self,
        client: httpx.AsyncClient,
        tokens: CursorTokens,
        request_id: str,
        sequence: int,
        request: wire.ExecRequest,
    ) -> int:
        """Refuse a built-in tool in the protocol's own terms, then close its channel.

        Refusing explicitly matters: the agent waits on an exec it asked for, so silence
        would stall the turn rather than move the model on to a tool it does have."""
        if request.args_field in wire.REJECTABLE_EXECS:
            _label, result_field, rejected_field, _leading = wire.REJECTABLE_EXECS[request.args_field]
            result = wire.rejected_result(rejected_field, request.identifying_strings, _BUILTIN_REFUSAL)
        elif request.args_field == 5:  # grep_args has no rejection variant, only an error
            result_field, result = 5, wire.grep_error_result(_BUILTIN_REFUSAL)
        elif request.args_field == 10:  # request_context_args — answerable, and harmless
            result_field = 10
            # RequestContextResult.success → RequestContextSuccess.request_context
            result = wire.blob(1, wire.blob(1, wire.request_context(self._environment(), [], "")))
        else:
            return sequence  # an exec kind with no reply we can shape; let it lapse
        await self._append(
            client, tokens, request_id, sequence,
            wire.client_message_exec_result(request.exec_id_number, request.exec_id, result_field, result),
        )
        await self._append(
            client, tokens, request_id, sequence + 1,
            wire.client_message_stream_close(request.exec_id_number),
        )
        return sequence + 2

    # Aggregation.

    @staticmethod
    def _aggregate_to_result(aggregate: Optional[ChatGenerationChunk]) -> ChatResult:
        if aggregate is None:
            return ChatResult(generations=[])
        chunk_message = cast(AIMessageChunk, aggregate.message)
        message = AIMessage(
            content=chunk_message.content,
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
        """``BaseChatModel`` requires a synchronous path; the harness has none.

        Unlike the other two clients this cannot be written twice, because one turn here
        is a download and a series of concurrent uploads — a shape that needs a loop. So
        it borrows one when there is none to borrow from, and says so plainly when there
        is, rather than deadlocking inside somebody else's."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs))
        raise RuntimeError(
            "ChatCursorModel has no synchronous path inside a running event loop — "
            "await ainvoke/astream instead."
        )


# Chunk construction, shared by the stream and its aggregation. These mirror the Codex
# client's helpers so both providers' streams merge into the same shapes the harness
# already knows how to render.

def _chunk(
    content_block: ContentBlock | None = None,
    tool_call_chunk: Optional[ToolCallChunk] = None,
) -> ChatGenerationChunk:
    blocks = [content_block] if content_block is not None else []
    return ChatGenerationChunk(message=AIMessageChunk(
        content=content_blocks_to_message_content(blocks),
        tool_call_chunks=[tool_call_chunk] if tool_call_chunk else [],
    ))


def _text_block(body: str, run_identifier: str) -> TextContentBlock:
    # The id and index together identify the block a delta belongs to, and every text
    # delta of a turn belongs to the same one — Cursor streams a turn's prose as a single
    # run, so the whole of it merges into one part.
    return TextContentBlock(type="text", text=body, id=f"{run_identifier}-text", index=0)


def _reasoning_block(body: str, run_identifier: str) -> ReasoningContentBlock:
    return ReasoningContentBlock(
        type="reasoning", reasoning=body, id=f"{run_identifier}-reasoning", index=1,
    )


def _tool_call_chunk(call: wire.ToolCall) -> ChatGenerationChunk:
    """A tool call, whole. Cursor delivers a call's arguments complete rather than as a
    stream of argument text, so this is one chunk and not a run of partial ones."""
    return _chunk(tool_call_chunk={
        "index": 0,
        "id": call.call_id or str(uuid.uuid4()),
        "name": call.tool_name,
        "args": compact(call.arguments),
        "type": "tool_call_chunk",
    })


def _final_chunk(finish_reason: str, input_tokens: int, output_tokens: int) -> ChatGenerationChunk:
    """The turn's last chunk: why it ended, and what it cost.

    Both figures are the server's own. Neither is estimated here, and neither is reported
    when the turn carried no accounting for it — a zero is a real zero, not a stand-in."""
    usage: Optional[UsageMetadata] = None
    if input_tokens or output_tokens:
        usage = cast(UsageMetadata, {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        })
    return ChatGenerationChunk(
        message=AIMessageChunk(content="", usage_metadata=usage),
        generation_info={"finish_reason": finish_reason},
    )


def _time_zone() -> str:
    """The machine's IANA zone name, as Cursor's own client reports it.

    Python has no portable API for this — ``time.tzname`` gives an abbreviation like
    ``CET``, not ``Europe/Rome`` — so ``TZ`` is preferred and the ``/etc/localtime``
    symlink is read next, which is where the name actually lives on Linux and macOS. The
    abbreviation is the last resort rather than the first, because a plausible-looking
    wrong value is worse than a coarse right one."""
    if configured := os.environ.get("TZ", "").strip():
        return configured
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return time.tzname[0] if time.tzname else "UTC"


def _checksum(access_token: str) -> str:
    """The ``x-cursor-checksum`` the service expects alongside the bearer token.

    It is not a signature and proves nothing — it is a client-side obfuscation of a
    half-hour-rounded timestamp plus two truncated digests of the token. It is here
    because Cursor's own client sends it and a request without it is refused; there is
    nothing to verify and nothing to keep secret."""
    slot = int(time.time() // 1800) * 1800
    stamp = (slot * 1000) // 1_000_000
    obfuscated = bytearray(stamp.to_bytes(6, "big"))
    previous = 165
    for index in range(len(obfuscated)):
        obfuscated[index] = ((obfuscated[index] ^ previous) + index) & 0xFF
        previous = obfuscated[index]
    prefix = base64.urlsafe_b64encode(bytes(obfuscated)).rstrip(b"=").decode()
    segments = access_token.split(".")
    payload_digest = hashlib.sha256(segments[1].encode()).hexdigest()[:8] if len(segments) > 1 else "00000000"
    token_digest = hashlib.sha256(access_token.encode()).hexdigest()[:8]
    return f"{prefix}{payload_digest}/{token_digest}"


# Live per-account model discovery. Which models a Cursor plan serves is an account
# fact, so this is the authoritative list; the static catalog in
# :mod:`daisy.base.models` is only the offline superset the picker greys against.
#
# ``GetUsableModels`` is reachable over Connect's JSON encoding, so this one call needs
# no protobuf at all — a plain JSON POST answers with the model list.
_models_cache: Optional[tuple[float, dict[str, dict[str, Any]]]] = None
_models_cache_lock = asyncio.Lock()

# The floor for a model the catalog named without stating a window, which is the only case
# left once both endpoints have been asked. It is the smallest window any model on the
# service currently has, so it under-promises: a context gauge that reads too full compacts
# early, while one that reads too empty overruns the model.
UNKNOWN_CONTEXT_WINDOW = 200_000

# What the server itself said a model's window was, learned from a checkpoint during a turn
# and keyed by model id. This is the most authoritative source there is — it is per-account
# and per-model, measured rather than published — so it outranks the catalog once it exists.
_observed_context_windows: dict[str, int] = {}


def _record_context_window(model_id: str, maximum_tokens: int) -> None:
    """Remember a window the server reported. Keeps the largest seen for a model: a turn that
    has not yet grown past a smaller budget can be told a smaller one."""
    if maximum_tokens > _observed_context_windows.get(model_id, 0):
        _observed_context_windows[model_id] = maximum_tokens


# The model ids Cursor serves are not plain model names: the reasoning effort is part of
# the id (``claude-4.6-opus-high``, ``gpt-5.4-medium``). That is also why this provider
# ignores Daisy's reasoning_effort setting — picking the model already said it.
_EFFORT_SUFFIX = re.compile(r"-(high|medium|low|max|xhigh)$")


def _display_name(entry: dict[str, Any], model_id: str) -> str:
    """A model's label, with its effort named when the id carries one.

    Cursor gives the same display name to every effort variant of a model, so a picker
    that trusted it would list "Claude 4.6 Opus" three times with no way to tell which is
    which. The effort is in the id, so it goes in the label."""
    name = model_id
    for key in ("displayName", "displayNameShort", "displayModelId"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    effort = _EFFORT_SUFFIX.search(model_id)
    if effort and effort.group(1) not in name.lower():
        return f"{name} ({effort.group(1)})"
    return name


def _token_limit(value: Any) -> int:
    """A Cursor parameter value as a token count. It states windows the way its interface
    displays them — ``200k``, ``1m``, ``272000`` — so all three have to read."""
    text_value = str(value or "").strip().lower().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([km])?", text_value)
    if match is None:
        return 0
    scale = {"k": 1_000, "m": 1_000_000}.get(match.group(2) or "", 1)
    return round(float(match.group(1)) * scale)


async def _connect_json(url: str, body: dict, tokens: CursorTokens) -> dict:
    """One unary RPC over Connect's JSON encoding, which these two model endpoints accept —
    so discovering models needs no protobuf at all, unlike running a turn."""
    headers = {
        **ChatCursorModel._headers(tokens, str(uuid.uuid4())),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "connect-protocol-version": "1",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
    return payload if isinstance(payload, dict) else {}


async def _fetch_context_windows(tokens: CursorTokens) -> dict[str, int]:
    """Context windows per model, from ``AvailableModels``.

    This is the only endpoint that states them. It answers with base models rather than the
    effort-suffixed ids a run request takes, each carrying variants whose parameters include
    the window, so the result is keyed two ways: by a variant's own ``legacySlug``, which
    matches a run id exactly when it is present, and by the base name, which matches by
    prefix. Where a base name's variants disagree — a model offered at both its normal window
    and an extended one — the smallest is kept, because a window that reads too large
    overruns the model while one that reads too small only compacts early.

    Nothing is requested by name. ``additionalModelNames`` exists to make the service mention
    models it would otherwise omit, and reaching for it would mean hardcoding model names to
    discover models — which is the thing discovery is for."""
    payload = await _connect_json(
        AVAILABLE_MODELS_URL,
        {"isNightly": False, "excludeMaxNamedModels": True, "additionalModelNames": [],
         "useModelParameters": True, "useReactModelPicker": True},
        tokens,
    )
    windows: dict[str, int] = {}

    def remember(key: str, window: int) -> None:
        if not key or window <= 0:
            return
        existing = windows.get(key)
        windows[key] = window if existing is None else min(existing, window)

    for entry in payload.get("models") or []:
        if not isinstance(entry, dict) or not (base_name := entry.get("name")):
            continue
        for variant in entry.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            values = {
                parameter.get("id"): parameter.get("value")
                for parameter in variant.get("parameterValues") or []
                if isinstance(parameter, dict)
            }
            window = _token_limit(values.get("context"))
            remember(str(variant.get("legacySlug") or ""), window)
            remember(str(base_name), window)
    return windows


def _window_for(model_id: str, windows: dict[str, int]) -> int:
    """The window for a run id: an exact match on a variant slug, else the longest base name
    the id starts with — ``claude-4.6-opus-high`` resolving through ``claude-4.6-opus``."""
    if exact := windows.get(model_id):
        return exact
    candidates = [name for name in windows if model_id.startswith(name)]
    return windows[max(candidates, key=len)] if candidates else 0


async def fetch_subscription_models() -> dict[str, dict[str, Any]]:
    """The account's live Cursor model list as ``{model_id: {"name", "context"}}``.

    Two endpoints, because each knows half of it. ``GetUsableModels`` answers with the ids a
    run request accepts verbatim — it returns the very message type the request echoes back —
    so it owns the list. ``AvailableModels`` is the only one that states context windows, so
    it owns those, and a failure there costs windows rather than the whole catalog.

    Returns an empty dict when signed out or on any failure, which is what greys this
    provider's models in the picker. Cached briefly because the interface polls ``/models``
    and this must not be a round-trip each time."""
    global _models_cache
    ttl = active_tuning().duration(Tunable.model_catalogue_ttl_seconds)
    if _models_cache is not None and time.monotonic() - _models_cache[0] < ttl:
        return _models_cache[1]
    async with _models_cache_lock:
        if _models_cache is not None and time.monotonic() - _models_cache[0] < ttl:
            return _models_cache[1]
        result: dict[str, dict[str, Any]] = {}
        try:
            tokens = await valid_tokens()
            listing = await _connect_json(USABLE_MODELS_URL, {}, tokens)
            try:
                windows = await _fetch_context_windows(tokens)
            except (httpx.HTTPError, ValueError, TypeError):
                windows = {}  # a listing without windows still beats no listing
            for entry in listing.get("models") or []:
                if not isinstance(entry, dict):
                    continue
                model_id = entry.get("modelId") or entry.get("displayModelId")
                if not model_id:
                    continue
                result[model_id] = {
                    "name": _display_name(entry, model_id),
                    "context": _window_for(model_id, windows),
                }
        except (CursorAuthError, httpx.HTTPError, ValueError, KeyError, TypeError):
            result = {}
        _models_cache = (time.monotonic(), result)
        return result


def cached_subscription_models() -> dict[str, dict[str, Any]]:
    """The last live list fetched, without a network round-trip (``{}`` if never
    fetched). For sync callers that only want the freshest *known* value."""
    return _models_cache[1] if _models_cache is not None else {}


def clear_subscription_models_cache() -> None:
    """Drop the cached list so the next ``/models`` reflects a fresh sign-in or sign-out
    immediately rather than waiting out the TTL."""
    global _models_cache
    _models_cache = None
