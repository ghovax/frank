from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from harness.core.configuration import harness_home_directory
from harness.core.sqlite_lock import sqlite_write_lock
from harness.identifiers import new_id
from harness.research.schema import (
    BOARD_ACTIONS,
    BOARD_TARGETS,
    EVIDENCE_MODALITIES,
    ORIGIN_CHANNELS,
    SOURCE_KINDS,
    compact_text,
    normalize_origin_channel,
    normalize_source_kind,
)


RESEARCH_DATABASE_FILENAME = "research.db"


class ResearchStoreError(ValueError):
    pass


def research_database_path() -> Path:
    return harness_home_directory() / RESEARCH_DATABASE_FILENAME


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _json_load(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return {} if fallback is None else fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {} if fallback is None else fallback


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ResearchStore:
    """Append-only research event store.

    The event log is the source of truth. The view methods below derive current
    state from events, so concurrent agents append facts/decisions without
    clobbering each other's work.
    """

    def __init__(self, database_path: Path | None = None):
        self.database_path = database_path or research_database_path()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with sqlite_write_lock():
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS research_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        workspace_id TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        parent_event_ids_json TEXT NOT NULL,
                        idempotency_key TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_research_events_idempotency
                    ON research_events(workspace_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL AND idempotency_key != ''
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_research_events_workspace ON research_events(workspace_id, sequence)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_research_events_target ON research_events(workspace_id, target, target_id)"
                )

    def append_event(
        self,
        *,
        action: str,
        target: str,
        workspace_id: str = "",
        target_id: str = "",
        payload: dict[str, Any] | None = None,
        actor_id: str = "agent",
        parent_event_ids: list[str] | None = None,
        idempotency_key: str = "",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        action = (action or "").strip().lower()
        target = (target or "").strip().lower()
        if action not in BOARD_ACTIONS:
            raise ResearchStoreError(f"Unsupported research board action: {action}")
        if target not in BOARD_TARGETS:
            raise ResearchStoreError(f"Unsupported research board target: {target}")

        payload = dict(payload or {})
        if target == "workspace" and action == "insert":
            workspace_id = (workspace_id or target_id or new_id("research")).strip()
            target_id = workspace_id
            payload.setdefault("title", payload.get("objective") or "Research workspace")
        else:
            workspace_id = workspace_id.strip()
            if not workspace_id:
                raise ResearchStoreError("workspace_id is required for research board events.")
            target_id = (target_id or payload.get("id") or new_id(target)).strip()

        payload = self._normalize_payload(target, payload)
        parent_event_ids = [str(event_id) for event_id in (parent_event_ids or []) if str(event_id).strip()]
        actor_id = (actor_id or "agent").strip()
        idempotency_key = (idempotency_key or "").strip()

        with sqlite_write_lock():
            with self._connect() as connection:
                revision = self.revision(workspace_id, connection=connection)
                if expected_revision is not None and int(expected_revision) != revision:
                    raise ResearchStoreError(
                        f"Research workspace revision conflict: expected {expected_revision}, current {revision}."
                    )
                if idempotency_key:
                    existing = connection.execute(
                        """
                        SELECT * FROM research_events
                        WHERE workspace_id = ? AND idempotency_key = ?
                        LIMIT 1
                        """,
                        (workspace_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        return self._row_to_event(existing, idempotent_replay=True)

                event_id = new_id("rev")
                created_at = _now()
                connection.execute(
                    """
                    INSERT INTO research_events (
                        event_id, workspace_id, actor_id, action, target, target_id,
                        payload_json, parent_event_ids_json, idempotency_key, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        workspace_id,
                        actor_id,
                        action,
                        target,
                        target_id,
                        _json_dump(payload),
                        _json_dump(parent_event_ids),
                        idempotency_key or None,
                        created_at,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM research_events WHERE event_id = ?", (event_id,)
                ).fetchone()
                if row is None:
                    raise ResearchStoreError("Research event append failed.")
                return self._row_to_event(row)

    def _normalize_payload(self, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        if target == "source":
            payload["origin_channel"] = normalize_origin_channel(str(payload.get("origin_channel") or "manual"))
            payload["source_kind"] = normalize_source_kind(str(payload.get("source_kind") or "document"))
            payload.setdefault("title", payload.get("title") or payload.get("uri") or payload.get("path") or "Untitled source")
        if target == "evidence":
            modality = str(payload.get("evidence_modality") or payload.get("modality") or "text").strip().lower()
            payload["evidence_modality"] = modality if modality in EVIDENCE_MODALITIES else "text"
            payload.setdefault("support_status", "unclear")
            payload.setdefault("verification_status", "machine_extracted")
        return payload

    def revision(self, workspace_id: str, *, connection: sqlite3.Connection | None = None) -> int:
        if connection is not None:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM research_events WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            return int(row["count"] if row is not None else 0)
        with self._connect() as own_connection:
            return self.revision(workspace_id, connection=own_connection)

    def events(self, workspace_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM research_events WHERE workspace_id = ? ORDER BY sequence ASC"
        params: tuple[Any, ...] = (workspace_id,)
        if limit is not None:
            query += " LIMIT ?"
            params = (workspace_id, int(limit))
        with self._connect() as connection:
            return [self._row_to_event(row) for row in connection.execute(query, params).fetchall()]

    def event(self, workspace_id: str, target: str, target_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM research_events
                WHERE workspace_id = ? AND target = ? AND target_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (workspace_id, target, target_id),
            ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def workspace_state(self, workspace_id: str) -> dict[str, Any]:
        events = self.events(workspace_id)
        workspace_events = [event for event in events if event["target"] == "workspace"]
        insert = next((event for event in workspace_events if event["action"] == "insert"), None)
        payload = dict(insert["payload"] if insert else {})
        active_sources = self.sources(workspace_id, include_inactive=False, events=events)
        all_sources = self.sources(workspace_id, include_inactive=True, events=events)
        quarantined = self.quarantine(workspace_id, events=events)
        reports = self.reports(workspace_id, events=events)
        return {
            "workspace_id": workspace_id,
            "revision": len(events),
            "objective": payload.get("objective", ""),
            "title": payload.get("title", payload.get("objective", "Research workspace")),
            "notes": payload.get("notes", ""),
            "counts": {
                "events": len(events),
                "sources": len(active_sources),
                "all_sources": len(all_sources),
                "quarantined": len(quarantined),
                "evidence_cards": len(self.evidence(workspace_id, events=events)),
                "anchors": len(self.anchors(workspace_id, events=events)),
                "reports": len(reports),
            },
            "sources": active_sources[:20],
            "quarantine": quarantined[:20],
            "reports": reports[:20],
        }

    def sources(
        self,
        workspace_id: str,
        *,
        include_inactive: bool = False,
        events: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        events = events if events is not None else self.events(workspace_id)
        sources: dict[str, dict[str, Any]] = {}
        excluded: set[str] = set()
        superseded: set[str] = set()
        quarantined: set[str] = set()
        evidence_sources: set[str] = set()
        for event in events:
            if event["target"] == "source" and event["action"] == "insert":
                source = dict(event["payload"])
                source.update({
                    "source_id": event["target_id"],
                    "created_at": event["created_at"],
                    "event_id": event["event_id"],
                    "status": "candidate",
                })
                sources[event["target_id"]] = source
            elif event["target"] == "source" and event["action"] == "exclude":
                excluded.add(event["target_id"])
            elif event["target"] == "source" and event["action"] == "supersede":
                superseded.add(event["target_id"])
            elif event["target"] == "preparation_run" and event["action"] == "insert":
                source_id = str(event["payload"].get("source_id") or "")
                if source_id in sources:
                    sources[source_id]["status"] = event["payload"].get("status", "prepared")
                    sources[source_id]["last_preparation_run_id"] = event["target_id"]
            elif event["target"] == "quarantine" and event["action"] == "insert":
                source_id = str(event["payload"].get("source_id") or "")
                if source_id:
                    quarantined.add(source_id)
            elif event["target"] == "evidence" and event["action"] == "insert":
                source_id = str(event["payload"].get("source_id") or "")
                if source_id:
                    evidence_sources.add(source_id)
        results = []
        for source_id, source in sources.items():
            if source_id in evidence_sources:
                source["status"] = "evidence_eligible"
            elif source_id in quarantined:
                source["status"] = "quarantined"
            inactive_reason = ""
            if source_id in excluded:
                inactive_reason = "excluded"
            elif source_id in superseded:
                inactive_reason = "superseded"
            if inactive_reason:
                source = {**source, "inactive": True, "inactive_reason": inactive_reason}
            if include_inactive or not inactive_reason:
                results.append(source)
        return sorted(results, key=lambda item: item.get("created_at", ""))

    def anchors(self, workspace_id: str, *, events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        events = events if events is not None else self.events(workspace_id)
        excluded = {
            event["target_id"]
            for event in events
            if event["target"] == "anchor" and event["action"] in {"exclude", "supersede"}
        }
        anchors = []
        for event in events:
            if event["target"] == "anchor" and event["action"] == "insert" and event["target_id"] not in excluded:
                anchor = dict(event["payload"])
                anchor.update({"anchor_id": event["target_id"], "event_id": event["event_id"]})
                anchors.append(anchor)
        return anchors

    def evidence(self, workspace_id: str, *, events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        events = events if events is not None else self.events(workspace_id)
        excluded = {
            event["target_id"]
            for event in events
            if event["target"] == "evidence" and event["action"] in {"exclude", "supersede"}
        }
        cards = []
        for event in events:
            if event["target"] == "evidence" and event["action"] == "insert" and event["target_id"] not in excluded:
                card = dict(event["payload"])
                card.update({
                    "evidence_card_id": event["target_id"],
                    "event_id": event["event_id"],
                    "created_at": event["created_at"],
                })
                card.setdefault("summary", compact_text(card.get("claim") or card.get("text") or card.get("data")))
                cards.append(card)
        return cards

    def quarantine(self, workspace_id: str, *, events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        events = events if events is not None else self.events(workspace_id)
        records = []
        for event in events:
            if event["target"] == "quarantine" and event["action"] == "insert":
                record = dict(event["payload"])
                record.update({
                    "quarantine_id": event["target_id"],
                    "event_id": event["event_id"],
                    "created_at": event["created_at"],
                })
                records.append(record)
        return records

    def reports(self, workspace_id: str, *, events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        events = events if events is not None else self.events(workspace_id)
        excluded = {
            event["target_id"]
            for event in events
            if event["target"] == "report" and event["action"] in {"exclude", "supersede"}
        }
        published = {
            event["target_id"]
            for event in events
            if event["target"] == "report" and event["action"] == "publish"
        }
        reports = []
        for event in events:
            if event["target"] == "report" and event["action"] == "insert" and event["target_id"] not in excluded:
                report = dict(event["payload"])
                report.update({
                    "report_id": event["target_id"],
                    "event_id": event["event_id"],
                    "created_at": event["created_at"],
                    "published": event["target_id"] in published,
                })
                reports.append(report)
        return reports

    def _row_to_event(self, row: sqlite3.Row, *, idempotent_replay: bool = False) -> dict[str, Any]:
        event = {
            "sequence": int(row["sequence"]),
            "event_id": row["event_id"],
            "workspace_id": row["workspace_id"],
            "actor_id": row["actor_id"],
            "action": row["action"],
            "target": row["target"],
            "target_id": row["target_id"],
            "payload": _json_load(row["payload_json"]),
            "parent_event_ids": _json_load(row["parent_event_ids_json"], []),
            "idempotency_key": row["idempotency_key"] or "",
            "created_at": row["created_at"],
        }
        if idempotent_replay:
            event["idempotent_replay"] = True
        return event


_STORE: ResearchStore | None = None


def get_store() -> ResearchStore:
    global _STORE
    if _STORE is None:
        _STORE = ResearchStore()
    return _STORE
