"""Datalab hosted document parser.

Turns a PDF or image into Marker's structured JSON — a tree of pages and layout
blocks (block type, HTML, bbox, polygon, section hierarchy, base64 images) — via
Datalab's hosted API through the official ``datalab_sdk``. Nothing heavy runs
locally; parsing is a hosted, per-page-billed call, so the raw result is cached by
document content hash and only the derived evidence views are recomputed.

The public entry point, :func:`parse_document`, returns the raw Marker JSON plus a
flattened list of content blocks (the anchor granularity the blackboard indexes)
and the document's base64 image map.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from markdownify import markdownify

from .board_common import parse_cache_directory
from .board_schema import modality_for_category


@dataclass
class ParseOptions:
    """How to call Datalab. The API key comes from ``DATALAB_API_KEY`` in the
    environment (or a project ``.env``); the rest have sensible defaults."""

    api_key: str = ""
    base_url: str = "https://www.datalab.to"
    mode: str = "fast"          # fast | balanced | accurate
    maximum_pages: int = 0      # 0 = all pages
    extract_images: bool = True
    timeout_seconds: int = 300

    @classmethod
    def from_env(cls) -> "ParseOptions":
        truthy = os.environ.get("BLACKBOARD_PARSE_EXTRACT_IMAGES", "1").strip().lower() not in {"0", "false", "no"}
        return cls(
            api_key=os.environ.get("DATALAB_API_KEY", ""),
            base_url=os.environ.get("DATALAB_BASE_URL", "https://www.datalab.to"),
            mode=os.environ.get("BLACKBOARD_PARSE_MODE", "fast"),
            maximum_pages=int(os.environ.get("BLACKBOARD_PARSE_MAXIMUM_PAGES", "0") or 0),
            extract_images=truthy,
        )

    @property
    def effective_api_key(self) -> str:
        return self.api_key or os.environ.get("DATALAB_API_KEY", "")


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
DOCUMENT_SUFFIXES = {".pdf", *IMAGE_SUFFIXES}

# Tree nodes that are pure containers (no evidence of their own) — walked through
# rather than emitted as anchors. Groups are transparent too: their members (a
# figure and its caption, a list's items) are the real anchors.
_CONTAINER_BLOCK_TYPES = {"Document", "Page"}
_GROUP_BLOCK_TYPES = {"ListGroup", "TableGroup", "FigureGroup", "PictureGroup"}

# Bumped when the parse/flatten contract changes, so cache keys invalidate cleanly.
PARSER_VERSION = "datalab-marker-1"


class DatalabUnavailable(RuntimeError):
    pass


class DatalabParseError(RuntimeError):
    pass


def datalab_available() -> bool:
    return importlib.util.find_spec("datalab_sdk") is not None


def _faithful_text(node: dict[str, Any], html: str) -> str:
    """A plain-text rendering of a block for search and display, without altering
    the content. Datalab's own per-block ``markdown`` is preferred (it is the
    authoritative rendering); otherwise the HTML is converted losslessly with the
    ``markdownify`` library. Nothing is truncated and no hand-rolled parsing is
    used — the verbatim ``html`` is kept separately as the source of truth."""
    markdown = node.get("markdown")
    if isinstance(markdown, str) and markdown.strip():
        return markdown
    if html:
        return markdownify(html)
    return ""


def _bbox_from_polygon(polygon: Any) -> list[float]:
    if not isinstance(polygon, list) or not polygon:
        return [0.0, 0.0, 0.0, 0.0]
    xs = [float(point[0]) for point in polygon if isinstance(point, (list, tuple)) and len(point) >= 2]
    ys = [float(point[1]) for point in polygon if isinstance(point, (list, tuple)) and len(point) >= 2]
    if not xs or not ys:
        return [0.0, 0.0, 0.0, 0.0]
    return [min(xs), min(ys), max(xs), max(ys)]


def _block_record(node: dict[str, Any]) -> dict[str, Any]:
    block_type = str(node.get("block_type") or "Text")
    bbox = node.get("bbox")
    if not (isinstance(bbox, list) and len(bbox) == 4):
        bbox = _bbox_from_polygon(node.get("polygon"))
    html = str(node.get("html") or "")
    markdown = node.get("markdown")
    return {
        "block_id": str(node.get("id") or ""),
        "block_type": block_type,
        "evidence_modality": modality_for_category(block_type),
        "html": html,
        "text": _faithful_text(node, html),
        "markdown": markdown if isinstance(markdown, str) else None,
        "page": int(node.get("page") or 0),
        "bbox": [float(value or 0) for value in bbox],
        "polygon": node.get("polygon"),
        "section_hierarchy": node.get("section_hierarchy") or {},
        "image_keys": list((node.get("images") or {}).keys()),
    }


def flatten_marker_tree(root: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk Marker's page/block tree into a flat, reading-order list of content
    blocks. Containers (Document, Page) are walked through; groups are transparent
    (their members are emitted); everything else is emitted as one anchor and not
    descended into (so spans/lines inside a paragraph do not become anchors)."""
    blocks: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        if not isinstance(node, dict):
            return
        block_type = str(node.get("block_type") or "")
        children = node.get("children") or []
        if block_type in _CONTAINER_BLOCK_TYPES or block_type in _GROUP_BLOCK_TYPES:
            for child in children:
                walk(child)
            return
        blocks.append(_block_record(node))

    walk(root)
    return blocks


def _document_hash(pdf_bytes: bytes, configuration: ParseOptions) -> str:
    hasher = hashlib.sha256()
    hasher.update(pdf_bytes)
    options_signature = json.dumps(
        {
            "version": PARSER_VERSION,
            "mode": configuration.mode,
            "maximum_pages": configuration.maximum_pages,
            "extract_images": configuration.extract_images,
        },
        sort_keys=True,
    )
    hasher.update(options_signature.encode("utf-8"))
    return hasher.hexdigest()


def _convert(input_path: Path, configuration: ParseOptions) -> Any:
    """Run the hosted conversion. Isolated so the cache path around it stays clean."""
    try:
        from datalab_sdk import DatalabClient, ConvertOptions
    except Exception as exception:  # noqa: BLE001
        raise DatalabUnavailable(f"Could not import datalab_sdk: {exception}") from exception

    api_key = configuration.effective_api_key
    if not api_key:
        raise DatalabUnavailable(
            "No Datalab API key configured. Set the DATALAB_API_KEY environment variable (or a project .env)."
        )
    option_kwargs: dict[str, Any] = {
        "output_format": "json",
        "mode": configuration.mode,
        "disable_image_extraction": not configuration.extract_images,
    }
    if configuration.maximum_pages and configuration.maximum_pages > 0:
        option_kwargs["max_pages"] = configuration.maximum_pages  # Datalab SDK kwarg name
    client = DatalabClient(
        api_key=api_key,
        base_url=configuration.base_url or "https://www.datalab.to",
        timeout=int(configuration.timeout_seconds),
    )
    try:
        return client.convert(str(input_path), options=ConvertOptions(**option_kwargs))
    except Exception as exception:  # noqa: BLE001
        raise DatalabParseError(f"Datalab conversion failed: {exception}") from exception


def parse_document(
    input_path: Path,
    configuration: ParseOptions | None = None,
    cache_directory: Path | None = None,
) -> dict[str, Any]:
    """Parse a document via Datalab and return the raw Marker JSON plus a flattened
    block list and base64 image map. Results are cached by content hash under
    ``cache_directory`` (``~/.blackboard/parse-cache`` by default), so re-parsing the
    same document never re-pays the API."""
    configuration = configuration or ParseOptions.from_env()
    cache_directory = cache_directory or parse_cache_directory()
    if not input_path.is_file():
        raise DatalabParseError(f"Source file not found: {input_path}")

    pdf_bytes = input_path.read_bytes()
    document_hash = _document_hash(pdf_bytes, configuration)
    cache_directory.mkdir(parents=True, exist_ok=True)
    cache_path = cache_directory / f"{document_hash}.json"

    from_cache = cache_path.is_file()
    if from_cache:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        raw_json = cached.get("json") or {}
        images = cached.get("images") or {}
        page_count = int(cached.get("page_count") or 0)
    else:
        result = _convert(input_path, configuration)
        if not getattr(result, "success", False):
            raise DatalabParseError(f"Datalab returned no result: {getattr(result, 'error', 'unknown error')}")
        raw_json = getattr(result, "json", None) or {}
        images = getattr(result, "images", None) or {}
        page_count = int(getattr(result, "page_count", 0) or 0)
        # Immutable cache: the raw parse is the source of truth; all derived views
        # recompute from it without re-billing Datalab.
        cache_path.write_text(
            json.dumps({"json": raw_json, "images": images, "page_count": page_count}, ensure_ascii=False),
            encoding="utf-8",
        )

    blocks = flatten_marker_tree(raw_json) if isinstance(raw_json, dict) else []
    return {
        "document_hash": document_hash,
        "from_cache": from_cache,
        "raw_json_path": str(cache_path),
        "page_count": page_count,
        "blocks": blocks,
        "images": images,
    }
