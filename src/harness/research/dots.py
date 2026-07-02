from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import httpx

from harness.core.configuration import DotsOCRConfiguration
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


def parse_with_dots(
    input_path: Path,
    output_directory: Path,
    configuration: DotsOCRConfiguration,
) -> list[dict[str, Any]]:
    """Run the configured Dots/MOCR parser and return normalized page records."""
    if not configuration.enabled:
        raise DotsBackendUnavailable("Dots/MOCR parsing is disabled in settings.")
    if configuration.endpoint.strip():
        return parse_with_dots_endpoint(input_path, output_directory, configuration)
    return parse_with_local_dots(input_path, output_directory, configuration)


def parse_with_dots_endpoint(
    input_path: Path,
    output_directory: Path,
    configuration: DotsOCRConfiguration,
) -> list[dict[str, Any]]:
    """Call a Dots-compatible parser endpoint.

    Endpoint contract: POST multipart form data with ``file`` plus parser options;
    return either a list of page records or an object containing ``pages`` or
    ``results``. Page records may contain Dots parser paths and/or inline cells.
    """
    output_directory.mkdir(parents=True, exist_ok=True)
    headers = {}
    if configuration.effective_api_key:
        headers["Authorization"] = f"Bearer {configuration.effective_api_key}"
    try:
        with input_path.open("rb") as file_handle:
            response = httpx.post(
                configuration.endpoint,
                headers=headers,
                files={"file": (input_path.name, file_handle, "application/octet-stream")},
                data={
                    "prompt_mode": configuration.prompt_mode,
                    "model_name": configuration.model_name,
                    "output_format": "dots_json",
                },
                timeout=configuration.timeout_seconds,
            )
        response.raise_for_status()
        payload = response.json()
    except Exception as exception:  # noqa: BLE001
        raise DotsParseError(f"Dots/MOCR endpoint request failed: {exception}") from exception
    pages = _extract_pages(payload)
    if not pages:
        raise DotsParseError("Dots/MOCR endpoint returned no page records.")
    raw_path = output_directory / "endpoint-response.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return [_normalize_page_result(page, fallback_layout_path=raw_path) for page in pages]


def parse_with_local_dots(
    input_path: Path,
    output_directory: Path,
    configuration: DotsOCRConfiguration,
) -> list[dict[str, Any]]:
    """Run local in-process Dots/MOCR parsing and return normalized page records.

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
    prompt_mode = configuration.prompt_mode.strip() or "prompt_layout_all_en"
    try:
        parser = DotsMOCRParser(
            output_dir=str(output_directory),
            use_hf=use_hf,
            model_name=configuration.model_name,
        )
        results = parser.parse_file(str(input_path), output_dir=str(output_directory), prompt_mode=prompt_mode)
    except Exception as exception:  # noqa: BLE001
        raise DotsParseError(f"Dots/MOCR parsing failed: {exception}") from exception

    return [_normalize_page_result(page_result) for page_result in results]


def _extract_pages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [page for page in payload if isinstance(page, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("pages", "results", "layout", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [page for page in value if isinstance(page, dict)]
    return []


def _normalize_page_result(page_result: dict[str, Any], fallback_layout_path: Path | None = None) -> dict[str, Any]:
    layout_path_raw = str(page_result.get("layout_info_path") or "")
    layout_path = Path(layout_path_raw) if layout_path_raw else None
    cells = _cells_from_page_result(page_result, layout_path)
    return {
        "page_index": int(page_result.get("page_no") or page_result.get("page_index") or 0),
        "input_width": int(page_result.get("input_width") or page_result.get("page_width") or 0),
        "input_height": int(page_result.get("input_height") or page_result.get("page_height") or 0),
        "layout_info_path": str(layout_path) if layout_path is not None else str(fallback_layout_path or ""),
        "layout_image_path": str(page_result.get("layout_image_path") or ""),
        "md_content_path": str(page_result.get("md_content_path") or ""),
        "md_content_nohf_path": str(page_result.get("md_content_nohf_path") or ""),
        "cells": [_normalize_cell(cell) for cell in cells],
    }


def _cells_from_page_result(page_result: dict[str, Any], layout_path: Path | None) -> list[dict[str, Any]]:
    for key in ("cells", "layout", "result", "elements"):
        cells = page_result.get(key)
        if isinstance(cells, list):
            return [cell for cell in cells if isinstance(cell, dict)]
    if layout_path is None or not layout_path.is_file():
        return []
    try:
        parsed = json.loads(layout_path.read_text(encoding="utf-8"))
    except Exception as exception:  # noqa: BLE001
        raise DotsParseError(f"Could not read Dots layout JSON {layout_path}: {exception}") from exception
    if isinstance(parsed, list):
        return [cell for cell in parsed if isinstance(cell, dict)]
    if isinstance(parsed, dict):
        for key in ("layout", "cells", "result", "elements"):
            cells = parsed.get(key)
            if isinstance(cells, list):
                return [cell for cell in cells if isinstance(cell, dict)]
    return []


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
