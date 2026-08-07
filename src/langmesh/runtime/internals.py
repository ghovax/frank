"""The helpers and small dataclasses the AgentRuntime mixins share, in a leaf module so the graph stays a DAG."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from langmesh.base.credentials import is_signed_in
from langmesh.base.cursor_credentials import is_signed_in as cursor_is_signed_in
from langmesh.base.configuration import Configuration, PromptLoader
from langmesh.protocol.events import tool_status_from_result, ToolStatus
from langmesh.base.providers import resolve_api_key
from langmesh.base.tuning import active_tuning, clip_to_tokens, count_tokens, Tunable
from langchain_core.messages import AIMessageChunk
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Any, AsyncIterator, Literal, Optional
import json
from langmesh.base.serialization import compact, content_address


#: The kinds an entry can be, each answering a different question the next reader will have.
ENTRY_CATEGORY = Literal["fact", "decision", "constraint", "failure", "artifact", "open"]
#: How well an entry is established, stated rather than left for the reader to guess.
ENTRY_STANDING = Literal["verified", "reported", "inferred"]

#: What each field means, written where prose belongs rather than wedged into the model.
_FIELDS = PromptLoader(Path(__file__).parent / "descriptions")


def _says(name: str) -> str:
    """One field's description, from its own file, so the wording is edited as prose and not as code."""
    text = _FIELDS.load(name, {}).strip()
    if not text:
        raise RuntimeError(f"No description file for {name} in runtime/descriptions.")
    return text


class Observation(BaseModel):
    """One immutable finding in the ledger. A correction is a new entry naming the old, never an edit."""

    category: ENTRY_CATEGORY = Field(description=_says("observation_category"))
    claim: str = Field(description=_says("observation_claim"))
    detail: str = Field(description=_says("observation_detail"))
    evidence: str = Field(default="", description=_says("observation_evidence"))
    standing: ENTRY_STANDING = Field(description=_says("observation_standing"))
    supersedes: list[str] = Field(default_factory=list, description=_says("observation_supersedes"))

    def identity(self) -> str:
        """Addressed by what it says, not by when it was written, so the same finding twice is one entry."""
        return content_address(self.model_dump(exclude={"supersedes"}))


class ObservationBatch(BaseModel):
    """Everything one pass appends to the ledger. Nothing is ever removed from it."""

    observations: list[Observation] = Field(default_factory=list, description=_says("observation_batch"))


class Directive(BaseModel):
    """Something the person asked for, kept because an instruction outlives the turn that carried it."""

    kind: Literal["requirement", "correction", "preference"] = Field(description=_says("directive_kind"))
    summary: str = Field(description=_says("directive_summary"))
    still_binding: bool = Field(default=True, description=_says("directive_still_binding"))
    supersedes: list[str] = Field(default_factory=list, description=_says("directive_supersedes"))

    def identity(self) -> str:
        """Addressed by the instruction itself; whether it still binds is state, and state is not identity."""
        return content_address(self.model_dump(exclude={"supersedes", "still_binding"}))


class DirectiveBatch(BaseModel):
    """Everything one pass appends to the directive ledger."""

    directives: list[Directive] = Field(default_factory=list, description=_says("directive_batch"))


# What ``_stream_next`` returns at the end, since a StopAsyncIteration inside a Task is mishandled.
_STREAM_EXHAUSTED = object()


async def _stream_next(iterator: AsyncIterator) -> Any:
    """The next item, or ``_STREAM_EXHAUSTED``, so each read can be raced against the abort."""
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return _STREAM_EXHAUSTED


def model_is_authorized(
    model_identifier: str,
    global_configuration: Configuration,
) -> bool:
    """Whether we hold credentials for ``model_identifier``. The one authority, mirroring ``build_chat_model``."""
    provider_identifier = model_identifier.split("/", 1)[0]
    if provider_identifier == "chatgpt":
        return is_signed_in()
    if provider_identifier == "cursor":
        return cursor_is_signed_in()
    if provider_identifier == "custom":
        return True
    return bool(resolve_api_key(provider_identifier, global_configuration.configured_provider_keys()))


def _maybe_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


# Background handles from the tool registries. Not A2A tasks: their results are delivered when ready.
_BACKGROUND_HANDLE_PREFIXES = {
    "search-": "search_web",
    "bg-": "bash",
}


def _coerce_mcp_arguments(value: Any) -> dict:
    """An MCP call's `arguments` as a dict, since models often emit the object as a JSON string."""
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


def _background_handle_kind(turn_id: str) -> str | None:
    """The background kind ``turn_id`` names, or ``None`` when it is a readable A2A task."""
    for prefix, kind in _BACKGROUND_HANDLE_PREFIXES.items():
        if turn_id.startswith(prefix):
            return kind
    return None


def _cap_model_result_payload(result: str, *, code: str = "tool_result_truncated") -> str:
    """Bound a model-facing result to the output budget, by dropping whole fields and saying which went."""
    budget = active_tuning().amount(Tunable.output_tokens)
    _, was_truncated = clip_to_tokens(result, budget)
    if not was_truncated:
        return result

    parsed = _maybe_json(result)
    if not isinstance(parsed, dict):
        excerpt, _ = clip_to_tokens(result, budget)
        return compact({"code": code, "truncated": True,
                        "omitted_characters": len(result) - len(excerpt),
                        "output_excerpt": excerpt})

    kept = dict(parsed)
    omitted: dict[str, int] = {}

    def rendered_with(fields: dict) -> str:
        return compact({**fields, "truncated": True, **({"omitted": omitted} if omitted else {})})

    def over(fields: dict) -> bool:
        return clip_to_tokens(rendered_with(fields), budget)[1]

    # Largest first, but never the fields that say what happened: a failure is what a model can act on.
    essential = {"ok", "error", "error_code", "code", "status"}
    for key in sorted(kept, key=lambda key: len(compact(kept[key])), reverse=True):
        if not over(kept):
            break
        if key not in essential:
            omitted[key] = len(compact(kept.pop(key)))

    if not over(kept):
        return rendered_with(kept)

    # One field enormous on its own: clip its text in place, so the result keeps its shape.
    for key in sorted(kept, key=lambda key: len(compact(kept[key])), reverse=True):
        if not over(kept) or not isinstance(kept[key], str):
            continue
        elsewhere = count_tokens(rendered_with(
            {other: value for other, value in kept.items() if other != key}
        ))
        excerpt, clipped = clip_to_tokens(kept[key], max(1, budget - elsewhere))
        if clipped:
            omitted[f"{key} (clipped)"] = len(kept[key]) - len(excerpt)
            kept[key] = excerpt
    return rendered_with(kept)


def message_tokens(message: Any) -> int:
    """How much window one message occupies, counting the tool calls and results sent with it."""
    from langmesh.base.message_content import message_text

    total = count_tokens(message_text(message))
    for tool_call in getattr(message, "tool_calls", None) or []:
        arguments = tool_call.get("args")
        total += count_tokens(
            arguments if isinstance(arguments, str) else compact(arguments)
        )
        total += count_tokens(str(tool_call.get("name") or ""))
    return total


def conversation_tokens(messages: Any) -> int:
    """:func:`message_tokens` over a whole message list."""
    return sum(message_tokens(message) for message in messages)


def _utc_timestamp(datetime_value: datetime) -> str:
    return datetime_value.isoformat()


def _tool_timing_metadata(
    *,
    tool_name: str,
    tool_call_identifier: str,
    started_at: datetime,
    completed_at: datetime,
    duration_milliseconds: int,
    background_job_id: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_call_id": tool_call_identifier,
        "started_at": _utc_timestamp(started_at),
        "completed_at": _utc_timestamp(completed_at),
        "duration_milliseconds": duration_milliseconds,
    }
    if background_job_id:
        metadata["background_job_id"] = background_job_id
    return metadata


_MODEL_PROMPT_LOADER = PromptLoader(Path(__file__).parent / "prompts")


def _model_visible_tool_result(
    content: str, metadata: dict[str, Any], status: str, code: str | None = None, *, kind: str = "tool_result",
) -> str:
    """A model-facing tool result: a one-line JSON header, a blank line, then the tool's output as-is."""
    header: dict[str, Any] = {
        "kind": kind,
        "tool_name": metadata.get("tool_name", ""),
        "tool_call_id": metadata.get("tool_call_id", ""),
        "status": status,
        "code": code,
    }
    for key in ("started_at", "completed_at", "duration_milliseconds", "background_job_id"):
        value = metadata.get(key)
        if value is not None:
            header[key] = value
    return _MODEL_PROMPT_LOADER.load("model_tool_result", {
        "header": compact(header),
        "content": content,
    })


def _model_result_status(content: str, *, ok: bool, backgrounded: bool) -> tuple[str, str | None]:
    """The (status, code) for a model-facing result: failed is error, backgrounded is still running."""
    parsed = _maybe_json(content)
    code = parsed.get("code") if isinstance(parsed, dict) else None
    if not ok:
        return ToolStatus.ERROR.value, code
    if backgrounded:
        return ToolStatus.RUNNING.value, code
    return tool_status_from_result(parsed).value, code


def _detect_workspace(working_directory: str) -> tuple[str, bool]:
    """``(worktree_root, is_git_repo)``, walking up for a ``.git`` marker and falling back to the directory."""
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
    """The container origins an annotation can be, so a string argument can be JSON-parsed or not."""
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
    """Parse arguments a model serialized as JSON strings back into the containers the schema types."""
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


def _escape_to_dict(escape: Any) -> dict:
    """An :class:`~langmesh.runtime.boundary.Escape` as plain data, since this is about to be JSON."""
    return {
        "reads": list(escape.reads),
        "writes": list(escape.writes),
        "network": escape.network,
    }


def _escape_from_dict(data: Any) -> Any:
    """The inverse, passing through a value that is already an ``Escape``."""
    from langmesh.runtime.boundary import Escape

    if isinstance(data, Escape):
        return data
    data = data or {}
    return Escape(
        reads=tuple(data.get("reads") or ()),
        writes=tuple(data.get("writes") or ()),
        network=bool(data.get("network", False)),
    )


@dataclass
class _PreflightGate:
    """One interaction a call needs before it can run: a permission prompt, or an ``ask_user`` question."""

    request_id: str
    tool_call_id: str
    kind: str  # "permission" | "question"
    # The call this gate stands in front of, since it is shown before that call is announced.
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    command: str = ""
    # Why approval is needed, as against ``arguments["explanation"]``, which is why the model wants the call.
    explanation: str = ""
    # The same, as facts rather than prose, so a client writes the sentence in its own language.
    reason: Any = None
    questions: list = field(default_factory=list)
    # A bash command approval remembers an "always allow" as a session rule.
    is_bash: bool = False
    # The model-facing error if the gate is answered no.
    deny_message: str = ""
    # For an egress gate, the remote agent name (an "always allow" is remembered).
    egress_agent: str = ""
    # The widening asked for, carried so approving records exactly what the planner worked out.
    escape: Any = field(default_factory=lambda: _escape_from_dict(None))
    # Whether approving lets this one command reach past the workspace. Set only by a retry gate.
    whole_disk: bool = False
    # What the refusal looked like, so the reviewer judges a command that hit a wall.
    denial_evidence: str = ""
    # What the confined run produced, which a refused retry still owes the model.
    refused_result: Any = None
    # Whether approving this lets a screen script call the primitives that change something.
    grants_screen_mutations: bool = False

    def to_dict(self) -> dict:
        """Every field as plain data: this dict crosses to a client and into the durable plan, so an omission disappears."""
        return {
            "request_id": self.request_id, "tool_call_id": self.tool_call_id, "kind": self.kind,
            "tool_name": self.tool_name, "arguments": self.arguments,
            "command": self.command, "explanation": self.explanation,
            "reason": self.reason.model_dump() if hasattr(self.reason, "model_dump") else self.reason,
            "questions": self.questions, "is_bash": self.is_bash,
            "deny_message": self.deny_message, "egress_agent": self.egress_agent,
            "escape": _escape_to_dict(self.escape),
            "whole_disk": self.whole_disk, "denial_evidence": self.denial_evidence,
            "refused_result": self.refused_result,
            "grants_screen_mutations": self.grants_screen_mutations,
        }

    @classmethod
    def from_dict(cls, data: dict) -> _PreflightGate:
        return cls(
            request_id=str(data.get("request_id", "")), tool_call_id=str(data.get("tool_call_id", "")),
            kind=str(data.get("kind", "permission")),
            tool_name=str(data.get("tool_name", "")), arguments=dict(data.get("arguments") or {}),
            command=str(data.get("command", "")),
            explanation=str(data.get("explanation", "")),
            reason=data.get("reason"),
            questions=list(data.get("questions", []) or []), is_bash=bool(data.get("is_bash", False)),
            deny_message=str(data.get("deny_message", "")), egress_agent=str(data.get("egress_agent", "")),
            # Rebuilt as the real thing: `_approve` reads `.reads`, `.writes` and `.network` off it.
            escape=_escape_from_dict(data.get("escape")),
            whole_disk=bool(data.get("whole_disk", False)),
            denial_evidence=str(data.get("denial_evidence", "")),
            refused_result=data.get("refused_result"),
            grants_screen_mutations=bool(data.get("grants_screen_mutations", False)),
        )


@dataclass
class _ToolPlan:
    """The verdict for one call: a refusal, some gates, or neither. Computed once, so execution only carries it out."""

    tool_call_id: str
    refusal: Optional[dict] = None  # {"code", "message", "denied_injection", "raw_command", "reason"}
    gates: list[_PreflightGate] = field(default_factory=list)
    # Whether a screen script may change something. False by default, so the narrow set is what a call gets.
    screen_mutations: bool = False
    # Set when this call is a second run of a command the operating system refused.
    retry_grant: Any = None
    # A finished call's outcome, held across a suspension so the resumed batch replays rather than re-runs.
    completed: Optional[dict] = None

    @property
    def needs_human(self) -> bool:
        return bool(self.gates)

    @property
    def approved(self) -> bool:
        return self.refusal is None and not self.gates

    def to_dict(self) -> dict:
        return {
            "tool_call_id": self.tool_call_id, "refusal": self.refusal,
            "gates": [gate.to_dict() for gate in self.gates],
            "screen_mutations": self.screen_mutations,
            "retry_grant": self.retry_grant.as_dict() if self.retry_grant is not None else None,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> _ToolPlan:
        from langmesh.base.confinement import Grant

        retry = data.get("retry_grant")
        return cls(
            tool_call_id=str(data.get("tool_call_id", "")),
            refusal=data.get("refusal"),
            gates=[_PreflightGate.from_dict(gate) for gate in (data.get("gates") or [])],
            screen_mutations=bool(data.get("screen_mutations", False)),
            retry_grant=Grant.from_dict(retry) if retry else None,
            completed=data.get("completed"),
        )


@dataclass
class _ToolCall:
    """One call as middleware sees it. Mutable arguments, so a layer can rewrite a path without rebuilding it."""

    name: str
    arguments: dict


@dataclass
class _ResolvedToolDecision:
    """The verdict a batch runner hands each tool: run it, deny it, or return the ``ask_user`` answers."""

    tool_call_id: str
    approved: bool = True
    denial: Optional[dict] = None  # {"code", "message", "denied_injection", "raw_command", "reason"}
    answers: Any = None  # ask_user: the answers list, or the decline sentinel
    # Whether a screen script may change something. False unless a rule or an answer said otherwise.
    screen_mutations: bool = False
    # The widening an approved second run uses.
    retry_grant: Any = None
    # A finished call's outcome, replayed rather than re-run, so a suspension repeats no side effects.
    completed: Optional[dict] = None


# How a turn phase tells the driver what to do next: a generator cannot return through ``async for``.
_PROCEED = "proceed"    # fall through to the rest of the iteration
_CONTINUE = "continue"  # the phase already advanced loop bookkeeping; loop again
_STOP = "stop"          # the turn is over (a terminal event was already yielded); return


@dataclass
class _ModelCallOutcome:
    """What one streamed model call produced: the response, or the terminal condition the loop must act on."""

    response: Optional[AIMessageChunk] = None
    aborted_for_steering: bool = False
    cancelled: bool = False


@dataclass
class _StepOutcome:
    """The loop directive one step of a turn hands back (see ``_PROCEED``/``_CONTINUE``/``_STOP``)."""

    directive: str = _PROCEED
