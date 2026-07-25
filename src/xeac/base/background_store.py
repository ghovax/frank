"""Durable record of background jobs, so an interrupted long-running task can
survive a server restart.

Background jobs (slow bash, web searches, document parses) run as in-memory
``asyncio`` tasks. That is fine while the process lives, but a restart loses any
job that was still running — and, worse, any job that *finished* while the model
was idle but whose result had not yet been delivered as an autonomous wake. This
store persists both, so on startup the harness can:

* **deliver** results that completed-but-were-never-delivered (just wake the
  context), and
* **recover** jobs that were still running — re-issuing the idempotent ones
  (a web search, a cache-backed parse) and abandoning-with-a-note the ones that
  are unsafe to repeat (an arbitrary shell command).

The in-memory :class:`~xeac.runtime.background.BackgroundJobs` remains the source
of truth for *live* scheduling; this store mirrors it for durability and is the
authority only across a restart.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from xeac.base.paths import background_database_path as _background_database_path
from xeac.base.sqlite_lock import background_sqlite_write_lock, configure_background_sqlite_lock


BACKGROUND_DATABASE_FILENAME = "background.db"

# Lifecycle of a persisted job.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"      # finished; result stored; not yet delivered to the model
STATUS_DELIVERED = "delivered"      # result has reached the model (in-turn or via an autonomous wake)
STATUS_ABANDONED = "abandoned"      # could not be recovered after a restart (e.g. a bash command)


def background_database_path() -> Path:
    return _background_database_path()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _json_load(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return {} if fallback is None else fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {} if fallback is None else fallback


class BackgroundJobStore:
    """SQLite-backed mirror of every background job's lifecycle.

    Charter: this store owns background-job lifecycle and OS process-group reaping — it is NOT
    turn state. A turn's durable control-state and conversation live in the
    :class:`~xeac.daemon.persistence.turn_store.AppendOnlyTaskStore` (as a :class:`~xeac.protocol.turn_record.TurnRecord`
    and a checkpoint). This is a deliberately separate subsystem — results-durable and
    execution-ephemeral, additionally recovering running jobs and reaping orphaned process groups
    on restart — so it is not folded into the task store, and the two are never confused for one."""

    def __init__(self, database_path: Path | None = None):
        self.database_path = database_path or background_database_path()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        configure_background_sqlite_lock(self.database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with background_sqlite_write_lock():
            with self._connect() as connection:
                existing_table_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(background_jobs)").fetchall()
                }
                if existing_table_columns and "arguments_json" not in existing_table_columns:
                    connection.execute("DROP TABLE background_jobs")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS background_jobs (
                        job_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        agent_name TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        arguments_json TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        result_json TEXT,
                        created_at TEXT NOT NULL,
                        completed_at TEXT,
                        delivered_at TEXT,
                        -- OS process-group id of a job's shell subtree, so a reaper
                        -- can kill an orphan left by an unclean shutdown (SIGKILL /
                        -- crash) on the next start.
                        process_group INTEGER
                    )
                    """
                )
                existing_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(background_jobs)").fetchall()
                }
                if "process_group" not in existing_columns:
                    connection.execute("ALTER TABLE background_jobs ADD COLUMN process_group INTEGER")
                # Indices matched to the hot queries:
                #  - undelivered_jobs / has_undelivered_jobs (run after every turn and
                #    on startup) filter (session_id, agent_name, status);
                #  - running_jobs(agent) and contexts_with_undelivered filter
                #    (agent_name, status);
                #  - running_jobs() / orphaned_process_groups filter status alone.
                # The first covers session_id-only prefixes too, so no separate
                # (session_id, status) index is needed.
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_background_jobs_context_agent_status "
                    "ON background_jobs(session_id, agent_name, status)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_background_jobs_agent_status "
                    "ON background_jobs(agent_name, status)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_background_jobs_status ON background_jobs(status)"
                )

    def record_started(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        kind: str,
        arguments: dict[str, Any],
        tool_call_id: str = "",
    ) -> None:
        with background_sqlite_write_lock():
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO background_jobs (
                        job_id, session_id, agent_name, kind, arguments_json, tool_call_id,
                        status, result_json, created_at, completed_at, delivered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL)
                    """,
                    (
                        job_id,
                        session_id,
                        agent_name,
                        kind,
                        _json_dump(arguments),
                        tool_call_id,
                        STATUS_RUNNING,
                        _now(),
                    ),
                )

    def record_process_group(self, job_id: str, process_group: int) -> None:
        """Record the OS process-group id of a job's shell subtree once it has
        started, so :func:`reap_orphaned_processes` can kill it after a crash."""
        with background_sqlite_write_lock():
            with self._connect() as connection:
                connection.execute(
                    "UPDATE background_jobs SET process_group = ? WHERE job_id = ?",
                    (process_group, job_id),
                )

    def record_finished(self, job_id: str, result: str, *, status: str = STATUS_COMPLETED) -> None:
        """Mark a job completed (or failed — both carry a result payload the model reads)."""
        with background_sqlite_write_lock():
            with self._connect() as connection:
                connection.execute(
                    "UPDATE background_jobs SET status = ?, result_json = ?, completed_at = ? WHERE job_id = ?",
                    (status, result, _now(), job_id),
                )

    def mark_delivered(self, job_id: str) -> None:
        with background_sqlite_write_lock():
            with self._connect() as connection:
                connection.execute(
                    "UPDATE background_jobs SET status = ?, delivered_at = ? WHERE job_id = ?",
                    (STATUS_DELIVERED, _now(), job_id),
                )

    def mark_abandoned(self, job_id: str, result: str) -> None:
        with background_sqlite_write_lock():
            with self._connect() as connection:
                connection.execute(
                    "UPDATE background_jobs SET status = ?, result_json = ?, completed_at = ? WHERE job_id = ?",
                    (STATUS_ABANDONED, result, _now(), job_id),
                )

    def running_jobs(self, agent_name: str | None = None) -> list[dict[str, Any]]:
        """Jobs still marked running — i.e. in flight when the process last stopped."""
        if agent_name is None:
            return self._rows_where("status = ?", (STATUS_RUNNING,))
        return self._rows_where("status = ? AND agent_name = ?", (STATUS_RUNNING, agent_name))

    def orphaned_process_groups(self) -> list[int]:
        """Process groups of jobs still marked running from a previous, unclean
        shutdown — the orphans a reaper must kill on startup."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT process_group FROM background_jobs WHERE status = ? AND process_group IS NOT NULL",
                (STATUS_RUNNING,),
            ).fetchall()
        return [row["process_group"] for row in rows if row["process_group"]]

    def undelivered_jobs(self, session_id: str, agent_name: str) -> list[dict[str, Any]]:
        """A context's jobs that carry a result the model has not yet seen — whether
        they completed normally or were abandoned by a restart."""
        return self._rows_where(
            "session_id = ? AND agent_name = ? AND status IN (?, ?)",
            (session_id, agent_name, STATUS_COMPLETED, STATUS_ABANDONED),
        )

    def has_undelivered_jobs(self, session_id: str, agent_name: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM background_jobs WHERE session_id = ? AND agent_name = ? AND status IN (?, ?) LIMIT 1",
                (session_id, agent_name, STATUS_COMPLETED, STATUS_ABANDONED),
            ).fetchone()
        return row is not None

    def contexts_with_undelivered(self, agent_name: str) -> list[str]:
        """Distinct contexts of an agent that have any deliverable-but-undelivered
        result — the ones to wake on startup."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT session_id FROM background_jobs WHERE agent_name = ? AND status IN (?, ?)",
                (agent_name, STATUS_COMPLETED, STATUS_ABANDONED),
            ).fetchall()
        return [row["session_id"] for row in rows]

    def _rows_where(self, clause: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM background_jobs WHERE {clause} ORDER BY created_at ASC",
                params,
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def _row_to_job(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "session_id": row["session_id"],
            "agent_name": row["agent_name"],
            "kind": row["kind"],
            "arguments": _json_load(row["arguments_json"]),
            "tool_call_id": row["tool_call_id"],
            "status": row["status"],
            "result": row["result_json"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "delivered_at": row["delivered_at"],
            "process_group": row["process_group"],
        }


_STORE: BackgroundJobStore | None = None


def get_background_job_store() -> BackgroundJobStore:
    global _STORE
    if _STORE is None:
        _STORE = BackgroundJobStore()
    return _STORE
