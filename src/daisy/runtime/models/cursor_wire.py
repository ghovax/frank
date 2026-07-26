"""The Cursor agent protocol, on the wire.

Cursor's subscription has no HTTP+JSON chat endpoint. What it has is a Connect-RPC
service, ``agent.v1.AgentService``, speaking protocol buffers, and everything a client
wants — send a turn, receive text, receive a tool call, answer a tool call — is a field
in one of two envelope messages, ``AgentClientMessage`` and ``AgentServerMessage``.

This module is that protocol and nothing else: the wire codec, the framing, and the
handful of messages :mod:`daisy.runtime.models.cursor` needs to build and read. The
chat model above it never sees a tag or a varint.

**Why hand-rolled and not generated.** Cursor's ``agent.proto`` is a five-hundred-message
file describing an entire coding agent — every built-in tool, its arguments, its
results, its rejections. Generating it would put fifteen thousand lines of machine
output in the tree to use perhaps thirty messages of it, and would add a protobuf
runtime to a dependency list that has no other use for one. The wire format is simple
enough that a couple of hundred lines covers the parts in play, and writing them out
makes the field numbers visible where the code uses them, which is exactly where you
want them when the protocol shifts underneath you. Every number below was read off the
current descriptor rather than inferred from traffic; the field comments name the
message so a future reader can re-check them against a newer one.

**Framing.** Connect and gRPC-web both prefix each message with five bytes — one flag
byte, then a big-endian length — and a stream is a sequence of those. The trailer frame
sets bit 7 of the flag byte and carries HTTP-style ``grpc-status``/``grpc-message``
lines instead of a message, which is how a call reports failure after the response
headers have already said 200.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterator, Optional
from urllib.parse import unquote

# Wire types. Only these four occur in the messages we touch.
_VARINT = 0
_FIXED64 = 1
_LENGTH_DELIMITED = 2
_FIXED32 = 5

# The flag bit that marks a frame as end-of-stream trailers rather than a message.
TRAILER_FLAG = 0x80


# Encoding.

def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _tag(number: int, wire_type: int) -> bytes:
    return _varint((number << 3) | wire_type)


def scalar(number: int, value: int) -> bytes:
    """A varint field (int32/uint32/int64/enum). Proto3 omits zero, and so do we."""
    return b"" if value == 0 else _tag(number, _VARINT) + _varint(value)


def boolean(number: int, value: bool) -> bytes:
    return b"" if not value else _tag(number, _VARINT) + b"\x01"


def double(number: int, value: float) -> bytes:
    return _tag(number, _FIXED64) + struct.pack("<d", value)


def blob(number: int, value: bytes) -> bytes:
    """A length-delimited field: ``bytes``, or a nested message already serialized."""
    return _tag(number, _LENGTH_DELIMITED) + _varint(len(value)) + value


def text(number: int, value: str) -> bytes:
    """A string field. Empty strings are omitted, as proto3 does."""
    return b"" if not value else blob(number, value.encode("utf-8"))


# Decoding.

@dataclass(frozen=True)
class Field:
    """One field as it appeared on the wire. ``data`` is set for length-delimited and
    fixed-width fields, ``number_value`` for varints."""

    number: int
    wire_type: int
    data: bytes = b""
    number_value: int = 0

    @property
    def is_message(self) -> bool:
        return self.wire_type == _LENGTH_DELIMITED

    def as_text(self) -> str:
        return self.data.decode("utf-8", "replace")


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    return value, offset


def parse(data: bytes) -> list[Field]:
    """Every field of a serialized message, in wire order.

    Unknown fields come back like any other rather than being an error: this parses
    messages from a service that adds fields continuously, and a reader that only looks
    at the numbers it knows survives that. A wire type we cannot skip safely ends the
    parse, because guessing a length would desynchronise everything after it."""
    fields: list[Field] = []
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 0x7
        if wire_type == _LENGTH_DELIMITED:
            length, offset = _read_varint(data, offset)
            fields.append(Field(number, wire_type, data=data[offset:offset + length]))
            offset += length
        elif wire_type == _VARINT:
            value, offset = _read_varint(data, offset)
            fields.append(Field(number, wire_type, number_value=value))
        elif wire_type in (_FIXED64, _FIXED32):
            width = 8 if wire_type == _FIXED64 else 4
            fields.append(Field(number, wire_type, data=data[offset:offset + width]))
            offset += width
        else:
            break  # a wire type we cannot skip; anything past here is unreadable
    return fields


def first(fields: list[Field], number: int) -> Optional[Field]:
    return next((entry for entry in fields if entry.number == number), None)


def string_at(fields: list[Field], number: int) -> str:
    entry = first(fields, number)
    return entry.as_text() if entry is not None and entry.is_message else ""


def message_at(fields: list[Field], number: int) -> Optional[list[Field]]:
    entry = first(fields, number)
    return parse(entry.data) if entry is not None and entry.is_message else None


# google.protobuf.Value. Cursor carries JSON-shaped data — a tool's JSON Schema, a tool
# call's arguments — as a serialized Value, so the two conversions below are how a
# Python object crosses into the protocol and back.
#
# google.protobuf.Value: null_value=1, number_value=2, string_value=3, bool_value=4,
# struct_value=5, list_value=6. Struct.fields=1 (map entry: key=1, value=2).
# ListValue.values=1.

def encode_value(value: Any) -> bytes:
    if value is None:
        return _tag(1, _VARINT) + b"\x00"  # NullValue is enum 0, so it must be explicit
    if isinstance(value, bool):
        return boolean(4, value) or (_tag(4, _VARINT) + b"\x00")
    if isinstance(value, (int, float)):
        return double(2, float(value))
    if isinstance(value, str):
        return text(3, value) or blob(3, b"")
    if isinstance(value, (list, tuple)):
        return blob(6, b"".join(blob(1, encode_value(item)) for item in value))
    if isinstance(value, dict):
        entries = b"".join(
            blob(1, text(1, str(key)) + blob(2, encode_value(item)))
            for key, item in value.items()
        )
        return blob(5, entries)
    return text(3, str(value))


def decode_value(data: bytes) -> Any:
    for entry in parse(data):
        if entry.number == 1:
            return None
        if entry.number == 2 and entry.wire_type == _FIXED64:
            # Value has one numeric case, a double, so an integer argument arrives as
            # 5.0. Handing that back as a float would pass an integer-typed tool
            # parameter a non-integer, so a whole number becomes one again.
            number = struct.unpack("<d", entry.data)[0]
            return int(number) if number.is_integer() else number
        if entry.number == 3 and entry.is_message:
            return entry.as_text()
        if entry.number == 4:
            return bool(entry.number_value)
        if entry.number == 5 and entry.is_message:
            return _decode_struct(entry.data)
        if entry.number == 6 and entry.is_message:
            return [decode_value(item.data) for item in parse(entry.data) if item.number == 1]
    return None


def _decode_struct(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for entry in parse(data):
        if entry.number != 1 or not entry.is_message:
            continue
        pair = parse(entry.data)
        key = string_at(pair, 1)
        if key:
            nested = first(pair, 2)
            result[key] = decode_value(nested.data) if nested is not None else None
    return result


# Connect / gRPC-web framing.

def frame(payload: bytes, flags: int = 0) -> bytes:
    return bytes([flags]) + struct.pack(">I", len(payload)) + payload


@dataclass
class Deframer:
    """Turns a byte stream into whole frames, holding a partial tail between feeds.

    A streaming response arrives in chunks that have nothing to do with message
    boundaries, so the buffer here is the whole point: :meth:`feed` yields only frames
    that have fully arrived and keeps the remainder for the next chunk."""

    _buffer: bytearray = dataclass_field(default_factory=bytearray)

    def feed(self, chunk: bytes) -> Iterator[tuple[int, bytes]]:
        self._buffer.extend(chunk)
        while len(self._buffer) >= 5:
            length = struct.unpack(">I", self._buffer[1:5])[0]
            if len(self._buffer) < 5 + length:
                return
            flags = self._buffer[0]
            payload = bytes(self._buffer[5:5 + length])
            del self._buffer[:5 + length]
            yield flags, payload


def parse_trailer(payload: bytes) -> tuple[int, str]:
    """The ``(grpc-status, grpc-message)`` a trailer frame reports. Status 0 is success;
    anything else is the real outcome of a call whose headers already said 200."""
    status = 0
    message = ""
    for line in payload.decode("utf-8", "replace").splitlines():
        name, separator, value = line.partition(":")
        if not separator:
            continue
        key, value = name.strip().lower(), value.strip()
        if key == "grpc-status":
            try:
                status = int(value)
            except ValueError:
                status = -1
        elif key == "grpc-message":
            # gRPC percent-encodes anything outside its allowed byte range.
            message = unquote(value)
    return status, message


# The Cursor messages. Each builder names the message it serializes and each field
# comment names the message the number belongs to, because the numbers are the contract
# and nothing else in the tree records them.

# agent.v1.UserMessage.mode — the agent mode enum. AGENT is the tool-using one.
AGENT_MODE = 1
# The MCP server identity Daisy presents its own tools under. Cursor groups tools by
# provider, and this is the group name the model sees them in.
TOOL_PROVIDER = "daisy"


def bidi_request_id(request_id: str) -> bytes:
    """agent.v1.BidiRequestId — the input to ``RunSSE``, naming the stream to open."""
    return text(1, request_id)


def bidi_append_request(request_id: str, sequence: int, payload: bytes) -> bytes:
    """aiserver.v1.BidiAppendRequest — one client message pushed into an open stream.

    The payload is hex, not bytes: this request carries a serialized
    ``AgentClientMessage`` in a *string* field, so it has to survive as text."""
    return (
        text(1, payload.hex())
        + blob(2, bidi_request_id(request_id))
        + (_tag(3, _VARINT) + _varint(sequence))  # append_seqno; sent even when 0
    )


def mcp_tool_definition(name: str, description: str, schema: dict) -> bytes:
    """agent.v1.McpToolDefinition — one of Daisy's tools, as Cursor sees it."""
    return (
        text(1, name)
        + text(2, description)
        + blob(3, encode_value(schema))  # input_schema is bytes holding a Value
        + text(4, TOOL_PROVIDER)
        + text(5, name)
    )


def request_context_env(workspace: str, shell: str, os_version: str, time_zone: str) -> bytes:
    """agent.v1.RequestContextEnv — what the client's machine looks like."""
    return (
        text(1, os_version)
        + text(2, workspace)  # workspace_paths is repeated; one entry
        + text(3, shell)
        + text(10, time_zone)
        + text(11, workspace)  # project_folder
    )


def request_context(env: bytes, tools: list[bytes], instructions: str) -> bytes:
    """agent.v1.RequestContext — the environment and the tool surface for a turn."""
    parts = [blob(4, env)]
    parts.extend(blob(7, tool) for tool in tools)
    if tools and instructions:
        parts.append(blob(14, text(1, TOOL_PROVIDER) + text(2, instructions)))  # mcp_instructions
    return b"".join(parts)


def user_message(body: str, message_id: str) -> bytes:
    """agent.v1.UserMessage — the turn's text."""
    return text(1, body) + text(2, message_id) + scalar(4, AGENT_MODE)


def conversation_state(root_prompt_blob_ids: list[bytes]) -> bytes:
    """agent.v1.ConversationStateStructure — a fresh conversation's state.

    Only ``root_prompt_messages_json`` is set, and it holds blob *ids*: the system
    prompt travels as a blob the server asks for by id over the KV channel rather than
    inline. Turns are deliberately left empty — their ``user_message`` fields are blob
    references too, and referring to blobs a fresh conversation has never stored is
    what produces "blob not found"."""
    return b"".join(blob(1, blob_id) for blob_id in root_prompt_blob_ids)


def model_details(model_id: str) -> bytes:
    """agent.v1.ModelDetails — which model answers."""
    return text(1, model_id) + text(3, model_id)


def mcp_file_system_options(workspace: str, instructions: str) -> bytes:
    """agent.v1.McpFileSystemOptions — declares Daisy's tool server to the agent."""
    descriptor = (
        text(1, "Daisy")  # server_name
        + text(2, TOOL_PROVIDER)  # server_identifier
        + text(3, workspace)  # folder_path
        + text(4, instructions)  # server_use_instructions
    )
    return boolean(1, True) + text(2, workspace) + blob(3, descriptor)


def agent_run_request(
    *,
    state: bytes,
    action: bytes,
    model: bytes,
    tools: list[bytes],
    conversation_id: str,
    file_system_options: bytes,
) -> bytes:
    """agent.v1.AgentRunRequest — one turn, complete."""
    parts = [blob(1, state), blob(2, action), blob(3, model)]
    if tools:
        parts.append(blob(4, b"".join(blob(1, tool) for tool in tools)))  # McpTools.mcp_tools
    parts.append(text(5, conversation_id))
    if file_system_options:
        parts.append(blob(6, file_system_options))
    return b"".join(parts)


def client_message_run(run_request: bytes) -> bytes:
    """agent.v1.AgentClientMessage carrying a run request."""
    return blob(1, run_request)


def client_message_exec_result(exec_id_number: int, exec_id: str, result_field: int, result: bytes) -> bytes:
    """agent.v1.AgentClientMessage carrying an ``ExecClientMessage``.

    ``result_field`` is the field number of the specific result inside
    ``ExecClientMessage`` — 11 for ``mcp_result``, 2 for ``shell_result``, and so on —
    which is what makes one builder enough for every tool the server can ask us to run."""
    exec_message = scalar(1, exec_id_number) + text(15, exec_id) + blob(result_field, result)
    return blob(2, exec_message)


def client_message_stream_close(exec_id_number: int) -> bytes:
    """agent.v1.AgentClientMessage carrying ``ExecClientControlMessage.stream_close``.

    Every exec result is followed by one of these: the result says what happened, the
    close says nothing further is coming on that exec's channel."""
    return blob(5, blob(1, scalar(1, exec_id_number)))


def client_message_kv(kv_id: int, result_field: int, result: bytes) -> bytes:
    """agent.v1.AgentClientMessage carrying a ``KvClientMessage``.

    ``result_field`` is 2 for ``get_blob_result`` and 3 for ``set_blob_result``."""
    return blob(3, scalar(1, kv_id) + blob(result_field, result))


def mcp_success_result(body: str, is_error: bool) -> bytes:
    """agent.v1.McpResult carrying success — one text content item.

    ``is_error`` is MCP's own notion: the call ran and the tool reported a failure,
    which is different from the call not happening. Daisy's failing tools produce text,
    so they arrive here rather than as ``McpError``."""
    content_item = blob(1, text(1, body))  # McpToolResultContentItem.text → McpTextContent.text
    return blob(1, blob(1, content_item) + boolean(2, is_error))


def mcp_rejected_result(reason: str) -> bytes:
    """agent.v1.McpResult carrying a rejection — the client declined to run the tool."""
    return blob(3, text(1, reason))


# Where each built-in tool's rejection lives: the field number of the tool's result
# inside ``ExecClientMessage``, the field number of the "rejected" variant inside that
# result, and how many string fields the rejected message takes before its reason.
#
# The agent has its own built-in tools — shell, read, write, grep — and it will reach
# for them regardless of what we offered, because its toolset is decided server-side.
# Daisy cannot run them: doing so would execute commands and touch files outside the
# harness's permission model, from inside a model client, with no session to attribute
# them to. So each one is declined, by name, using the protocol's own rejection variant
# rather than a silence the agent would wait out.
REJECTABLE_EXECS: dict[int, tuple[str, int, int, int]] = {
    # args field in ExecServerMessage: (label, result field, rejected field, leading strings)
    2: ("shell", 2, 4, 2),   # ShellResult.rejected → ShellRejected{command, working_directory, reason}
    14: ("shell", 2, 4, 2),  # shell_stream_args answers on shell_result too
    3: ("write", 3, 6, 1),   # WriteResult.rejected → WriteRejected{path, reason}
    4: ("delete", 4, 6, 1),  # DeleteResult.rejected → DeleteRejected{path, reason}
    7: ("read", 7, 3, 1),    # ReadResult.rejected → ReadRejected{path, reason}
    8: ("ls", 8, 3, 1),      # LsResult.rejected → LsRejected{path, reason}
    9: ("diagnostics", 9, 3, 1),  # DiagnosticsResult.rejected → DiagnosticsRejected{path, reason}
}


def rejected_result(rejected_field: int, leading: list[str], reason: str) -> bytes:
    """A tool result whose only populated variant is its rejection.

    ``leading`` echoes back the identifying strings the rejected message starts with
    (the command, or the path) so the agent's own transcript names what it was refused."""
    body = b"".join(text(index + 1, value) for index, value in enumerate(leading))
    return blob(rejected_field, body + text(len(leading) + 1, reason))


def grep_error_result(message: str) -> bytes:
    """agent.v1.GrepResult carrying an error — grep has no rejection variant."""
    return blob(2, text(1, message))


# Parsing the server's side.

@dataclass
class ToolCall:
    """An MCP tool call the model made: Daisy's tool name, its arguments, and the id
    Cursor will match our result to."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ExecRequest:
    """The server asking the client to run something. ``mcp`` carries a tool call the
    model made; every other kind is one of the agent's built-ins."""

    exec_id_number: int
    exec_id: str
    args_field: int
    tool_call: Optional[ToolCall] = None
    identifying_strings: list[str] = dataclass_field(default_factory=list)


@dataclass
class BlobRequest:
    """A KV message: the server reading a blob we hold, or handing us one to hold."""

    kv_id: int
    blob_id: bytes
    blob_data: Optional[bytes] = None

    @property
    def is_read(self) -> bool:
        return self.blob_data is None


@dataclass
class ServerMessage:
    """One ``AgentServerMessage``, reduced to what the chat model acts on.

    Everything else the envelope can carry — checkpoints, step timings, interaction
    queries — is dropped here rather than upstream, so the model's stream loop reads as
    a short list of things that can happen in a turn."""

    text_delta: str = ""
    thinking_delta: str = ""
    tool_call: Optional[ToolCall] = None
    exec_request: Optional[ExecRequest] = None
    blob_request: Optional[BlobRequest] = None
    turn_ended: bool = False
    heartbeat: bool = False
    used_tokens: int = 0


def _parse_mcp_args(data: bytes) -> ToolCall:
    """agent.v1.McpArgs → a tool call. Argument values are serialized Values."""
    fields = parse(data)
    arguments: dict[str, Any] = {}
    for entry in fields:
        if entry.number != 2 or not entry.is_message:
            continue
        pair = parse(entry.data)
        key = string_at(pair, 1)
        if key:
            value = first(pair, 2)
            arguments[key] = decode_value(value.data) if value is not None else None
    return ToolCall(
        call_id=string_at(fields, 3),  # tool_call_id
        tool_name=string_at(fields, 5) or string_at(fields, 1),  # tool_name, else name
        arguments=arguments,
    )


def _parse_tool_call(data: bytes) -> Optional[ToolCall]:
    """agent.v1.ToolCall → a tool call, when it is the MCP variant (field 15).

    A built-in tool call is not one of ours to execute, so it is not reported here; it
    arrives separately as an exec request and is declined there."""
    mcp = message_at(parse(data), 15)
    if mcp is None:
        return None
    args = first(mcp, 1)
    return _parse_mcp_args(args.data) if args is not None and args.is_message else None


def _parse_tool_call_update(data: bytes) -> Optional[ToolCall]:
    """agent.v1.ToolCallStartedUpdate / ToolCallCompletedUpdate — same shape."""
    fields = parse(data)
    inner = first(fields, 2)
    if inner is None or not inner.is_message:
        return None
    call = _parse_tool_call(inner.data)
    if call is None:
        return None
    # The update's own call_id is the stable one; McpArgs.tool_call_id can be empty.
    return ToolCall(
        call_id=string_at(fields, 1) or call.call_id,
        tool_name=call.tool_name,
        arguments=call.arguments,
    )


def _parse_interaction_update(data: bytes, message: ServerMessage) -> None:
    """agent.v1.InteractionUpdate — the model's own output for the turn."""
    for entry in parse(data):
        if entry.number == 1 and entry.is_message:  # text_delta
            message.text_delta = string_at(parse(entry.data), 1)
        elif entry.number == 4 and entry.is_message:  # thinking_delta
            message.thinking_delta = string_at(parse(entry.data), 1)
        elif entry.number == 2 and entry.is_message:  # tool_call_started
            message.tool_call = _parse_tool_call_update(entry.data)
        elif entry.number == 8 and entry.is_message:  # token_delta (a count, not text)
            counter = first(parse(entry.data), 1)
            message.used_tokens = counter.number_value if counter is not None else 0
        elif entry.number == 13:  # heartbeat
            message.heartbeat = True
        elif entry.number == 14:  # turn_ended
            message.turn_ended = True


def _parse_exec_server_message(data: bytes) -> Optional[ExecRequest]:
    """agent.v1.ExecServerMessage — a request to run a tool, ours or the agent's."""
    fields = parse(data)
    identifier = first(fields, 1)
    request = ExecRequest(
        exec_id_number=identifier.number_value if identifier is not None else 0,
        exec_id=string_at(fields, 15),
        args_field=0,
    )
    for entry in fields:
        if not entry.is_message:
            continue
        if entry.number == 11:  # mcp_args — one of Daisy's tools
            request.args_field = 11
            request.tool_call = _parse_mcp_args(entry.data)
            return request
        if entry.number in REJECTABLE_EXECS:
            _label, _result_field, _rejected_field, leading = REJECTABLE_EXECS[entry.number]
            inner = parse(entry.data)
            request.args_field = entry.number
            # ShellArgs leads with command + working_directory; every other rejectable
            # exec leads with a path. Both are just the message's first strings.
            request.identifying_strings = [string_at(inner, index + 1) for index in range(leading)]
            return request
        if entry.number in (5, 10):  # grep_args, request_context_args
            request.args_field = entry.number
            return request
    return None


def _parse_kv_server_message(data: bytes) -> Optional[BlobRequest]:
    """agent.v1.KvServerMessage — the blob store the conversation state refers into."""
    fields = parse(data)
    identifier = first(fields, 1)
    kv_id = identifier.number_value if identifier is not None else 0
    if (get_args := message_at(fields, 2)) is not None:  # get_blob_args
        target = first(get_args, 1)
        return BlobRequest(kv_id=kv_id, blob_id=target.data if target is not None else b"")
    if (set_args := message_at(fields, 3)) is not None:  # set_blob_args
        target = first(set_args, 1)
        payload = first(set_args, 2)
        return BlobRequest(
            kv_id=kv_id,
            blob_id=target.data if target is not None else b"",
            blob_data=payload.data if payload is not None else b"",
        )
    return None


def parse_server_message(payload: bytes) -> ServerMessage:
    """agent.v1.AgentServerMessage → the parts of it a turn reacts to."""
    message = ServerMessage()
    for entry in parse(payload):
        if not entry.is_message:
            continue
        if entry.number == 1:  # interaction_update
            _parse_interaction_update(entry.data, message)
        elif entry.number == 2:  # exec_server_message
            message.exec_request = _parse_exec_server_message(entry.data)
        elif entry.number == 4:  # kv_server_message
            message.blob_request = _parse_kv_server_message(entry.data)
    return message
