"""Shared runtime internals extracted from agent.py.

The helper functions, small dataclasses, and support classes the AgentRuntime concern
mixins reference. Kept in a leaf module (it imports only stable modules, never agent.py or
the mixin files) so the dependency graph is a clean DAG — agent_internals -> mixin files ->
agent.py — with no import cycle."""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from xeac.base.credentials import is_signed_in
from xeac.base.configuration import GlobalConfiguration
from xeac.base.configuration import PromptLoader
from xeac.protocol.events import ToolStatus
from xeac.protocol.events import tool_status_from_result
from xeac.base.providers import resolve_api_key
from xeac.base.tuning import Limit
from xeac.base.tuning import active_tuning
from xeac.base.tuning import clip_to_tokens
from xeac.base.identifiers import new_id
from langchain_core.messages import AIMessageChunk
from pathlib import Path
from pydantic import BaseModel
from pydantic import Field
from typing import Any
from typing import AsyncIterator
from typing import Literal
from typing import Optional
import json
from xeac.base.serialization import compact




class Observation(BaseModel):
    """One entry in the conversation-memory log (Observational Memory)."""

    category: Literal["decision", "fact", "artifact", "goal", "open"] = Field(
        description=(
            "decision — a choice or approach the agent committed to; "
            "fact — something learned about the codebase, system, or world; "
            "artifact — a file/path/resource created or modified (record the exact path); "
            "goal — the user's objective, preference, or constraint; "
            "open — an unfinished thread or an agreed next step."
        )
    )
    detail: str = Field(
        description="A terse, information-dense note. Record outcomes and state, and keep "
        "concrete identifiers (paths, ids, names, commands, numbers) exact."
    )


class ObservationBatch(BaseModel):
    """The structured memory the Observer/Reflector emits as a tool call — so the shape
    is guaranteed by the model's tool-calling, never scraped from free text."""

    observations: list[Observation] = Field(default_factory=list)


# Sentinel returned by ``_stream_next`` when an async iterator is exhausted, so a
# stream read can be raced against an abort inside a Task without a
# StopAsyncIteration propagating through it (which asyncio mishandles).
_STREAM_EXHAUSTED = object()


async def _stream_next(iterator: AsyncIterator) -> Any:
    """Return the next item from ``iterator``, or ``_STREAM_EXHAUSTED`` when it is
    done. Wrapped so the model stream can be driven one read at a time and each read
    raced against the abort event — a Stop then interrupts the turn even while it is
    parked on the network awaiting the next token."""
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return _STREAM_EXHAUSTED


def model_is_authorized(
    model_identifier: str,
    global_configuration: GlobalConfiguration,
) -> bool:
    """Whether we currently hold credentials to call ``model_identifier``.

    The single authorization authority, mirroring how ``build_chat_model`` resolves
    credentials so every LLM call site authorizes identically: the native
    ``chatgpt`` subscription provider is unlocked by an OAuth sign-in (token store),
    each LiteLLM provider by a configured key or one of its env vars, and ``custom``
    is selectable on demand. Auxiliary calls (session titling, ...) consult this
    before building a model instead of re-deriving the check per call site — which
    is how titling used to silently exclude the OAuth-only chatgpt provider."""
    provider_identifier = model_identifier.split("/", 1)[0]
    if provider_identifier == "chatgpt":
        return is_signed_in()
    if provider_identifier == "custom":
        return True
    return bool(resolve_api_key(provider_identifier, global_configuration.configured_provider_keys()))


def _maybe_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


# Background-task handles minted by the tool registries: search_web ids carry the
# "search-" prefix, background bash the "bg-" prefix. These are NOT A2A tasks and
# can never be read with read_task — their results are auto-delivered when ready.
_BACKGROUND_HANDLE_PREFIXES = {
    "search-": "search_web",
    "bg-": "bash",
}


def _coerce_mcp_arguments(value: Any) -> dict:
    """Normalize the `arguments` of a call_mcp_tool call to a dict. Models often
    emit the nested arguments object as a JSON *string* rather than a real object;
    the previous `isinstance(dict)`-only guard silently dropped those to `{}`, so
    the MCP server saw every field as undefined. Parse a JSON string back to the
    dict it represents; fall back to empty only when there is genuinely nothing
    usable."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _background_handle_kind(task_id: str) -> str | None:
    """The background-task kind if ``task_id`` is one of
    those handles rather than a readable A2A task; otherwise ``None``."""
    for prefix, kind in _BACKGROUND_HANDLE_PREFIXES.items():
        if task_id.startswith(prefix):
            return kind
    return None


def _cap_model_result_payload(result: str, *, code: str = "tool_result_truncated") -> str:
    """Keep model-facing tool results bounded while preserving a full-output file. The cap is the
    window-scaled output budget, so a larger model may hold a larger result inline."""
    excerpt, was_truncated = clip_to_tokens(result, active_tuning().amount(Limit.OUTPUT_TOKENS))
    if not was_truncated:
        return result
    output_path = Path("/tmp") / f"{new_id('tool-result')}.json"
    output_path.write_text(result)
    parsed = _maybe_json(result)
    if isinstance(parsed, dict):
        payload = {
            **parsed,
            "truncated": True,
            "full_output_file": str(output_path),
            "output_excerpt": excerpt,
        }
        for large_key in ("output", "content", "summary", "results"):
            if large_key in payload:
                payload.pop(large_key, None)
        return compact(payload)
    return compact({
        "code": code,
        "truncated": True,
        "full_output_file": str(output_path),
        "output_excerpt": excerpt,
        "size": len(result),
    })


def _utc_timestamp(datetime_value: datetime) -> str:
    return datetime_value.isoformat()


def _tool_timing_metadata(
    *,
    tool_name: str,
    tool_call_identifier: str,
    started_at: datetime,
    completed_at: datetime,
    duration_milliseconds: int,
    background_task_identifier: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_call_id": tool_call_identifier,
        "started_at": _utc_timestamp(started_at),
        "completed_at": _utc_timestamp(completed_at),
        "duration_ms": duration_milliseconds,
    }
    if background_task_identifier:
        metadata["background_task_identifier"] = background_task_identifier
    return metadata


_MODEL_PROMPT_LOADER = PromptLoader(Path(__file__).parent / "prompts")


def _model_visible_tool_result(
    content: str, metadata: dict[str, Any], status: str, code: str | None = None, *, kind: str = "tool_result",
) -> str:
    """The canonical model-facing tool result: a one-line JSON metadata header, a blank
    line, then the tool's raw output body. The body is delivered **as-is** — prose stays
    prose (never re-encoded into an escaped JSON string), structured output stays JSON — so
    a long log or a fetched page reads cleanly instead of collapsing onto one escaped line.
    Used for both an inline ToolMessage (``kind="tool_result"``) and a background-completion
    system message (``kind="background_result"``). Mirrors :class:`events.ModelToolResult`."""
    header: dict[str, Any] = {
        "kind": kind,
        "tool_name": metadata.get("tool_name", ""),
        "tool_call_id": metadata.get("tool_call_id", ""),
        "status": status,
        "code": code,
    }
    for key in ("started_at", "completed_at", "duration_ms", "background_task_identifier"):
        value = metadata.get(key)
        if value is not None:
            header[key] = value
    return _MODEL_PROMPT_LOADER.load("model_tool_result", {
        "header": compact(header),
        "content": content,
    })


def _model_result_status(content: str, *, ok: bool, backgrounded: bool) -> tuple[str, str | None]:
    """The (status, code) for a model-facing tool result. A failed tool is an error; a
    backgrounded one is still running; otherwise fall back to the payload's own code."""
    parsed = _maybe_json(content)
    code = parsed.get("code") if isinstance(parsed, dict) else None
    if not ok:
        return ToolStatus.ERROR.value, code
    if backgrounded:
        return ToolStatus.RUNNING.value, code
    return tool_status_from_result(parsed).value, code


def _detect_workspace(working_directory: str) -> tuple[str, bool]:
    """Return ``(workspace_root, is_git_repo)``. Walks up from the working
    directory for a ``.git`` marker; if found the workspace root is the repo
    top level, otherwise it falls back to the working directory itself."""
    base = Path(working_directory).expanduser().resolve() if working_directory else Path.cwd().resolve()
    current = base
    while True:
        if (current / ".git").exists():
            return str(current), True
        if current == current.parent:
            break
        current = current.parent
    return str(base), False


def _container_origins(annotation: Any) -> set:
    """The container origins (``list`` and/or ``dict``) an annotation can be, seen through
    Optional/Union wrappers. Used to decide whether a string argument should be JSON-parsed."""
    import types as types_module
    import typing

    origins: set = set()

    def visit(current: Any) -> None:
        origin = typing.get_origin(current)
        if origin in (list, dict):
            origins.add(origin)
        elif origin is typing.Union or origin is getattr(types_module, "UnionType", None):
            for argument in typing.get_args(current):
                visit(argument)

    visit(annotation)
    return origins


def _coerce_structured_arguments(schema: Any, arguments: dict) -> dict:
    """Models frequently serialize array/object tool arguments as a JSON *string* rather than a
    native value (e.g. ``"[\\"a\\"]"`` for a ``list[str]`` field). For any field the schema types
    as a list or dict, parse a string that is well-formed JSON of that shape and use the parsed
    value, so a stringified-but-valid argument is accepted by both validation and dispatch
    instead of being rejected by the typed field. Returns a new dict; values that do not apply
    pass through unchanged."""
    if not isinstance(arguments, dict):
        return arguments
    model_fields = getattr(schema, "model_fields", {})
    coerced = dict(arguments)
    for name, value in arguments.items():
        if not isinstance(value, str):
            continue
        field = model_fields.get(name)
        if field is None:
            continue
        origins = _container_origins(field.annotation)
        if not origins:
            continue
        text = value.strip()
        if not text or text[0] not in "[{":
            continue
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            continue
        if (list in origins and isinstance(parsed, list)) or (dict in origins and isinstance(parsed, dict)):
            coerced[name] = parsed
    return coerced


@dataclass
class _PreflightGate:
    """One human-in-the-loop interaction a tool call needs before it can run: a
    permission prompt or an ``ask_user`` question. Surfaced by the preflight pass
    and carried in a ``SUSPENDED`` event; the durable pending-interaction record the
    executor persists is built from these, and a later answer resolves each by
    ``request_id``."""

    request_id: str
    tool_call_id: str
    kind: str  # "permission" | "question"
    command: str = ""
    justification: str = ""
    risk: str = ""
    questions: list = field(default_factory=list)
    # A bash command approval remembers an "always allow" as a session rule.
    is_bash: bool = False
    # The model-facing error if the user denies this specific gate.
    deny_message: str = ""
    # For an egress gate, the remote agent name (an "always allow" is remembered).
    egress_agent: str = ""

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id, "tool_call_id": self.tool_call_id, "kind": self.kind,
            "command": self.command, "justification": self.justification, "risk": self.risk,
            "questions": self.questions, "is_bash": self.is_bash,
            "deny_message": self.deny_message, "egress_agent": self.egress_agent,
        }

    @classmethod
    def from_dict(cls, data: dict) -> _PreflightGate:
        return cls(
            request_id=str(data.get("request_id", "")), tool_call_id=str(data.get("tool_call_id", "")),
            kind=str(data.get("kind", "permission")), command=str(data.get("command", "")),
            justification=str(data.get("justification", "")), risk=str(data.get("risk", "")),
            questions=list(data.get("questions", []) or []), is_bash=bool(data.get("is_bash", False)),
            deny_message=str(data.get("deny_message", "")), egress_agent=str(data.get("egress_agent", "")),
        )


@dataclass
class _ToolPlan:
    """The preflight verdict for one tool call. Exactly one shape holds: a hard
    ``denial`` (a policy block — the tool never runs and the model gets this error),
    one or more pending ``gates`` (needs a human), or neither (auto-approved: run it).
    The decision logic is computed once, here, so ``_execute_tool`` only ever carries
    out a verdict and can no longer approve anything itself."""

    tool_call_id: str
    denial: Optional[dict] = None  # {"code", "message", "denied_injection", "raw_command"}
    gates: list[_PreflightGate] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return bool(self.gates)

    @property
    def approved(self) -> bool:
        return self.denial is None and not self.gates

    def to_dict(self) -> dict:
        return {"tool_call_id": self.tool_call_id, "denial": self.denial, "gates": [g.to_dict() for g in self.gates]}

    @classmethod
    def from_dict(cls, data: dict) -> _ToolPlan:
        return cls(
            tool_call_id=str(data.get("tool_call_id", "")),
            denial=data.get("denial"),
            gates=[_PreflightGate.from_dict(g) for g in (data.get("gates") or [])],
        )


@dataclass
class _ResolvedToolDecision:
    """The verdict a batch runner hands each tool: run it, deny it (with the exact
    error the gate would have produced), or — for ``ask_user`` — the answers to return.
    Produced from the preflight plans plus any human answers, on both the fresh and the
    resumed path, so ``_execute_tool`` only carries a decision out."""

    tool_call_id: str
    approved: bool = True
    denial: Optional[dict] = None  # {"code", "message", "denied_injection", "raw_command"}
    answers: Any = None  # ask_user: the answers list, or the decline sentinel


# How a turn-loop phase tells the driver what to do next. A phase is an async generator
# (it yields wire events), so it cannot return a value through ``async for``; it writes
# its directive into a small holder the driver inspects once the phase drains.
_PROCEED = "proceed"    # fall through to the rest of the iteration
_CONTINUE = "continue"  # the phase already advanced loop bookkeeping; loop again
_STOP = "stop"          # the turn is over (a terminal event was already yielded); return


@dataclass
class _ModelCallOutcome:
    """What one streamed model call produced: the assembled response, or a terminal
    condition the turn loop must act on instead. ``cancelled`` means a Stop with nothing
    queued (a ``Done`` was already yielded); ``aborted_for_steering`` means a Stop that
    found queued steering, so the loop should drain it and iterate again; otherwise
    ``response`` holds the assembled ``AIMessageChunk``."""

    response: Optional[AIMessageChunk] = None
    aborted_for_steering: bool = False
    cancelled: bool = False


@dataclass
class _PhaseStep:
    """The loop directive a turn phase hands back (see ``_PROCEED``/``_CONTINUE``/``_STOP``)."""

    directive: str = _PROCEED
