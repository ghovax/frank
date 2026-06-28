"""An append-only A2A :class:`TaskStore`.

Why this exists
---------------
A2A streams a turn as many small events (text-chunk flushes, thinking labels,
tool calls/results, and — for the chat agent — every relayed sub-agent event).
The SDK's :class:`~a2a.server.tasks.TaskManager` appends each event's message to
``task.history`` *in memory* and then calls ``task_store.save(task)`` for every
event. The bundled :class:`~a2a.server.tasks.DatabaseTaskStore` persists a task
by ``session.merge`` of the whole row — i.e. it re-serializes and rewrites the
*entire, ever-growing* ``history`` JSON blob on every event.

For a turn that emits *N* events that is ``1 + 2 + … + N = O(N²)`` bytes written,
and with a chat agent relaying sub-agent activity (plus large web/MCP tool
results) the blob reaches megabytes and is rewritten ~20×/second — multiple
megabytes per second of disk I/O, and each write holds SQLite's single write
lock long enough to stall the concurrent sub-agent turns.

The fix is to normalize the history into append-only rows: the task's small,
mutable head (status + metadata) is upserted, and each new history message /
artifact is inserted exactly once. ``history`` only ever grows (the TaskManager
appends), so ``save`` writes just the new suffix — O(delta) per event, O(N) per
turn, with per-event cost independent of how long the turn has been running.

The live transport is unchanged: the SDK still streams incremental
``TaskStatusUpdateEvent``/``TaskArtifactUpdateEvent`` objects over SSE. Only the
persistence layer changes.
"""

import json
from typing import Optional

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import Task


def _dump(model) -> str:
    """Serialize a pydantic model to a JSON string using field names, mirroring
    how the SDK's DatabaseTaskStore round-trips (field names, not aliases)."""
    return json.dumps(model.model_dump(mode="json"))


class AppendOnlyTaskStore(TaskStore):
    """A2A task store that persists history/artifacts incrementally.

    Drop-in replacement for ``DatabaseTaskStore``: implements the same
    ``save``/``get``/``delete`` contract, but stores a task across three tables
    so a save is O(new messages) rather than O(whole history).
    """

    def __init__(self, engine: AsyncEngine):
        self._engine = engine
        self._metadata = MetaData()
        # The task head: small and mutable. A distinct table name (not ``tasks``)
        # so it never collides with a pre-existing DatabaseTaskStore schema in an
        # older database; a context lookup reads one compact row per task.
        self._head = Table(
            "task_head",
            self._metadata,
            Column("id", String, primary_key=True),
            Column("context_id", String, index=True),
            Column("kind", String),
            Column("status", Text),
            Column("task_metadata", Text),
        )
        # Append-only history: one row per message, ordered by ``seq``.
        self._history = Table(
            "task_history",
            self._metadata,
            Column("row_id", Integer, primary_key=True, autoincrement=True),
            Column("task_id", String, index=True),
            Column("seq", Integer),
            Column("message", Text),
            UniqueConstraint("task_id", "seq", name="uq_task_history_seq"),
        )
        # Artifacts are few and may be revised in place, so they upsert by id
        # (bounded by artifact count, never by history length).
        self._artifacts = Table(
            "task_artifacts",
            self._metadata,
            Column("row_id", Integer, primary_key=True, autoincrement=True),
            Column("task_id", String, index=True),
            Column("artifact_id", String),
            Column("artifact", Text),
            UniqueConstraint("task_id", "artifact_id", name="uq_task_artifact_id"),
        )
        # How many history rows are already persisted per task. Events for a given
        # task are processed sequentially by its TaskManager, so this needs no
        # locking; it lets the hot path skip a COUNT query.
        self._persisted_count: dict[str, int] = {}
        self._initialized = False

    async def initialize(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(self._metadata.create_all)
        self._initialized = True

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.initialize()

    async def _history_count(self, connection, task_id: str) -> int:
        cached = self._persisted_count.get(task_id)
        if cached is not None:
            return cached
        result = await connection.execute(
            select(func.count()).select_from(self._history).where(self._history.c.task_id == task_id)
        )
        count = int(result.scalar() or 0)
        self._persisted_count[task_id] = count
        return count

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        await self._ensure_initialized()
        history = task.history or []
        artifacts = task.artifacts or []
        async with self._engine.begin() as connection:
            # Head: tiny upsert of the latest status + metadata.
            head_values = {
                "id": task.id,
                "context_id": task.context_id,
                "kind": task.kind,
                "status": _dump(task.status),
                "task_metadata": json.dumps(task.metadata) if task.metadata is not None else None,
            }
            head_insert = sqlite_insert(self._head).values(**head_values)
            await connection.execute(
                head_insert.on_conflict_do_update(
                    index_elements=[self._head.c.id],
                    set_={
                        "context_id": head_values["context_id"],
                        "kind": head_values["kind"],
                        "status": head_values["status"],
                        "task_metadata": head_values["task_metadata"],
                    },
                )
            )

            # History: insert only the messages not yet persisted. The list only
            # ever grows, so the already-stored prefix is never rewritten.
            persisted = await self._history_count(connection, task.id)
            new_messages = history[persisted:]
            if new_messages:
                await connection.execute(
                    self._history.insert(),
                    [
                        {"task_id": task.id, "seq": persisted + offset, "message": _dump(message)}
                        for offset, message in enumerate(new_messages)
                    ],
                )
                self._persisted_count[task.id] = persisted + len(new_messages)

            # Artifacts: upsert each by id (replace-in-place is safe and bounded).
            for artifact in artifacts:
                artifact_insert = sqlite_insert(self._artifacts).values(
                    task_id=task.id,
                    artifact_id=artifact.artifact_id,
                    artifact=_dump(artifact),
                )
                await connection.execute(
                    artifact_insert.on_conflict_do_update(
                        index_elements=[self._artifacts.c.task_id, self._artifacts.c.artifact_id],
                        set_={"artifact": _dump(artifact)},
                    )
                )

    async def get(self, task_id: str, context: ServerCallContext | None = None) -> Optional[Task]:
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            head_row = (
                await connection.execute(select(self._head).where(self._head.c.id == task_id))
            ).mappings().first()
            if head_row is None:
                return None
            history_rows = (
                await connection.execute(
                    select(self._history.c.message)
                    .where(self._history.c.task_id == task_id)
                    .order_by(self._history.c.seq)
                )
            ).scalars().all()
            artifact_rows = (
                await connection.execute(
                    select(self._artifacts.c.artifact).where(self._artifacts.c.task_id == task_id)
                )
            ).scalars().all()

        data = {
            "id": head_row["id"],
            "context_id": head_row["context_id"],
            "kind": head_row["kind"] or "task",
            "status": json.loads(head_row["status"]),
            "metadata": json.loads(head_row["task_metadata"]) if head_row["task_metadata"] else None,
            "history": [json.loads(message) for message in history_rows],
            "artifacts": [json.loads(artifact) for artifact in artifact_rows] or None,
        }
        return Task.model_validate(data)

    async def delete(self, task_id: str, context: ServerCallContext | None = None) -> None:
        await self._ensure_initialized()
        async with self._engine.begin() as connection:
            await connection.execute(delete(self._history).where(self._history.c.task_id == task_id))
            await connection.execute(delete(self._artifacts).where(self._artifacts.c.task_id == task_id))
            await connection.execute(delete(self._head).where(self._head.c.id == task_id))
        self._persisted_count.pop(task_id, None)

    async def task_ids_for_context(self, context_id: str) -> list[str]:
        """The ids of every task in a context — for replaying a session."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(self._head.c.id).where(self._head.c.context_id == context_id)
                )
            ).scalars().all()
        return list(rows)
