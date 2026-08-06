"""Shared runtime internals extracted from agent.py."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from frank.base.credentials import is_signed_in
from frank.base.cursor_credentials import is_signed_in as cursor_is_signed_in
from frank.base.configuration import Configuration, PromptLoader
from frank.protocol.events import tool_status_from_result, ToolStatus
from frank.base.providers import resolve_api_key
from frank.base.tuning import active_tuning, clip_to_tokens, count_tokens, Tunable
from langchain_core.messages import AIMessageChunk
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Any, AsyncIterator, Literal, Optional
import json
from frank.base.serialization import compact


class Observation(BaseModel):
    """One entry in the conversation-memory log (Observational Memory)."""

    category: Literal["decision", "fact", "artifact", "goal", "open"] = Field(
        description=(
            "decision — a choice that was committed to, and why it beat the alternative; "
            "fact — something established about the codebase, system, or world, and whether it "
            "was verified or assumed; "
            "artifact — a file, path, command or resource created, changed, or found to matter "
            "(record the exact identifier); "
            "goal — an objective or constraint, and what it rules out; "
            "open — work unfinished, agreed but not done, or blocked, with its next concrete step."
        )
    )
    detail: str = Field(
        description=(
            "One dense note written for a model that will resume this work with no memory of "
            "the turns behind it. State outcomes, not narration. Keep every concrete identifier "
            "— paths, ids, names, commands, numbers, versions, error codes — exactly as written, "
            "and keep measurements as measurements: they cannot be re-derived by thinking."
        )
    )


class ObservationBatch(BaseModel):
    """The structured memory the Observer/Reflector emits as a tool call — so the shape is guaranteed by the model's tool-calling, never scraped from free text."""

    observations: list[Observation] = Field(default_factory=list)


# Sentinel returned by ``_stream_next`` when an async iterator is exhausted, so a stream read can be raced against an abort inside a Task without a StopAsyncIteration propagating through it (which asyncio mishandles).
_STREAM_EXHAUSTED = object()


async def _stream_next(iterator: AsyncIterator) -> Any:
    """Return the next item from ``iterator``, or ``_STREAM_EXHAUSTED`` when it is done."""
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return _STREAM_EXHAUSTED


def model_is_authorized(
    model_identifier: str,
    global_configuration: Configuration,
) -> bool:
    """Whether we currently hold credentials to call ``model_identifier``."""
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


# Background-task handles minted by the tool registries: search_web ids carry the "search-" prefix, background bash the "bg-" prefix.
_BACKGROUND_HANDLE_PREFIXES = {
    "search-": "search_web",
    "bg-": "bash",
}


def _coerce_mcp_arguments(value: Any) -> dict:
    """Normalize the `arguments` of a call_mcp_tool call to a dict."""
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
    """The background-task kind if ``turn_id`` is one of those handles rather than a readable A2A task; otherwise ``None``."""
    for prefix, kind in _BACKGROUND_HANDLE_PREFIXES.items():
        if turn_id.startswith(prefix):
            return kind
    return None


def _cap_model_result_payload(result: str, *, code: str = "tool_result_truncated") -> str:
    """Bound a model-facing tool result to the window-scaled output budget."""
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

    # Largest first, so the fewest fields are lost — but never the ones that say what happened.
    essential = {"ok", "error", "error_code", "code", "status"}
    for key in sorted(kept, key=lambda key: len(compact(kept[key])), reverse=True):
        if not over(kept):
            break
        if key not in essential:
            omitted[key] = len(compact(kept.pop(key)))

    if not over(kept):
        return rendered_with(kept)

    # What is left is essential and still too large, which means one field is enormous on its own — a script that raised with a page's worth of records interpolated into its message.
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
    """How much of the context window one conversation message occupies."""
    from frank.base.message_content import message_text

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
        "duration_ms": duration_milliseconds,
    }
    if background_job_id:
        metadata["background_job_id"] = background_job_id
    return metadata


_MODEL_PROMPT_LOADER = PromptLoader(Path(__file__).parent / "prompts")


def _model_visible_tool_result(
    content: str, metadata: dict[str, Any], status: str, code: str | None = None, *, kind: str = "tool_result",
) -> str:
    """The canonical model-facing tool result: a one-line JSON metadata header, a blank line, then the tool's raw output body."""
    header: dict[str, Any] = {
        "kind": kind,
        "tool_name": metadata.get("tool_name", ""),
        "tool_call_id": metadata.get("tool_call_id", ""),
        "status": status,
        "code": code,
    }
    for key in ("started_at", "completed_at", "duration_ms", "background_job_id"):
        value = metadata.get(key)
        if value is not None:
            header[key] = value
    return _MODEL_PROMPT_LOADER.load("model_tool_result", {
        "header": compact(header),
        "content": content,
    })


def _model_result_status(content: str, *, ok: bool, backgrounded: bool) -> tuple[str, str | None]:
    """The (status, code) for a model-facing tool result."""
    parsed = _maybe_json(content)
    code = parsed.get("code") if isinstance(parsed, dict) else None
    if not ok:
        return ToolStatus.ERROR.value, code
    if backgrounded:
        return ToolStatus.RUNNING.value, code
    return tool_status_from_result(parsed).value, code


def _detect_workspace(working_directory: str) -> tuple[str, bool]:
    """Return ``(worktree_root, is_git_repo)``."""
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
    """The container origins (``list`` and/or ``dict``) an annotation can be, seen through Optional/Union wrappers."""
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
    """Models frequently serialize array/object tool arguments as a JSON *string* rather than a native value (e.g."""
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
    """An :class:`~frank.runtime.boundary.Escape` as plain data."""
    return {
        "reads": list(escape.reads),
        "writes": list(escape.writes),
        "network": escape.network,
    }


def _escape_from_dict(data: Any) -> Any:
    """The inverse."""
    from frank.runtime.boundary import Escape

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
    """One human-in-the-loop interaction a tool call needs before it can run: a permission prompt or an ``ask_user`` question."""

    request_id: str
    tool_call_id: str
    kind: str  # "permission" | "question"
    # The call this gate stands in front of.
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    command: str = ""
    # Why approval is needed, from the rules or the boundary.
    explanation: str = ""
    # Why approval is needed, as facts rather than as a sentence, so the client writes the prose in its own language.
    reason: Any = None
    questions: list = field(default_factory=list)
    # A bash command approval remembers an "always allow" as a session rule.
    is_bash: bool = False
    # The model-facing error if the gate is answered no.
    deny_message: str = ""
    # For an egress gate, the remote agent name (an "always allow" is remembered).
    egress_agent: str = ""
    # The widening being asked for.
    escape: Any = field(default_factory=lambda: _escape_from_dict(None))
    # Whether approving this means "let this one command reach past the workspace".
    whole_disk: bool = False
    # What the refusal looked like, for a retry gate.
    denial_evidence: str = ""
    # What the confined run produced.
    refused_result: Any = None
    # Whether approving this lets a screen script call the primitives that change something.
    grants_screen_mutations: bool = False

    def to_dict(self) -> dict:
        """Every field, as plain JSON-safe data."""
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
            # Rebuilt as the real thing rather than left as a dict: what reads it on the way back is `_approve`, which takes `.reads`, `.writes` and `.network` off it.
            escape=_escape_from_dict(data.get("escape")),
            whole_disk=bool(data.get("whole_disk", False)),
            denial_evidence=str(data.get("denial_evidence", "")),
            refused_result=data.get("refused_result"),
            grants_screen_mutations=bool(data.get("grants_screen_mutations", False)),
        )


@dataclass
class _ToolPlan:
    """The preflight verdict for one tool call."""

    tool_call_id: str
    refusal: Optional[dict] = None  # {"code", "message", "denied_injection", "raw_command", "reason"}
    gates: list[_PreflightGate] = field(default_factory=list)
    # Whether a screen script may call the primitives that change something.
    screen_mutations: bool = False
    # Set when this call is a second run of a command the operating system refused.
    retry_grant: Any = None
    # The outcome of a call that already ran, held across a suspension so the resumed batch replays it instead of running the tool a second time.
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
        from frank.base.confinement import Grant

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
    """One call, as middleware sees it."""

    name: str
    arguments: dict


@dataclass
class _ResolvedToolDecision:
    """The verdict a batch runner hands each tool: run it, deny it (with the exact error the gate would have produced), or — for ``ask_user`` — the answers to return."""

    tool_call_id: str
    approved: bool = True
    denial: Optional[dict] = None  # {"code", "message", "denied_injection", "raw_command", "reason"}
    answers: Any = None  # ask_user: the answers list, or the decline sentinel
    # Whether a screen script may call the primitives that change something.
    screen_mutations: bool = False
    # The widening a second run of this command was approved to use, when the first was refused by the operating system.
    retry_grant: Any = None
    # The outcome of a call that already ran before the turn suspended.
    completed: Optional[dict] = None


# How a turn-loop phase tells the driver what to do next.
_PROCEED = "proceed"    # fall through to the rest of the iteration
_CONTINUE = "continue"  # the phase already advanced loop bookkeeping; loop again
_STOP = "stop"          # the turn is over (a terminal event was already yielded); return


@dataclass
class _ModelCallOutcome:
    """What one streamed model call produced: the assembled response, or a terminal condition the turn loop must act on instead."""

    response: Optional[AIMessageChunk] = None
    aborted_for_steering: bool = False
    cancelled: bool = False


@dataclass
class _PhaseStep:
    """The loop directive a turn phase hands back (see ``_PROCEED``/``_CONTINUE``/``_STOP``)."""

    directive: str = _PROCEED
