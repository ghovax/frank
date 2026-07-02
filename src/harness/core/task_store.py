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
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import Task

from harness.core.sqlite_lock import acquire_sqlite_write_lock, release_sqlite_write_lock


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


def _agent_text(message: object) -> str | None:
    if not isinstance(message, dict) or message.get("role") != "agent":
        return None
    part = _sole_part(message)
    if not part or part.get("kind") != "text":
        return None
    return str(part.get("text", ""))


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


def _compact_history(messages: list) -> list:
    """Merge adjacent same-kind single-part agent messages (plain text, sub-task
    text in the same step, and reasoning) into one message each."""
    compacted: list = []
    for message in messages:
        text = _agent_text(message)
        if text is not None:
            last = compacted[-1] if compacted else None
            if last is not None and _agent_text(last) is not None:
                last["parts"] = [{"kind": "text", "text": _agent_text(last) + text}]  # type: ignore[index]
                continue
            compacted.append(message)
            continue
        sub = _sole_data(message, "sub_task_text")
        if sub is not None:
            key = (sub.get("groupId"), sub.get("stepId"), sub.get("childTaskId"))
            last = compacted[-1] if compacted else None
            last_sub = _sole_data(last, "sub_task_text") if last is not None else None
            last_key = (
                (last_sub.get("groupId"), last_sub.get("stepId"), last_sub.get("childTaskId"))
                if last_sub is not None else None
            )
            if last_sub is not None and last_key == key:
                last["parts"] = [{"kind": "data", "data": {**last_sub, "text": str(last_sub.get("text", "")) + str(sub.get("text", ""))}}]  # type: ignore[index]
                continue
            compacted.append(message)
            continue
        thinking = _sole_data(message, "thinking")
        if thinking is not None:
            last = compacted[-1] if compacted else None
            last_thinking = _sole_data(last, "thinking") if last is not None else None
            if last_thinking is not None:
                last["parts"] = [{"kind": "data", "data": {**last_thinking, "text": str(last_thinking.get("text", "")) + str(thinking.get("text", ""))}}]  # type: ignore[index]
                continue
            compacted.append(message)
            continue
        compacted.append(message)
    return compacted


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
        # User message history, scoped to the working directory. Used for
        # up/down arrow recall of previously sent messages within a project.
        self._user_messages = Table(
            "user_message_history",
            self._metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("working_directory", String, index=True),
            Column("message", Text),
            Column("created_at", DateTime, server_default=func.now()),
        )
        self._initialized = False

    async def initialize(self) -> None:
        await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                await connection.run_sync(self._metadata.create_all)
        finally:
            release_sqlite_write_lock()
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
        await acquire_sqlite_write_lock()
        try:
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

                # History: insert only the messages not yet persisted. The list
                # only ever grows, so the already-stored prefix is never rewritten.
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
        finally:
            release_sqlite_write_lock()

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

    async def tasks_for_context(self, context_id: str) -> list[Task]:
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
                    .where(self._head.c.context_id == context_id)
                    .order_by(self._head.c.id)
                )
            ).mappings().all()
            task_ids = [str(row["id"]) for row in head_rows]
            if not task_ids:
                return []

            history_rows = (
                await connection.execute(
                    select(self._history.c.task_id, self._history.c.message)
                    .where(self._history.c.task_id.in_(task_ids))
                    .order_by(self._history.c.task_id, self._history.c.seq)
                )
            ).all()
            artifact_rows = (
                await connection.execute(
                    select(self._artifacts.c.task_id, self._artifacts.c.artifact)
                    .where(self._artifacts.c.task_id.in_(task_ids))
                    .order_by(self._artifacts.c.task_id, self._artifacts.c.row_id)
                )
            ).all()

        histories: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
        artifacts: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
        for task_id, message in history_rows:
            histories[str(task_id)].append(message)
        for task_id, artifact in artifact_rows:
            artifacts[str(task_id)].append(artifact)

        tasks: list[Task] = []
        for head_row in head_rows:
            task_id = str(head_row["id"])
            data = {
                "id": task_id,
                "context_id": head_row["context_id"],
                "kind": head_row["kind"] or "task",
                "status": json.loads(head_row["status"]),
                "metadata": json.loads(head_row["task_metadata"]) if head_row["task_metadata"] else None,
                "history": _compact_history([json.loads(message) for message in histories[task_id]]),
                "artifacts": [json.loads(artifact) for artifact in artifacts[task_id]] or None,
            }
            tasks.append(Task.model_validate(data))
        return tasks

    async def task_page_for_context(
        self,
        context_id: str,
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
                    select(self._head).where(self._head.c.context_id == context_id)
                )
            ).mappings().all()
            if not head_rows:
                return {"tasks": [], "next_before_row_id": None, "has_more": False}

            head_by_id = {str(row["id"]): row for row in head_rows}
            task_ids: list[str] = []
            for row in head_rows:
                metadata = json.loads(row["task_metadata"]) if row["task_metadata"] else None
                if isinstance(metadata, dict) and isinstance(metadata.get("referenceTaskIds"), list):
                    continue
                task_ids.append(str(row["id"]))
            if not task_ids:
                return {"tasks": [], "next_before_row_id": None, "has_more": False}

            history_query = (
                select(self._history.c.row_id, self._history.c.task_id, self._history.c.seq, self._history.c.message)
                .where(self._history.c.task_id.in_(task_ids))
                .order_by(self._history.c.row_id.desc())
                .limit(page_limit + 1)
            )
            if before_row_id is not None:
                history_query = history_query.where(self._history.c.row_id < before_row_id)
            fetched_rows = (await connection.execute(history_query)).all()
            has_more = len(fetched_rows) > page_limit
            page_rows = fetched_rows[:page_limit]
            if not page_rows:
                return {"tasks": [], "next_before_row_id": None, "has_more": False}

            first_row_by_task: dict[str, int] = {}
            for row in page_rows:
                task_id = str(row.task_id)
                first_row_by_task[task_id] = min(first_row_by_task.get(task_id, int(row.row_id)), int(row.row_id))
            page_task_ids = sorted(first_row_by_task, key=first_row_by_task.__getitem__)
            maximum_seq_rows = (
                await connection.execute(
                    select(self._history.c.task_id, func.max(self._history.c.seq))
                    .where(self._history.c.task_id.in_(page_task_ids))
                    .group_by(self._history.c.task_id)
                )
            ).all()
            maximum_seq_by_task = {str(task_id): int(maximum_seq) for task_id, maximum_seq in maximum_seq_rows if maximum_seq is not None}
            artifact_rows = (
                await connection.execute(
                    select(self._artifacts.c.task_id, self._artifacts.c.artifact)
                    .where(self._artifacts.c.task_id.in_(page_task_ids))
                    .order_by(self._artifacts.c.task_id, self._artifacts.c.row_id)
                )
            ).all()

        histories: dict[str, list[tuple[int, str]]] = {task_id: [] for task_id in page_task_ids}
        include_status_message: dict[str, bool] = {task_id: False for task_id in page_task_ids}
        for row in sorted(page_rows, key=lambda value: value.row_id):
            task_id = str(row.task_id)
            histories[task_id].append((int(row.row_id), row.message))
            if int(row.seq) == maximum_seq_by_task.get(task_id):
                include_status_message[task_id] = True

        artifacts: dict[str, list[str]] = {task_id: [] for task_id in page_task_ids}
        for task_id, artifact in artifact_rows:
            artifacts[str(task_id)].append(artifact)

        tasks: list[Task] = []
        for task_id in page_task_ids:
            head_row = head_by_id[task_id]
            status = json.loads(head_row["status"])
            if not include_status_message[task_id] and isinstance(status, dict):
                status = {key: value for key, value in status.items() if key != "message"}
            data = {
                "id": task_id,
                "context_id": head_row["context_id"],
                "kind": head_row["kind"] or "task",
                "status": status,
                "metadata": json.loads(head_row["task_metadata"]) if head_row["task_metadata"] else None,
                "history": _compact_history([json.loads(message) for _, message in histories[task_id]]),
                "artifacts": [json.loads(artifact) for artifact in artifacts[task_id]] or None,
            }
            tasks.append(Task.model_validate(data))

        next_before_row_id = min(int(row.row_id) for row in page_rows)
        return {"tasks": tasks, "next_before_row_id": next_before_row_id, "has_more": has_more}

    async def delete(self, task_id: str, context: ServerCallContext | None = None) -> None:
        await self._ensure_initialized()
        await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                await connection.execute(delete(self._history).where(self._history.c.task_id == task_id))
                await connection.execute(delete(self._artifacts).where(self._artifacts.c.task_id == task_id))
                await connection.execute(delete(self._head).where(self._head.c.id == task_id))
        finally:
            release_sqlite_write_lock()
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
