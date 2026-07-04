"""The evidence blackboard.

A durable, append-only knowledge base (SQLite under ``~/.blackboard``) the agent
scratches findings onto and queries with read-only SQL. It turns documents (PDFs,
DOIs, URLs, structured records) into queryable anchors: Datalab (Marker) parses each
into layout blocks, a fan-out extracts citation markers and figure descriptions and
splits the bibliography, and everything is read back with SQL. On top of that sit
three durable structures: canonical **works** (identity + the cross-document citation
graph), first-class **claims** with evidence edges, and a **topics** classification.

This package is standalone — import it and call its functions from Python run with
``uv run python`` from this package's directory (installed editable, so
``import blackboard`` works with no setup and edits are live).
"""
from .board import (
    add_claim,
    add_source,
    board_state,
    cite_evidence,
    classify,
    ingest,
    link_reference,
    note,
    open_board,
    query,
)

__all__ = [
    # Board lifecycle and ingestion.
    "open_board", "add_source", "ingest",
    # Findings the agent asserts.
    "add_claim", "cite_evidence", "link_reference", "classify", "note",
    # Reading.
    "query", "board_state",
]
