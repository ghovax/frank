from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from harness.identifiers import new_id
from harness.research.schema import DOTS_LAYOUT_CATEGORIES, modality_for_category


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
DOCUMENT_SUFFIXES = {".pdf", *IMAGE_SUFFIXES}


class DotsBackendUnavailable(RuntimeError):
    pass


class DotsParseError(RuntimeError):
    pass


def dots_available() -> bool:
    return importlib.util.find_spec("dots_mocr") is not None


def parse_with_local_dots(input_path: Path, output_directory: Path) -> list[dict[str, Any]]:
    """Run local Dots/MOCR parsing and return normalized page records.

    This intentionally does not install anything. The machine is declaratively
    managed, so the backend is used only when the local Python environment already
    exposes ``dots_mocr``. The caller records a quarantine event when unavailable.
    """
    if not dots_available():
        raise DotsBackendUnavailable(
            "Local dots_mocr package is not importable. Add it declaratively before preparing PDFs/images."
        )
    try:
        from dots_mocr.parser import DotsMOCRParser
    except Exception as exception:  # noqa: BLE001
        raise DotsBackendUnavailable(f"Could not import dots_mocr parser: {exception}") from exception

    output_directory.mkdir(parents=True, exist_ok=True)
    use_hf = os.environ.get("DAISY_DOTS_MOCR_USE_HF", "").strip().lower() in {"1", "true", "yes"}
    prompt_mode = os.environ.get("DAISY_DOTS_MOCR_PROMPT", "prompt_layout_all_en").strip() or "prompt_layout_all_en"
    try:
        parser = DotsMOCRParser(
            output_dir=str(output_directory),
            use_hf=use_hf,
        )
        results = parser.parse_file(str(input_path), output_dir=str(output_directory), prompt_mode=prompt_mode)
    except Exception as exception:  # noqa: BLE001
        raise DotsParseError(f"Dots/MOCR parsing failed: {exception}") from exception

    normalized = []
    for page_result in results:
        layout_path = Path(str(page_result.get("layout_info_path") or ""))
        cells: list[dict[str, Any]] = []
        if layout_path.is_file():
            try:
                parsed = json.loads(layout_path.read_text(encoding="utf-8"))
                if isinstance(parsed, list):
                    cells = [cell for cell in parsed if isinstance(cell, dict)]
                elif isinstance(parsed, dict):
                    maybe_cells = parsed.get("layout") or parsed.get("cells") or parsed.get("result")
                    if isinstance(maybe_cells, list):
                        cells = [cell for cell in maybe_cells if isinstance(cell, dict)]
            except Exception as exception:  # noqa: BLE001
                raise DotsParseError(f"Could not read Dots layout JSON {layout_path}: {exception}") from exception
        normalized.append({
            "page_index": int(page_result.get("page_no") or 0),
            "input_width": int(page_result.get("input_width") or 0),
            "input_height": int(page_result.get("input_height") or 0),
            "layout_info_path": str(layout_path) if layout_path else "",
            "layout_image_path": str(page_result.get("layout_image_path") or ""),
            "md_content_path": str(page_result.get("md_content_path") or ""),
            "md_content_nohf_path": str(page_result.get("md_content_nohf_path") or ""),
            "cells": [_normalize_cell(cell) for cell in cells],
        })
    return normalized


def _normalize_cell(cell: dict[str, Any]) -> dict[str, Any]:
    category = str(cell.get("category") or cell.get("label") or cell.get("type") or "Text")
    if category not in DOTS_LAYOUT_CATEGORIES:
        category = "Text"
    bbox = cell.get("bbox") or cell.get("box") or []
    if not isinstance(bbox, list) or len(bbox) != 4:
        bbox = [0, 0, 0, 0]
    text = cell.get("text")
    return {
        "layout_cell_id": str(cell.get("id") or new_id("cell")),
        "category": category,
        "evidence_modality": modality_for_category(category),
        "bbox": [float(value or 0) for value in bbox],
        "text": text if isinstance(text, str) else "",
        "raw": cell,
    }
