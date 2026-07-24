"""The one typed value for a turn's durable control-state.

A turn's control-state — is it a user turn or a harness-initiated one, is it paused on a human
gate, which parent tasks it references — used to live as
bare string keys poked into the A2A ``Task.metadata`` dict and read back with ``metadata.get(...)``
three calls deep. :class:`TurnRecord` is where that state lives as one validated object: every
backend site reads ``record.pending.gates[0].request_id`` rather than
``metadata["pendingInteraction"]["gates"][0]["request_id"]``, and a missing or misshapen field is a
validation error at the boundary rather than a ``KeyError`` at the point of use.

Serialization is deliberately byte-compatible with the historical flat keys (``pendingInteraction``,
``xeacTurnKind``, ``referenceTaskIds``): they are
read by the web client off ``Task.metadata``, so the persisted/wire shape is unchanged — only the
in-process access is now typed. Reshaping these under a single namespaced key is a later, separate
step (see the typed-turn-core plan), not this one.

The large, write-hot conversation checkpoint stays out of this record — it lives in its own table,
keyed by context — so a ``TurnRecord`` is small and cheap to rewrite on every turn.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional, Literal

from pydantic import BaseModel, Field

# Every turn-state key still lives at the top level of ``Task.metadata`` for now (the web client
# reads two of them there); these are the names the record (de)serializes to.
PENDING_INTERACTION_KEY = "pendingInteraction"
TURN_KIND_KEY = "xeacTurnKind"
REFERENCE_TASK_IDS_KEY = "referenceTaskIds"


class TurnKind(StrEnum):
    """What opened a turn: a person, a background result waking the session, or compaction."""

    USER = "user"
    AUTONOMOUS = "autonomous"
    COMPACTION = "compaction"


class ReconcileAction(StrEnum):
    """What restart reconciliation does with a non-terminal task it finds — the two outcomes a
    total decision over ``(turn kind, task state)`` produces."""

    PRESERVE = "preserve"  # a durable pause; leave it for a later answer to resume
    FAIL = "fail"          # interrupted; mark it failed so nothing stale replays as active


def reconcile_action(kind: Optional[TurnKind], state: str, *, input_required: str) -> ReconcileAction:
    """What to do with a non-terminal task found after a restart, as one total function.

    * an ``input-required`` pause is durable — its checkpoint and pending interactions survive,
      so a later answer resumes it → :attr:`ReconcileAction.PRESERVE`;
    * every other non-terminal task was caught mid-execution, and resume is at-most-once, so its
      in-flight tools did not complete → :attr:`ReconcileAction.FAIL`.

    The turn kind no longer changes the answer: it did when a delegated turn was an in-process
    continuation of its parent's, which a restart could not restore. A delegated turn is a
    separate session now, reaped with the daemon and reconciled on its own terms."""
    if state == input_required:
        return ReconcileAction.PRESERVE
    return ReconcileAction.FAIL


class ToolGate(BaseModel):
    """One human decision a turn is blocked on: a permission request for a tool call, or a
    question posed to the user. ``kind`` discriminates (``"permission"`` | ``"question"``); the
    permission fields (``command``/``justification``/``risk``) and the question field
    (``questions``) are populated per kind. Every field is declared and typed — the durable
    twin of the in-process :class:`~xeac.runtime.turn_events.SuspensionGate`, so a suspend
    round-trips through it with no ``extra="allow"`` catch-all."""

    request_id: str = ""
    kind: Literal["permission", "question"] = "permission"
    tool_call_id: str = ""
    command: str = ""
    justification: str = ""
    risk: str = ""
    questions: list[Any] = Field(default_factory=list)
    # Permission-gate detail carried through a suspend so a resume can re-apply an
    # "always allow" (a bash session rule / an egress approval) and a denial message.
    is_bash: bool = False
    deny_message: str = ""
    egress_agent: str = ""

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

    def gate_for(self, request_id: str) -> Optional[ToolGate]:
        return next((gate for gate in self.gates if gate.request_id == request_id), None)

    @property
    def fully_answered(self) -> bool:
        return bool(self.gates) and all(gate.request_id in self.answers for gate in self.gates)



class TurnRecord(BaseModel):
    """A turn's durable control-state, as one typed value. Constructed from (and serialized back
    into) an A2A ``Task.metadata`` dict via :meth:`from_metadata` / :meth:`apply_to`, preserving
    any non-turn keys the dict happens to carry."""

    kind: Optional[TurnKind] = None
    pending: Optional[PendingInteraction] = None
    reference_task_ids: list[str] = Field(default_factory=list)

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> TurnRecord:
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
        return cls(kind=kind, pending=pending, reference_task_ids=reference_task_ids)

    def apply_to(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        """A new metadata dict: a copy of ``metadata`` with the turn-state keys set to this record's
        contents and any turn-state key this record does not carry removed. Non-turn keys pass
        through untouched, so this owns exactly the turn-state slice of the task's metadata."""
        result = {
            key: value
            for key, value in (metadata or {}).items()
            if key not in (PENDING_INTERACTION_KEY, TURN_KIND_KEY, REFERENCE_TASK_IDS_KEY)
        }
        if self.kind is not None:
            result[TURN_KIND_KEY] = str(self.kind)
        if self.pending is not None:
            result[PENDING_INTERACTION_KEY] = self.pending.model_dump()
        if self.reference_task_ids:
            result[REFERENCE_TASK_IDS_KEY] = list(self.reference_task_ids)
        return result
