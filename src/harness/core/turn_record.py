"""The one typed value for a turn's durable control-state.

A turn's control-state — is it a user turn or a harness-initiated one, is it paused on a human
gate, which parent tasks it references, which agent-panel lane it belongs to — used to live as
bare string keys poked into the A2A ``Task.metadata`` dict and read back with ``metadata.get(...)``
three calls deep. :class:`TurnRecord` is where that state lives as one validated object: every
backend site reads ``record.pending.gates[0].request_id`` rather than
``metadata["pendingInteraction"]["gates"][0]["request_id"]``, and a missing or misshapen field is a
validation error at the boundary rather than a ``KeyError`` at the point of use.

Serialization is deliberately byte-compatible with the historical flat keys (``pendingInteraction``,
``daisyTurnKind``, ``referenceTaskIds``, ``agentLane``): ``referenceTaskIds`` and ``agentLane`` are
read by the web client off ``Task.metadata``, so the persisted/wire shape is unchanged — only the
in-process access is now typed. Reshaping these under a single namespaced key is a later, separate
step (see the typed-turn-core plan), not this one.

The large, write-hot conversation checkpoint stays out of this record — it lives in its own table,
keyed by context — so a ``TurnRecord`` is small and cheap to rewrite on every turn.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, Field

# Every turn-state key still lives at the top level of ``Task.metadata`` for now (the web client
# reads two of them there); these are the names the record (de)serializes to.
PENDING_INTERACTION_KEY = "pendingInteraction"
TURN_KIND_KEY = "daisyTurnKind"
REFERENCE_TASK_IDS_KEY = "referenceTaskIds"
AGENT_LANE_KEY = "agentLane"


class TurnKind(StrEnum):
    """What opened a turn — the fact its restart policy is derived from (a top-level user pause is
    resumable and preserved across a restart; a harness-initiated or delegated one is not)."""

    USER = "user"
    AUTONOMOUS = "autonomous"
    COMPACTION = "compaction"
    DELEGATED = "delegated"


class ReconcileAction(StrEnum):
    """What restart reconciliation does with a non-terminal task it finds — the two outcomes a
    total decision over ``(turn kind, task state)`` produces."""

    PRESERVE = "preserve"  # a durable pause; leave it for a later answer to resume
    FAIL = "fail"          # interrupted; mark it failed so nothing stale replays as active


def reconcile_action(kind: "Optional[TurnKind]", state: str, *, input_required: str) -> ReconcileAction:
    """The reconciliation policy as one total function. In-memory executors cannot be resumed
    after a restart, so:

    * a **top-level** ``input-required`` pause is durable (checkpoint + pending interactions
      survive) → :attr:`ReconcileAction.PRESERVE`;
    * a **delegated** ``input-required`` pause depends on the parent's in-process driver, which a
      restart does not restore → :attr:`ReconcileAction.FAIL`;
    * every **other** non-terminal task was caught mid-execution (resume is at-most-once, so its
      in-flight tools did not complete) → :attr:`ReconcileAction.FAIL`.
    """
    if state == input_required and kind is not TurnKind.DELEGATED:
        return ReconcileAction.PRESERVE
    return ReconcileAction.FAIL


class ToolGate(BaseModel):
    """One human decision a turn is blocked on: a permission request for a tool call, or a
    question posed to the user. The ``kind`` discriminates; the permission fields
    (``command``/``justification``/``risk``) and the question field (``questions``) are populated
    per kind. A closed typed union for the two shapes is a later step (TurnEvent); this keeps the
    persisted fields addressable by name in the meantime."""

    model_config = {"extra": "allow"}

    request_id: str = ""
    kind: str = ""
    tool_call_id: str = ""
    command: str = ""
    justification: str = ""
    risk: str = ""
    questions: list[Any] = Field(default_factory=list)

    @property
    def is_question(self) -> bool:
        return self.kind == "question"


class PendingInteraction(BaseModel):
    """The durable record of a paused turn: the gates awaiting a human, the full preflight plans a
    resume rebuilds the tool batch from, the answers gathered so far, and which agent handler owns
    the resume. This is the source of truth an input-required task survives a restart on."""

    gates: list[ToolGate] = Field(default_factory=list)
    plans: dict[str, Any] = Field(default_factory=dict)
    answers: dict[str, Any] = Field(default_factory=dict)
    agent: str = ""

    def gate_for(self, request_id: str) -> "Optional[ToolGate]":
        return next((gate for gate in self.gates if gate.request_id == request_id), None)

    @property
    def fully_answered(self) -> bool:
        return bool(self.gates) and all(gate.request_id in self.answers for gate in self.gates)


class AgentLane(BaseModel):
    """A delegated task's lane in the parent's agents panel — persisted so replay can reconcile a
    child that reached a terminal state after the parent turn stopped streaming. Serializes with
    the historical camelCase keys the web client reads."""

    group_id: str = Field(default="", alias="groupId")
    step_id: str = Field(default="", alias="stepId")

    model_config = {"populate_by_name": True}


class TurnRecord(BaseModel):
    """A turn's durable control-state, as one typed value. Constructed from (and serialized back
    into) an A2A ``Task.metadata`` dict via :meth:`from_metadata` / :meth:`apply_to`, preserving
    any non-turn keys the dict happens to carry."""

    kind: Optional[TurnKind] = None
    pending: Optional[PendingInteraction] = None
    reference_task_ids: list[str] = Field(default_factory=list)
    lane: Optional[AgentLane] = None

    @classmethod
    def from_metadata(cls, metadata: "dict[str, Any] | None") -> "TurnRecord":
        """Read the turn-state out of a task's metadata. Absent or malformed pieces yield an empty
        record rather than raising, so a task written before any state was stamped still loads."""
        data = metadata or {}
        raw_kind = data.get(TURN_KIND_KEY)
        kind = None
        if isinstance(raw_kind, str) and raw_kind:
            try:
                kind = TurnKind(raw_kind)
            except ValueError:
                kind = None
        raw_pending = data.get(PENDING_INTERACTION_KEY)
        pending = PendingInteraction.model_validate(raw_pending) if isinstance(raw_pending, dict) else None
        raw_reference = data.get(REFERENCE_TASK_IDS_KEY)
        reference_task_ids = [str(item) for item in raw_reference] if isinstance(raw_reference, list) else []
        raw_lane = data.get(AGENT_LANE_KEY)
        lane = AgentLane.model_validate(raw_lane) if isinstance(raw_lane, dict) else None
        return cls(kind=kind, pending=pending, reference_task_ids=reference_task_ids, lane=lane)

    def apply_to(self, metadata: "dict[str, Any] | None") -> "dict[str, Any]":
        """A new metadata dict: a copy of ``metadata`` with the turn-state keys set to this record's
        contents and any turn-state key this record does not carry removed. Non-turn keys pass
        through untouched, so this owns exactly the turn-state slice of the task's metadata."""
        result = {
            key: value
            for key, value in (metadata or {}).items()
            if key not in (PENDING_INTERACTION_KEY, TURN_KIND_KEY, REFERENCE_TASK_IDS_KEY, AGENT_LANE_KEY)
        }
        if self.kind is not None:
            result[TURN_KIND_KEY] = str(self.kind)
        if self.pending is not None:
            result[PENDING_INTERACTION_KEY] = self.pending.model_dump()
        if self.reference_task_ids:
            result[REFERENCE_TASK_IDS_KEY] = list(self.reference_task_ids)
        if self.lane is not None:
            result[AGENT_LANE_KEY] = self.lane.model_dump(by_alias=True)
        return result
