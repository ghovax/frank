"""The one typed value for a turn's durable control-state.

A turn's control-state — is it a user turn or a harness-initiated one, is it paused on a human
gate, which parent tasks it references — used to live as
bare string keys poked into the A2A ``Task.metadata`` dict and read back with ``metadata.get(...)``
three calls deep. :class:`TurnRecord` is where that state lives as one validated object: every
backend site reads ``record.pending.gates[0].request_id`` rather than
``metadata["pendingInteraction"]["gates"][0]["request_id"]``, and a missing or misshapen field is a
validation error at the boundary rather than a ``KeyError`` at the point of use.

Serialization puts the whole record under one URI-namespaced key in ``Task.metadata``, which is
the convention A2A defines for an extension and the one a message's turn metadata already
followed. The keys inside it are plain — ``kind``, ``peerSender`` — because the namespace has
already said whose they are. Spelling the harness's name into each field as well
(``xeacTurnKind`` beside an unprefixed ``pendingInteraction``) named the owner twice in one
place and not at all in the next, which is the shape of a convention nobody is applying.

The large, write-hot conversation checkpoint stays out of this record — it lives in its own table,
keyed by context — so a ``TurnRecord`` is small and cheap to rewrite on every turn.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional, Literal

from pydantic import BaseModel, Field

# One key in ``Task.metadata`` holds the whole record — the same extension URI a message's turn
# metadata uses, because it is the same extension. The harness's name belongs here, once, where
# it means "these are XEAC's attributes"; it does not belong in the field names underneath.
from xeac.protocol.metadata import XEAC_METADATA_KEY

TURN_STATE_KEY = XEAC_METADATA_KEY

# The field names inside that object. Plain, because the namespace already answered whose.
PENDING_INTERACTION_FIELD = "pending"
TURN_KIND_FIELD = "kind"
REFERENCE_TURN_IDS_FIELD = "referenceTurnIds"
# Which session sent a peer turn's message. Carried beside the kind rather than folded into
# it: "a peer wrote this" and "*which* peer wrote it" are different facts, and a reader that
# wants to attribute a report needs the second one.
PEER_SENDER_FIELD = "peerSender"


class TurnKind(StrEnum):
    """What opened a turn: a person, another session, a background result waking this one, or
    compaction."""

    USER = "user"
    # A message from another session — a peer reporting its result, or a parent following up.
    # Distinct from USER because it is not the user speaking, and the difference is not
    # cosmetic: without it a peer's report reaches the model as an instruction from the person
    # the session is working for, and reaches the desktop client as a message rendered in the
    # user's own style, attributing words to someone who never wrote them.
    PEER = "peer"
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
    # Set only on a PEER turn: the session that sent the message. Durable, because a person
    # reading the transcript later needs to know a report came from a peer as much as one
    # watching it arrive does.
    peer_sender: str = ""
    pending: Optional[PendingInteraction] = None
    reference_task_ids: list[str] = Field(default_factory=list)

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> TurnRecord:
        """Read the turn-state out of a task's metadata. Absent or malformed pieces yield an empty
        record rather than raising, so a task carrying no state at all still loads."""
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
        """A new metadata dict: a copy of ``metadata`` whose turn-state key holds this record.

        Everything outside that one key passes through untouched, so this owns exactly its own
        slice — and an empty record removes the key rather than leaving an empty object behind."""
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
