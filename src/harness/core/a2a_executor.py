"""A2A server adapter for the harness.

Bridges the harness's :class:`AgentRuntime` to the A2A protocol: a user
turn is an A2A *Task*, its progress is streamed as ``TaskStatusUpdateEvent`` /
``TaskArtifactUpdateEvent`` (via :class:`TaskUpdater`), and its deliverable is an
``Artifact``. Agents (spawned via A2A) become *related* A2A tasks sharing
the parent's ``contextId`` and referencing it through ``referenceTaskIds``; they
are persisted to the same :class:`TaskStore`, so each is independently fetchable
and replayable via ``tasks/get``.

The harness's own event vocabulary (text/thinking/tool calls/agent activity)
is carried as typed ``Part``s — ``TextPart`` for prose, ``DataPart`` for
everything structured (tool calls/results, sub-task lifecycle, permission
prompts) — so the live stream is fully A2A-shaped, not a bespoke side channel.
"""

import asyncio
import base64
import json
import logging
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from litellm import exceptions as litellm_exceptions

logger = logging.getLogger(__name__)

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.tasks import TaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    DataPart,
    Message,
    MessageSendParams,
    Part,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskIdParams,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)
from a2a.utils import new_task

from harness.core.agent import AgentRuntime, StreamEvent
from harness.core.annotation_stamping import annotation_image_blocks, normalize_annotation_payloads
from harness.core.events import tool_status_from_result
from harness.core.configuration import (
    AgentConfiguration,
    GlobalConfiguration,
    PromptLoader,
    load_agent_configuration,
)
from harness.core.background_store import get_background_job_store
from harness.core.file_leases import FileLeaseManager
from harness.core.models import find_model
from harness.core.session_workspaces import SessionWorkspace
from harness.core.skills import Skill

# The A2A convention is that an extension places its custom attributes under a single
# URI-namespaced key in the message `metadata` map, not as bare top-level keys — so they
# can never collide with another extension's. All of the harness's per-turn metadata
# lives under this one namespaced key as a nested object.
# (See https://a2a-protocol.org/latest/topics/extensions — "extensions should place
# custom attributes in the metadata map … using this URI-namespaced convention".)
DAISY_METADATA_KEY = "urn:daisy:ext:turn:v1"


class Metadata:
    """Field names inside the harness's turn-metadata object (the value stored under
    :data:`DAISY_METADATA_KEY`). The UI only sets ``WORKING_DIRECTORY`` /
    ``WORKSPACE_STRATEGY`` / ``PERMISSION_MODE``; the rest are set internally once the
    backend has resolved a session's isolated runtime checkout or is delegating."""

    WORKING_DIRECTORY = "workingDirectory"
    WORKSPACE_STRATEGY = "workspaceStrategy"
    RUNTIME_WORKING_DIRECTORY = "runtimeWorkingDirectory"
    PROJECT_DIRECTORY = "projectDirectory"
    # The project this turn runs in; its locations are resolved server-side and the
    # agent addresses them per tool call via `location`.
    PROJECT_ID = "projectId"
    PERMISSION_MODE = "permissionMode"
    # Marks an agent call delegated from another agent (one-shot, fresh state),
    # optionally forced read-only, and how many hops deep it is.
    DELEGATED = "delegated"
    READ_ONLY = "readOnly"
    DEPTH = "depth"
    # Marks a harness-initiated turn (not user input): an autonomous background wake, or
    # an on-demand compaction pass. See AUTONOMOUS_RESUME_KIND / COMPACTION_KIND below.
    AUTONOMOUS_RESUME = "autonomousResume"
    COMPACTION = "compaction"


def _harness_metadata(message) -> dict:
    """The harness turn-metadata object carried by an incoming message (the nested value
    under :data:`DAISY_METADATA_KEY`), or ``{}`` when absent."""
    raw = (getattr(message, "metadata", None) or {}).get(DAISY_METADATA_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _harness_metadata_envelope(fields: dict) -> dict:
    """Wrap harness turn fields under the namespaced key for an outgoing message; drops
    keys whose value is ``None`` so the object only carries what was actually set."""
    return {DAISY_METADATA_KEY: {key: value for key, value in fields.items() if value is not None}}


# DataPart discriminator: every structured part declares its kind in `data.kind`.
PART_KIND = "kind"

# Harness-authored, model-facing notes live as markdown in prompts/, not as string
# literals in code — same loader the agent runtime uses for its templates.
_PROMPTS = PromptLoader(Path(__file__).parent / "prompts")

# An agent's events that are surfaced in the parent's agents panel (relayed with a
# path prefix). Everything else a child emits — token_usage, permission/question,
# compaction, steering — is its private bookkeeping and stays out of the parent stream.
_RELAYABLE_CHILD_KINDS = frozenset({
    "text", "thinking", "thinking_done", "status",
    "tool_call", "tool_result", "mcp_event", "group_started", "error",
})
# A user turn whose input is an artifact interaction carries it as a DataPart of
# this kind rather than as prose, so the payload reaches the model intact.
ARTIFACT_EVENT_KIND = "artifact_event"
# Kind that opens an on-demand compaction turn (Metadata.COMPACTION marks the turn). It
# runs no model turn — it summarizes older history and emits the compaction parts — so
# it is modelled like an autonomous wake: an agent-role message with one prose-less part.
COMPACTION_KIND = "compaction_request"
# DataPart kind that opens an autonomous wake task. A2A has no "system/harness"
# message role — only `user` and `agent` — so a harness-initiated turn is modelled
# honestly as an *agent* message (the agent resumed itself) carrying this single,
# prose-less part. It renders as nothing (the reducer ignores unknown kinds), so the
# wake never fabricates a user message; the framing note reaches only the model, as a
# system note. This is the explicit marker the transcript is keyed on, not a heuristic.
AUTONOMOUS_RESUME_KIND = "autonomous_resume"
# The system note that opens an autonomous wake turn lives in
# prompts/background_resume_note.md (loaded via _PROMPTS). The actual result is drained
# into the conversation as a `background_result` message before the model runs, so the
# note only frames why the model is being re-engaged.


def _artifact_event_payload(message) -> Optional[dict]:
    """The incoming artifact-interaction DataPart, if this turn carries one. Returns
    ``None`` when the message has no artifact event, so the caller falls back to the
    plain text input."""
    for part in (message.parts or []):
        root = getattr(part, "root", part)
        if isinstance(root, DataPart) and root.data.get(PART_KIND) == ARTIFACT_EVENT_KIND:
            return {
                "artifact_id": root.data.get("artifactId", ""),
                "title": root.data.get("title", ""),
                "event": root.data.get("event", ""),
                "data": root.data.get("data"),
            }
    return None


def _structured_data_payloads(message) -> list[dict]:
    """Return non-artifact DataPart payloads carried by the user turn."""
    payloads: list[dict] = []
    for part in (message.parts or []):
        root = getattr(part, "root", part)
        if isinstance(root, DataPart):
            data = root.data
            if data.get(PART_KIND) == ARTIFACT_EVENT_KIND:
                continue
            payloads.append(dict(data))
    return payloads


# Attachments whose mime type starts with this are viewable by a vision model, so
# they are inlined into the turn as image content blocks. Everything else stays a
# text-only reference (path + metadata) the model opens with its file tools.
_INLINE_IMAGE_MIME_PREFIX = "image/"
# A generous ceiling on an inlined image so a huge upload cannot blow up the
# request (and the persisted conversation it becomes part of). Larger images are
# left as text references rather than inlined.
_MAXIMUM_INLINE_IMAGE_BYTES = 20 * 1024 * 1024


def _image_attachments(structured_payloads: list[dict]) -> list[dict]:
    """Every image attachment carried by the turn's ``attachments`` parts."""
    images: list[dict] = []
    for payload in structured_payloads:
        if payload.get(PART_KIND) != "attachments":
            continue
        for attachment in payload.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            if str(attachment.get("mime_type", "")).startswith(_INLINE_IMAGE_MIME_PREFIX):
                images.append(attachment)
    return images


def _model_supports_vision(model_identifier: str) -> bool:
    if not model_identifier:
        return True
    model = find_model(model_identifier)
    if model is None:
        return True
    return model.vision


def _attachment_warning_payload(image_count: int, model_identifier: str) -> dict[str, str]:
    plural = "s" if image_count != 1 else ""
    return {
        "kind": "warning",
        "code": "image_metadata_only",
        "title": "Image attached as metadata only",
        "message": (
            f"{image_count} image{plural} attached, but {model_identifier or 'the agent model'} "
            "does not advertise vision support. The file metadata and path were provided to the model, "
            "but the image pixels were not inlined. Configure a vision-capable model for this agent if it needs to inspect the image directly."
        ),
    }


def _image_content_block(attachment: dict) -> Optional[dict]:
    """Build an OpenAI-shaped ``image_url`` content block from an attachment by
    reading the stored file and base64-encoding it as a data URI. Returns ``None``
    when the file is missing, unreadable, or too large to inline."""
    path = str(attachment.get("path") or "")
    if not path:
        return None
    mime_type = str(attachment.get("mime_type") or "application/octet-stream")
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) > _MAXIMUM_INLINE_IMAGE_BYTES:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}}

# Each agent profile is served as its own A2A agent under this prefix.
AGENT_RPC_PREFIX = "/a2a/agents"


def agent_rpc_path(agent_name: str) -> str:
    return f"{AGENT_RPC_PREFIX}/{agent_name}"


def build_agent_card(configuration: AgentConfiguration, available_skills: list[Skill], base_url: str) -> AgentCard:
    """Compile an agent's markdown definition into its A2A AgentCard — the
    A2A-native way to broadcast an agent's identity and capabilities. Each
    profile becomes an independently addressable, discoverable A2A agent.

    The agent's available skills (discovered from the skills directory) are
    advertised on the card; if there are none, a single default skill describing
    the agent's role is synthesised so the card always carries at least one skill.
    """
    display_name = configuration.display_name
    capability = (
        "Investigates and reports read-only — cannot modify the system."
        if configuration.permission_mode == "read_only"
        else "Can read and modify the system."
    )
    skills = [
        AgentSkill(
            id=skill.identifier,
            name=skill.identifier,
            description=skill.description or skill.display_title,
            tags=["harness", "skill"],
        )
        for skill in available_skills
    ]
    if not skills:
        skills.append(
            AgentSkill(
                id=configuration.identifier,
                name=configuration.identifier,
                description=(configuration.description or display_name) + f" {capability}",
                tags=["harness", configuration.permission_mode, configuration.model or "unconfigured-model"],
                examples=[f"Ask {display_name} to help with a task in its domain."],
            )
        )
    return AgentCard(
        name=configuration.identifier,
        description=configuration.description or f"The '{display_name}' agent.",
        url=f"{base_url.rstrip('/')}{agent_rpc_path(configuration.identifier)}",
        version="1.0.0",
        protocol_version="0.3.0",
        preferred_transport="JSONRPC",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True, state_transition_history=True),
        skills=skills,
    )


def _text_part(text: str) -> Part:
    return Part(root=TextPart(text=text))


def _data_part(kind: str, **fields) -> Part:
    return Part(root=DataPart(data={PART_KIND: kind, **fields}))


# Fields in a tool result that are guidance addressed to the model, or bulk payload it
# reads from the conversation — never something the transcript should render. They are
# stripped from `display` so the wire (live and on replay) carries only what the UI shows.
_MODEL_ONLY_RESULT_KEYS = frozenset({"hint", "note"})

# Per-tool heavy payloads the UI summarizes (a count, a range) rather than dumping: the
# model still reads them from the conversation; the client never needs the raw blob.
_HEAVY_RESULT_KEYS: dict[str, frozenset[str]] = {
    "computer": frozenset({"elements", "changes_since_last_observe"}),
    "browser": frozenset({"elements"}),
    "read_file": frozenset({"content"}),
}


def _project_display(tool_name: str, result: object) -> object:
    """Trim a tool result down to the UI-facing view. The model reads the untrimmed
    result from the conversation; only this projection reaches the client, so
    model-directed guidance and bulk payloads never leak into the transcript."""
    if not isinstance(result, dict):
        return result
    drop = _MODEL_ONLY_RESULT_KEYS | _HEAVY_RESULT_KEYS.get(tool_name, frozenset())
    return {key: value for key, value in result.items() if key not in drop}


def _tool_result_part(tool_name: str, tool_call_id: str, result: object) -> Part:
    """The unified ``tool_result`` wire event for a root-agent tool. Lifecycle is the
    explicit ``status``; ``display`` is the projected payload the UI renders (the
    model-facing view travels only in the conversation). ``metadata`` here is the minimal
    display correlation — full timing rides the model envelope."""
    record = result if isinstance(result, dict) else {}
    return _data_part(
        "tool_result",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        status=tool_status_from_result(result).value,
        code=record.get("code"),
        display=_project_display(tool_name, result),
        metadata={"tool_name": tool_name, "tool_call_id": tool_call_id},
    )


def _provider_error_body(error: object) -> dict:
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        return body
    response = getattr(error, "response", None)
    if response is None:
        return {}
    try:
        parsed = response.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _provider_error_code(error: object) -> str:
    code = getattr(error, "code", None)
    if code:
        return str(code)
    body = _provider_error_body(error)
    nested = body.get("error") if isinstance(body.get("error"), dict) else body
    return str(nested.get("code") or "") if isinstance(nested, dict) else ""


def _provider_status_code(error: object) -> int | None:
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _safe_turn_error(error: object, had_images: bool = False) -> dict[str, object]:
    """Classify a turn-level failure without exposing raw provider/tool text.

    ``had_images`` marks a turn that carried an attached image, so a provider's
    generic 400 is reframed into the real cause the user can act on: a text-only
    model cannot read images."""
    status_code = _provider_status_code(error)
    provider_code = _provider_error_code(error)
    fields: dict[str, object] = {}
    if status_code is not None:
        fields["status"] = status_code
    if provider_code:
        fields["providerCode"] = provider_code

    # A turn with an image that the provider rejects almost always means the
    # agent model is text-only — the most common, most actionable cause, and one
    # the raw "invalid request" gives no hint of.
    if had_images and (isinstance(error, litellm_exceptions.BadRequestError) or status_code == 400):
        return {
            **fields,
            "code": "image_unsupported",
            "title": "This model can't read images",
            "message": "The agent's model rejected the attached image — it looks like a text-only model. Configure a vision-capable model for this agent and try again.",
        }

    if isinstance(error, litellm_exceptions.RateLimitError) or status_code == 429:
        return {
            **fields,
            "code": "rate_limited",
            "title": "Model rate limit reached",
            "message": "The selected provider is rate limiting requests. Wait a bit or switch to another model.",
        }
    if isinstance(error, litellm_exceptions.AuthenticationError) or status_code in {401, 403}:
        return {
            **fields,
            "code": "authentication_failed",
            "title": "Provider credentials need attention",
            "message": "The selected provider rejected the configured credentials. Check the API key or choose another model.",
        }
    if isinstance(error, (litellm_exceptions.ServiceUnavailableError, litellm_exceptions.InternalServerError)) or status_code in {500, 502, 503, 504}:
        return {
            **fields,
            "code": "provider_unavailable",
            "title": "Model temporarily unavailable",
            "message": "The agent's model provider is temporarily unavailable. Try again in a moment or configure a different model for this agent.",
        }
    if isinstance(error, (litellm_exceptions.APIConnectionError, litellm_exceptions.Timeout, TimeoutError)) or status_code == 408:
        return {
            **fields,
            "code": "connection_failed",
            "title": "Connection interrupted",
            "message": "The model connection dropped before the turn finished. Check the connection and retry.",
        }
    if isinstance(error, litellm_exceptions.BadRequestError) or status_code == 400:
        request_too_large_codes = {"context_length_exceeded", "context_length_error", "input_too_large"}
        if provider_code in request_too_large_codes:
            return {
                **fields,
                "code": "request_too_large",
                "title": "Request is too large",
                "message": "The model could not accept this much context. Start a smaller follow-up or switch to a model with more capacity.",
            }
        return {
            **fields,
            "code": "request_rejected",
            "title": "Model rejected the request",
            "message": "The model could not accept this turn. Adjust the request or switch models.",
        }
    return {
        **fields,
        "code": "turn_failed",
        "title": "Turn could not complete",
        "message": "The turn stopped unexpectedly. The raw details were written to the server log.",
    }


class _TextPartBuffer:
    """Coalesce adjacent text chunks before publishing A2A task updates.

    The buffering is intentionally at the semantic event layer, not the SSE/ASGI
    layer: structured parts such as tool calls, status changes, and agent
    lifecycle events must force a flush so replay order remains exact.
    """

    def __init__(
        self,
        emit: Callable[[tuple[str, ...], str], Awaitable[None]],
        *,
        flush_interval: float = 0.041667,
        flush_size: int = 512,
    ):
        self._emit = emit
        self._flush_interval = flush_interval
        self._flush_size = flush_size
        self._key: tuple[str, ...] | None = None
        self._chunks: list[str] = []
        self._length = 0
        self._last_flush = 0.0

    async def push(self, text: str, key: tuple[str, ...] = ()) -> None:
        if not text:
            return
        if self._chunks and self._key != key:
            await self.flush(force=True)
        if not self._chunks:
            self._key = key
            self._last_flush = asyncio.get_running_loop().time()
        self._chunks.append(text)
        self._length += len(text)
        await self.flush()

    async def flush(self, force: bool = False) -> None:
        if not self._chunks or self._key is None:
            return
        now = asyncio.get_running_loop().time()
        if not force and self._length < self._flush_size and now - self._last_flush < self._flush_interval:
            return
        key = self._key
        text = "".join(self._chunks)
        self._key = None
        self._chunks = []
        self._length = 0
        self._last_flush = now
        await self._emit(key, text)


class HarnessAgentExecutor(AgentExecutor):
    """The A2A executor for a single agent profile. One of these is served per
    agent, so each agent is an independently addressable A2A endpoint."""

    def __init__(
        self,
        agent_name: str,
        global_configuration: GlobalConfiguration,
        task_store: TaskStore,
        pending_permissions: dict[str, asyncio.Future],
        pending_questions: dict[str, asyncio.Future],
        registry: Optional["AgentRegistry"] = None,
        on_new_context: Optional[Callable[..., Any]] = None,
        conversations: Optional[dict[str, list]] = None,
        on_turn_state: Optional[Callable[..., Any]] = None,
        on_permission_state: Optional[Callable[..., Any]] = None,
        load_conversation: Optional[Callable[..., Any]] = None,
        save_conversation: Optional[Callable[..., Any]] = None,
        session_permission_mode_for: Optional[Callable[..., Any]] = None,
        on_stream_event: Optional[Callable[..., Any]] = None,
        file_lease_manager: FileLeaseManager | None = None,
        ensure_session_workspace: Optional[Callable[..., Any]] = None,
        ensure_mcp_servers: Optional[Callable[..., Any]] = None,
        resolve_locations: Optional[Callable[..., Any]] = None,
        capture_artifacts: Optional[Callable[..., Any]] = None,
    ):
        self._agent_name = agent_name
        self._global_configuration = global_configuration
        self._task_store = task_store
        self._pending_permissions = pending_permissions
        self._pending_questions = pending_questions
        self._registry = registry
        self._on_new_context = on_new_context
        # Persist/restore the dialogue history so a session keeps its context across
        # a server restart. ``_conversations`` is in-memory; without these the agent
        # would resume a reopened session with an empty history while the UI still
        # replays the transcript from the task store, silently losing all context.
        self._load_conversation = load_conversation
        self._save_conversation = save_conversation
        # Resolves a context's persisted per-session permission mode so resumed
        # sessions keep approval behavior after a runtime rebuild.
        self._session_permission_mode_for = session_permission_mode_for
        # Notified (context_id, running) when a top-level turn starts/ends, so the
        # server can track which sessions are active and show a sidebar spinner.
        self._on_turn_state = on_turn_state
        # Notified (context_id) when a turn raises a permission request, so the
        # sidebar can swap the spinner for an attention marker on that session.
        self._on_permission_state = on_permission_state
        # Notified (context_id, part) for every structured part a turn emits, after
        # it is persisted. The server fans these out to non-driving viewers over a
        # live SSE stream so they follow the turn in real time (O(delta)) instead of
        # polling the task store and re-replaying the whole transcript (O(N)).
        self._on_stream_event = on_stream_event
        self._file_lease_manager = file_lease_manager
        self._ensure_session_workspace = ensure_session_workspace
        self._ensure_mcp_servers = ensure_mcp_servers
        # Resolves a project_id to its serialized locations (uri/name/kind/base_directory/perms),
        # so a turn's tools can address any of the project's locations. Provided by the
        # server, which owns the projects DB.
        self._resolve_locations = resolve_locations
        # Enqueues a shadow-git capture of what a write-ish tool call produced (the agent
        # runtime calls this after edit/write/bash and on open_artifact). Non-blocking and
        # best-effort — the server owns the queue, worker, and history.db, so the runtime
        # stays free of any direct database or git dependency.
        self._capture_artifacts = capture_artifacts
        # One runtime per context preserves the conversation across turns.
        self._runtimes: dict[str, AgentRuntime] = {}
        self._aborts: dict[str, AgentRuntime] = {}
        self._active_contexts: set[str] = set()
        # Dialogue history keyed by context, shared across *all* agent executors
        # in the process. The persona (system prompt) is applied per-turn on top
        # of this, so switching agents mid-session continues the same conversation
        # with a different persona rather than starting over.
        self._conversations: dict[str, list] = conversations if conversations is not None else {}
        # Serializes turns per context so a user turn and an autonomous background
        # wake never drive the same (shared) runtime concurrently. Delegated
        # agent turns share their parent's context and run *inside* the parent
        # turn, so they deliberately do not take this lock (that would deadlock).
        self._context_locks: dict[str, asyncio.Lock] = {}
        # One background-resume pump per context: after a turn ends with work still
        # in flight, the pump waits (event-driven) for each result and drives an
        # autonomous turn to deliver it, so the agent wakes itself up.
        self._resume_pumps: dict[str, asyncio.Task] = {}
        # Contexts whose cached runtime a reset asked to drop but which still had
        # background work in flight. Evicting such a runtime strands the pending
        # result — the resume pump keys off the live runtime, and the durable store
        # is only replayed on startup / the next user turn — so the drop is deferred
        # until the runtime is idle (see `_maybe_evict`).
        self._runtimes_pending_reset: set[str] = set()
        # Holds startup-recovery wake tasks so they are not garbage-collected before
        # they finish driving their turn.
        self._startup_resume_tasks: set[asyncio.Task] = set()
        # Holds in-flight manual-compaction turns for the same reason.
        self._compaction_tasks: set[asyncio.Task] = set()

    def compact_context(self, context_id: str) -> bool:
        """Trigger a manual compaction of a context's conversation (the user pressed
        the compact button). Runs as a background turn — serialized behind any active
        turn via the per-context lock — so the endpoint returns immediately while the
        compaction streams to the UI.

        A live runtime is not required: the compaction turn goes through the normal
        turn path, which rehydrates a persisted conversation into a fresh runtime, so
        a session reopened after a restart compacts without a warm-up message. The
        caller is responsible for routing to the owning agent's executor. Returns
        False only when there is no handler to drive the turn."""
        handler = self._registry._handlers.get(self._agent_name) if self._registry is not None else None
        if handler is None:
            return False
        task = asyncio.create_task(self._run_compaction_turn(context_id))
        self._compaction_tasks.add(task)
        task.add_done_callback(self._compaction_tasks.discard)
        return True

    async def _run_compaction_turn(self, context_id: str) -> None:
        """Drive one manual-compaction turn via a self-sent agent-role message
        carrying only the prose-less compaction part, so it is a real, persisted,
        replayable task streamed to viewers like any other turn."""
        handler = self._registry._handlers.get(self._agent_name) if self._registry is not None else None
        if handler is None:
            return
        message = Message(
            role=Role.agent,
            parts=[_data_part(COMPACTION_KIND)],
            message_id=uuid.uuid4().hex,
            context_id=context_id,
            metadata=_harness_metadata_envelope({Metadata.COMPACTION: True}),
        )
        async for _event in handler.on_message_send_stream(MessageSendParams(message=message)):
            pass

    def abort_context(self, context_id: str) -> bool:
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            return False
        runtime.abort()
        return True

    def abort_tool(self, context_id: str, tool_call_identifier: str) -> bool:
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            return False
        return runtime.abort_tool(tool_call_identifier)

    def send_tool_to_background(self, context_id: str, tool_call_identifier: str) -> bool:
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            return False
        return runtime.send_tool_to_background(tool_call_identifier)

    def background_snapshots(self, context_id: str) -> list[dict]:
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            return []
        return runtime.background_snapshots()

    def reset_runtimes(self) -> None:
        """Drop cached runtimes so the next turn rebuilds them — used after a
        configuration change (e.g. new API credentials, an mcp.json edit) so it takes
        effect without a restart. Eviction is deferred for any context that still has
        background work in flight, so a reset in one session never strands another
        session's in-flight result: the resume pump keys off the live runtime, and the
        durable store is only replayed on startup / the next user turn, so dropping the
        runtime mid-flight would silently hang that session. In-flight turns keep their
        own runtime reference and are unaffected."""
        for context_id in list(self._runtimes.keys()):
            self._runtimes_pending_reset.add(context_id)
            self._maybe_evict(context_id)

    def set_permission_mode(self, context_id: str, mode: str) -> bool:
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            return False
        runtime.set_permission_mode(mode)
        return True

    def steer_context(self, context_id: str, message: str) -> bool:
        if context_id not in self._active_contexts:
            return False
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            return False
        return runtime.enqueue_steering(message)

    def reset_runtime(self, context_id: str) -> None:
        """Drop a single context's cached runtime so the next turn rebuilds it —
        used when the session's model override changes, so the new model takes
        effect without a server restart. Deferred while the context still has
        background work in flight, so evicting the runtime never strands a completed
        result the resume pump still needs to deliver (which would hang the session)."""
        self._runtimes_pending_reset.add(context_id)
        self._maybe_evict(context_id)

    def _maybe_evict(self, context_id: str) -> None:
        """Apply a deferred runtime reset once the runtime is idle. A reset requested
        while the context still had background work in flight is held back (the live
        resume pump must keep delivering results, since the durable store is only
        replayed on startup / the next user turn). Dropping the runtime here, only
        after its jobs have drained, lets the next turn rebuild with the new
        configuration without ever orphaning an undelivered result."""
        if context_id not in self._runtimes_pending_reset:
            return
        runtime = self._runtimes.get(context_id)
        if runtime is not None and runtime.background_jobs.has_pending():
            return  # still delivering — keep the runtime (and its pump) alive
        self._runtimes.pop(context_id, None)
        self._runtimes_pending_reset.discard(context_id)

    def _context_lock(self, context_id: str) -> asyncio.Lock:
        lock = self._context_locks.get(context_id)
        if lock is None:
            lock = asyncio.Lock()
            self._context_locks[context_id] = lock
        return lock

    def _arm_resume_pump(self, context_id: str, runtime: Optional[AgentRuntime] = None) -> None:
        """Ensure a resume pump watches this context while it has background work in
        flight. Idempotent — a live pump is left running. ``runtime`` is the runtime
        the just-finished turn actually used; it is passed explicitly rather than
        re-read from ``self._runtimes`` so a concurrent reset that cleared the cache
        entry cannot strand the pending work between the turn ending and the pump
        arming. If that runtime still has pending jobs but its cache slot is empty
        (a reset dropped it), restore it — marked for a deferred reset — so both the
        pump and the autonomous delivery drain the very same ``BackgroundJobs``."""
        runtime = runtime or self._runtimes.get(context_id)
        if runtime is None or not runtime.background_jobs.has_pending():
            return
        if self._runtimes.get(context_id) is None:
            self._runtimes[context_id] = runtime
            self._runtimes_pending_reset.add(context_id)
        existing = self._resume_pumps.get(context_id)
        if existing is not None and not existing.done():
            return
        self._resume_pumps[context_id] = asyncio.create_task(self._resume_pump(context_id))

    async def _resume_pump(self, context_id: str) -> None:
        """Wait (event-driven, at zero cost) for each background result to land while
        the context is otherwise idle, driving an autonomous turn to deliver each. It
        loops until nothing is pending, then retires."""
        try:
            while True:
                runtime = self._runtimes.get(context_id)
                if runtime is None or not runtime.background_jobs.has_pending():
                    return
                await runtime.background_jobs.wait_for_completion()
                # A result landed — drive an autonomous turn to deliver it. If a
                # concurrent user turn already drained it, that turn no-ops.
                await self._run_autonomous_turn(context_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background-resume pump failed for context %s", context_id)
        finally:
            self._resume_pumps.pop(context_id, None)
            # The context is idle now, so any reset deferred while it had work in
            # flight can finally take effect (rebuilding with the new configuration).
            self._maybe_evict(context_id)

    async def _run_autonomous_turn(self, context_id: str) -> None:
        """Start a turn the user did not initiate, to deliver a completed background
        result. It reuses the ordinary turn path via a self-sent A2A message, so the
        wake is a real, persisted, replayable task streamed to viewers like any other
        turn — the agent genuinely picks the work back up on its own."""
        handler = self._registry._handlers.get(self._agent_name) if self._registry is not None else None
        if handler is None:
            return
        # Nothing left to deliver — a concurrent user turn already drained the result
        # while the pump was scheduling this wake — so don't even mint a task. The
        # executor re-checks this under the per-context lock (that's the authoritative
        # guard against the race); short-circuiting here just avoids creating an empty,
        # invisible wake task in the common case.
        runtime = self._runtimes.get(context_id)
        has_live_result = runtime is not None and runtime.background_jobs.has_completed_undelivered()
        has_stored_result = get_background_job_store().has_undelivered_jobs(context_id, self._agent_name)
        if not has_live_result and not has_stored_result:
            return
        # An agent-authored message (the agent resumed itself), carrying only the
        # prose-less `autonomous_resume` part. Modelling it as `agent` rather than
        # `user` keeps the persisted A2A task honest — no consumer, ours or an
        # external one, sees a user message the user never sent — and the part renders
        # as nothing. The framing note reaches only the model, injected as a system
        # note in `execute`. It is still a real, replayable task streamed to viewers.
        message = Message(
            role=Role.agent,
            parts=[_data_part(AUTONOMOUS_RESUME_KIND)],
            message_id=uuid.uuid4().hex,
            context_id=context_id,
            metadata=_harness_metadata_envelope({Metadata.AUTONOMOUS_RESUME: True}),
        )
        async for _event in handler.on_message_send_stream(MessageSendParams(message=message)):
            # Drive to completion; viewers receive the turn through the stream-event
            # fan-out and the persisted task, so the yielded events are not needed here.
            pass

    def _build_runtime(
        self,
        context_id: str,
        working_directory: str,
        project_directory: str,
        conversation: Optional[list] = None,
        is_agent: bool = False,
        locations: Optional[list[dict]] = None,
    ) -> AgentRuntime:
        configuration = load_agent_configuration(
            self._agent_name, self._global_configuration.agent_directories_for(project_directory)
        )
        runtime = AgentRuntime(
            agent_configuration=configuration,
            global_configuration=self._global_configuration,
            pending_permissions=self._pending_permissions,
            pending_questions=self._pending_questions,
            session_id=context_id,
            conversation=conversation,
            working_directory=working_directory or "",
            project_directory=project_directory or working_directory or "",
            is_agent=is_agent,
            file_lease_manager=self._file_lease_manager,
            locations=locations,
        )
        if self._registry is not None:
            runtime.set_delegate(self._registry.make_delegate(context_id))
            runtime.set_cancel_delegated(self._registry.cancel_delegated)
        runtime.set_task_reader(self._make_task_reader())
        if self._capture_artifacts is not None:
            runtime.set_artifact_capture(self._capture_artifacts)
        return runtime

    def _make_task_reader(self):
        async def read_task(task_id: str):
            if not task_id:
                return None
            task = await self._task_store.get(task_id)
            # Exclude `history` for the same reason as the delegate's done payload:
            # read_task feeds a sibling/agent task straight into the caller's
            # model context, and the full transcript (incl. raw web-search text)
            # would overflow it. Only the status + deliverable artifact are needed.
            return task.model_dump(by_alias=True, exclude_none=True, mode="json", exclude={"history"}) if task else None
        return read_task

    async def _runtime_for(self, context_id: str, workspace: SessionWorkspace) -> AgentRuntime:
        # Apply any reset that was deferred while this context had background work in
        # flight: if the runtime has since gone idle, drop it now so this turn rebuilds
        # it with the new configuration rather than reusing the stale one.
        self._maybe_evict(context_id)
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            # Restore a persisted conversation the first time a context is seen this
            # process (e.g. a session reopened after a restart), so the agent resumes
            # with the same history the UI is replaying rather than a blank slate.
            if context_id not in self._conversations and self._load_conversation is not None:
                restored = await asyncio.to_thread(self._load_conversation, context_id)
                if restored:
                    self._conversations[context_id] = restored
            # Seed from (and bind to) the process-wide dialogue history for this
            # context — the same list object another agent may have been writing
            # to — so a persona switch picks up exactly where the last turn left off.
            conversation = self._conversations.setdefault(context_id, [])
            locations = None
            if self._resolve_locations is not None:
                locations = await asyncio.to_thread(self._resolve_locations, context_id) or None
            runtime = self._build_runtime(
                context_id,
                workspace.runtime_working_directory,
                workspace.source_working_directory,
                conversation=conversation,
                locations=locations,
            )
            if self._session_permission_mode_for is not None:
                runtime.set_permission_mode(await asyncio.to_thread(self._session_permission_mode_for, context_id))
            self._runtimes[context_id] = runtime
            # First time this process builds a runtime for the context: replay any
            # background results the durable store holds but never delivered (e.g.
            # a parse that finished, or was interrupted, across a restart), so the
            # model sees them as soon as this — or the autonomous wake — runs.
            self._replay_stored_background_results(context_id, runtime)
        # A context's working directory is fixed at creation — a session stays
        # bound to the folder it was started in, so later turns never repoint it.
        return runtime

    def _replay_stored_background_results(self, context_id: str, runtime: AgentRuntime) -> None:
        store = get_background_job_store()
        for job in store.undelivered_jobs(context_id, self._agent_name):
            runtime.inject_stored_background_result(
                kind=job["kind"],
                identifier=job["job_id"],
                tool_call_identifier=job["tool_call_id"],
                result=job["result"] or "",
            )
            store.mark_delivered(job["job_id"])

    async def resume_pending_on_startup(self) -> None:
        """After a restart, recover this agent's persisted background work. A job that
        was still running cannot be resurrected as a live coroutine, so it is recorded
        as interrupted (the model is told to re-run it if the result is still needed);
        then every context that now has a deliverable result is woken autonomously."""
        store = get_background_job_store()
        for job in store.running_jobs(self._agent_name):
            store.mark_abandoned(job["job_id"], json.dumps({
                "code": f"{job['kind']}_interrupted",
                "task_identifier": job["job_id"],
                "message": (
                    "This task was interrupted by a server restart before it finished. "
                    "Re-run it if the result is still needed."
                ),
                "arguments": job["arguments"],
            }))
        for context_id in store.contexts_with_undelivered(self._agent_name):
            wake_task = asyncio.create_task(self._run_autonomous_turn(context_id))
            self._startup_resume_tasks.add(wake_task)
            wake_task.add_done_callback(self._startup_resume_tasks.discard)

    async def _workspace_for(
        self,
        *,
        context_id: str,
        requested_working_directory: str,
        requested_workspace_strategy: str,
        requested_permission_mode: str,
        first_message: str,
        delegated: bool,
        metadata: dict,
    ) -> SessionWorkspace:
        if delegated:
            runtime_directory = str(
                metadata.get(Metadata.RUNTIME_WORKING_DIRECTORY)
                or metadata.get(Metadata.WORKING_DIRECTORY)
                or requested_working_directory
                or ""
            )
            project_directory = str(
                metadata.get(Metadata.PROJECT_DIRECTORY)
                or requested_working_directory
                or runtime_directory
            )
            return SessionWorkspace(
                source_working_directory=project_directory,
                runtime_working_directory=runtime_directory,
                strategy="worktree" if metadata.get(Metadata.RUNTIME_WORKING_DIRECTORY) else "none",
            )
        if self._ensure_session_workspace is not None:
            return await asyncio.to_thread(
                self._ensure_session_workspace,
                context_id,
                self._agent_name,
                requested_working_directory,
                requested_workspace_strategy,
                requested_permission_mode,
                first_message,
                str(metadata.get(Metadata.PROJECT_ID, "")),
            )
        directory = requested_working_directory or ""
        return SessionWorkspace(
            source_working_directory=directory,
            runtime_working_directory=directory,
            strategy="none",
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = context.get_user_input()
        message = context.message
        if message is None:
            raise ValueError("Request context message is required.")
        # An artifact interaction arrives as a structured DataPart. A normal
        # interaction becomes the turn's JSON input; a render_error is reframed
        # below (once the runtime's prompt loader exists) into a behind-the-scenes
        # self-realization note the model repairs as its own output.
        artifact_payload = _artifact_event_payload(message)
        structured_payloads = _structured_data_payloads(message)
        metadata = _harness_metadata(message)
        requested_working_directory = str(metadata.get(Metadata.WORKING_DIRECTORY, ""))
        requested_workspace_strategy = str(metadata.get(Metadata.WORKSPACE_STRATEGY, ""))
        permission_mode = str(metadata.get(Metadata.PERMISSION_MODE, ""))
        delegated = bool(metadata.get(Metadata.DELEGATED))
        autonomous = bool(metadata.get(Metadata.AUTONOMOUS_RESUME))
        compaction = bool(metadata.get(Metadata.COMPACTION))

        task = context.current_task
        if task is None:
            task = new_task(message)
            # Link a delegated child to its parent task so the relationship is
            # discoverable on the persisted A2A task.
            reference_task_ids = message.reference_task_ids
            if reference_task_ids:
                task.metadata = {**(task.metadata or {}), "referenceTaskIds": reference_task_ids}
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        # Serialize non-delegated turns per context so a user turn and an autonomous
        # background wake never drive the shared runtime concurrently. Delegated
        # agent turns share the parent's context and run inside it, so they must
        # not take this lock (that would deadlock against the parent).
        context_serialization_lock = None if delegated else self._context_lock(task.context_id)
        if context_serialization_lock is not None:
            await context_serialization_lock.acquire()

        final_text = ""
        failed_message = ""
        stop_reason = ""
        # Whether this turn carried an inlined image, so a provider rejection can be
        # reframed as the actionable "this model can't read images" case.
        turn_has_images = False
        runtime: AgentRuntime | None = None
        # Whether this turn has marked the context running/steerable. Set inside the
        # try (past the autonomous no-op check) so the finally only reverts state this
        # turn actually established. Everything after acquire() lives inside that try,
        # so no early return or raised exception can strand the per-context lock — a
        # leaked lock deadlocks every later turn on this session until a restart.
        track_running = False
        on_turn_state = self._on_turn_state

        async def emit(part: Part) -> None:
            await updater.update_status(TaskState.working, updater.new_agent_message([part]))
            # Fan the part out to non-driving viewers. publish runs AFTER the status
            # update has persisted, so a viewer's snapshot (taken at subscribe time)
            # plus the live tail are gap- and duplicate-free (see /sessions/.../stream).
            if self._on_stream_event is not None:
                self._on_stream_event(task.context_id, part)

        async def emit_compaction(event) -> None:
            """Map a runtime compaction event to its ``compaction`` DataPart, so both
            the manual pass and mid-turn auto-compaction render identically (a live
            "compacting" indicator, then the separator)."""
            if event.type == StreamEvent.Type.COMPACTION_STARTED:
                await emit(_data_part(
                    "compaction", status="started",
                    reason=event.data.get("reason", ""),
                    messages_before=event.data.get("messages_before", 0),
                    tokens_before=event.data.get("tokens_before", 0),
                ))
            elif event.type == StreamEvent.Type.COMPACTION_DONE:
                await emit(_data_part(
                    "compaction", status="done",
                    reason=event.data.get("reason", ""),
                    ok=event.data.get("ok", True),
                    messages_before=event.data.get("messages_before", 0),
                    messages_after=event.data.get("messages_after", 0),
                    tokens_before=event.data.get("tokens_before", 0),
                ))

        async def emit_text_buffer(_key: tuple[str, ...], text: str) -> None:
            await emit(_text_part(text))

        text_buffer = _TextPartBuffer(emit_text_buffer)

        async def flush_stream_buffers(force: bool = True) -> None:
            await text_buffer.flush(force=force)

        async def save_runtime_conversation() -> None:
            if not delegated and self._save_conversation is not None and runtime is not None:
                await asyncio.to_thread(self._save_conversation, task.context_id, runtime.conversation)

        # The runtime setup — building the agent runtime and its model client —
        # runs inside the try so any failure (e.g. missing API credentials) is
        # surfaced as a clean A2A `failed` status rather than escaping and tearing
        # down the SSE stream mid-flight.
        try:
            # An autonomous wake with nothing left to deliver — a concurrent user turn
            # already drained the result while this one waited on the lock — is a no-op:
            # close the task without a model call rather than emit an empty turn. The
            # finally still runs on this early return, releasing the lock.
            if autonomous:
                existing_runtime = self._runtimes.get(task.context_id)
                has_live_result = existing_runtime is not None and existing_runtime.background_jobs.has_completed_undelivered()
                has_stored_result = get_background_job_store().has_undelivered_jobs(task.context_id, self._agent_name)
                if not has_live_result and not has_stored_result:
                    await updater.complete()
                    return

            # A top-level user turn marks its session as running so the sidebar can
            # show a spinner and the turn becomes steerable. Delegated agent turns
            # run within their parent turn, which is already counted, so they are not
            # tracked separately.
            track_running = not delegated and on_turn_state is not None
            if track_running and on_turn_state is not None:
                on_turn_state(task.context_id, True)
                self._active_contexts.add(task.context_id)

            await updater.start_work()

            workspace = await self._workspace_for(
                context_id=task.context_id,
                requested_working_directory=requested_working_directory,
                requested_workspace_strategy=requested_workspace_strategy,
                requested_permission_mode=permission_mode,
                first_message=user_text,
                delegated=delegated,
                metadata=metadata,
            )
            if self._ensure_mcp_servers is not None and workspace.source_working_directory:
                await self._ensure_mcp_servers(workspace.source_working_directory)

            if delegated:
                # A delegated agent call is a fresh, one-shot run (no shared
                # conversation state with the parent turn). It inherits the whole
                # project — the same locations as the parent.
                sub_locations = None
                if self._resolve_locations is not None:
                    sub_locations = await asyncio.to_thread(self._resolve_locations, task.context_id) or None
                runtime = self._build_runtime(
                    task.context_id,
                    workspace.runtime_working_directory,
                    workspace.source_working_directory,
                    is_agent=True,
                    locations=sub_locations,
                )
                if Metadata.READ_ONLY in metadata:
                    runtime.set_read_only(bool(metadata[Metadata.READ_ONLY]))
            else:
                is_new_context = task.context_id not in self._runtimes
                runtime = await self._runtime_for(task.context_id, workspace)
                if permission_mode:
                    runtime.set_permission_mode(permission_mode)
                if is_new_context and self._on_new_context is not None:
                    await asyncio.to_thread(self._on_new_context, task.context_id)
            self._aborts[task.id] = runtime
            runtime.set_a2a_task_id(task.id)
            runtime.set_delegation_depth(int(metadata.get(Metadata.DEPTH, 0)))

            # A manual compaction turn runs no model turn: it summarizes the older
            # history in place and emits the compaction parts (the live indicator +
            # the separator), then completes. Persisted like any turn, so the
            # separator replays; fanned out, so viewers see it live.
            if compaction:
                async for compaction_event in runtime.compact(reason="manual"):
                    await emit_compaction(compaction_event)
                await save_runtime_conversation()
                await updater.complete()
                return

            # A render_error is injected as the model's own realization (a harness
            # note delivered in a <system-reminder> block), never as user prose;
            # every other artifact event is the turn's structured JSON input.
            as_system_note = autonomous
            if autonomous:
                # The wake message carries no prose (only an `autonomous_resume`
                # part); the framing note is supplied here and injected into the
                # model as a <system-reminder> harness note — cache-safe user-role
                # delivery (see AgentRuntime._harness_note_message) that never
                # appears as user input in the transcript.
                turn_input = _PROMPTS.load("background_resume_note", {})
            elif artifact_payload is not None:
                payload_json = json.dumps({"artifact_event": artifact_payload}, ensure_ascii=False)
                if artifact_payload.get("event") == "render_error":
                    # The same JSON payload, wrapped in a self-realization note and
                    # injected as a harness note rather than user input.
                    turn_input = runtime.artifact_render_error_note(payload_json)
                    as_system_note = True
                else:
                    turn_input = payload_json
            elif structured_payloads:
                # The structured metadata (attachment file paths and any per-attachment
                # annotations) always rides along as a text block so the model can act
                # on the attachments with its tools. Images are inlined only when the
                # agent model advertises vision support; otherwise the model gets
                # metadata/path access and the UI receives a non-fatal warning.
                # The model-facing copy of the data parts keeps annotation positions
                # on the normalized 0-999 grid documented in the system prompt.
                text_payload = json.dumps({
                    "text": user_text,
                    "data_parts": normalize_annotation_payloads(structured_payloads),
                }, ensure_ascii=False)
                image_attachments = _image_attachments(structured_payloads)
                model_identifier = runtime.effective_model_identifier if runtime is not None else ""
                image_blocks = []
                if image_attachments and _model_supports_vision(model_identifier):
                    image_blocks = [
                        block
                        for attachment in image_attachments
                        if (block := _image_content_block(attachment)) is not None
                    ]
                elif image_attachments:
                    await emit(_data_part(**_attachment_warning_payload(len(image_attachments), model_identifier)))
                if _model_supports_vision(model_identifier):
                    # Artifact-image annotations: alongside the structured facts, a
                    # vision model also gets the image itself with numbered circle
                    # badges stamped at each annotation position (numbers match the
                    # payload's `sequence` values). Pure Pillow work — run off-loop.
                    image_blocks.extend(
                        await asyncio.to_thread(annotation_image_blocks, structured_payloads)
                    )
                if image_blocks:
                    turn_input = [{"type": "text", "text": text_payload}, *image_blocks]
                    turn_has_images = True
                else:
                    turn_input = text_payload
            else:
                turn_input = user_text

            async for event in runtime.stream(turn_input, as_system_note=as_system_note):
                kind = event.type
                data = event.data
                if kind == StreamEvent.Type.TEXT_CHUNK:
                    await text_buffer.push(data.get("text", ""))
                elif kind == StreamEvent.Type.RELAYED:
                    # An agent's event, already in the unified vocabulary. Its `path`
                    # was extended with this agent's segment by the relay (see
                    # AgentRuntime._run_spawned_agent), so it renders in the agents panel;
                    # emit it verbatim — no per-kind re-encoding.
                    await flush_stream_buffers()
                    await emit(Part(root=DataPart(data=data["event"])))
                elif kind == StreamEvent.Type.THINKING:
                    await flush_stream_buffers()
                    await emit(_data_part("thinking", text=data.get("text", "")))
                elif kind == StreamEvent.Type.THINKING_DONE:
                    await flush_stream_buffers()
                    await emit(_data_part("thinking_done", duration_ms=data.get("duration_ms", 0)))
                elif kind == StreamEvent.Type.STATUS:
                    await flush_stream_buffers()
                    await emit(_data_part("status", code=data.get("code", "")))
                elif kind == StreamEvent.Type.TOOL_CALL:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "tool_call", tool_name=data.get("name", ""),
                        arguments=data.get("arguments", {}), tool_call_id=data.get("id", ""),
                    ))
                elif kind == StreamEvent.Type.TOOL_RESULT:
                    await flush_stream_buffers()
                    await emit(_tool_result_part(data.get("name", ""), data.get("id", ""), data.get("result")))
                elif kind == StreamEvent.Type.MCP_EVENT:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "mcp_event",
                        server=data.get("server", ""),
                        tool=data.get("tool", ""),
                        event=data.get("event", {}),
                        tool_call_id=data.get("id", ""),
                    ))
                elif kind == StreamEvent.Type.USAGE:
                    await flush_stream_buffers()
                    cumulative = data.get("cumulative", {})
                    agents = data.get("agents", {}) or {}
                    await emit(_data_part(
                        "token_usage",
                        input_tokens=data.get("input_tokens", 0),
                        output_tokens=data.get("output_tokens", 0),
                        context_window=data.get("context_window", 0),
                        cumulative={
                            "input_tokens": cumulative.get("input_tokens", 0),
                            "output_tokens": cumulative.get("output_tokens", 0),
                            "total_tokens": cumulative.get("total_tokens", 0),
                            "cache_read_tokens": cumulative.get("cache_read_tokens", 0),
                            "reasoning_tokens": cumulative.get("reasoning_tokens", 0),
                            "model_calls": cumulative.get("model_calls", 0),
                        },
                        agents={
                            "input_tokens": agents.get("input_tokens", 0),
                            "output_tokens": agents.get("output_tokens", 0),
                            "total_tokens": agents.get("total_tokens", 0),
                            "model_calls": agents.get("model_calls", 0),
                        },
                    ))
                elif kind == StreamEvent.Type.PERMISSION_REQUEST:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "permission_request", request_id=data.get("request_id", ""),
                        tool_call_id=data.get("id", ""),
                        command=data.get("command", ""), justification=data.get("justification", ""),
                        risk=data.get("risk", ""),
                    ))
                    # Let the sidebar flag this session as awaiting input.
                    if self._on_permission_state is not None:
                        self._on_permission_state(task.context_id)
                elif kind == StreamEvent.Type.QUESTION:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "question",
                        request_id=data.get("request_id", ""),
                        tool_call_id=data.get("id", ""),
                        questions=data.get("questions", []) or [],
                    ))
                    # A question is human-in-the-loop input, same as a permission.
                    if self._on_permission_state is not None:
                        self._on_permission_state(task.context_id)
                elif kind == StreamEvent.Type.ERROR:
                    await flush_stream_buffers()
                    failed_message = data.get("message", "error")
                    await emit(_data_part(
                        "error",
                        message=failed_message,
                        tool_call_id=data.get("id", ""),
                        tool_name=data.get("tool", ""),
                    ))
                elif kind == StreamEvent.Type.GROUP_STARTED:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "group_started",
                        path=[{"group_id": data.get("group_id", ""), "step_id": data.get("step_id", "")}],
                        agent_name=data.get("agent_name", ""),
                        title=data.get("title", ""),
                        tool_call_id=data.get("tool_call_id", ""),
                    ))
                elif kind == StreamEvent.Type.STEERING:
                    await flush_stream_buffers()
                    await emit(_data_part("steering", text=data.get("text", "")))
                elif kind in (StreamEvent.Type.COMPACTION_STARTED, StreamEvent.Type.COMPACTION_DONE):
                    await flush_stream_buffers()
                    await emit_compaction(event)
                elif kind == StreamEvent.Type.DONE:
                    await flush_stream_buffers()
                    final_text = data.get("text", "") or final_text
                    stop_reason = data.get("stop_reason", "") or stop_reason

            await flush_stream_buffers()

            if final_text.strip():
                await updater.add_artifact([_text_part(final_text)], name="result", last_chunk=True)
            await save_runtime_conversation()
            if stop_reason == "cancelled":
                # The user pressed Stop — end the task as canceled, not completed, so the
                # transcript and replay read it honestly as a stopped turn.
                await updater.cancel()
            elif failed_message and not final_text.strip():
                # failed_message carries raw, model-facing error text (e.g. a tool
                # exception) — never leak it to the user. Surface a safe category.
                await updater.failed(updater.new_agent_message([_data_part("error", **_safe_turn_error(failed_message))]))
            else:
                await updater.complete()
        except Exception as exception:  # noqa: BLE001 — surface any failure as A2A failed
            await save_runtime_conversation()
            # Log the real exception server-side for debugging, but show the user
            # only a safe category — never the raw exception text.
            logger.exception("Agent turn failed: %s", exception)
            await updater.failed(updater.new_agent_message([_data_part("error", **_safe_turn_error(exception, had_images=turn_has_images))]))
        finally:
            self._aborts.pop(task.id, None)
            # Persist the conversation after a top-level turn so a later restart can
            # restore it. Delegated agent runs have their own throwaway history
            # and don't touch the shared context, so they are not persisted. Only save
            # when there is something to save (a built runtime, or an already-cached
            # conversation) — an autonomous no-op wake has neither, and blindly saving
            # an empty list would clobber the persisted history.
            if not delegated and self._save_conversation is not None and (
                runtime is not None or task.context_id in self._conversations
            ):
                await asyncio.to_thread(
                    self._save_conversation,
                    task.context_id,
                    runtime.conversation if runtime is not None else self._conversations.get(task.context_id, []),
                )
            # Stop accepting steering for this context before draining the queue, then
            # discard anything that arrived too late to be honored (raced in after the
            # loop's final drain, or while the turn was ending/failing). Such messages
            # were never applied to the conversation; the client re-sends them as a
            # fresh turn on stream close, so leaving them queued here would double-
            # apply them when the next turn drains the runtime at its first boundary.
            if track_running:
                self._active_contexts.discard(task.context_id)
            cached_runtime = self._runtimes.get(task.context_id)
            if cached_runtime is not None:
                cached_runtime.discard_pending_steering()
            if track_running and on_turn_state is not None:
                on_turn_state(task.context_id, False)
            if context_serialization_lock is not None:
                context_serialization_lock.release()
            # If background work is still in flight after this turn, make sure a
            # resume pump is watching so the next completion wakes the agent on its
            # own. Pass the runtime this turn used (not a cache lookup) so a reset
            # that cleared the cache mid-turn cannot strand the pending work.
            # Delegated turns don't own the context lifecycle, so they skip it.
            if not delegated:
                self._arm_resume_pump(task.context_id, runtime)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or (context.current_task.id if context.current_task else "")
        runtime = self._aborts.get(task_id)
        if runtime is not None:
            runtime.abort()
        if context.current_task:
            updater = TaskUpdater(event_queue, context.current_task.id, context.current_task.context_id)
            await updater.cancel()


class AgentRegistry:
    """The A2A agent directory.

    Holds every served agent's request handler and AgentCard, and provides A2A
    *delegation*: one agent invokes another by sending it a real A2A message
    (``message/stream``) and receiving its task back. This is how agents
    communicate — there is no separate, custom inter-agent channel; an agent
    call is identical to an external client calling that agent's endpoint.
    """

    def __init__(self, task_store: TaskStore):
        self._task_store = task_store
        self._handlers: dict[str, RequestHandler] = {}
        self._cards: dict[str, AgentCard] = {}

    def register(self, name: str, handler: RequestHandler, card: AgentCard) -> None:
        self._handlers[name] = handler
        self._cards[name] = card

    def names(self) -> list[str]:
        return list(self._cards.keys())

    def cards(self) -> list[AgentCard]:
        return list(self._cards.values())

    def card(self, name: str) -> Optional[AgentCard]:
        return self._cards.get(name)

    async def cancel_delegated(self, agent_name: str, task_id: str) -> None:
        """Cancel a running agent's own A2A task (via its handler's tasks/cancel),
        so a parent Stop actually reaps the child instead of just abandoning the wait on
        it. A no-op if the child already finished."""
        handler = self._handlers.get(agent_name)
        if handler is not None and task_id:
            with suppress(Exception):
                await handler.on_cancel_task(TaskIdParams(id=task_id))

    def make_delegate(self, context_id: str):
        """Return a delegate bound to a context. Calling it invokes another agent
        as a related A2A task and yields its activity as it streams, ending with
        the child's final task."""

        async def delegate(
            agent_name: str,
            prompt: str,
            parent_task_id: str,
            read_only: Optional[bool] = None,
            depth: int = 1,
            working_directory: str = "",
            project_directory: str = "",
        ):
            handler = self._handlers.get(agent_name)
            if handler is None:
                yield {"type": "done", "child_task_id": "", "task": None}
                return
            turn_fields: dict = {Metadata.DELEGATED: True, Metadata.DEPTH: depth}
            if read_only is not None:
                turn_fields[Metadata.READ_ONLY] = bool(read_only)
            if project_directory:
                turn_fields[Metadata.WORKING_DIRECTORY] = project_directory
                turn_fields[Metadata.PROJECT_DIRECTORY] = project_directory
            if working_directory:
                turn_fields[Metadata.RUNTIME_WORKING_DIRECTORY] = working_directory
            message = Message(
                role=Role.user,
                parts=[Part(root=TextPart(text=prompt))],
                message_id=uuid.uuid4().hex,
                context_id=context_id,
                reference_task_ids=[parent_task_id] if parent_task_id else None,
                metadata=_harness_metadata_envelope(turn_fields),
            )
            child_task_id = ""
            async for event in handler.on_message_send_stream(MessageSendParams(message=message)):
                if isinstance(event, Task):
                    child_task_id = event.id
                    yield {"type": "started", "child_task_id": child_task_id}
                elif isinstance(event, TaskStatusUpdateEvent):
                    child_task_id = event.task_id or child_task_id
                    if event.status.message:
                        for part in event.status.message.parts:
                            root = part.root
                            # The child already speaks the unified vocabulary. Relay its
                            # panel-relevant events verbatim (the parent prepends the path);
                            # a plain TextPart becomes a `text` event. Everything else
                            # (token_usage, permission/question, compaction, steering) is
                            # the child's private bookkeeping and is not surfaced.
                            if isinstance(root, TextPart):
                                yield {"type": "event", "event": {PART_KIND: "text", "text": root.text}}
                            elif isinstance(root, DataPart) and root.data.get(PART_KIND) == "token_usage":
                                # Not a panel event — the parent folds the child's spend
                                # into its separate agent token bucket.
                                yield {"type": "usage", "event": dict(root.data)}
                            elif isinstance(root, DataPart) and root.data.get(PART_KIND) in _RELAYABLE_CHILD_KINDS:
                                yield {"type": "event", "event": dict(root.data)}
                elif isinstance(event, TaskArtifactUpdateEvent):
                    child_task_id = event.task_id or child_task_id
            final_task = await self._task_store.get(child_task_id) if child_task_id else None
            yield {
                "type": "done",
                "child_task_id": child_task_id,
                # Hand back only the agent's deliverable (status + result artifact),
                # never its `history`. The history holds every relayed event — including
                # full web-search page text — and the parent serializes this task into
                # its own model context as the spawn_agent result; injecting the whole
                # transcript overflows the context window. The live agents panel is fed
                # by the separate streamed events above, so it is unaffected, and the UI
                # result card only reads the artifact.
                "task": final_task.model_dump(by_alias=True, exclude_none=True, mode="json", exclude={"history"}) if final_task else None,
            }

        return delegate
