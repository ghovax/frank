"""The registry's durable half: session records that outlive the daemon that made them."""

from __future__ import annotations

import json
import logging
from typing import Any

from langmesh.base.sqlite_lock import sqlite_write_lock
from langmesh.commons.database import SessionRecord as SessionRow
from langmesh.daemon.registry import LIVE, SessionRecord

logger = logging.getLogger(__name__)


def _decode_sandbox(raw: str) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class SqliteSessionStore:
    """Session records in the history database, beside the turns they own."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def load_all(self) -> list[SessionRecord]:
        """Every session ever recorded, with live ones coming back with no process, which is what they are."""
        database_session = self._session_factory()
        try:
            rows = database_session.query(SessionRow).all()
            return [
                SessionRecord(
                    id=row.id,
                    agent=row.agent,
                    working_directory=row.working_directory or "",
                    runtime_working_directory=row.runtime_working_directory or "",
                    permission_mode=row.permission_mode or "ask",
                    sandbox=_decode_sandbox(row.sandbox or ""),
                    workspace_id=row.workspace_id or "",
                    parent=row.parent or "",
                    title=row.title or "",
                    created_at=row.created_at or "",
                    updated_at=row.updated_at or row.created_at or "",
                    lifecycle=row.lifecycle or LIVE,
                    outcome=row.outcome or "",
                    exit_reason=row.exit_reason or "",
                )
                for row in rows
            ]
        except Exception:  # noqa: BLE001 — a daemon that cannot read its registry still starts
            logger.exception("could not load the session registry; starting with none")
            return []
        finally:
            database_session.close()

    def save(self, record: SessionRecord) -> None:
        """Write one record, upsert-shaped because another surface may have created the row first."""
        with sqlite_write_lock():
            database_session = self._session_factory()
            try:
                row = database_session.get(SessionRow, record.id)
                if row is None:
                    row = SessionRow(id=record.id, agent=record.agent, created_at=record.created_at)
                    database_session.add(row)
                row.agent = record.agent
                row.parent = record.parent
                row.workspace_id = record.workspace_id
                row.working_directory = record.working_directory
                row.runtime_working_directory = record.runtime_working_directory
                row.permission_mode = record.permission_mode
                row.sandbox = json.dumps(record.sandbox or {}, sort_keys=True)
                row.lifecycle = record.lifecycle
                row.outcome = record.outcome
                row.exit_reason = record.exit_reason
                row.updated_at = record.updated_at
                if record.title:
                    row.title = record.title
                database_session.commit()
            except Exception:  # noqa: BLE001 — a failed registry write must not fail the call
                database_session.rollback()
                logger.exception("could not persist session %s", record.id)
            finally:
                database_session.close()

    def delete(self, session_id: str) -> None:
        with sqlite_write_lock():
            database_session = self._session_factory()
            try:
                row = database_session.get(SessionRow, session_id)
                if row is not None:
                    database_session.delete(row)
                    database_session.commit()
            except Exception:  # noqa: BLE001
                database_session.rollback()
                logger.exception("could not delete session %s", session_id)
            finally:
                database_session.close()

    def claim_work_habits_acknowledgement(self, session_id: str) -> bool:
        """Claim the one-time work-habits acknowledgement atomically, durably because a worker is per activation."""
        from datetime import datetime, timezone

        if not session_id:
            return False
        with sqlite_write_lock():
            database_session = self._session_factory()
            try:
                row = database_session.get(SessionRow, session_id)
                if row is None or row.work_habits_acknowledged_at:
                    return False
                row.work_habits_acknowledged_at = datetime.now(timezone.utc).isoformat()
                database_session.commit()
                return True
            except Exception:  # noqa: BLE001
                database_session.rollback()
                logger.exception("could not claim the work-habits acknowledgement for %s", session_id)
                return False
            finally:
                database_session.close()

    def reset_work_habits_acknowledgements(self) -> None:
        """Allow one fresh acknowledgement everywhere, after the setting changes."""
        with sqlite_write_lock():
            database_session = self._session_factory()
            try:
                database_session.query(SessionRow).update(
                    {SessionRow.work_habits_acknowledged_at: ""}, synchronize_session=False,
                )
                database_session.commit()
            except Exception:  # noqa: BLE001
                database_session.rollback()
                logger.exception("could not reset the work-habits acknowledgements")
            finally:
                database_session.close()


__all__ = ["SqliteSessionStore"]
