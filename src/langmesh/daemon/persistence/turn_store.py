"""An append-only A2A task store, so a save costs the new messages rather than the whole turn."""

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

from langmesh.base.message_content import content_block_identifier
from langmesh.base.serialization import compact, conversation_snapshot_id
from langmesh.base.sqlite_lock import acquire_sqlite_write_lock, release_sqlite_write_lock
from langmesh.protocol.turn_record import ReconcileAction, TurnRecord, reconcile_action


def _dump(model) -> str:
    """Serialize a model to JSON by field names, mirroring how the SDK's own store round-trips."""
    return json.dumps(model.model_dump(mode="json"))


# Adjacent same-kind deltas merge into one message, so a replay re-reduces far fewer and larger rows.

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
    """A hashable identity for the agent that produced an event, so merging only happens within one agent."""
    path = data.get("path") or []
    return tuple((segment.get("group_id"), segment.get("step_id")) for segment in path)


def _compact_history(messages: list) -> list:
    """Merge adjacent same-kind single-part agent messages into one message each."""
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
            # An agent's text arrives path-tagged, so only adjacent events from the same path merge.
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


# The turn kind carried in a task's head metadata, which restart reconciliation reads to decide its fate.
def _task_state_value(task: Task) -> str:
    state = task.status.state
    return state.value if isinstance(state, TaskState) else str(state)


def _is_terminal_task(task: Task) -> bool:
    return _task_state_value(task) in _TERMINAL_TASK_STATES


class AppendOnlyTaskStore(TaskStore):
    """An A2A task store that persists history and artifacts incrementally, across three tables."""

    def __init__(self, engine: AsyncEngine):
        self._engine = engine
        self._metadata = MetaData()
        # The task head: small, mutable, and under its own table name so it cannot collide with an older schema.
        self._head = Table(
            "turn_head",
            self._metadata,
            Column("id", String, primary_key=True),
            Column("session_id", String),
            Column("kind", String),
            Column("status", Text),
            Column("turn_metadata", Text),
        )
        # Append-only history ordered by the database's own autoincrement, so concurrent appends cannot collide.
        self._history = Table(
            "turn_history",
            self._metadata,
            Column("row_id", Integer, primary_key=True, autoincrement=True),
            Column("turn_id", String),
            Column("message", Text),
            sqlite_autoincrement=True,
        )
        # Artifacts are few and may be revised, so they upsert by id.
        self._artifacts = Table(
            "turn_artifacts",
            self._metadata,
            Column("row_id", Integer, primary_key=True, autoincrement=True),
            Column("turn_id", String),
            Column("artifact_id", String),
            Column("artifact", Text),
            UniqueConstraint("turn_id", "artifact_id", name="uq_task_artifact_id"),
        )
        # The turn's durable resume checkpoint: the model-facing conversation, one row per context.
        self._checkpoint = Table(
            "turn_checkpoint",
            self._metadata,
            Column("session_id", String, primary_key=True),
            Column("turn_id", String),
            Column("messages", Text),
            Column("updated_at", String),
        )
        # An inherited prefix is content-addressed so identical forks share one row, and the checkpoint above holds only what the child wrote after the fork.
        self._conversation_snapshot = Table(
            "conversation_snapshot",
            self._metadata,
            Column("snapshot_id", String, primary_key=True),
            Column("messages", Text),
        )
        self._conversation_inheritance = Table(
            "conversation_inheritance",
            self._metadata,
            Column("session_id", String, primary_key=True),
            Column("snapshot_id", String),
        )
        # A context's durable goal and task list, beside the checkpoint so a restart restores the objective too.
        self._session_state = Table(
            "session_state",
            self._metadata,
            Column("session_id", String, primary_key=True),
            Column("state", Text),
            Column("updated_at", String),
        )
        # User message history scoped to the working directory, for arrow-key recall within a project.
        self._user_messages = Table(
            "user_message_history",
            self._metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("working_directory", String),
            Column("message", Text),
            Column("created_at", DateTime, server_default=func.now()),
        )
        self._initialized = False
        # How many history rows are persisted per task, kept in memory under the write lock rather than re-counted.
        self._persisted_counts: dict[str, int] = {}
        # Tasks whose history has been terminally compacted, so a stray later save is caught rather than duplicating rows.
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
                    "CREATE INDEX IF NOT EXISTS idx_conversation_inheritance_snapshot_id "
                    "ON conversation_inheritance(snapshot_id)"
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
        """Restart reconciliation: a durable pause is restored, and anything else non-terminal is marked failed."""
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
                            "message": "This turn was interrupted because the daemon restarted.",
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
        inherited_snapshot_id: str = "",
    ) -> None:
        """Atomically snapshot a context's conversation checkpoint and, when it changed, its goal and task state — a child writing only the suffix beyond ``inherited_snapshot_id``, and omitting that pointer detaching the prefix into a whole snapshot."""
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
                detached = False
                if inherited_snapshot_id:
                    snapshot_exists = (
                        await connection.execute(
                            select(self._conversation_snapshot.c.snapshot_id).where(
                                self._conversation_snapshot.c.snapshot_id == inherited_snapshot_id
                            )
                        )
                    ).scalar()
                    if snapshot_exists is None:
                        raise ValueError(f"Unknown inherited conversation snapshot {inherited_snapshot_id!r}")
                    inheritance_insert = sqlite_insert(self._conversation_inheritance).values(
                        session_id=session_id,
                        snapshot_id=inherited_snapshot_id,
                    )
                    await connection.execute(
                        inheritance_insert.on_conflict_do_update(
                            index_elements=[self._conversation_inheritance.c.session_id],
                            set_={"snapshot_id": inheritance_insert.excluded.snapshot_id},
                        )
                    )
                else:
                    result = await connection.execute(
                        delete(self._conversation_inheritance).where(
                            self._conversation_inheritance.c.session_id == session_id
                        )
                    )
                    detached = bool(result.rowcount)
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
                if detached:
                    await self._delete_unreferenced_conversation_snapshots(connection)
        finally:
            release_sqlite_write_lock(write_lock)

    async def seed_inherited_conversation(self, session_id: str, messages: list[dict]) -> str:
        """Point a new child at a shared snapshot and start its local checkpoint empty."""
        await self._ensure_initialized()
        if not session_id or not messages:
            return ""
        snapshot_id = conversation_snapshot_id(messages)
        now = datetime.now(timezone.utc).isoformat()
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    sqlite_insert(self._conversation_snapshot)
                    .values(snapshot_id=snapshot_id, messages=compact(messages))
                    .on_conflict_do_nothing(index_elements=[self._conversation_snapshot.c.snapshot_id])
                )
                checkpoint_insert = sqlite_insert(self._checkpoint).values(
                    session_id=session_id, turn_id="", messages="[]", updated_at=now,
                )
                await connection.execute(
                    checkpoint_insert.on_conflict_do_update(
                        index_elements=[self._checkpoint.c.session_id],
                        set_={"turn_id": "", "messages": "[]", "updated_at": now},
                    )
                )
                inheritance_insert = sqlite_insert(self._conversation_inheritance).values(
                    session_id=session_id, snapshot_id=snapshot_id,
                )
                await connection.execute(
                    inheritance_insert.on_conflict_do_update(
                        index_elements=[self._conversation_inheritance.c.session_id],
                        set_={"snapshot_id": snapshot_id},
                    )
                )
        finally:
            release_sqlite_write_lock(write_lock)
        return snapshot_id

    async def _delete_unreferenced_conversation_snapshots(self, connection) -> None:
        referenced = select(self._conversation_inheritance.c.snapshot_id)
        await connection.execute(
            delete(self._conversation_snapshot).where(
                self._conversation_snapshot.c.snapshot_id.not_in(referenced)
            )
        )

    async def save_session_state(self, session_id: str, session_state: dict) -> None:
        """Write a context's goal and task state alone, for the changes that happen outside a turn."""
        await self._ensure_initialized()
        if not session_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
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

    async def load_checkpoint(self, session_id: str) -> dict:
        """The context's whole conversation and its shared-prefix boundary, for the caller to rehydrate and repair."""
        await self._ensure_initialized()
        if not session_id:
            return {"messages": [], "inherited_snapshot_id": "", "inherited_message_count": 0}
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        self._checkpoint.c.messages,
                        self._conversation_inheritance.c.snapshot_id,
                        self._conversation_snapshot.c.messages,
                    )
                    .select_from(
                        self._checkpoint
                        .outerjoin(
                            self._conversation_inheritance,
                            self._conversation_inheritance.c.session_id == self._checkpoint.c.session_id,
                        )
                        .outerjoin(
                            self._conversation_snapshot,
                            self._conversation_snapshot.c.snapshot_id == self._conversation_inheritance.c.snapshot_id,
                        )
                    )
                    .where(self._checkpoint.c.session_id == session_id)
                )
            ).first()
        if not row:
            return {"messages": [], "inherited_snapshot_id": "", "inherited_message_count": 0}
        try:
            local_messages = json.loads(row[0])
            inherited_messages = json.loads(row[2]) if row[1] and row[2] else []
            if not isinstance(local_messages, list) or not isinstance(inherited_messages, list):
                raise TypeError("Conversation checkpoints must contain message lists")
            return {
                "messages": [*inherited_messages, *local_messages],
                "inherited_snapshot_id": str(row[1] or ""),
                "inherited_message_count": len(inherited_messages),
            }
        except (json.JSONDecodeError, TypeError):
            return {"messages": [], "inherited_snapshot_id": "", "inherited_message_count": 0}

    async def load_session_state(self, session_id: str) -> dict:
        """The context's persisted goal and task state, or an empty dict for a fresh context."""
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
        """How many history rows are already persisted, so a save appends only the suffix not yet stored."""
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
        """Rewrite a task's already-persisted history in place with its compacted form, keeping row order."""
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
            # The persisted rows are already the compacted merge, so a later non-terminal save would duplicate them.
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

                # Insert only the messages not yet persisted, since the list only ever grows.
                persisted = await self._persisted_count(connection, task.id)
                new_messages = history[persisted:]
                if new_messages:
                    await connection.execute(
                        self._history.insert(),
                        [{"turn_id": task.id, "message": _dump(message)} for message in new_messages],
                    )
                    self._persisted_counts[task.id] = persisted + len(new_messages)

                if terminal:
                    # Compact in place with no new row ids, and record the terminal count so a stray save is caught.
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
        """Every task in a context, batched into one head, history and artifact pass rather than three queries per task."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            head_rows = (
                await connection.execute(
                    select(self._head)
                    .where(self._head.c.session_id == session_id)
                    # Ordered by when each turn actually started, since a turn's id is a random UUID and sorts arbitrarily.
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
                    # Globally by row id rather than grouped, because the append order is the chronology.
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

        # When each turn began, from its first appended message; a turn with no history yet sorts last.
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
        """A newest-first page of persisted history, returning task fragments carrying only this page's rows."""
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
        """Drop every durable trace of a context, so session deletion does not need to know this store's tables."""
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
                await connection.execute(
                    delete(self._conversation_inheritance).where(
                        self._conversation_inheritance.c.session_id == session_id
                    )
                )
                await self._delete_unreferenced_conversation_snapshots(connection)
                await connection.execute(delete(self._session_state).where(self._session_state.c.session_id == session_id))
            for turn_id in turn_ids:
                self._persisted_counts.pop(str(turn_id), None)
                self._terminal_turns.discard(str(turn_id))
        finally:
            release_sqlite_write_lock(write_lock)

    async def input_required_session_ids(self) -> list[str]:
        """Context ids whose persisted task is input-required, so the awaiting-input marker survives a restart."""
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
        """Raw history JSON for every task in a context, used to find the uploads a session references."""
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
        """Whether any persisted history mentions `needle`, so a shared upload survives one session's deletion."""
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
