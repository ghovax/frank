"""A turn's durable control-state as one typed value, rather than bare keys poked into task metadata."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional, Literal

from pydantic import BaseModel, Field

# One key holds the whole record, under the same extension URI a message's turn metadata uses.
from langmesh.protocol.metadata import METADATA_KEY

TURN_STATE_KEY = METADATA_KEY

# The field names inside that object. Plain, because the namespace already answered whose.
PENDING_INTERACTION_FIELD = "pending"
TURN_KIND_FIELD = "kind"
REFERENCE_TURN_IDS_FIELD = "referenceTurnIds"
# Which session sent a peer turn. Beside the kind, since attributing a report needs the sender.
PEER_SENDER_FIELD = "peerSender"


class TurnKind(StrEnum):
    """What opened a turn: a person, a peer, a background result, or compaction."""

    USER = "user"
    # A message from another session. Distinct from USER, or a peer's report reads as the user's instruction.
    PEER = "peer"
    AUTONOMOUS = "autonomous"
    COMPACTION = "compaction"


class ReconcileAction(StrEnum):
    """What restart reconciliation does with a non-terminal task: the two outcomes over kind and state."""

    PRESERVE = "preserve"  # a durable pause; leave it for a later answer to resume
    FAIL = "fail"  # interrupted; mark it failed so nothing stale replays as active


def reconcile_action(
    kind: Optional[TurnKind], state: str, *, input_required: str
) -> ReconcileAction:
    """What to do with a non-terminal task after a restart: an `input-required` pause survives, the rest fail."""
    if state == input_required:
        return ReconcileAction.PRESERVE
    return ReconcileAction.FAIL


class ToolGate(BaseModel):
    """One decision a turn is blocked on, and the durable twin of the in-process gate it round-trips through."""

    request_id: str = ""
    kind: Literal["permission", "question"] = "permission"
    tool_call_id: str = ""
    command: str = ""
    explanation: str = ""
    reason: Optional[dict[str, Any]] = None
    questions: list[Any] = Field(default_factory=list)
    # Gate detail carried through a suspend, so a resume can re-apply an always-allow and its denial message.
    is_bash: bool = False
    deny_message: str = ""
    egress_agent: str = ""
    # The widening asked for, and for a refused command the whole-disk offer and what the confined run did.
    escape: Optional[dict[str, Any]] = None
    whole_disk: bool = False
    denial_evidence: str = ""
    refused_result: Any = None
    grants_screen_mutations: bool = False

    @property
    def is_question(self) -> bool:
        return self.kind == "question"


class PendingInteraction(BaseModel):
    """The durable record of a paused turn: its gates, its plans, its answers, and who owns the resume."""

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
    """A turn's durable control-state, read from and written back into task metadata."""

    kind: Optional[TurnKind] = None
    # Set only on a peer turn, and durable: a transcript read later still needs to attribute the report.
    peer_sender: str = ""
    pending: Optional[PendingInteraction] = None
    reference_task_ids: list[str] = Field(default_factory=list)

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> TurnRecord:
        """Read the turn-state out of a task's metadata, answering an empty record rather than raising."""
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
        pending = (
            PendingInteraction.model_validate(raw_pending)
            if isinstance(raw_pending, dict)
            else None
        )
        raw_reference = data.get(REFERENCE_TURN_IDS_FIELD)
        reference_task_ids = (
            [str(item) for item in raw_reference] if isinstance(raw_reference, list) else []
        )
        raw_sender = data.get(PEER_SENDER_FIELD)
        peer_sender = raw_sender if isinstance(raw_sender, str) else ""
        return cls(
            kind=kind,
            peer_sender=peer_sender,
            pending=pending,
            reference_task_ids=reference_task_ids,
        )

    def apply_to(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        """A metadata copy carrying this record. Everything outside the one key passes through untouched."""
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
