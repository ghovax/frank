from __future__ import annotations

from typing import Any


BOARD_ACTIONS = {
    "insert",
    "annotate",
    "exclude",
    "supersede",
    "prepare",
    "publish",
    "inspect",
}

BOARD_TARGETS = {
    "workspace",
    "source",
    "preparation_run",
    "evidence",
    "anchor",
    "report",
    "note",
    "quarantine",
}

EVIDENCE_OPERATIONS = {
    "search",
    "source",
    "anchor",
    "quarantine",
    "validate_report",
}

OPEN_TARGETS = {"anchor", "source", "report"}

ORIGIN_CHANNELS = {
    "zotero",
    "literature",
    "web_page",
    "database",
    "upload",
    "manual",
}

SOURCE_KINDS = {
    "document",
    "structured_record",
    "annotation",
}

EVIDENCE_MODALITIES = {
    "text",
    "image",
    "table",
    "formula",
    "structured_data",
}

DOTS_LAYOUT_CATEGORIES = {
    "Caption",
    "Footnote",
    "Formula",
    "List-item",
    "Page-footer",
    "Page-header",
    "Picture",
    "Section-header",
    "Table",
    "Text",
    "Title",
}

DOTS_CATEGORY_MODALITY = {
    "Caption": "text",
    "Footnote": "text",
    "Formula": "formula",
    "List-item": "text",
    "Page-footer": "text",
    "Page-header": "text",
    "Picture": "image",
    "Section-header": "text",
    "Table": "table",
    "Text": "text",
    "Title": "text",
}


def normalize_origin_channel(value: str) -> str:
    normalized = (value or "manual").strip().lower().replace("-", "_")
    if normalized == "web":
        normalized = "web_page"
    if normalized == "literature_api":
        normalized = "literature"
    return normalized if normalized in ORIGIN_CHANNELS else "manual"


def normalize_source_kind(value: str) -> str:
    normalized = (value or "document").strip().lower().replace("-", "_")
    return normalized if normalized in SOURCE_KINDS else "document"


def modality_for_category(category: str, fallback: str = "text") -> str:
    if category in DOTS_CATEGORY_MODALITY:
        return DOTS_CATEGORY_MODALITY[category]
    normalized = (fallback or "text").strip().lower().replace("-", "_")
    return normalized if normalized in EVIDENCE_MODALITIES else "text"


def compact_text(value: Any, maximum: int = 600) -> str:
    text = str(value or "").strip()
    text = " ".join(text.split())
    if len(text) <= maximum:
        return text
    return text[: maximum - 1].rstrip() + "…"
