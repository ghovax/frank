"""Shared runtime internals extracted from agent.py.

The helper functions, small dataclasses, and support classes the AgentRuntime concern
mixins reference. Kept in a leaf module (it imports only stable modules, never agent.py or
the mixin files) so the dependency graph is a clean DAG — agent_internals -> mixin files ->
agent.py — with no import cycle."""
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
    """The structured memory the Observer/Reflector emits as a tool call — so the shape
    is guaranteed by the model's tool-calling, never scraped from free text."""

    observations: list[Observation] = Field(default_factory=list)


# Sentinel returned by ``_stream_next`` when an async iterator is exhausted, so a stream read can be raced against an abort inside a Task without a StopAsyncIteration propagating through it (which asyncio mishandles).
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
    global_configuration: Configuration,
) -> bool:
    """Whether we currently hold credentials to call ``model_identifier``.

    The single authorization authority, mirroring how ``build_chat_model`` resolves
    credentials so every LLM call site authorizes identically: the two native
    subscription providers (``chatgpt``, ``cursor``) are unlocked by an OAuth sign-in
    (their own token stores), each LiteLLM provider by a configured key or one of its env
    vars, and ``custom`` is selectable on demand. Auxiliary calls (session titling, ...)
    consult this before building a model instead of re-deriving the check per call site —
    which is how titling used to silently exclude the OAuth-only chatgpt provider."""
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


def _background_handle_kind(turn_id: str) -> str | None:
    """The background-task kind if ``turn_id`` is one of
    those handles rather than a readable A2A task; otherwise ``None``."""
    for prefix, kind in _BACKGROUND_HANDLE_PREFIXES.items():
        if turn_id.startswith(prefix):
            return kind
    return None


def _cap_model_result_payload(result: str, *, code: str = "tool_result_truncated") -> str:
    """Bound a model-facing tool result to the window-scaled output budget.

    This is the backstop: whatever a tool produces, what reaches the conversation is smaller than
    the budget. It has to hold unconditionally, because every tool that grows a result is a tool
    that can end a turn, and the tools cannot each be trusted to bound themselves.

    It did not hold. The previous version clipped the result to an ``excerpt``, then returned
    ``{**parsed, "output_excerpt": excerpt, …}`` — every original key *plus* a copy of the whole
    thing, popping only four hard-coded names (``output``, ``content``, ``summary``, ``results``)
    that a ``control_screen`` result does not have. Measured on a real payload it turned 533,013
    characters into 855,823. The one function whose job was to make results smaller was the
    largest single contributor to the result that overran a context window, and it announced this
    by setting ``"truncated": true`` on a payload it had just enlarged.

    What replaces it drops whole fields, largest first, and says which ones went. A structured
    result is read field by field, so losing ``ran`` entirely and being told so leaves something a
    model can still act on, where a payload clipped mid-string leaves it holding the first 60% of
    a JSON document. Only when a single field is itself over budget is that field's text clipped.

    Nothing is written to disk. The full result used to be spooled to ``/tmp`` and its path handed
    over, which read as a way to recover the rest and was not: the path outlived the turn, nothing
    ever cleaned it up, and no model in a recorded session ever read one back."""
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
    """How much of the context window one conversation message occupies.

    Counts what is actually *sent*, which is more than the message's prose. A turn that calls
    tools carries most of its weight in the tool calls' arguments and the results that come back,
    and a sizing routine that read only text blocks would look at a conversation of a hundred
    shell results and see almost nothing. That undercount is not academic: it is measured against
    the model's window to decide whether to fold history, and a fold that never triggers is how a
    context reaches the wall.

    An approximation either way — the encoding is one general tokenizer standing in for every
    model's own, and the provider's own framing is not modelled — so it is right for deciding
    *when* a conversation has grown too large, and not for deciding whether one more token fits.
    """
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
    for key in ("started_at", "completed_at", "duration_ms", "background_job_id"):
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
    """Return ``(worktree_root, is_git_repo)``. Walks up from the working
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


def _escape_to_dict(escape: Any) -> dict:
    """An :class:`~frank.runtime.boundary.Escape` as plain data. Its tuples become lists,
    because this is about to be JSON."""
    return {
        "reads": list(escape.reads),
        "writes": list(escape.writes),
        "network": escape.network,
    }


def _escape_from_dict(data: Any) -> Any:
    """The inverse. A value that is already an ``Escape`` is passed through, so this is safe on
    a gate that never left the process."""
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
    """One human-in-the-loop interaction a tool call needs before it can run: a
    permission prompt or an ``ask_user`` question. Surfaced by the preflight pass
    and carried in a ``SUSPENDED`` event; the durable pending-interaction record the
    executor persists is built from these, and a later answer resolves each by
    ``request_id``."""

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
        """Every field, as plain JSON-safe data.

        *Every* field, and that is the whole of a bug this used to have. A gate's dict is what
        crosses two boundaries — the suspension event a client draws the prompt from, and the
        durable plan a resumed turn is rebuilt out of — so a field omitted here does not degrade,
        it disappears. `reason` was missing and the sink read it off the other side, which
        crashed every turn that raised a permission gate; the widening was missing and an
        approval that arrived after the suspension recorded no grant, because the gate it came
        back to had forgotten what was being asked for.

        The two that are not already plain data are flattened here: a reason is a Pydantic
        model, and an escape is a frozen dataclass of tuples."""
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
    """The preflight verdict for one tool call. Exactly one shape holds: a hard ``refusal``
    (the tool never runs and the model gets this error), one or more pending ``gates`` (needs
    an answer), or neither (cleared: run it). The decision is computed once, here, so
    ``_execute_tool`` only ever carries out a verdict and can no longer approve anything
    itself."""

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
    """One call, as middleware sees it.

    Mutable arguments, deliberately: a layer that rewrites a path or injects a default is a
    legitimate use, and a frozen value would force every such layer to reconstruct the call.
    """

    name: str
    arguments: dict


@dataclass
class _ResolvedToolDecision:
    """The verdict a batch runner hands each tool: run it, deny it (with the exact
    error the gate would have produced), or — for ``ask_user`` — the answers to return.
    Produced from the preflight plans plus any human answers, on both the fresh and the
    resumed path, so ``_execute_tool`` only carries a decision out."""

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
