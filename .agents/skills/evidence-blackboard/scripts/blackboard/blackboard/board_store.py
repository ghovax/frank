from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .board_common import board_database_path, board_write_lock, new_id
from .board_schema import (
    ANCHOR_MODALITIES,
    BOARD_ACTIONS,
    BOARD_TARGETS,
    normalize_aspect,
    normalize_origin_channel,
    normalize_source_kind,
)


class BoardError(ValueError):
    pass


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


# The projected read model the agent queries with free read-only SQL. Every table is
# derived from the append-only event log and kept current on each append. The names
# are plain domain nouns — one table per event target.
_READ_MODEL_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS boards (
        board_id TEXT PRIMARY KEY, objective TEXT, title TEXT, created_at TEXT
    )
    """,
    # A canonical scholarly work. Both an ingested source and a cited reference
    # resolve to one of these (via identity_key), so the citation graph spans papers
    # and the same work is never double-counted.
    """
    CREATE TABLE IF NOT EXISTS works (
        work_id TEXT PRIMARY KEY, board_id TEXT, identity_key TEXT,
        doi TEXT, arxiv_id TEXT, external_id TEXT, title TEXT, year TEXT,
        status TEXT, created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY, board_id TEXT, work_id TEXT, origin_channel TEXT, source_kind TEXT,
        title TEXT, uri TEXT, path TEXT, doi TEXT, status TEXT,
        inactive INTEGER DEFAULT 0, inactive_reason TEXT, created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS anchors (
        anchor_id TEXT PRIMARY KEY, board_id TEXT, source_id TEXT, work_id TEXT, source_title TEXT,
        document_hash TEXT, page_index INTEGER, block_id TEXT, category TEXT, evidence_modality TEXT,
        aspect TEXT, bbox_json TEXT, polygon_json TEXT, section_hierarchy_json TEXT, html TEXT, text TEXT,
        image_keys_json TEXT, file_path TEXT, created_at TEXT
    )
    """,
    # One bibliography entry inside a source. work_id is filled once the entry is
    # resolved to a canonical work (via link_reference); it starts empty.
    """
    CREATE TABLE IF NOT EXISTS bibliography (
        reference_id TEXT PRIMARY KEY, board_id TEXT, source_id TEXT, work_id TEXT, marker_number INTEGER,
        raw_string TEXT, external_id TEXT, resolved_title TEXT, resolution_status TEXT, created_at TEXT
    )
    """,
    # A citation-graph edge: an anchor in one document cites a work (via a
    # bibliography entry). cited_work_id is filled when the reference is resolved.
    """
    CREATE TABLE IF NOT EXISTS citations (
        citation_id TEXT PRIMARY KEY, board_id TEXT, source_id TEXT, source_anchor_id TEXT,
        marker_number INTEGER, reference_id TEXT, cited_work_id TEXT, external_id TEXT,
        excluded INTEGER DEFAULT 0, created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS figures (
        figure_id TEXT PRIMARY KEY, board_id TEXT, anchor_id TEXT, source_id TEXT, work_id TEXT,
        blob_path TEXT, mime TEXT, description TEXT, description_status TEXT, created_at TEXT
    )
    """,
    # A first-class assertion the agent asserts and tracks the standing of.
    """
    CREATE TABLE IF NOT EXISTS claims (
        claim_id TEXT PRIMARY KEY, board_id TEXT, statement TEXT, topic TEXT, status TEXT,
        retracted INTEGER DEFAULT 0, created_at TEXT
    )
    """,
    # An edge from a claim to a piece of evidence (an anchor) in a work, carrying the
    # stance (supports / contradicts / qualifies). This is what makes a claim a node
    # backed by evidence across many papers.
    """
    CREATE TABLE IF NOT EXISTS claim_support (
        support_id TEXT PRIMARY KEY, board_id TEXT, claim_id TEXT, anchor_id TEXT, work_id TEXT,
        stance TEXT, note TEXT, excluded INTEGER DEFAULT 0, created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topics (
        topic_id TEXT PRIMARY KEY, board_id TEXT, label TEXT, origin TEXT, created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_topics (
        link_id TEXT PRIMARY KEY, board_id TEXT, work_id TEXT, topic_id TEXT, created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notes (
        note_id TEXT PRIMARY KEY, board_id TEXT, body TEXT, topic TEXT, created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quarantine (
        quarantine_id TEXT PRIMARY KEY, board_id TEXT, source_id TEXT,
        reason_code TEXT, message TEXT, created_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_works_identity ON works(board_id, identity_key)",
    "CREATE INDEX IF NOT EXISTS idx_sources_board ON sources(board_id, work_id)",
    "CREATE INDEX IF NOT EXISTS idx_anchors_board ON anchors(board_id, source_id, page_index)",
    "CREATE INDEX IF NOT EXISTS idx_anchors_work ON anchors(board_id, work_id)",
    "CREATE INDEX IF NOT EXISTS idx_anchors_aspect ON anchors(board_id, aspect)",
    "CREATE INDEX IF NOT EXISTS idx_bibliography_board ON bibliography(board_id, source_id, marker_number)",
    "CREATE INDEX IF NOT EXISTS idx_citations_board ON citations(board_id, source_id, marker_number)",
    "CREATE INDEX IF NOT EXISTS idx_citations_work ON citations(board_id, cited_work_id)",
    "CREATE INDEX IF NOT EXISTS idx_figures_board ON figures(board_id, anchor_id)",
    "CREATE INDEX IF NOT EXISTS idx_claim_support_claim ON claim_support(board_id, claim_id)",
    "CREATE INDEX IF NOT EXISTS idx_work_topics_work ON work_topics(board_id, work_id)",
    # Full-text search over anchor text (figure descriptions land here via their anchor too).
    "CREATE VIRTUAL TABLE IF NOT EXISTS anchors_fts USING fts5(anchor_id UNINDEXED, board_id UNINDEXED, text)",
]


class BoardStore:
    """Append-only knowledge event store.

    The event log is the source of truth. The read-model tables below are projected
    from events and kept current on each append, so concurrent agents append
    facts/decisions without clobbering each other's work and a query never replays.
    """

    def __init__(self, database_path: Path | None = None):
        self.database_path = database_path or board_database_path()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with board_write_lock():
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        board_id TEXT NOT NULL,
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
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency
                    ON events(board_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL AND idempotency_key != ''
                    """
                )
                connection.execute("CREATE INDEX IF NOT EXISTS idx_events_board ON events(board_id, sequence)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_events_target ON events(board_id, target, target_id)")
                for statement in _READ_MODEL_SCHEMA:
                    connection.execute(statement)

    def append_event(
        self,
        *,
        action: str,
        target: str,
        board_id: str = "",
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
            raise BoardError(f"Unsupported board action: {action}")
        if target not in BOARD_TARGETS:
            raise BoardError(f"Unsupported board target: {target}")

        payload = dict(payload or {})
        if target == "board" and action == "insert":
            board_id = (board_id or target_id or new_id("board")).strip()
            target_id = board_id
            payload.setdefault("title", payload.get("objective") or "Research board")
        else:
            board_id = board_id.strip()
            if not board_id:
                raise BoardError("board_id is required for board events.")
            target_id = (target_id or payload.get("id") or new_id(target)).strip()

        payload = self._normalize_payload(target, payload)
        parent_event_ids = [str(event_id) for event_id in (parent_event_ids or []) if str(event_id).strip()]
        actor_id = (actor_id or "agent").strip()
        idempotency_key = (idempotency_key or "").strip()

        with board_write_lock():
            with self._connect() as connection:
                revision = self.revision(board_id, connection=connection)
                if expected_revision is not None and int(expected_revision) != revision:
                    raise BoardError(f"Board revision conflict: expected {expected_revision}, current {revision}.")
                if idempotency_key:
                    existing = connection.execute(
                        "SELECT * FROM events WHERE board_id = ? AND idempotency_key = ? LIMIT 1",
                        (board_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        return self._row_to_event(existing, idempotent_replay=True)

                event_id = new_id("event")
                created_at = _now()
                connection.execute(
                    """
                    INSERT INTO events (
                        event_id, board_id, actor_id, action, target, target_id,
                        payload_json, parent_event_ids_json, idempotency_key, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id, board_id, actor_id, action, target, target_id,
                        _json_dump(payload), _json_dump(parent_event_ids), idempotency_key or None, created_at,
                    ),
                )
                row = connection.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
                if row is None:
                    raise BoardError("Board event append failed.")
                projected_event = self._row_to_event(row)
                self._project(connection, projected_event)
                return projected_event

    def _normalize_payload(self, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        if target == "source":
            payload["origin_channel"] = normalize_origin_channel(str(payload.get("origin_channel") or "manual"))
            payload["source_kind"] = normalize_source_kind(str(payload.get("source_kind") or "document"))
            payload.setdefault("title", payload.get("title") or payload.get("uri") or payload.get("path") or "Untitled source")
        if target == "work":
            payload.setdefault("status", "cited")
        if target == "anchor":
            modality = str(payload.get("evidence_modality") or "text").strip().lower()
            payload["evidence_modality"] = modality if modality in ANCHOR_MODALITIES else "text"
            payload["aspect"] = normalize_aspect(str(payload.get("aspect") or ""))
        if target == "reference":
            payload.setdefault("resolution_status", "unresolved")
        if target == "claim":
            payload.setdefault("status", "open")
        if target == "figure":
            payload.setdefault("description_status", "pending")
        return payload

    def _project(self, connection: sqlite3.Connection, event: dict[str, Any]) -> None:
        """Fold one appended event into the read model. Idempotent per event id via
        INSERT OR REPLACE / targeted UPDATEs, so a replay never duplicates a row."""
        action = event["action"]
        target = event["target"]
        payload = event["payload"]
        board_id = event["board_id"]
        target_id = event["target_id"]
        created_at = event["created_at"]

        if target == "board" and action == "insert":
            connection.execute(
                "INSERT OR REPLACE INTO boards(board_id, objective, title, created_at) VALUES (?, ?, ?, ?)",
                (board_id, payload.get("objective", ""), payload.get("title", ""), created_at),
            )
        elif target == "work":
            if action == "insert":
                connection.execute(
                    """INSERT OR REPLACE INTO works(work_id, board_id, identity_key, doi, arxiv_id, external_id,
                       title, year, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (target_id, board_id, payload.get("identity_key", ""), payload.get("doi", ""),
                     payload.get("arxiv_id", ""), payload.get("external_id", ""), payload.get("title", ""),
                     payload.get("year", ""), payload.get("status", "cited"), created_at),
                )
            elif action == "annotate":
                for column in ("status", "doi", "arxiv_id", "external_id", "title", "year", "identity_key"):
                    if column in payload:
                        connection.execute(f"UPDATE works SET {column} = ? WHERE work_id = ?", (payload[column], target_id))
        elif target == "source":
            if action == "insert":
                connection.execute(
                    """INSERT OR REPLACE INTO sources(source_id, board_id, work_id, origin_channel, source_kind,
                       title, uri, path, doi, status, inactive, inactive_reason, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', 0, '', ?)""",
                    (target_id, board_id, payload.get("work_id", ""), payload.get("origin_channel", ""),
                     payload.get("source_kind", ""), payload.get("title", ""), payload.get("uri", ""),
                     payload.get("path", ""), payload.get("doi", ""), created_at),
                )
            elif action in {"exclude", "supersede"}:
                connection.execute("UPDATE sources SET inactive = 1, inactive_reason = ? WHERE source_id = ?", (action, target_id))
        elif target == "ingest_run" and action == "insert":
            source_id = str(payload.get("source_id") or "")
            if source_id:
                connection.execute("UPDATE sources SET status = ? WHERE source_id = ?", (payload.get("status", "ingesting"), source_id))
        elif target == "quarantine" and action == "insert":
            connection.execute(
                "INSERT OR REPLACE INTO quarantine(quarantine_id, board_id, source_id, reason_code, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (target_id, board_id, payload.get("source_id", ""), payload.get("reason_code", ""), payload.get("message", ""), created_at),
            )
            source_id = str(payload.get("source_id") or "")
            if source_id:
                connection.execute("UPDATE sources SET status = 'quarantined' WHERE source_id = ?", (source_id,))
        elif target == "anchor" and action == "insert":
            connection.execute(
                """INSERT OR REPLACE INTO anchors(anchor_id, board_id, source_id, work_id, source_title, document_hash,
                   page_index, block_id, category, evidence_modality, aspect, bbox_json, polygon_json, section_hierarchy_json,
                   html, text, image_keys_json, file_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (target_id, board_id, payload.get("source_id", ""), payload.get("work_id", ""),
                 payload.get("source_title", ""), payload.get("document_hash", ""), payload.get("page_index"),
                 payload.get("block_id", ""), payload.get("category", ""), payload.get("evidence_modality", "text"),
                 payload.get("aspect", "other"), _json_dump(payload.get("bbox")), _json_dump(payload.get("polygon")),
                 _json_dump(payload.get("section_hierarchy") or {}), payload.get("html", ""),
                 payload.get("text", ""), _json_dump(payload.get("image_keys") or []), payload.get("file_path", ""), created_at),
            )
            connection.execute("DELETE FROM anchors_fts WHERE anchor_id = ?", (target_id,))
            connection.execute(
                "INSERT INTO anchors_fts(anchor_id, board_id, text) VALUES (?, ?, ?)",
                (target_id, board_id, payload.get("text", "")),
            )
        elif target == "reference":
            if action == "insert":
                connection.execute(
                    """INSERT OR REPLACE INTO bibliography(reference_id, board_id, source_id, work_id, marker_number,
                       raw_string, external_id, resolved_title, resolution_status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (target_id, board_id, payload.get("source_id", ""), payload.get("work_id", ""),
                     payload.get("marker_number"), payload.get("raw_string", ""), payload.get("external_id", ""),
                     payload.get("resolved_title", ""), payload.get("resolution_status", "unresolved"), created_at),
                )
            elif action == "annotate":
                for column in ("work_id", "external_id", "resolved_title", "resolution_status"):
                    if column in payload:
                        connection.execute(f"UPDATE bibliography SET {column} = ? WHERE reference_id = ?", (payload[column], target_id))
        elif target == "citation":
            if action == "insert":
                connection.execute(
                    """INSERT OR REPLACE INTO citations(citation_id, board_id, source_id, source_anchor_id,
                       marker_number, reference_id, cited_work_id, external_id, excluded, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    (target_id, board_id, payload.get("source_id", ""), payload.get("source_anchor_id", ""),
                     payload.get("marker_number"), payload.get("reference_id", ""), payload.get("cited_work_id", ""),
                     payload.get("external_id", ""), created_at),
                )
            elif action in {"exclude", "supersede"}:
                connection.execute("UPDATE citations SET excluded = 1 WHERE citation_id = ?", (target_id,))
            elif action == "annotate":
                for column in ("cited_work_id", "external_id", "reference_id"):
                    if column in payload:
                        connection.execute(f"UPDATE citations SET {column} = ? WHERE citation_id = ?", (payload[column], target_id))
        elif target == "figure":
            if action == "insert":
                connection.execute(
                    """INSERT OR REPLACE INTO figures(figure_id, board_id, anchor_id, source_id, work_id, blob_path,
                       mime, description, description_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (target_id, board_id, payload.get("anchor_id", ""), payload.get("source_id", ""),
                     payload.get("work_id", ""), payload.get("blob_path", ""), payload.get("mime", ""),
                     payload.get("description", ""), payload.get("description_status", "pending"), created_at),
                )
            elif action == "annotate":
                for column in ("description", "description_status"):
                    if column in payload:
                        connection.execute(f"UPDATE figures SET {column} = ? WHERE figure_id = ?", (payload[column], target_id))
        elif target == "claim":
            if action == "insert":
                connection.execute(
                    "INSERT OR REPLACE INTO claims(claim_id, board_id, statement, topic, status, retracted, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (target_id, board_id, payload.get("statement", ""), payload.get("topic", ""), payload.get("status", "open"), created_at),
                )
            elif action == "annotate":
                for column in ("statement", "topic", "status"):
                    if column in payload:
                        connection.execute(f"UPDATE claims SET {column} = ? WHERE claim_id = ?", (payload[column], target_id))
            elif action in {"exclude", "supersede"}:
                connection.execute("UPDATE claims SET retracted = 1 WHERE claim_id = ?", (target_id,))
        elif target == "claim_support":
            if action == "insert":
                connection.execute(
                    """INSERT OR REPLACE INTO claim_support(support_id, board_id, claim_id, anchor_id, work_id,
                       stance, note, excluded, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    (target_id, board_id, payload.get("claim_id", ""), payload.get("anchor_id", ""),
                     payload.get("work_id", ""), payload.get("stance", "supports"), payload.get("note", ""), created_at),
                )
            elif action in {"exclude", "supersede"}:
                connection.execute("UPDATE claim_support SET excluded = 1 WHERE support_id = ?", (target_id,))
        elif target == "topic" and action == "insert":
            connection.execute(
                "INSERT OR REPLACE INTO topics(topic_id, board_id, label, origin, created_at) VALUES (?, ?, ?, ?, ?)",
                (target_id, board_id, payload.get("label", ""), payload.get("origin", "agent"), created_at),
            )
        elif target == "work_topic" and action == "insert":
            connection.execute(
                "INSERT OR REPLACE INTO work_topics(link_id, board_id, work_id, topic_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (target_id, board_id, payload.get("work_id", ""), payload.get("topic_id", ""), created_at),
            )
        elif target == "note" and action == "insert":
            connection.execute(
                "INSERT OR REPLACE INTO notes(note_id, board_id, body, topic, created_at) VALUES (?, ?, ?, ?, ?)",
                (target_id, board_id, payload.get("body", ""), payload.get("topic", ""), created_at),
            )

    def query(self, sql: str, parameters: tuple[Any, ...] = (), *, maximum_rows: int = 200) -> list[dict[str, Any]]:
        """Run a read-only SELECT over the projected read model and return rows. The
        database is opened read-only and non-SELECT statements are rejected, so the
        agent can compose queries freely without ever mutating the append-only log."""
        statement = (sql or "").strip().rstrip(";").strip()
        lowered = statement.lower()
        if not (lowered.startswith("select") or lowered.startswith("with")):
            raise BoardError("Only read-only SELECT (or WITH ... SELECT) queries are allowed.")
        forbidden = ("insert ", "update ", "delete ", "drop ", "alter ", "create ", "replace ", "attach ", "pragma ", "vacuum")
        padded = f" {lowered} "
        if any(keyword in padded for keyword in forbidden):
            raise BoardError("Query contains a disallowed (non-read-only) statement.")
        read_only_uri = f"file:{self.database_path}?mode=ro"
        connection = sqlite3.connect(read_only_uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(statement, parameters).fetchmany(max(1, min(int(maximum_rows), 1000)))
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def revision(self, board_id: str, *, connection: sqlite3.Connection | None = None) -> int:
        if connection is not None:
            row = connection.execute("SELECT COUNT(*) AS count FROM events WHERE board_id = ?", (board_id,)).fetchone()
            return int(row["count"] if row is not None else 0)
        with self._connect() as own_connection:
            return self.revision(board_id, connection=own_connection)

    def events(self, board_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE board_id = ? ORDER BY sequence ASC"
        parameters: tuple[Any, ...] = (board_id,)
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (board_id, int(limit))
        with self._connect() as connection:
            return [self._row_to_event(row) for row in connection.execute(sql, parameters).fetchall()]

    def board_state(self, board_id: str) -> dict[str, Any]:
        events = self.events(board_id)
        insert = next((event for event in events if event["target"] == "board" and event["action"] == "insert"), None)
        payload = dict(insert["payload"] if insert else {})
        active_sources = self.sources(board_id, include_inactive=False, events=events)
        quarantined = self.quarantine(board_id, events=events)
        return {
            "board_id": board_id,
            "revision": len(events),
            "objective": payload.get("objective", ""),
            "title": payload.get("title", payload.get("objective", "Research board")),
            "counts": {
                "events": len(events),
                "works": self._count(board_id, "SELECT COUNT(*) AS count FROM works WHERE board_id = ?"),
                "sources": len(active_sources),
                "anchors": self._count(board_id, "SELECT COUNT(*) AS count FROM anchors WHERE board_id = ?"),
                "claims": self._count(board_id, "SELECT COUNT(*) AS count FROM claims WHERE board_id = ? AND retracted = 0"),
                "citations": self._count(board_id, "SELECT COUNT(*) AS count FROM citations WHERE board_id = ? AND excluded = 0"),
                "references": self._count(board_id, "SELECT COUNT(*) AS count FROM bibliography WHERE board_id = ?"),
                "topics": self._count(board_id, "SELECT COUNT(*) AS count FROM topics WHERE board_id = ?"),
                "quarantined": len(quarantined),
            },
            "anchors_by_aspect": {
                row["aspect"]: row["count"]
                for row in self.query(
                    "SELECT aspect, COUNT(*) AS count FROM anchors WHERE board_id = ? GROUP BY aspect ORDER BY count DESC",
                    (board_id,),
                )
            },
            "sources": active_sources[:20],
            "quarantine": quarantined[:20],
        }

    def _count(self, board_id: str, sql: str) -> int:
        rows = self.query(sql, (board_id,))
        return int(rows[0]["count"]) if rows else 0

    def sources(
        self,
        board_id: str,
        *,
        include_inactive: bool = False,
        events: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Reconstruct sources from the event log (not the projection) so callers get
        the full insert payload — including a structured record's ``record`` — which
        the projected ``sources`` table does not carry."""
        events = events if events is not None else self.events(board_id)
        sources: dict[str, dict[str, Any]] = {}
        excluded: set[str] = set()
        superseded: set[str] = set()
        quarantined: set[str] = set()
        for event in events:
            target, action = event["target"], event["action"]
            if target == "source" and action == "insert":
                source = dict(event["payload"])
                source.update({"source_id": event["target_id"], "created_at": event["created_at"], "status": "candidate"})
                sources[event["target_id"]] = source
            elif target == "source" and action == "exclude":
                excluded.add(event["target_id"])
            elif target == "source" and action == "supersede":
                superseded.add(event["target_id"])
            elif target == "ingest_run" and action == "insert":
                source_id = str(event["payload"].get("source_id") or "")
                if source_id in sources:
                    sources[source_id]["status"] = event["payload"].get("status", "ingesting")
            elif target == "quarantine" and action == "insert":
                source_id = str(event["payload"].get("source_id") or "")
                if source_id:
                    quarantined.add(source_id)
        results = []
        for source_id, source in sources.items():
            if source_id in quarantined:
                source["status"] = "quarantined"
            inactive_reason = "excluded" if source_id in excluded else ("superseded" if source_id in superseded else "")
            if inactive_reason:
                source = {**source, "inactive": True, "inactive_reason": inactive_reason}
            if include_inactive or not inactive_reason:
                results.append(source)
        return sorted(results, key=lambda item: item.get("created_at", ""))

    def quarantine(self, board_id: str, *, events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        events = events if events is not None else self.events(board_id)
        records = []
        for event in events:
            if event["target"] == "quarantine" and event["action"] == "insert":
                record = dict(event["payload"])
                record.update({"quarantine_id": event["target_id"], "created_at": event["created_at"]})
                records.append(record)
        return records

    def _row_to_event(self, row: sqlite3.Row, *, idempotent_replay: bool = False) -> dict[str, Any]:
        event = {
            "sequence": int(row["sequence"]),
            "event_id": row["event_id"],
            "board_id": row["board_id"],
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


_STORE: BoardStore | None = None


def get_board_store() -> BoardStore:
    global _STORE
    if _STORE is None:
        _STORE = BoardStore()
    return _STORE
