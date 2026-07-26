"""An append-only A2A :class:`TaskStore`.

Why this exists.
A2A streams a turn as many small events (text-chunk flushes, thinking labels,
tool calls/results, and — for the chat agent — every relayed agent event).
The SDK's :class:`~a2a.server.tasks.TaskManager` appends each event's message to
``task.history`` *in memory* and then calls ``turn_store.save(task)`` for every
event. The bundled :class:`~a2a.server.tasks.DatabaseTaskStore` persists a task
by ``session.merge`` of the whole row — i.e. it re-serializes and rewrites the
*entire, ever-growing* ``history`` JSON blob on every event.

For a turn that emits *N* events that is ``1 + 2 + … + N = O(N²)`` bytes written,
and with a chat agent relaying agent activity (plus large web/MCP tool
results) the blob reaches megabytes and is rewritten ~20×/second — multiple
megabytes per second of disk I/O, and each write holds SQLite's single write
lock long enough to stall the concurrent agent turns.

The fix is to normalize the history into append-only rows: the task's small,
mutable head (status + metadata) is upserted, and each new history message /
artifact is inserted exactly once. ``history`` only ever grows (the TaskManager
appends), so ``save`` writes just the new suffix — O(delta) per event, O(N) per
turn, with per-event cost independent of how long the turn has been running.

The live transport is unchanged: the SDK still streams incremental
``TaskStatusUpdateEvent``/``TaskArtifactUpdateEvent`` objects over SSE. Only the
persistence layer changes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import DataPart, Message, Part, Role, Task, TaskState, TaskStatus

from daisy.base.message_content import content_block_identifier
from daisy.base.sqlite_lock import acquire_sqlite_write_lock, release_sqlite_write_lock
from daisy.protocol.turn_record import ReconcileAction, TurnRecord, reconcile_action


def _dump(model) -> str:
    """Serialize a pydantic model to a JSON string using field names, mirroring
    how the SDK's DatabaseTaskStore round-trips (field names, not aliases)."""
    return json.dumps(model.model_dump(mode="json"))


# A turn persists many small history rows (one per text flush, reasoning chunk,
# etc.). For replay that granularity is pure overhead — adjacent same-kind deltas
# are merged into one message so a session loads and re-reduces far fewer, larger
# rows. Mirrors what the client used to do, now server-side so both the REST tasks
# endpoint and the live-stream snapshot benefit and the client no longer needs to.

def _sole_part(message: object) -> dict | None:
    parts = message.get("parts") if isinstance(message, dict) else None  # type: ignore[union-attr]
    if not parts or not isinstance(parts, list) or len(parts) != 1:
        return None
    part = parts[0]
    return part if isinstance(part, dict) else None


def _agent_text_part(message: object) -> dict | None:
    if not isinstance(message, dict) or message.get("role") != "agent":
        return None
    part = _sole_part(message)
    if not part or part.get("kind") != "text":
        return None
    return part


def _agent_text(message: object) -> str | None:
    part = _agent_text_part(message)
    return str(part.get("text", "")) if part is not None else None


def _sole_data(message: object, kind: str) -> dict | None:
    """The data dict if the message is a single data-part agent message of `kind`."""
    if not isinstance(message, dict) or message.get("role") != "agent":
        return None
    part = _sole_part(message)
    if not part or part.get("kind") != "data":
        return None
    data = part.get("data")
    if not isinstance(data, dict) or data.get("kind") != kind:
        return None
    return data


def _path_key(data: dict) -> tuple:
    """A hashable identity for the agent that produced an event, from its ``path``
    (empty for the root agent). Adjacent same-kind events merge only within one agent."""
    path = data.get("path") or []
    return tuple((segment.get("group_id"), segment.get("step_id")) for segment in path)


def _compact_history(messages: list) -> list:
    """Merge adjacent same-kind single-part agent messages (plain text, sub-task
    text in the same step, and reasoning) into one message each."""
    compacted: list = []
    for message in messages:
        text = _agent_text(message)
        if text is not None:
            last = compacted[-1] if compacted else None
            current_part = _agent_text_part(message)
            last_part = _agent_text_part(last) if last is not None else None
            current_block_identifier = (
                content_block_identifier(current_part.get("metadata"))
                if current_part is not None else None
            )
            last_block_identifier = (
                content_block_identifier(last_part.get("metadata"))
                if last_part is not None else None
            )
            if (
                last_part is not None
                and current_part is not None
                and current_block_identifier is not None
                and current_block_identifier == last_block_identifier
            ):
                last_part["text"] = str(last_part.get("text", "")) + text
                continue
            compacted.append(message)
            continue
        sub = _sole_data(message, "text")
        if sub is not None:
            # An agent's text arrives as a path-tagged `text` data event; merge only
            # adjacent ones from the same agent (same path).
            key = _path_key(sub)
            last = compacted[-1] if compacted else None
            last_sub = _sole_data(last, "text") if last is not None else None
            if (
                last_sub is not None
                and _path_key(last_sub) == key
                and str(last_sub.get("block_id", "")) == str(sub.get("block_id", ""))
            ):
                last["parts"] = [{"kind": "data", "data": {**last_sub, "text": str(last_sub.get("text", "")) + str(sub.get("text", ""))}}]  # type: ignore[index]
                continue
            compacted.append(message)
            continue
        thinking = _sole_data(message, "thinking")
        if thinking is not None:
            key = _path_key(thinking)
            last = compacted[-1] if compacted else None
            last_thinking = _sole_data(last, "thinking") if last is not None else None
            if (
                last_thinking is not None
                and _path_key(last_thinking) == key
                and str(last_thinking.get("block_id", "")) == str(thinking.get("block_id", ""))
            ):
                last["parts"] = [{"kind": "data", "data": {**last_thinking, "text": str(last_thinking.get("text", "")) + str(thinking.get("text", ""))}}]  # type: ignore[index]
                continue
            compacted.append(message)
            continue
        compacted.append(message)
    return compacted


_TERMINAL_TASK_STATES = {
    TaskState.completed.value,
    TaskState.canceled.value,
    TaskState.failed.value,
    TaskState.rejected.value,
}


# The turn kind carried in a task's head ``metadata`` — the field the restart
# reconciliation reads to decide a non-terminal task's fate. (Background-result delivery
# stays in ``background_store``, which is already results-durable / execution-ephemeral and
# additionally reaps orphaned OS process groups and recovers running jobs — capabilities a
# task-metadata inbox would not carry, so it is not folded in.)
def _task_state_value(task: Task) -> str:
    state = task.status.state
    return state.value if isinstance(state, TaskState) else str(state)


def _is_terminal_task(task: Task) -> bool:
    return _task_state_value(task) in _TERMINAL_TASK_STATES


class AppendOnlyTaskStore(TaskStore):
    """A2A task store that persists history/artifacts incrementally.

    Drop-in replacement for ``DatabaseTaskStore``: implements the same
    ``save``/``get``/``delete`` contract, but stores a task across three tables
    so a save is O(new messages) rather than O(whole history).

    Charter: this is the single durable surface for a turn — its wire history/artifacts, its
    control-state (the :class:`~daisy.protocol.turn_record.TurnRecord` on the task head), and its
    conversation checkpoint (``save_turn_state``/``load_checkpoint``). Background jobs are the one
    thing it does NOT own; those live in the separate
    :class:`~daisy.base.background_store.BackgroundJobStore`.
    """

    def __init__(self, engine: AsyncEngine):
        self._engine = engine
        self._metadata = MetaData()
        # The task head: small and mutable. A distinct table name (not ``tasks``)
        # so it never collides with a pre-existing DatabaseTaskStore schema in an
        # older database; a context lookup reads one compact row per task.
        self._head = Table(
            "turn_head",
            self._metadata,
            Column("id", String, primary_key=True),
            Column("session_id", String),
            Column("kind", String),
            Column("status", Text),
            Column("turn_metadata", Text),
        )
        # Append-only history: one row per message, ordered by ``row_id`` — the database's
        # own autoincrement insert order. There is no hand-computed per-task position: an
        # append is a bare insert and the database assigns the monotonic id atomically, so
        # two concurrent saves of one task can never collide on a position. ``sqlite_auto
        # increment`` makes the id strictly increasing and never reused, so it stays a valid
        # ordering key even after compaction deletes the tail of a task's history.
        self._history = Table(
            "turn_history",
            self._metadata,
            Column("row_id", Integer, primary_key=True, autoincrement=True),
            Column("turn_id", String),
            Column("message", Text),
            sqlite_autoincrement=True,
        )
        # Artifacts are few and may be revised in place, so they upsert by id
        # (bounded by artifact count, never by history length).
        self._artifacts = Table(
            "turn_artifacts",
            self._metadata,
            Column("row_id", Integer, primary_key=True, autoincrement=True),
            Column("turn_id", String),
            Column("artifact_id", String),
            Column("artifact", Text),
            UniqueConstraint("turn_id", "artifact_id", name="uq_task_artifact_id"),
        )
        # The turn's durable resume checkpoint: the model-facing LangChain conversation,
        # snapshotted (messages_to_dict) at each safe point of the running turn. One row
        # per context — the running dialogue accumulates across a session's turns and
        # compaction rewrites it in place (summarizing earlier turns), so a whole-snapshot
        # is the only representation that stays correct; a per-turn append-only log cannot
        # express an in-place rewrite. Distinct from ``history`` (the A2A wire view): the
        # wire messages and the internal model-facing list are not losslessly
        # interconvertible, so this snapshot is authoritative for resume. It lives in the
        # task store (the single durable surface) rather than a separate conversations
        # database, and NOT on the write-hot task head (which upserts per stream event) —
        # it is written only at safe points, a few times per turn. ``turn_id`` records
        # which turn last wrote it, for reconciliation.
        self._checkpoint = Table(
            "turn_checkpoint",
            self._metadata,
            Column("session_id", String, primary_key=True),
            Column("turn_id", String),
            Column("messages", Text),
            Column("updated_at", String),
        )
        # A context's durable non-conversation state — the agent's active goal and task
        # list — kept beside the conversation checkpoint so a restart restores the agent's
        # objective, not just its transcript. One row per context, whole-row upsert at the
        # same safe points as the checkpoint. Compaction rewrites the conversation but never
        # touches this, so goal and tasks are never folded away.
        self._session_state = Table(
            "session_state",
            self._metadata,
            Column("session_id", String, primary_key=True),
            Column("state", Text),
            Column("updated_at", String),
        )
        # User message history, scoped to the working directory. Used for
        # up/down arrow recall of previously sent messages within a project.
        self._user_messages = Table(
            "user_message_history",
            self._metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("working_directory", String),
            Column("message", Text),
            Column("created_at", DateTime, server_default=func.now()),
        )
        self._initialized = False
        # How many history rows are persisted for each task, maintained authoritatively in
        # memory rather than re-counted from the database on every save. The store's write
        # lock serializes all writers, so a lock-guarded counter is exact: seeded once (a
        # single COUNT) the first time a task is saved in this process, incremented by the
        # appended delta, and reset to the compacted length when a terminal save rewrites the
        # rows. This keeps a save O(delta) — the module's whole reason to exist — instead of
        # the O(rows) COUNT-per-event a per-save COUNT reintroduces (O(N²) over a long turn).
        self._persisted_counts: dict[str, int] = {}
        # Tasks whose history has been terminally compacted. Once a task goes terminal its
        # persisted rows are the compacted merge of its whole history while the in-memory
        # `task.history` is still the raw list, so a *later* non-terminal save would re-append
        # already-merged messages and silently duplicate them. Terminal is the last save for a
        # task (a new turn is a new task id), so a non-terminal save after it is a real bug and
        # is rejected rather than corrupting the stored history.
        self._terminal_turns: set[str] = set()

    async def initialize(self) -> None:
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                await connection.run_sync(self._metadata.create_all)
                await connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_turn_head_session_id_id "
                    "ON turn_head(session_id, id)"
                )
                await connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_turn_history_turn_id_row_id "
                    "ON turn_history(turn_id, row_id)"
                )
                await connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_turn_artifacts_turn_id_row_id "
                    "ON turn_artifacts(turn_id, row_id)"
                )
                await connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_user_message_history_working_directory_created_at "
                    "ON user_message_history(working_directory, created_at DESC)"
                )
        finally:
            release_sqlite_write_lock(write_lock)
        self._initialized = True

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.initialize()

    async def reconcile_orphaned_turns(self) -> list[str]:
        """Restart reconciliation, driven by each turn's own durable record.

        A session's process does not survive its daemon, so every task left non-terminal by a
        restart is reconciled against one rule:

        * an ``input-required`` pause is durable — its checkpoint and pending interactions
          survive — and is preserved for a later answer to resume;
        * every **other non-terminal** task was caught mid-execution and is failed: resume is
          at-most-once, so its in-flight tools did not complete and there is nothing safe to
          resume into.

        Returns the ids that were failed. Failing an interrupted turn persists an explicit
        error status so stale approvals, tools, and agent lanes cannot replay as active.
        """
        await self._ensure_initialized()
        write_lock = await acquire_sqlite_write_lock()
        failed_task_ids: list[str] = []
        input_required = TaskState.input_required.value
        try:
            async with self._engine.begin() as connection:
                head_rows = (await connection.execute(select(self._head))).mappings().all()
                for head_row in head_rows:
                    current_state = str(json.loads(head_row["status"]).get("state", ""))
                    if current_state in _TERMINAL_TASK_STATES:
                        continue
                    metadata = json.loads(head_row["turn_metadata"]) if head_row["turn_metadata"] else {}
                    kind = TurnRecord.from_metadata(metadata).kind
                    if reconcile_action(kind, current_state, input_required=input_required) is ReconcileAction.PRESERVE:
                        continue
                    turn_id = str(head_row["id"])
                    session_id = str(head_row["session_id"] or "")
                    interrupted_message = Message(
                        role=Role.agent,
                        parts=[Part(root=DataPart(data={
                            "kind": "error",
                            "code": "turn_interrupted",
                            "message": "This turn was interrupted because the server restarted.",
                        }))],
                        message_id=uuid.uuid4().hex,
                        task_id=turn_id,
                        context_id=session_id or None,
                    )
                    interrupted_status = TaskStatus(
                        state=TaskState.failed,
                        message=interrupted_message,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                    await connection.execute(
                        update(self._head)
                        .where(self._head.c.id == turn_id)
                        .values(status=_dump(interrupted_status))
                    )
                    failed_task_ids.append(turn_id)
        finally:
            release_sqlite_write_lock(write_lock)
        return failed_task_ids

    async def save_turn_state(
        self,
        session_id: str,
        turn_id: str,
        messages: list,
        session_state: dict | None = None,
    ) -> None:
        """Atomically snapshot a context's model-facing conversation checkpoint and — when it
        changed this turn — its durable goal/task session state, in one transaction under one
        write lock. Both ride the running turn's safe points (a few times per turn, never per
        stream event), so the whole-row writes are cheap relative to the turn. Doing them
        together is what keeps them consistent: a crash can never leave the conversation newer
        than the objective, or lose one while writing the other. ``session_state`` is ``None``
        when the goal/tasks did not change since the last save (dirty-gated by the caller), and
        the caller clears its dirty flag only after this returns — so a failed write loses
        nothing. The conversation snapshot is whole-row upserted per context: it accumulates
        across turns and compaction rewrites it in place, so a whole snapshot is the only
        representation that stays correct."""
        await self._ensure_initialized()
        if not session_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                checkpoint_insert = sqlite_insert(self._checkpoint).values(
                    session_id=session_id,
                    turn_id=turn_id,
                    messages=json.dumps(messages),
                    updated_at=now,
                )
                await connection.execute(
                    checkpoint_insert.on_conflict_do_update(
                        index_elements=[self._checkpoint.c.session_id],
                        set_={
                            "turn_id": turn_id,
                            "messages": checkpoint_insert.excluded.messages,
                            "updated_at": now,
                        },
                    )
                )
                if session_state is not None:
                    state_insert = sqlite_insert(self._session_state).values(
                        session_id=session_id,
                        state=json.dumps(session_state),
                        updated_at=now,
                    )
                    await connection.execute(
                        state_insert.on_conflict_do_update(
                            index_elements=[self._session_state.c.session_id],
                            set_={"state": state_insert.excluded.state, "updated_at": now},
                        )
                    )
        finally:
            release_sqlite_write_lock(write_lock)

    async def load_checkpoint(self, session_id: str) -> list:
        """The context's model-facing conversation snapshot (``messages_to_dict`` form),
        or ``[]`` when there is none. The caller rehydrates it with ``messages_from_dict``
        and repairs any dangling tool-call left by a mid-execution interruption."""
        await self._ensure_initialized()
        if not session_id:
            return []
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(self._checkpoint.c.messages).where(self._checkpoint.c.session_id == session_id)
                )
            ).scalar()
        if not row:
            return []
        try:
            data = json.loads(row)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    async def load_session_state(self, session_id: str) -> dict:
        """The context's persisted goal/task state (:meth:`save_turn_state` form), or an
        empty dict when there is none — a fresh context or a pre-persistence session."""
        await self._ensure_initialized()
        if not session_id:
            return {}
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(self._session_state.c.state).where(self._session_state.c.session_id == session_id)
                )
            ).scalar()
        if not row:
            return {}
        try:
            data = json.loads(row)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}


    async def _persisted_count(self, connection, turn_id: str) -> int:
        """How many history rows are already persisted for a task, so ``save`` appends only
        the suffix of the (only-ever-growing) ``task.history`` not yet stored. Authoritative
        in memory (guarded by the store's write lock, which admits one writer at a time),
        seeded once from a single COUNT the first time this process saves the task and kept
        current by every write thereafter — so a save is O(delta), never a COUNT-per-event."""
        cached = self._persisted_counts.get(turn_id)
        if cached is not None:
            return cached
        result = await connection.execute(
            select(func.count()).select_from(self._history).where(self._history.c.turn_id == turn_id)
        )
        seeded = int(result.scalar() or 0)
        self._persisted_counts[turn_id] = seeded
        return seeded

    async def _compact_persisted_history(self, connection, turn_id: str) -> int:
        """Rewrite a task's *already-persisted* history in place with its compacted form,
        ordered by ``row_id``: overwrite the first M rows' messages (their row_ids — and so
        their global order — unchanged) and delete the tail rows the compaction dropped. It
        never inserts: ``_compact_history`` only merges adjacent messages, so the compacted
        count is always ≤ the persisted count, and minting fresh (higher) row_ids here would
        reorder this task's tail after a concurrently-persisted task when a context is paged
        by global ``row_id``. The caller appends any unpersisted suffix *before* calling this,
        so ``task.history`` is already fully in the table and the compaction is pure
        update-and-delete."""
        existing_rows = (
            await connection.execute(
                select(self._history.c.row_id, self._history.c.message)
                .where(self._history.c.turn_id == turn_id)
                .order_by(self._history.c.row_id)
            )
        ).all()
        compacted_messages = _compact_history([json.loads(row.message) for row in existing_rows])
        if len(compacted_messages) > len(existing_rows):  # pragma: no cover - invariant guard
            raise AssertionError(
                f"compaction grew history for {turn_id}: {len(existing_rows)} -> {len(compacted_messages)}"
            )
        for message_index, message in enumerate(compacted_messages):
            await connection.execute(
                update(self._history)
                .where(self._history.c.row_id == existing_rows[message_index].row_id)
                .values(message=json.dumps(message))
            )
        surplus_row_ids = [row.row_id for row in existing_rows[len(compacted_messages):]]
        if surplus_row_ids:
            await connection.execute(
                delete(self._history).where(self._history.c.row_id.in_(surplus_row_ids))
            )
        return len(compacted_messages)

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        await self._ensure_initialized()
        history = task.history or []
        artifacts = task.artifacts or []
        terminal = _is_terminal_task(task)
        if task.id in self._terminal_turns and not terminal:
            # The persisted rows are the compacted merge of the whole history; re-appending the
            # raw suffix on top would duplicate already-merged messages. Terminal is the last
            # save for a task, so this is a wiring bug, not a state to tolerate.
            raise ValueError(
                f"non-terminal save for already-terminal task {task.id}: a terminal save must be the last save for a task"
            )
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                # Head: tiny upsert of the latest status + metadata.
                head_values = {
                    "id": task.id,
                    "session_id": task.context_id,
                    "kind": task.kind,
                    "status": _dump(task.status),
                    "turn_metadata": json.dumps(task.metadata) if task.metadata is not None else None,
                }
                head_insert = sqlite_insert(self._head).values(**head_values)
                await connection.execute(
                    head_insert.on_conflict_do_update(
                        index_elements=[self._head.c.id],
                        set_={
                            "session_id": head_values["session_id"],
                            "kind": head_values["kind"],
                            "status": head_values["status"],
                            "turn_metadata": head_values["turn_metadata"],
                        },
                    )
                )

                # History: insert only the messages not yet persisted. The list only ever
                # grows, so the already-stored prefix is never rewritten — an append is the
                # suffix past the (in-memory, lock-guarded) persisted count.
                persisted = await self._persisted_count(connection, task.id)
                new_messages = history[persisted:]
                if new_messages:
                    await connection.execute(
                        self._history.insert(),
                        [{"turn_id": task.id, "message": _dump(message)} for message in new_messages],
                    )
                    self._persisted_counts[task.id] = persisted + len(new_messages)

                if terminal:
                    # The whole history is now in the table (suffix appended above with natural
                    # contiguous row_ids); compact it in place — pure update-and-delete, no new
                    # row_ids — and record the terminal, compacted count so a stray later save
                    # is caught rather than duplicating.
                    compacted_count = await self._compact_persisted_history(connection, task.id)
                    self._persisted_counts[task.id] = compacted_count
                    self._terminal_turns.add(task.id)

                # Artifacts: upsert each by id (replace-in-place is safe and bounded).
                for artifact in artifacts:
                    artifact_json = _dump(artifact)
                    artifact_insert = sqlite_insert(self._artifacts).values(
                        turn_id=task.id,
                        artifact_id=artifact.artifact_id,
                        artifact=artifact_json,
                    )
                    await connection.execute(
                        artifact_insert.on_conflict_do_update(
                            index_elements=[self._artifacts.c.turn_id, self._artifacts.c.artifact_id],
                            set_={"artifact": artifact_json},
                        )
                    )
        finally:
            release_sqlite_write_lock(write_lock)

    async def get(self, turn_id: str, context: ServerCallContext | None = None) -> Optional[Task]:
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            head_row = (
                await connection.execute(select(self._head).where(self._head.c.id == turn_id))
            ).mappings().first()
            if head_row is None:
                return None
            history_rows = (
                await connection.execute(
                    select(self._history.c.message)
                    .where(self._history.c.turn_id == turn_id)
                    .order_by(self._history.c.row_id)
                )
            ).scalars().all()
            artifact_rows = (
                await connection.execute(
                    select(self._artifacts.c.artifact).where(self._artifacts.c.turn_id == turn_id)
                )
            ).scalars().all()

        data = {
            "id": head_row["id"],
            "context_id": head_row["session_id"],
            "kind": head_row["kind"] or "task",
            "status": json.loads(head_row["status"]),
            "metadata": json.loads(head_row["turn_metadata"]) if head_row["turn_metadata"] else None,
            "history": [json.loads(message) for message in history_rows],
            "artifacts": [json.loads(artifact) for artifact in artifact_rows] or None,
        }
        return Task.model_validate(data)

    async def turns_for_session(self, session_id: str) -> list[Task]:
        """All tasks in a context, loaded with one head/history/artifact pass.

        Session replay asks for every task in a context at once. Calling ``get``
        per task fans that into three queries per task; this keeps the same Task
        shape but batches those reads so opening a local session is not gated by
        request count.
        """
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            head_rows = (
                await connection.execute(
                    select(self._head)
                    .where(self._head.c.session_id == session_id)
                    # Ordered below by when each turn actually started. A turn's id is a
                    # random UUID, so ordering by it returned a session's turns shuffled —
                    # and anything reading "the last turn" off the end got an arbitrary one.
                    .order_by(self._head.c.id)
                )
            ).mappings().all()
            turn_ids = [str(row["id"]) for row in head_rows]
            if not turn_ids:
                return []

            history_rows = (
                await connection.execute(
                    select(self._history.c.turn_id, self._history.c.message)
                    .where(self._history.c.turn_id.in_(turn_ids))
                    # Globally by row_id, not grouped by turn: the append order *is* the
                    # chronology, so first appearance of a turn id here is when it began.
                    .order_by(self._history.c.row_id)
                )
            ).all()
            artifact_rows = (
                await connection.execute(
                    select(self._artifacts.c.turn_id, self._artifacts.c.artifact)
                    .where(self._artifacts.c.turn_id.in_(turn_ids))
                    .order_by(self._artifacts.c.turn_id, self._artifacts.c.row_id)
                )
            ).all()

        histories: dict[str, list[str]] = {turn_id: [] for turn_id in turn_ids}
        artifacts: dict[str, list[str]] = {turn_id: [] for turn_id in turn_ids}
        for turn_id, message in history_rows:
            histories[str(turn_id)].append(message)
        for turn_id, artifact in artifact_rows:
            artifacts[str(turn_id)].append(artifact)

        # When each turn began, from the order its first message was appended. A turn with no
        # history yet sorts last, which is where a just-opened turn belongs.
        started: dict[str, int] = {}
        for position, (turn_id, _message) in enumerate(history_rows):
            started.setdefault(str(turn_id), position)

        turns: list[Task] = []
        for head_row in sorted(head_rows, key=lambda row: started.get(str(row["id"]), len(history_rows))):
            turn_id = str(head_row["id"])
            data = {
                "id": turn_id,
                "context_id": head_row["session_id"],
                "kind": head_row["kind"] or "task",
                "status": json.loads(head_row["status"]),
                "metadata": json.loads(head_row["turn_metadata"]) if head_row["turn_metadata"] else None,
                "history": _compact_history([json.loads(message) for message in histories[turn_id]]),
                "artifacts": [json.loads(artifact) for artifact in artifacts[turn_id]] or None,
            }
            turns.append(Task.model_validate(data))
        return turns

    async def turn_page_for_session(
        self,
        session_id: str,
        *,
        before_row_id: int | None = None,
        limit: int = 400,
    ) -> dict:
        """A newest-first page of persisted task history for fast session replay.

        The returned tasks are fragments: each has the normal A2A task shape but
        only the history rows that fall in this page. Pages are queried newest
        first by append-only ``row_id`` and returned oldest-to-newest within the
        page so the client can prepend older pages and replay in chronological
        order. The terminal ``status.message`` and artifacts are included only
        when a fragment contains that task's newest persisted history row, which
        prevents duplicated failed/status messages when a long task spans pages.
        """
        await self._ensure_initialized()
        page_limit = max(1, min(limit, 1000))
        async with self._engine.connect() as connection:
            head_rows = (
                await connection.execute(
                    select(self._head).where(self._head.c.session_id == session_id)
                )
            ).mappings().all()
            if not head_rows:
                return {"turns": [], "next_before_row_id": None, "has_more": False}

            head_by_id = {str(row["id"]): row for row in head_rows}
            turn_ids: list[str] = []
            related_head_rows: list = []
            for row in head_rows:
                metadata = json.loads(row["turn_metadata"]) if row["turn_metadata"] else None
                if TurnRecord.from_metadata(metadata).reference_task_ids:
                    related_head_rows.append(row)
                    continue
                turn_ids.append(str(row["id"]))
            related_tasks = []
            if before_row_id is None:
                related_tasks = [
                    Task.model_validate({
                        "id": str(row["id"]),
                        "context_id": row["session_id"],
                        "kind": row["kind"] or "task",
                        "status": json.loads(row["status"]),
                        "metadata": json.loads(row["turn_metadata"]) if row["turn_metadata"] else None,
                        "history": [],
                        "artifacts": None,
                    })
                    for row in related_head_rows
                ]
            if not turn_ids:
                return {"turns": related_tasks, "next_before_row_id": None, "has_more": False}

            history_query = (
                select(self._history.c.row_id, self._history.c.turn_id, self._history.c.message)
                .where(self._history.c.turn_id.in_(turn_ids))
                .order_by(self._history.c.row_id.desc())
                .limit(page_limit + 1)
            )
            if before_row_id is not None:
                history_query = history_query.where(self._history.c.row_id < before_row_id)
            fetched_rows = (await connection.execute(history_query)).all()
            has_more = len(fetched_rows) > page_limit
            page_rows = fetched_rows[:page_limit]
            if not page_rows:
                return {"turns": related_tasks, "next_before_row_id": None, "has_more": False}

            first_row_by_turn: dict[str, int] = {}
            for row in page_rows:
                turn_id = str(row.turn_id)
                first_row_by_turn[turn_id] = min(first_row_by_turn.get(turn_id, int(row.row_id)), int(row.row_id))
            page_turn_ids = sorted(first_row_by_turn, key=first_row_by_turn.__getitem__)
            maximum_row_rows = (
                await connection.execute(
                    select(self._history.c.turn_id, func.max(self._history.c.row_id))
                    .where(self._history.c.turn_id.in_(page_turn_ids))
                    .group_by(self._history.c.turn_id)
                )
            ).all()
            maximum_row_by_turn = {str(turn_id): int(maximum_row) for turn_id, maximum_row in maximum_row_rows if maximum_row is not None}
            artifact_rows = (
                await connection.execute(
                    select(self._artifacts.c.turn_id, self._artifacts.c.artifact)
                    .where(self._artifacts.c.turn_id.in_(page_turn_ids))
                    .order_by(self._artifacts.c.turn_id, self._artifacts.c.row_id)
                )
            ).all()

        histories: dict[str, list[tuple[int, str]]] = {turn_id: [] for turn_id in page_turn_ids}
        include_status_message: dict[str, bool] = {turn_id: False for turn_id in page_turn_ids}
        for row in sorted(page_rows, key=lambda value: value.row_id):
            turn_id = str(row.turn_id)
            histories[turn_id].append((int(row.row_id), row.message))
            if int(row.row_id) == maximum_row_by_turn.get(turn_id):
                include_status_message[turn_id] = True

        artifacts: dict[str, list[str]] = {turn_id: [] for turn_id in page_turn_ids}
        for turn_id, artifact in artifact_rows:
            artifacts[str(turn_id)].append(artifact)

        tasks: list[Task] = []
        for turn_id in page_turn_ids:
            head_row = head_by_id[turn_id]
            status = json.loads(head_row["status"])
            if not include_status_message[turn_id] and isinstance(status, dict):
                status = {key: value for key, value in status.items() if key != "message"}
            data = {
                "id": turn_id,
                "context_id": head_row["session_id"],
                "kind": head_row["kind"] or "task",
                "status": status,
                "metadata": json.loads(head_row["turn_metadata"]) if head_row["turn_metadata"] else None,
                "history": _compact_history([json.loads(message) for _, message in histories[turn_id]]),
                "artifacts": [json.loads(artifact) for artifact in artifacts[turn_id]] or None,
            }
            tasks.append(Task.model_validate(data))

        tasks.extend(related_tasks)

        next_before_row_id = min(int(row.row_id) for row in page_rows)
        return {"turns": tasks, "next_before_row_id": next_before_row_id, "has_more": has_more}

    async def delete(self, turn_id: str, context: ServerCallContext | None = None) -> None:
        await self._ensure_initialized()
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                await connection.execute(delete(self._history).where(self._history.c.turn_id == turn_id))
                await connection.execute(delete(self._artifacts).where(self._artifacts.c.turn_id == turn_id))
                await connection.execute(delete(self._head).where(self._head.c.id == turn_id))
            self._persisted_counts.pop(turn_id, None)
            self._terminal_turns.discard(turn_id)
        finally:
            release_sqlite_write_lock(write_lock)

    async def delete_session(self, session_id: str) -> None:
        """Drop every durable trace of a context — its tasks (head/history/artifacts), its
        conversation checkpoint, and its goal/task session state — when a session is
        deleted. The single place that knows the turn store's tables, so session deletion
        does not reach into them."""
        await self._ensure_initialized()
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                turn_ids = (
                    await connection.execute(
                        select(self._head.c.id).where(self._head.c.session_id == session_id)
                    )
                ).scalars().all()
                for turn_id in turn_ids:
                    await connection.execute(delete(self._history).where(self._history.c.turn_id == turn_id))
                    await connection.execute(delete(self._artifacts).where(self._artifacts.c.turn_id == turn_id))
                await connection.execute(delete(self._head).where(self._head.c.session_id == session_id))
                await connection.execute(delete(self._checkpoint).where(self._checkpoint.c.session_id == session_id))
                await connection.execute(delete(self._session_state).where(self._session_state.c.session_id == session_id))
            for turn_id in turn_ids:
                self._persisted_counts.pop(str(turn_id), None)
                self._terminal_turns.discard(str(turn_id))
        finally:
            release_sqlite_write_lock(write_lock)

    async def input_required_session_ids(self) -> list[str]:
        """Context ids whose persisted task is input-required, so the sidebar's
        awaiting-input marker can be restored after a restart (the pause is durable)."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(select(self._head.c.session_id, self._head.c.status))
            ).all()
        contexts: list[str] = []
        for session_id, status in rows:
            try:
                state = str(json.loads(status).get("state", ""))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
            if state == TaskState.input_required.value and session_id:
                contexts.append(str(session_id))
        return contexts

    async def turn_ids_for_session(self, session_id: str) -> list[str]:
        """The ids of every task in a context — for replaying a session."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(self._head.c.id).where(self._head.c.session_id == session_id)
                )
            ).scalars().all()
        return list(rows)

    async def session_message_texts(self, session_id: str) -> list[str]:
        """Raw history-message JSON for every task in a context. Used to find the upload
        files a session references (attachment paths live in the message metadata) so they
        can be reclaimed when the session is deleted."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(self._history.c.message)
                    .select_from(self._history.join(self._head, self._history.c.turn_id == self._head.c.id))
                    .where(self._head.c.session_id == session_id)
                )
            ).scalars().all()
        return list(rows)

    async def any_history_references(self, needle: str) -> bool:
        """Whether any persisted history message contains ``needle`` (a file path). Used
        after a session delete to keep a content-addressed upload that is still referenced
        by another surviving session."""
        await self._ensure_initialized()
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(self._history.c.row_id)
                    .where(self._history.c.message.like(f"%{escaped}%", escape="\\"))
                    .limit(1)
                )
            ).first()
        return row is not None

    async def add_user_message(self, working_directory: str, message: str) -> None:
        """Store a user message in the project-scoped history."""
        await self._ensure_initialized()
        async with self._engine.begin() as connection:
            await connection.execute(
                self._user_messages.insert().values(
                    working_directory=working_directory,
                    message=message,
                )
            )

    async def get_user_messages(self, working_directory: str, limit: int = 100) -> list[str]:
        """Retrieve the most recent user messages for a project, newest first."""
        await self._ensure_initialized()
        async with self._engine.begin() as connection:
            result = await connection.execute(
                select(self._user_messages.c.message)
                .where(self._user_messages.c.working_directory == working_directory)
                .order_by(self._user_messages.c.created_at.desc())
                .limit(limit)
            )
        return [row[0] for row in result]
