"""An append-only A2A :class:`TaskStore`.

Why this exists.
A2A streams a turn as many small events (text-chunk flushes, thinking labels,
tool calls/results, and — for the chat agent — every relayed agent event).
The SDK's :class:`~a2a.server.tasks.TaskManager` appends each event's message to
``task.history`` *in memory* and then calls ``task_store.save(task)`` for every
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

from harness.core.message_content import content_block_identifier
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
            Column("context_id", String),
            Column("kind", String),
            Column("status", Text),
            Column("task_metadata", Text),
        )
        # Append-only history: one row per message, ordered by ``seq``.
        self._history = Table(
            "task_history",
            self._metadata,
            Column("row_id", Integer, primary_key=True, autoincrement=True),
            Column("task_id", String),
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
            Column("task_id", String),
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
            Column("working_directory", String),
            Column("message", Text),
            Column("created_at", DateTime, server_default=func.now()),
        )
        self._initialized = False

    async def initialize(self) -> None:
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                await connection.run_sync(self._metadata.create_all)
                await connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_task_head_context_id_id "
                    "ON task_head(context_id, id)"
                )
                await connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_task_history_task_id_row_id "
                    "ON task_history(task_id, row_id)"
                )
                await connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_task_artifacts_task_id_row_id "
                    "ON task_artifacts(task_id, row_id)"
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

    async def fail_orphaned_tasks(self) -> list[str]:
        """Fail tasks left nonterminal by a previous server process.

        In-memory executors cannot be resumed after a restart. Persisting an explicit
        interrupted failure prevents stale approvals, tools, and delegated-agent lanes
        from replaying as if they were still active.

        An ``input-required`` task is the exception: its resume checkpoint (the pending
        interactions and the conversation) is durable, so it is preserved rather than
        failed — a later answer rebuilds and resumes it from the database.
        """
        await self._ensure_initialized()
        write_lock = await acquire_sqlite_write_lock()
        orphaned_task_ids: list[str] = []
        preserved_states = _TERMINAL_TASK_STATES | {TaskState.input_required.value}
        try:
            async with self._engine.begin() as connection:
                head_rows = (await connection.execute(select(self._head))).mappings().all()
                for head_row in head_rows:
                    current_status = json.loads(head_row["status"])
                    current_state = str(current_status.get("state", ""))
                    if current_state in preserved_states:
                        continue
                    task_id = str(head_row["id"])
                    context_id = str(head_row["context_id"] or "")
                    interrupted_message = Message(
                        role=Role.agent,
                        parts=[Part(root=DataPart(data={
                            "kind": "error",
                            "code": "turn_interrupted",
                            "message": "This task was interrupted because the server restarted.",
                        }))],
                        message_id=uuid.uuid4().hex,
                        task_id=task_id,
                        context_id=context_id or None,
                    )
                    interrupted_status = TaskStatus(
                        state=TaskState.failed,
                        message=interrupted_message,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                    await connection.execute(
                        update(self._head)
                        .where(self._head.c.id == task_id)
                        .values(status=_dump(interrupted_status))
                    )
                    orphaned_task_ids.append(task_id)
        finally:
            release_sqlite_write_lock(write_lock)
        return orphaned_task_ids

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

    async def _compact_persisted_history(self, connection, task_id: str, messages: list) -> int:
        compacted_messages = _compact_history(messages)
        existing_rows = (
            await connection.execute(
                select(self._history.c.row_id)
                .where(self._history.c.task_id == task_id)
                .order_by(self._history.c.seq)
            )
        ).all()

        for message_index, message in enumerate(compacted_messages):
            if message_index < len(existing_rows):
                await connection.execute(
                    update(self._history)
                    .where(self._history.c.row_id == existing_rows[message_index].row_id)
                    .values(message=json.dumps(message))
                )
            else:
                await connection.execute(
                    self._history.insert().values(
                        task_id=task_id,
                        seq=message_index,
                        message=json.dumps(message),
                    )
                )

        if compacted_messages:
            await connection.execute(
                delete(self._history).where(
                    self._history.c.task_id == task_id,
                    self._history.c.seq >= len(compacted_messages),
                )
            )
        else:
            await connection.execute(delete(self._history).where(self._history.c.task_id == task_id))
        return len(compacted_messages)

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        await self._ensure_initialized()
        history = task.history or []
        artifacts = task.artifacts or []
        terminal = _is_terminal_task(task)
        write_lock = await acquire_sqlite_write_lock()
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

                if terminal:
                    compacted_count = await self._compact_persisted_history(
                        connection,
                        task.id,
                        [message.model_dump(mode="json") for message in history],
                    )
                    self._persisted_count[task.id] = compacted_count
                else:
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
            release_sqlite_write_lock(write_lock)

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
            related_head_rows: list = []
            for row in head_rows:
                metadata = json.loads(row["task_metadata"]) if row["task_metadata"] else None
                if isinstance(metadata, dict) and isinstance(metadata.get("referenceTaskIds"), list):
                    related_head_rows.append(row)
                    continue
                task_ids.append(str(row["id"]))
            related_tasks = []
            if before_row_id is None:
                related_tasks = [
                    Task.model_validate({
                        "id": str(row["id"]),
                        "context_id": row["context_id"],
                        "kind": row["kind"] or "task",
                        "status": json.loads(row["status"]),
                        "metadata": json.loads(row["task_metadata"]) if row["task_metadata"] else None,
                        "history": [],
                        "artifacts": None,
                    })
                    for row in related_head_rows
                ]
            if not task_ids:
                return {"tasks": related_tasks, "next_before_row_id": None, "has_more": False}

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
                return {"tasks": related_tasks, "next_before_row_id": None, "has_more": False}

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

        tasks.extend(related_tasks)

        next_before_row_id = min(int(row.row_id) for row in page_rows)
        return {"tasks": tasks, "next_before_row_id": next_before_row_id, "has_more": has_more}

    async def delete(self, task_id: str, context: ServerCallContext | None = None) -> None:
        await self._ensure_initialized()
        write_lock = await acquire_sqlite_write_lock()
        try:
            async with self._engine.begin() as connection:
                await connection.execute(delete(self._history).where(self._history.c.task_id == task_id))
                await connection.execute(delete(self._artifacts).where(self._artifacts.c.task_id == task_id))
                await connection.execute(delete(self._head).where(self._head.c.id == task_id))
        finally:
            release_sqlite_write_lock(write_lock)
        self._persisted_count.pop(task_id, None)

    async def input_required_context_ids(self) -> list[str]:
        """Context ids whose persisted task is input-required, so the sidebar's
        awaiting-input marker can be restored after a restart (the pause is durable)."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(select(self._head.c.context_id, self._head.c.status))
            ).all()
        contexts: list[str] = []
        for context_id, status in rows:
            try:
                state = str(json.loads(status).get("state", ""))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
            if state == TaskState.input_required.value and context_id:
                contexts.append(str(context_id))
        return contexts

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

    async def context_message_texts(self, context_id: str) -> list[str]:
        """Raw history-message JSON for every task in a context. Used to find the upload
        files a session references (attachment paths live in the message metadata) so they
        can be reclaimed when the session is deleted."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(self._history.c.message)
                    .select_from(self._history.join(self._head, self._history.c.task_id == self._head.c.id))
                    .where(self._head.c.context_id == context_id)
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
