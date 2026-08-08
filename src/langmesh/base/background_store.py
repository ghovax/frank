"""The durable record of background jobs, so an interrupted task survives a restart."""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langmesh.base.paths import background_database_path as _background_database_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langmesh.base.ports import JobStore
from langmesh.base.sqlite_lock import background_sqlite_write_lock, configure_background_sqlite_lock


BACKGROUND_DATABASE_FILENAME = "background.db"

# Lifecycle of a persisted job.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"  # finished; result stored; not yet delivered to the model
STATUS_DELIVERED = "delivered"  # result has reached the model (in-turn or via an autonomous wake)
STATUS_ABANDONED = "abandoned"  # could not be recovered after a restart (e.g. a bash command)


def background_database_path() -> Path:
    return _background_database_path()


def reap_orphaned_process_groups(store: "JobStore | None" = None) -> int:
    """Kill process groups left behind by a previous, unclean shutdown."""
    killed = 0
    for process_group in (store or get_background_job_store()).orphaned_process_groups():
        try:
            os.killpg(process_group, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            # Already gone — the common case (the child died with the crash).
            continue
        except OSError as error:
            logging.getLogger(__name__).warning(
                "Could not reap orphaned process group %s: %s", process_group, error
            )
    return killed


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
    """A SQLite mirror of every background job's lifecycle, which is not turn state."""

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
                # Indices matched to the hot queries, so the startup and per-turn scans stay cheap.
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_background_jobs_context_agent_status ON background_jobs(session_id, agent_name, status)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_background_jobs_agent_status ON background_jobs(agent_name, status)"
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
        """Record a job's process group once its shell subtree has started, so a reaper can kill it after a crash."""
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
        """Process groups of jobs still marked running from an unclean shutdown."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT process_group FROM background_jobs WHERE status = ? AND process_group IS NOT NULL",
                (STATUS_RUNNING,),
            ).fetchall()
        return [row["process_group"] for row in rows if row["process_group"]]

    def undelivered_jobs(self, session_id: str, agent_name: str) -> list[dict[str, Any]]:
        """A context's jobs carrying a result the model has not yet seen."""
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
        """Distinct contexts of an agent with a deliverable result, which are the ones to wake on startup."""
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
