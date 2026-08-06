"""The one typed value for a turn's durable control-state."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional, Literal

from pydantic import BaseModel, Field

# One key in ``Task.metadata`` holds the whole record — the same extension URI a message's turn metadata uses, because it is the same extension.
from frank.protocol.metadata import METADATA_KEY

TURN_STATE_KEY = METADATA_KEY

# The field names inside that object. Plain, because the namespace already answered whose.
PENDING_INTERACTION_FIELD = "pending"
TURN_KIND_FIELD = "kind"
REFERENCE_TURN_IDS_FIELD = "referenceTurnIds"
# Which session sent a peer turn's message.
PEER_SENDER_FIELD = "peerSender"


class TurnKind(StrEnum):
    """What opened a turn: a person, another session, a background result waking this one, or compaction."""

    USER = "user"
    # A message from another session — a peer reporting its result, or a parent following up.
    PEER = "peer"
    AUTONOMOUS = "autonomous"
    COMPACTION = "compaction"


class ReconcileAction(StrEnum):
    """What restart reconciliation does with a non-terminal task it finds — the two outcomes a total decision over ``(turn kind, task state)`` produces."""

    PRESERVE = "preserve"  # a durable pause; leave it for a later answer to resume
    FAIL = "fail"          # interrupted; mark it failed so nothing stale replays as active


def reconcile_action(kind: Optional[TurnKind], state: str, *, input_required: str) -> ReconcileAction:
    """What to do with a non-terminal task found after a restart, as one total function."""
    if state == input_required:
        return ReconcileAction.PRESERVE
    return ReconcileAction.FAIL


class ToolGate(BaseModel):
    """One human decision a turn is blocked on: a permission request for a tool call, or a question posed to the user."""

    request_id: str = ""
    kind: Literal["permission", "question"] = "permission"
    tool_call_id: str = ""
    command: str = ""
    explanation: str = ""
    reason: Optional[dict[str, Any]] = None
    questions: list[Any] = Field(default_factory=list)
    # Permission-gate detail carried through a suspend so a resume can re-apply an "always allow" (a bash session rule / an egress approval) and a denial message.
    is_bash: bool = False
    deny_message: str = ""
    egress_agent: str = ""
    # The widening being asked for, and — for a command the operating system refused — the offer to let it out of the box and what the confined run produced.
    escape: Optional[dict[str, Any]] = None
    whole_disk: bool = False
    denial_evidence: str = ""
    refused_result: Any = None
    grants_screen_mutations: bool = False

    @property
    def is_question(self) -> bool:
        return self.kind == "question"


class PendingInteraction(BaseModel):
    """The durable record of a paused turn: the gates awaiting a human, the full preflight plans a resume rebuilds the tool batch from, the answers gathered so far, and which agent handler owns the resume."""

    gates: list[ToolGate] = Field(default_factory=list)
    plans: dict[str, Any] = Field(default_factory=dict)
    answers: dict[str, Any] = Field(default_factory=dict)
    agent: str = ""

    def gate_for(self, request_id: str) -> Optional[ToolGate]:
        return next((gate for gate in self.gates if gate.request_id == request_id), None)

    @property
    def fully_answered(self) -> bool:
        return bool(self.gates) and all(gate.request_id in self.answers for gate in self.gates)


class TurnRecord(BaseModel):
    """A turn's durable control-state, as one typed value."""

    kind: Optional[TurnKind] = None
    # Set only on a PEER turn: the session that sent the message.
    peer_sender: str = ""
    pending: Optional[PendingInteraction] = None
    reference_task_ids: list[str] = Field(default_factory=list)

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> TurnRecord:
        """Read the turn-state out of a task's metadata."""
        data = (metadata or {}).get(TURN_STATE_KEY)
        if not isinstance(data, dict):
            return cls()
        raw_kind = data.get(TURN_KIND_FIELD)
        kind = None
        if isinstance(raw_kind, str) and raw_kind:
            try:
                kind = TurnKind(raw_kind)
            except ValueError:
                kind = None
        raw_pending = data.get(PENDING_INTERACTION_FIELD)
        pending = PendingInteraction.model_validate(raw_pending) if isinstance(raw_pending, dict) else None
        raw_reference = data.get(REFERENCE_TURN_IDS_FIELD)
        reference_task_ids = [str(item) for item in raw_reference] if isinstance(raw_reference, list) else []
        raw_sender = data.get(PEER_SENDER_FIELD)
        peer_sender = raw_sender if isinstance(raw_sender, str) else ""
        return cls(kind=kind, peer_sender=peer_sender, pending=pending, reference_task_ids=reference_task_ids)

    def apply_to(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        """A new metadata dict: a copy of ``metadata`` whose turn-state key holds this record."""
        result = {key: value for key, value in (metadata or {}).items() if key != TURN_STATE_KEY}
        state: dict[str, Any] = {}
        if self.kind is not None:
            state[TURN_KIND_FIELD] = str(self.kind)
        if self.peer_sender:
            state[PEER_SENDER_FIELD] = self.peer_sender
        if self.pending is not None:
            state[PENDING_INTERACTION_FIELD] = self.pending.model_dump()
        if self.reference_task_ids:
            state[REFERENCE_TURN_IDS_FIELD] = list(self.reference_task_ids)
        if state:
            result[TURN_STATE_KEY] = state
        return result
