"""A LangChain chat model backed by a Cursor subscription, speaking its agent protocol rather than a chat API."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
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

from langmesh.base.cursor_credentials import (
    CursorAuthError,
    CursorTokens,
    valid_tokens,
)
from langmesh.base.configuration import PromptLoader
from langmesh.base.cursor_subscription import (
    APPEND_PATH,
    RUN_HOSTS,
    RUN_PATH,
    STATUS_RESOURCE_EXHAUSTED,
    STATUS_UNAUTHENTICATED,
    UNKNOWN_CONTEXT_WINDOW,
    cached_subscription_models,
    machine_time_zone,
    observed_context_window,
    record_context_window,
    request_headers,
)
from langmesh.base.message_content import content_blocks_to_message_content, message_text
from langmesh.base.serialization import compact, upstream_detail
from langmesh.base.tuning import Tunable, active_tuning
from langmesh.runtime.models import cursor_wire as wire


# Everything this client says to a model is a prompt on disk, like every other prompt the harness sends.
_PROMPTS = PromptLoader(Path(__file__).resolve().parent.parent / "prompts")

# A translated built-in has no reason of its own, so it says what it truthfully is for the permission surface.
_BUILTIN_JUSTIFICATION = "Requested by the Cursor agent while working on this turn."


def _shell_arguments(values: list[str]) -> Optional[dict[str, Any]]:
    """`ShellArgs` becomes `bash`, taking the directory as a location only when the agent named one."""
    command, directory = (values + ["", ""])[:2]
    if not command:
        return None
    return {"command": command, **({"location": directory} if directory else {})}


def _read_arguments(values: list[str]) -> Optional[dict[str, Any]]:
    """``ReadArgs{path}`` becomes ``read_file``."""
    return {"file_path": values[0]} if values and values[0] else None


def _write_arguments(values: list[str]) -> Optional[dict[str, Any]]:
    """`WriteArgs` becomes `write_file`, and only a missing path disqualifies it since an empty body is a real write."""
    path, content = (values + ["", ""])[:2]
    return {"file_path": path, "content": content} if path else None


def _list_arguments(values: list[str]) -> Optional[dict[str, Any]]:
    """`LsArgs` becomes a fixed read-only `bash` listing, taking only the directory from the agent."""
    path = values[0] if values else ""
    if not path:
        return None
    # A synthesised call states its claim like any other, and an empty request with `mutates: false` is what `ls` is.
    return {
        "command": f"ls -la {shlex.quote(path)}",
        "access_request": {"mutates": False},
    }


def _background_shell_arguments(values: list[str]) -> Optional[dict[str, Any]]:
    """`BackgroundShellSpawnArgs` becomes `bash` in the background, which is the tool the harness already has."""
    arguments = _shell_arguments(values)
    return {**arguments, "background": True} if arguments else None


def _fetch_arguments(values: list[str]) -> Optional[dict[str, Any]]:
    """``FetchArgs{url}`` becomes ``fetch_url``."""
    return {"url": values[0]} if values and values[0] else None


def _list_resources_arguments(values: list[str]) -> Optional[dict[str, Any]]:
    """`ListMcpResourcesExecArgs` becomes `list_mcp_resources`, whose parameter is spelled the same."""
    return {"server": values[0]} if values and values[0] else None


def _read_resource_arguments(values: list[str]) -> Optional[dict[str, Any]]:
    """``ReadMcpResourceExecArgs{server, uri}`` becomes ``read_mcp_resource``, again field for field."""
    server, uri = (values + ["", ""])[:2]
    return {"server": server, "uri": uri} if server and uri else None


# Cursor's built-in tools and the harness tool each becomes; anything not here is refused by name rather than left unanswered.
_BUILTIN_TRANSLATIONS: dict[str, tuple[str, Callable[[list[str]], Optional[dict[str, Any]]]]] = {
    "shell": ("bash", _shell_arguments),
    "background_shell": ("bash", _background_shell_arguments),
    "read": ("read_file", _read_arguments),
    "write": ("write_file", _write_arguments),
    "ls": ("bash", _list_arguments),
    "fetch": ("fetch_url", _fetch_arguments),
    "list_mcp_resources": ("list_mcp_resources", _list_resources_arguments),
    "read_mcp_resource": ("read_mcp_resource", _read_resource_arguments),
}


class _HostUnavailable(RuntimeError):
    """This backend did not serve the request and another one might, which is the only failure worth retrying elsewhere."""


class _Channel:
    """The upload half of a run, owning the sequence number every `BidiAppend` is ordered by."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        tokens: CursorTokens,
        request_id: str,
        append_url: str,
    ) -> None:
        self._client = client
        self._tokens = tokens
        self._request_id = request_id
        self._append_url = append_url
        self._sequence = 0

    async def push(self, payload: bytes) -> None:
        """Send one ``AgentClientMessage`` up."""
        response = await self._client.post(
            self._append_url,
            content=wire.frame(wire.bidi_append_request(self._request_id, self._sequence, payload)),
            headers=request_headers(self._tokens, self._request_id),
        )
        self._sequence += 1
        if response.status_code >= 400:
            raise ChatCursorModel._auth_error(response.status_code, response.text)


class ChatCursorModel(BaseChatModel):
    """A `BaseChatModel` backed by a Cursor subscription, reading its token from the shared store on every call."""

    model: str
    workspace: str = ""
    context_length: int = 0
    # A generous bound so a dead connection cannot hang a turn forever, matching the other two clients.
    timeout: Optional[float] = 300.0

    @property
    def _llm_type(self) -> str:
        return "cursor"

    def context_window(self) -> int:
        """The model's context window, preferring an observed turn over the catalog over the configured default."""
        if observed := observed_context_window(self.model):
            return observed
        discovered = cached_subscription_models().get(self.model) or {}
        return int(discovered.get("context") or 0) or self.context_length or UNKNOWN_CONTEXT_WINDOW

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model}

    # The same tool-binding surface as the other two clients; the Cursor-shaped translation happens at request-build time.

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: Optional[str] = None,
        parallel_tool_calls: Optional[bool] = None,
        **kwargs: Any,
    ) -> Runnable:
        # `tool_choice` and `parallel_tool_calls` have no counterpart in Cursor's protocol, so they are dropped rather than faked.
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    # Turning the harness's messages into one Cursor turn.

    @staticmethod
    def _system_prompt(messages: Sequence[BaseMessage]) -> str:
        return "\n\n".join(
            text for text in (message_text(message) for message in messages
                              if isinstance(message, SystemMessage)) if text
        )

    def _render(self, messages: Sequence[BaseMessage]) -> str:
        """The conversation as the single user message Cursor's turn accepts, labelled once there is history to tell apart."""
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
        """What to say about the transcript that follows, including how to read tool results the protocol cannot hand back structurally."""
        note = _PROMPTS.load("cursor_tool_results_note", {}) if carries_tool_results else ""
        return _PROMPTS.load("cursor_transcript_preamble", {"tool_results_note": note}).strip()

    def _environment(self) -> bytes:
        """The `RequestContextEnv` a turn describes itself with: where the client believes it is running, and on what."""
        workspace = self.workspace or os.getcwd()
        return wire.request_context_env(
            workspace=workspace,
            shell=os.environ.get("SHELL", "/bin/sh"),
            os_version=f"{platform.system().lower()} {platform.release()}",
            time_zone=machine_time_zone(),
        )

    @staticmethod
    def _conversation_key(messages: Sequence[BaseMessage]) -> str:
        """Which conversation this is, keyed on the first non-system exchange so the key is stable for its whole life."""
        conversation = [m for m in messages if not isinstance(m, SystemMessage)]
        return _digest_messages(conversation[:1])

    def _resumption_for(self, messages: Sequence[BaseMessage]) -> Optional[_Resumption]:
        """A checkpoint worth resuming from, or `None` when the transcript no longer begins with what it was made from."""
        _prune_resumptions()
        entry = _resumptions.get(self._conversation_key(messages))
        if entry is None or entry.prefix_length >= len(messages):
            return None
        if _digest_messages(messages[:entry.prefix_length]) != entry.prefix_digest:
            return None  # history was rewritten under us; the checkpoint describes something else
        return entry

    def _remember_resumption(
        self,
        messages: Sequence[BaseMessage],
        checkpoint: bytes,
        blobs: dict[bytes, bytes],
        conversation_id: str,
    ) -> None:
        if not checkpoint:
            return
        _resumptions[self._conversation_key(messages)] = _Resumption(
            prefix_length=len(messages),
            prefix_digest=_digest_messages(messages),
            checkpoint=checkpoint,
            blobs=dict(blobs),
            conversation_id=conversation_id,
            touched_at=time.monotonic(),
        )

    def _build_turn(
        self, messages: Sequence[BaseMessage], tools: list[dict[str, Any]]
    ) -> tuple[bytes, dict[bytes, bytes], str]:
        """One serialized turn, its blobs and its conversation id, rendering only what Cursor has not already seen."""
        resumption = self._resumption_for(messages)
        blobs: dict[bytes, bytes] = dict(resumption.blobs) if resumption else {}
        root_prompt_ids: list[bytes] = []
        if system_prompt := self._system_prompt(messages):
            payload = json.dumps({"role": "system", "content": system_prompt}).encode("utf-8")
            blob_id = hashlib.sha256(payload).digest()
            blobs[blob_id] = payload
            root_prompt_ids.append(blob_id)

        if resumption is not None:
            state = wire.resumed_conversation_state(resumption.checkpoint)
            body = self._render(messages[resumption.prefix_length:])
            conversation_id = resumption.conversation_id
        else:
            state = wire.conversation_state(root_prompt_ids)
            body = self._render(messages)
            conversation_id = str(uuid.uuid4())

        message_body = wire.user_message(body, str(uuid.uuid4()))
        # Cursor keys a user message's blob by its own serialized bytes, so a request for it is answerable from the same store.
        blobs[message_body] = message_body

        workspace = self.workspace or os.getcwd()
        tool_definitions = [
            wire.mcp_tool_definition(
                name=(function := tool.get("function", tool)).get("name", ""),
                description=function.get("description", "") or "",
                schema=function.get("parameters") or {"type": "object", "properties": {}},
            )
            for tool in tools
        ]
        tool_instructions = _PROMPTS.load("cursor_tool_instructions", {})
        action = wire.blob(
            1,  # ConversationAction.user_message_action
            wire.blob(1, message_body)
            + wire.blob(2, wire.request_context(self._environment(), tool_definitions, tool_instructions)),
        )
        run_request = wire.agent_run_request(
            state=state,
            action=action,
            model=wire.model_details(self.model),
            variant=self._variant(),
            tools=tool_definitions,
            conversation_id=conversation_id,
            file_system_options=(
                wire.mcp_file_system_options(workspace, tool_instructions) if tool_definitions else b""
            ),
        )
        return wire.client_message_run(run_request), blobs, conversation_id

    def _variant(self) -> bytes:
        """The `RequestedModel` for this model when discovery learned its variant, and nothing when it did not."""
        variant = cached_subscription_models().get(self.model, {}).get("variant")
        if not variant:
            return b""
        return wire.requested_model(
            model_id=variant["server_model"],
            maximum_mode=bool(variant["maximum_mode"]),
            parameters=[(key, value) for key, value in variant["parameters"]],
        )

    # Transport.

    @staticmethod
    def _auth_error(status: int, detail: str) -> Exception:
        """The exception an HTTP failure becomes, chosen so the caller can tell whether another backend could help."""
        if status in (401, 403):
            # Definitive: the token is the problem and every host will say the same.
            return CursorAuthError(
                "Cursor rejected the subscription token (expired, revoked, or the plan "
                f"lacks access). Sign in again. Detail: {upstream_detail(detail)}"
            )
        return _HostUnavailable(f"Cursor agent service returned {status}: {upstream_detail(detail)}")

    @staticmethod
    def _stream_error(status: int, message: str) -> Exception:
        if status == STATUS_RESOURCE_EXHAUSTED:
            return RuntimeError("Cursor reports this subscription's usage limit is reached.")
        if status == STATUS_UNAUTHENTICATED:
            return CursorAuthError("Cursor rejected the subscription token. Sign in again.")
        return RuntimeError(f"Cursor agent stream failed (grpc-status {status}): {message or 'no detail'}")

    # Streaming generation (the path the harness actually uses).

    async def _astream(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        tokens = await valid_tokens()
        turn, blobs, conversation_id = self._build_turn(messages, kwargs.get("tools") or [])
        tool_names = {
            (tool.get("function", tool)).get("name", "") for tool in (kwargs.get("tools") or [])
        }
        # Cursor moves this service between backends, so a run that failed before producing anything is retried against the others.
        errors: list[Exception] = []
        for host in RUN_HOSTS:
            try:
                produced = False
                async for chunk in self._run_once(
                    host, tokens, turn, blobs, conversation_id, messages, tool_names,
                ):
                    produced = True
                    yield chunk
                return
            except (httpx.HTTPError, _HostUnavailable) as error:
                # A run that already emitted something is never retried; anything else is definitive and propagates from the first host.
                if produced:
                    raise
                errors.append(error)
        raise errors[0]

    async def _run_once(
        self,
        host: str,
        tokens: CursorTokens,
        turn: bytes,
        blobs: dict[bytes, bytes],
        conversation_id: str,
        messages: Sequence[BaseMessage],
        tool_names: set[str],
    ) -> AsyncIterator[ChatGenerationChunk]:
        request_id = str(uuid.uuid4())
        headers = request_headers(tokens, request_id)
        run_url = f"{host}{RUN_PATH}"
        append_url = f"{host}{APPEND_PATH}"

        async with httpx.AsyncClient(timeout=self.timeout, http2=False) as client:
            request = client.build_request(
                "POST", run_url, content=wire.frame(wire.bidi_request_id(request_id)), headers=headers,
            )
            # The open is started, the turn pushed, and only then are headers awaited, since the server has nothing to send first.
            opening = asyncio.create_task(client.send(request, stream=True))
            channel = _Channel(client, tokens, request_id, append_url)
            try:
                await channel.push(turn)
            except BaseException:
                # The turn never made it up, so close the run rather than leaving an open stream behind the raised error.
                opening.cancel()
                with contextlib.suppress(BaseException):
                    await (await opening).aclose()
                raise
            response = await opening
            try:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")
                    raise self._auth_error(response.status_code, detail)
                async for chunk in self._read(
                    channel, response, blobs, conversation_id, messages, tool_names,
                ):
                    yield chunk
            finally:
                await response.aclose()

    async def _read(
        self,
        channel: _Channel,
        response: httpx.Response,
        blobs: dict[bytes, bytes],
        conversation_id: str,
        messages: Sequence[BaseMessage],
        tool_names: set[str],
    ) -> AsyncIterator[ChatGenerationChunk]:
        """Read the run to its end, answering what the server asks and stopping at the first tool call or the turn's end."""
        deframer = wire.Deframer()
        # Generated tokens arrive as deltas and are summed; the prompt size is reported whole, so the largest seen is kept.
        output_tokens = 0
        input_tokens = 0
        # A call is announced and then asked for, and it is the exec request that is handed back because it carries the arguments.
        announced: Optional[wire.ToolCall] = None
        run_identifier = str(uuid.uuid4())
        # Cursor holds a run open with heartbeats, so what is bounded is silence since the last thing that happened.
        silence_limit = active_tuning().duration(Tunable.model_silence_give_up_seconds)
        progressed_at = time.monotonic()
        went_quiet = False
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
                    record_context_window(self.model, details.maximum_tokens)
                if message.checkpoint:
                    # Kept rather than resumed from: a mid-turn checkpoint is the newest description, and the next turn uses it.
                    self._remember_resumption(messages, message.checkpoint, blobs, conversation_id)
                announced = message.tool_call or announced
                if message.heartbeat and time.monotonic() - progressed_at > silence_limit:
                    went_quiet = True
                    break
                if not message.heartbeat:
                    progressed_at = time.monotonic()
                if message.text_delta:
                    yield _chunk(content_block=_text_block(message.text_delta, run_identifier))
                if message.thinking_delta:
                    yield _chunk(content_block=_reasoning_block(message.thinking_delta, run_identifier))
                if message.blob_request is not None:
                    await self._answer_blob(channel, message.blob_request, blobs)
                if (request := message.exec_request) is not None:
                    call = self._tool_call_for(request, tool_names)
                    if call is not None:
                        # A tool call, the harness's or Cursor's own, ends this run: hand it back and stop reading.
                        yield _tool_call_chunk(call)
                        yield _final_chunk("tool_calls", input_tokens, output_tokens)
                        return
                    await self._decline(channel, request)
                if message.turn_ended:
                    async for chunk in self._close_turn(announced, input_tokens, output_tokens):
                        yield chunk
                    return
            if went_quiet:
                break
        # Reached when the stream closes without a turn ending or the model went quiet, so report what arrived rather than raising.
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

    @staticmethod
    def _tool_call_for(request: wire.ExecRequest, tool_names: set[str]) -> Optional[wire.ToolCall]:
        """The harness tool call this exec request becomes, translating one of Cursor's built-ins where there is an equivalent."""
        if request.tool_call is not None:
            return request.tool_call
        if request.builtin is None:
            return None
        translation = _BUILTIN_TRANSLATIONS.get(request.builtin.label)
        if translation is None:
            return None
        name, build = translation
        if name not in tool_names:
            return None  # this agent does not have the tool; refuse rather than invent one
        arguments = build(request.arguments)
        if arguments is None:
            return None
        return wire.ToolCall(call_id=f"cursor-{request.exec_id_number}", tool_name=name,
                             arguments={**arguments, "explanation": _BUILTIN_JUSTIFICATION})

    @staticmethod
    async def _answer_blob(
        channel: _Channel, request: wire.BlobRequest, blobs: dict[bytes, bytes]
    ) -> None:
        """Serve the conversation's blob store, answering an unsatisfiable read empty rather than stalling the run."""
        if request.is_read:
            body = blobs.get(request.blob_id, b"")
            await channel.push(wire.client_message_kv(request.kv_id, 2, wire.blob(1, body)))
        else:
            blobs[request.blob_id] = request.blob_data or b""
            await channel.push(wire.client_message_kv(request.kv_id, 3, b""))  # no error

    async def _decline(self, channel: _Channel, request: wire.ExecRequest) -> None:
        """Refuse an untranslatable exec in the protocol's own terms and close its channel, since silence would stall the turn."""
        builtin = request.builtin
        if builtin is not None:
            result_field = builtin.result_field
            result = wire.refused_result(
                builtin, request.arguments, _PROMPTS.load("cursor_builtin_denied", {})
            )
        elif request.args_field == 10:  # request_context_args — answerable, and harmless
            result_field = 10
            # RequestContextResult.success carries RequestContextSuccess.request_context
            result = wire.blob(1, wire.blob(1, wire.request_context(self._environment(), [], "")))
        else:
            return  # an exec kind with no reply this can shape; let it lapse
        await channel.push(wire.client_message_exec_result(
            request.exec_id_number, request.exec_id, result_field, result,
        ))
        await channel.push(wire.client_message_stream_close(request.exec_id_number))

    # Aggregation.

    @staticmethod
    def _aggregate_to_result(aggregate: Optional[ChatGenerationChunk]) -> ChatResult:
        if aggregate is None:
            return ChatResult(generations=[])
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
        """`BaseChatModel` requires a synchronous path, so this borrows a loop when there is none to borrow from."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs))
        raise RuntimeError(
            "ChatCursorModel has no synchronous path inside a running event loop — "
            "await ainvoke/astream instead."
        )


# Chunk construction shared by the stream and its aggregation, mirroring the Codex client's helpers.

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
    # Cursor streams a turn's prose as a single run, so every text delta merges into one block.
    return TextContentBlock(type="text", text=body, id=f"{run_identifier}-text", index=0)


def _reasoning_block(body: str, run_identifier: str) -> ReasoningContentBlock:
    return ReasoningContentBlock(
        type="reasoning", reasoning=body, id=f"{run_identifier}-reasoning", index=1,
    )


def _tool_call_chunk(call: wire.ToolCall) -> ChatGenerationChunk:
    """A tool call, whole, because Cursor delivers arguments complete rather than as a stream of text."""
    return _chunk(tool_call_chunk={
        "index": 0,
        "id": call.call_id or str(uuid.uuid4()),
        "name": call.tool_name,
        "args": compact(call.arguments),
        "type": "tool_call_chunk",
    })


def _final_chunk(finish_reason: str, input_tokens: int, output_tokens: int) -> ChatGenerationChunk:
    """The turn's last chunk: why it ended and what it cost, both from the server and neither estimated."""
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


@dataclass
class _Resumption:
    """A conversation Cursor already knows, fingerprinted by the exact messages its checkpoint was produced from."""

    prefix_length: int
    prefix_digest: str
    checkpoint: bytes
    blobs: dict[bytes, bytes]
    conversation_id: str
    touched_at: float


# Keyed by conversation identity and evicted by age, since losing this cache costs only a full replay.
_resumptions: dict[str, _Resumption] = {}


def _digest_messages(messages: Sequence[BaseMessage]) -> str:
    """A fingerprint of a message list by role, text and tool-call arguments, leaving out per-call ids."""
    hasher = hashlib.sha256()
    for message in messages:
        hasher.update(message.__class__.__name__.encode())
        hasher.update(b"\x00")
        hasher.update(message_text(message).encode())
        for call in getattr(message, "tool_calls", None) or []:
            arguments = call.get("args")
            hasher.update(str(call.get("name")).encode())
            hasher.update((arguments if isinstance(arguments, str) else compact(arguments)).encode())
        hasher.update(b"\x1e")
    return hasher.hexdigest()


def _prune_resumptions() -> None:
    horizon = time.monotonic() - active_tuning().duration(Tunable.subscription_resume_ttl_seconds)
    for key in [key for key, entry in _resumptions.items() if entry.touched_at < horizon]:
        del _resumptions[key]


def clear_resumptions() -> None:
    """Forget every resumable conversation, since the state belongs to the account that produced it."""
    _resumptions.clear()
