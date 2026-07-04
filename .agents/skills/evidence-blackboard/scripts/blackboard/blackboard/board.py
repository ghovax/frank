"""The blackboard.

A durable, append-only knowledge base the agent scratches findings onto and queries
freely, backed by SQLite under ``~/.blackboard`` (see ``board_common``). It is the
shared workspace across a research effort: several agents can add sources, ingest
documents, assert claims, and classify works without overwriting each other, because
every write is an append-only event and every record gets a random unique id.

The public surface is plain functions — import ``blackboard`` and call them from
``uv run python``. Reading is just SQL: :func:`query` runs read-only SQL over the
projected tables (``works``, ``sources``, ``anchors``, ``bibliography``,
``citations``, ``figures``, ``claims``, ``claim_support``, ``topics``,
``work_topics``, ``notes``, ``quarantine``) and the ``anchors_fts`` full-text index.

The model has three durable structures, and knowing which is which is the whole
point:

* **works** — canonical scholarly identity. A paper is one node whether you read it
  (an ingested source) or merely see it cited (a resolved reference). This is what
  turns a pile of parsed PDFs into a citation graph that spans papers.
* **claims** + **claim_support** — first-class facts, each backed by supporting or
  contradicting anchors across works. Reading compounds into knowledge here, not in
  chat history.
* **topics** + **work_topics** — a durable classification dimension.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from .board_common import image_blob_directory, new_id
from .board_schema import modality_for_category, work_identity_key
from .board_store import BoardError, get_board_store
from .enrich import enrich_document
from .parse import DOCUMENT_SUFFIXES, DatalabParseError, DatalabUnavailable, ParseOptions, parse_document


# Board lifecycle and identity.

def open_board(objective: str = "", board: str = "") -> dict[str, Any]:
    """Open (or create) a board for a research objective. Returns its ``board`` id
    and current state. Pass an existing ``board`` id to re-open it."""
    store = get_board_store()
    event = store.append_event(action="insert", target="board", board_id=board, payload={"objective": objective})
    return {"board": event["board_id"], "state": store.board_state(event["board_id"])}


def _resolve_work(
    board: str,
    *,
    doi: str = "",
    arxiv: str = "",
    external_id: str = "",
    title: str = "",
    year: str = "",
    status: str = "cited",
) -> str:
    """Return the id of the canonical work for these identifiers, creating it if new
    and deduping by identity key. When we now read a work we had only seen cited, it
    is promoted to ``ingested``."""
    store = get_board_store()
    identity_key = work_identity_key(doi=doi, arxiv=arxiv, external_id=external_id, title=title)
    if identity_key:
        existing = store.query(
            "SELECT work_id, status FROM works WHERE board_id = ? AND identity_key = ? LIMIT 1",
            (board, identity_key),
        )
        if existing:
            work_id = existing[0]["work_id"]
            if status == "ingested" and existing[0].get("status") != "ingested":
                promotion: dict[str, Any] = {"status": "ingested"}
                if doi:
                    promotion["doi"] = doi
                if title:
                    promotion["title"] = title
                if year:
                    promotion["year"] = year
                store.append_event(action="annotate", target="work", board_id=board, target_id=work_id, payload=promotion)
            return work_id
    event = store.append_event(
        action="insert", target="work", board_id=board, target_id=new_id("work"),
        payload={"identity_key": identity_key, "doi": doi, "arxiv_id": arxiv, "external_id": external_id,
                 "title": title, "year": year, "status": status},
    )
    return event["target_id"]


def add_source(
    board: str,
    *,
    title: str = "",
    path: str = "",
    uri: str = "",
    doi: str = "",
    year: str = "",
    origin_channel: str = "manual",
    source_kind: str = "document",
    record: dict[str, Any] | None = None,
    source_id: str = "",
) -> dict[str, Any]:
    """Register a source on a board (a PDF ``path``, a ``uri``, a ``doi``, or a
    structured ``record``) and bind it to a canonical work. It becomes citable
    evidence only after :func:`ingest`."""
    work_id = _resolve_work(board, doi=doi, title=title, year=year, status="ingested")
    payload: dict[str, Any] = {
        "title": title, "path": path, "uri": uri, "doi": doi, "work_id": work_id,
        "origin_channel": origin_channel, "source_kind": source_kind,
    }
    if record is not None:
        payload["record"] = record
    event = get_board_store().append_event(action="insert", target="source", board_id=board, target_id=source_id, payload=payload)
    return {"source_id": event["target_id"], "work_id": work_id, "board": board}


# Findings the agent asserts: claims, evidence links, classification, notes.

def add_claim(board: str, statement: str, *, topic: str = "", status: str = "open") -> dict[str, Any]:
    """Assert a first-class claim/fact on the board. Back it with :func:`cite_evidence`;
    its ``status`` (open/supported/contested/refuted) is yours to maintain."""
    event = get_board_store().append_event(
        action="insert", target="claim", board_id=board, target_id=new_id("claim"),
        payload={"statement": statement, "topic": topic, "status": status},
    )
    return {"claim_id": event["target_id"], "board": board}


def cite_evidence(board: str, claim: str, anchor: str, *, stance: str = "supports", note: str = "") -> dict[str, Any]:
    """Link a claim to a supporting/contradicting/qualifying anchor. The anchor's work
    is recorded on the edge, so a claim accumulates evidence across papers."""
    rows = get_board_store().query("SELECT work_id FROM anchors WHERE anchor_id = ? LIMIT 1", (anchor,))
    work_id = rows[0]["work_id"] if rows else ""
    event = get_board_store().append_event(
        action="insert", target="claim_support", board_id=board, target_id=new_id("support"),
        payload={"claim_id": claim, "anchor_id": anchor, "work_id": work_id, "stance": stance, "note": note},
    )
    return {"support_id": event["target_id"], "claim": claim, "anchor": anchor}


def link_reference(board: str, reference: str, *, doi: str = "", arxiv: str = "", title: str = "", year: str = "") -> dict[str, Any]:
    """Attach a resolved cited work to a bibliography entry and point every citation
    edge for it at that work — closing the cross-document citation graph. Resolving
    the raw reference to these identifiers is discovery (do it with ``scholar``); this
    only records the result."""
    store = get_board_store()
    work_id = _resolve_work(board, doi=doi, arxiv=arxiv, title=title, year=year, status="cited")
    external_id = f"https://doi.org/{doi}" if doi else ""
    store.append_event(
        action="annotate", target="reference", board_id=board, target_id=reference,
        payload={"work_id": work_id, "external_id": external_id, "resolved_title": title, "resolution_status": "resolved"},
    )
    citations = store.query("SELECT citation_id FROM citations WHERE board_id = ? AND reference_id = ?", (board, reference))
    for row in citations:
        store.append_event(
            action="annotate", target="citation", board_id=board, target_id=row["citation_id"],
            payload={"cited_work_id": work_id, "external_id": external_id},
        )
    return {"reference": reference, "work_id": work_id, "linked_citations": len(citations)}


def classify(board: str, work: str, *, topic: str, origin: str = "agent") -> dict[str, Any]:
    """Tag a work with a topic label (creating the topic if new), building a durable
    classification dimension over the corpus."""
    store = get_board_store()
    label = (topic or "").strip()
    existing = store.query("SELECT topic_id FROM topics WHERE board_id = ? AND lower(label) = lower(?) LIMIT 1", (board, label))
    if existing:
        topic_id = existing[0]["topic_id"]
    else:
        topic_id = store.append_event(
            action="insert", target="topic", board_id=board, target_id=new_id("topic"),
            payload={"label": label, "origin": origin},
        )["target_id"]
    link = store.append_event(
        action="insert", target="work_topic", board_id=board, target_id=new_id("worktopic"),
        payload={"work_id": work, "topic_id": topic_id},
    )
    return {"topic_id": topic_id, "work": work, "link_id": link["target_id"]}


def note(board: str, body: str = "", *, target: str = "note", action: str = "insert", target_id: str = "", **payload: Any) -> dict[str, Any]:
    """Write a free observation onto the blackboard, or annotate/exclude/supersede an
    existing record. Default: a free ``note`` whose ``body`` is your observation. Or
    pass a data ``target`` (`work`, `claim`, `reference`, `source`, …) with an
    ``action`` of `annotate`/`exclude`/`supersede` and the fields to set."""
    if target == "note" and body:
        payload.setdefault("body", body)
    event = get_board_store().append_event(action=action, target=target, board_id=board, target_id=target_id, payload=dict(payload))
    return {"event_id": event["event_id"], "target": event["target"], "target_id": event["target_id"]}


def query(sql: str, parameters: list[Any] | None = None, maximum_rows: int = 200) -> list[dict[str, Any]]:
    """Run a read-only SQL query over the projected read model and return the rows.
    Tables: `works`, `sources`, `anchors`, `bibliography`, `citations`, `figures`,
    `claims`, `claim_support`, `topics`, `work_topics`, `notes`, `quarantine`, and
    `anchors_fts` (full-text — join `anchors_fts.anchor_id` to `anchors.anchor_id`)."""
    return get_board_store().query(sql, tuple(parameters or ()), maximum_rows=int(maximum_rows or 200))


def board_state(board: str) -> dict[str, Any]:
    """A compact summary of a board: objective, per-table counts, and quarantine."""
    return get_board_store().board_state(board)


# Ingestion pipeline (parse + enrich).

def ingest(board: str, source: str = "") -> dict[str, Any]:
    """Parse a board's source document(s) via Datalab and enrich them (citation
    markers, figure descriptions, a parsed bibliography), writing anchors and the
    citation graph onto the blackboard. Slow and model/network-bound, so run it as a
    background bash command. Safe to re-run: parses are cached by content hash."""
    return asyncio.run(_ingest_async(board, source))


async def _ingest_async(board: str, source_filter: str) -> dict[str, Any]:
    store = get_board_store()
    options = ParseOptions.from_env()
    sources = store.sources(board, include_inactive=False)
    selected = [item for item in sources if not source_filter or item.get("source_id") == source_filter]
    if not selected:
        return {"code": "ingest_error", "message": "No active sources matched the ingest request."}
    results = []
    for source in selected:
        source_id = str(source.get("source_id"))
        run_id = store.append_event(
            action="insert", target="ingest_run", board_id=board, target_id=new_id("run"),
            payload={"source_id": source_id, "status": "ingesting", "backend": "datalab", "mode": options.mode},
        )["target_id"]
        try:
            result = await _ingest_source(board, source, run_id, options)
        except Exception as exception:  # noqa: BLE001 — a failed source is quarantined, never fatal
            result = _quarantine(board, source_id, "ingest_exception", str(exception), run_id)
        results.append({"source_id": source_id, "result": result})
    return {"code": "ingest_completed", "board": board, "results": results}


async def _ingest_source(board: str, source: dict[str, Any], run_id: str, options: ParseOptions) -> dict[str, Any]:
    source_id = str(source.get("source_id"))
    source_kind = str(source.get("source_kind") or "document")
    if source_kind == "structured_record":
        return _index_structured_record(board, source, run_id)
    path = Path(str(source.get("path") or "")).expanduser()
    if not path or not path.is_file():
        return _quarantine(board, source_id, "missing_local_artifact",
                           "The source has no readable local file. Acquire the full text/PDF before ingesting.", run_id)
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown"}:
        return _index_text_document(board, source, path, run_id)
    if suffix in DOCUMENT_SUFFIXES:
        try:
            parsed = await asyncio.to_thread(parse_document, path, options)
        except DatalabUnavailable as exception:
            return _quarantine(board, source_id, "parser_unavailable", str(exception), run_id)
        except DatalabParseError as exception:
            return _quarantine(board, source_id, "parse_failed", str(exception), run_id)
        return await _index_and_enrich(board, source, path, parsed, run_id)
    return _quarantine(board, source_id, "unsupported_file_type", f"Unsupported source artifact type: {suffix or '<none>'}", run_id)


async def _index_and_enrich(board: str, source: dict[str, Any], path: Path, parsed: dict[str, Any], run_id: str) -> dict[str, Any]:
    store = get_board_store()
    source_id = source["source_id"]
    work_id = str(source.get("work_id") or "")
    source_title = source.get("title", "")
    document_hash = parsed.get("document_hash", "")

    # 1. Fan-out enrichment first (parallel): citation markers, figure descriptions,
    # information aspects, references. It reads only the parsed blocks, so it runs
    # before anchors exist and its aspect tags ride onto each anchor below.
    enrichment = await enrich_document(parsed)
    aspects_by_block = enrichment.get("aspects_by_block", {})

    # 2. Anchors — the verbatim layout blocks (full html + full text, nothing
    # truncated), each tagged with its information aspect. Anchors ARE the evidence
    # substrate; claims are asserted on top of them deliberately, never auto-generated
    # as one-per-block duplicates.
    block_to_anchor: dict[str, str] = {}
    anchor_count = 0
    for block in parsed.get("blocks", []):
        anchor_id = new_id("anchor")
        block_id = str(block.get("block_id") or "")
        if block_id:
            block_to_anchor[block_id] = anchor_id
        block_type = str(block.get("block_type") or "Text")
        modality = str(block.get("evidence_modality") or modality_for_category(block_type))
        store.append_event(
            action="insert", target="anchor", board_id=board, target_id=anchor_id, parent_event_ids=[run_id],
            payload={
                "source_id": source_id, "work_id": work_id, "source_title": source_title, "file_path": str(path),
                "document_hash": document_hash, "page_index": block.get("page"), "block_id": block_id,
                "category": block_type, "evidence_modality": modality, "aspect": aspects_by_block.get(block_id, "other"),
                "bbox": block.get("bbox"), "polygon": block.get("polygon"),
                "section_hierarchy": block.get("section_hierarchy") or {},
                "html": str(block.get("html") or ""), "text": str(block.get("text") or ""),
                "image_keys": block.get("image_keys") or [],
            },
        )
        anchor_count += 1

    marker_to_reference: dict[int, str] = {}
    for reference in enrichment.get("references", []):
        reference_id = new_id("reference")
        marker_number = int(reference.get("marker_number") or 0)
        store.append_event(
            action="insert", target="reference", board_id=board, target_id=reference_id, parent_event_ids=[run_id],
            payload={
                "source_id": source_id, "marker_number": marker_number, "raw_string": reference.get("raw_string", ""),
                "external_id": "", "resolved_title": "", "resolution_status": "unresolved",
            },
        )
        if marker_number:
            marker_to_reference[marker_number] = reference_id

    for block_id, markers in enrichment.get("citations_by_block", {}).items():
        anchor_id = block_to_anchor.get(str(block_id))
        if not anchor_id:
            continue
        for marker in markers:
            store.append_event(
                action="insert", target="citation", board_id=board, target_id=new_id("citation"), parent_event_ids=[anchor_id],
                payload={
                    "source_id": source_id, "source_anchor_id": anchor_id, "marker_number": int(marker),
                    "reference_id": marker_to_reference.get(int(marker), ""), "cited_work_id": "", "external_id": "",
                },
            )

    images = parsed.get("images", {}) or {}
    image_directory = image_blob_directory(document_hash or source_id)
    figure_descriptions = enrichment.get("figure_descriptions", {})
    figure_count = 0
    for block in parsed.get("blocks", []):
        if str(block.get("block_type")) not in {"Figure", "FigureGroup", "Picture", "PictureGroup", "Diagram"}:
            continue
        block_id = str(block.get("block_id") or "")
        anchor_id = block_to_anchor.get(block_id, "")
        for image_key in block.get("image_keys") or []:
            base64_data = images.get(image_key)
            if not base64_data:
                continue
            blob_path = _write_image_blob(image_directory, image_key, base64_data)
            description = figure_descriptions.get(f"{block_id}::{image_key}", "")
            store.append_event(
                action="insert", target="figure", board_id=board, target_id=new_id("figure"),
                parent_event_ids=[anchor_id] if anchor_id else [run_id],
                payload={
                    "anchor_id": anchor_id, "source_id": source_id, "work_id": work_id, "blob_path": blob_path,
                    "mime": "image/png", "description": description,
                    "description_status": "described" if description else "pending",
                },
            )
            figure_count += 1

    return {
        "code": "source_ingested", "source_id": source_id, "work_id": work_id,
        "from_cache": parsed.get("from_cache", False), "page_count": parsed.get("page_count", 0),
        "anchor_count": anchor_count, "reference_count": len(enrichment.get("references", [])), "figure_count": figure_count,
    }


def _index_text_document(board: str, source: dict[str, Any], path: Path, run_id: str) -> dict[str, Any]:
    store = get_board_store()
    work_id = str(source.get("work_id") or "")
    text = path.read_text(encoding="utf-8", errors="replace")
    chunks = [chunk.strip() for chunk in text.split("\n\n") if chunk.strip()]
    if not chunks and text.strip():
        chunks = [text.strip()]
    for chunk in chunks:
        store.append_event(
            action="insert", target="anchor", board_id=board, target_id=new_id("anchor"), parent_event_ids=[run_id],
            payload={
                "source_id": source["source_id"], "work_id": work_id, "source_title": source.get("title", ""),
                "file_path": str(path), "page_index": None, "category": "Text", "evidence_modality": "text",
                "block_id": new_id("block"), "text": chunk,
            },
        )
    return {"code": "source_ingested", "source_id": source["source_id"], "work_id": work_id, "anchor_count": len(chunks)}


def _index_structured_record(board: str, source: dict[str, Any], run_id: str) -> dict[str, Any]:
    record = source.get("record") or source.get("data") or source.get("metadata") or {}
    if not record:
        return _quarantine(board, source["source_id"], "missing_structured_payload", "Structured record has no record/data payload.", run_id)
    store = get_board_store()
    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)
    store.append_event(
        action="insert", target="anchor", board_id=board, target_id=new_id("anchor"), parent_event_ids=[run_id],
        payload={
            "source_id": source["source_id"], "work_id": str(source.get("work_id") or ""), "source_title": source.get("title", ""),
            "category": "StructuredRecord", "evidence_modality": "structured_data", "text": serialized,
        },
    )
    return {"code": "source_ingested", "source_id": source["source_id"], "anchor_count": 1}


def _write_image_blob(image_directory: Path, image_key: str, base64_data: str) -> str:
    if not base64_data:
        return ""
    try:
        image_directory.mkdir(parents=True, exist_ok=True)
        encoded = base64_data.split(",", 1)[-1] if base64_data.startswith("data:") else base64_data
        raw = base64.b64decode(encoded)
        path = image_directory / image_key.replace("/", "_")
        if not path.suffix:
            path = path.with_suffix(".png")
        path.write_bytes(raw)
        return str(path)
    except Exception:  # noqa: BLE001 — a bad image must not fail the whole document
        return ""


def _quarantine(board: str, source_id: str, reason_code: str, message: str, run_id: str) -> dict[str, Any]:
    event = get_board_store().append_event(
        action="insert", target="quarantine", board_id=board, target_id=new_id("quarantine"), parent_event_ids=[run_id],
        payload={"source_id": source_id, "ingest_run_id": run_id, "reason_code": reason_code, "message": message},
    )
    return {"code": "source_quarantined", "reason_code": reason_code, "event_id": event["event_id"]}
