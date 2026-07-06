"""Vocabulary and validation for the blackboard's event log and read model.

Every write is an append-only event with an ``action`` (the verb) and a ``target``
(the kind of record). Each target projects into a read-model table of the same
plural name, which the agent queries with read-only SQL.
"""
from __future__ import annotations

import re


# The four verbs. ``insert`` creates a record; ``annotate`` patches fields on one;
# ``exclude``/``supersede`` retire one without deleting it (the log stays whole).
BOARD_ACTIONS = {"insert", "annotate", "exclude", "supersede"}

# The kinds of record a board holds. Each maps to a projected table (plural).
BOARD_TARGETS = {
    "board",          # the workspace itself
    "work",           # a canonical scholarly work (identity: DOI / arXiv / title)
    "source",         # a concrete artifact you added and can ingest (PDF, URL, record)
    "ingest_run",     # a marker for one parse+enrich pass over a source
    "anchor",         # a verbatim layout block from a parsed document
    "reference",      # one bibliography entry inside a source (cites a work)
    "citation",       # a graph edge: an anchor cites a work (via a reference)
    "figure",         # a figure/image with a vision-model description
    "claim",          # a first-class assertion the agent asserts and tracks
    "claim_support",  # an edge: an anchor supports / contradicts / qualifies a claim
    "topic",          # a classification label
    "work_topic",     # an edge: a work is classified under a topic
    "note",           # a free-form observation
    "quarantine",     # a source that could not be read or parsed
}

ORIGIN_CHANNELS = {"zotero", "literature", "webpage", "database", "upload", "manual"}
SOURCE_KINDS = {"document", "structured_record", "annotation"}
ANCHOR_MODALITIES = {"text", "image", "table", "formula", "structured_data"}

# The information *type* a passage conveys, assigned at ingest. This is the
# classification that makes the board more than keyword search: it lets you slice the
# whole corpus by what a passage IS (every method, every limitation, every reported
# metric) across papers, not just by the words it contains. A controlled vocabulary
# keeps the tags comparable across documents; an unknown label falls back to "other".
ANCHOR_ASPECTS = {
    "background",   # context, motivation, or prior state of the field
    "definition",   # a term, concept, or notation being defined
    "hypothesis",   # a research question, aim, or hypothesis
    "method",       # methodology, materials, apparatus, procedure, setup
    "dataset",      # the data, samples, or corpus used
    "result",       # a finding, observation, or outcome
    "metric",       # a specific quantitative value, measurement, or benchmark
    "limitation",   # a caveat, threat to validity, or acknowledged weakness
    "comparison",   # a comparison against other work, methods, or baselines
    "conclusion",   # a conclusion, implication, or takeaway
    "future_work",  # proposed future directions
    "other",        # none of the above
}

# A work's role in the board: one you have read (ingested), one merely cited by a
# read paper (cited), or one you have decided to chase but not yet read (frontier).
WORK_STATUSES = {"ingested", "cited", "frontier"}

# A claim's standing given the evidence gathered for it so far.
CLAIM_STATUSES = {"open", "supported", "contested", "refuted"}

# How a piece of evidence bears on a claim.
SUPPORT_STANCES = {"supports", "contradicts", "qualifies"}

# Marker (Datalab) block types → coarse anchor modality. Marker emits a richer
# document-tree label set than flat layout categories; the raw block_type is kept
# on every anchor, and this maps it to a modality for retrieval/filtering.
MARKER_BLOCK_MODALITY = {
    "Text": "text",
    "TextInlineMath": "text",
    "SectionHeader": "text",
    "Title": "text",
    "Caption": "text",
    "Footnote": "text",
    "PageHeader": "text",
    "PageFooter": "text",
    "ListGroup": "text",
    "ListItem": "text",
    "TableOfContents": "text",
    "Reference": "text",
    "Code": "text",
    "Handwriting": "text",
    "Form": "text",
    "Equation": "formula",
    "Table": "table",
    "TableGroup": "table",
    "Figure": "image",
    "FigureGroup": "image",
    "Picture": "image",
    "PictureGroup": "image",
    "Diagram": "image",
}

# Block types that carry a figure/image — these get a vision-model description
# rather than plain text extraction.
IMAGE_BLOCK_TYPES = {block for block, modality in MARKER_BLOCK_MODALITY.items() if modality == "image"}


def normalize_origin_channel(value: str) -> str:
    """Validate an origin channel against the canonical set. The value comes from
    the model, which is given the canonical names, so an unknown value falls back
    to ``manual`` rather than being silently rewritten."""
    normalized = (value or "").strip().lower()
    return normalized if normalized in ORIGIN_CHANNELS else "manual"


def normalize_source_kind(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in SOURCE_KINDS else "document"


def normalize_aspect(value: str) -> str:
    """Validate an information-aspect label against the controlled vocabulary. The
    label comes from the model, which is given the exact vocabulary, so an unknown or
    empty value falls back to ``other`` rather than polluting the classification."""
    normalized = (value or "").strip().lower()
    return normalized if normalized in ANCHOR_ASPECTS else "other"


def modality_for_category(category: str, fallback: str = "text") -> str:
    if category in MARKER_BLOCK_MODALITY:
        return MARKER_BLOCK_MODALITY[category]
    normalized = (fallback or "text").strip().lower()
    return normalized if normalized in ANCHOR_MODALITIES else "text"


def work_identity_key(*, doi: str = "", arxiv: str = "", external_id: str = "", title: str = "") -> str:
    """A stable dedup key for a scholarly work, preferring the strongest identifier
    available: DOI, then arXiv id, then any other external id, then a normalized
    title. Two records with the same key are the same work — this is what lets a
    cited reference and an ingested source collapse onto one node."""
    doi_value = (doi or "").strip().lower()
    if doi_value:
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if doi_value.startswith(prefix):
                doi_value = doi_value[len(prefix):]
        return f"doi:{doi_value.strip('/')}"
    arxiv_value = (arxiv or "").strip().lower().removeprefix("arxiv:")
    if arxiv_value:
        return f"arxiv:{arxiv_value}"
    external_value = (external_id or "").strip().lower()
    if external_value:
        return f"id:{external_value}"
    title_value = re.sub(r"\s+", " ", (title or "").strip().lower())
    if title_value:
        return f"title:{title_value}"
    return ""
