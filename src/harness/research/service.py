from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.core.configuration import DotsOCRConfiguration
from harness.identifiers import new_id
from harness.research.dots import (
    DOCUMENT_SUFFIXES,
    DotsBackendUnavailable,
    DotsParseError,
    parse_with_dots,
)
from harness.research.schema import (
    EVIDENCE_OPERATIONS,
    OPEN_TARGETS,
    compact_text,
    modality_for_category,
)
from harness.research.store import ResearchStoreError, get_store, research_database_path


def research_board(
    *,
    action: str,
    target: str,
    workspace_id: str = "",
    target_id: str = "",
    payload: dict[str, Any] | None = None,
    parent_event_ids: list[str] | None = None,
    expected_revision: int | None = None,
    idempotency_key: str = "",
    actor_id: str = "agent",
    dots_ocr: DotsOCRConfiguration | None = None,
) -> dict[str, Any]:
    store = get_store()
    action = (action or "").strip().lower()
    target = (target or "").strip().lower()
    payload = dict(payload or {})
    try:
        if action == "inspect":
            return _inspect(target, workspace_id, target_id)
        if action == "prepare":
            return _prepare(
                workspace_id,
                target,
                target_id,
                payload,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                dots_ocr=dots_ocr or DotsOCRConfiguration(),
            )
        event = store.append_event(
            action=action,
            target=target,
            workspace_id=workspace_id,
            target_id=target_id,
            payload=payload,
            actor_id=actor_id,
            parent_event_ids=parent_event_ids,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        return {
            "code": "research_board_event_appended",
            "event": event,
            "state": store.workspace_state(event["workspace_id"]) if event["target"] == "workspace" else None,
        }
    except ResearchStoreError as exception:
        return {"code": "research_board_error", "message": str(exception)}


def research_evidence(
    *,
    operation: str,
    workspace_id: str,
    query: str = "",
    target_id: str = "",
    filters: dict[str, Any] | None = None,
    limit: int = 12,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    operation = (operation or "").strip().lower()
    if operation not in EVIDENCE_OPERATIONS:
        return {"code": "research_evidence_error", "message": f"Unsupported evidence operation: {operation}"}
    if not workspace_id:
        return {"code": "research_evidence_error", "message": "workspace_id is required."}
    filters = dict(filters or {})
    store = get_store()
    if operation == "search":
        return _search_evidence(workspace_id, query=query, filters=filters, limit=limit)
    if operation == "source":
        sources = store.sources(workspace_id, include_inactive=True)
        source = next((item for item in sources if item.get("source_id") == target_id), None)
        if source is None:
            return {"code": "research_source_not_found", "workspace_id": workspace_id, "source_id": target_id}
        cards = [card for card in store.evidence(workspace_id) if card.get("source_id") == target_id]
        anchors = [anchor for anchor in store.anchors(workspace_id) if anchor.get("source_id") == target_id]
        return {"code": "research_source", "source": source, "evidence_cards": cards[:limit], "anchors": anchors[:limit]}
    if operation == "anchor":
        anchors = store.anchors(workspace_id)
        anchor = next((item for item in anchors if item.get("anchor_id") == target_id), None)
        if anchor is None:
            return {"code": "research_anchor_not_found", "workspace_id": workspace_id, "anchor_id": target_id}
        return {"code": "research_anchor", "anchor": anchor}
    if operation == "quarantine":
        return {"code": "research_quarantine", "records": store.quarantine(workspace_id), "count": len(store.quarantine(workspace_id))}
    if operation == "validate_report":
        return _validate_report(workspace_id, target_id=target_id, report=report)
    return {"code": "research_evidence_error", "message": "Unhandled evidence operation."}


def research_open(*, target: str, workspace_id: str, target_id: str) -> dict[str, Any]:
    target = (target or "").strip().lower()
    if target not in OPEN_TARGETS:
        return {"code": "research_open_error", "message": f"Unsupported open target: {target}"}
    if not workspace_id or not target_id:
        return {"code": "research_open_error", "message": "workspace_id and target_id are required."}
    store = get_store()
    if target == "anchor":
        anchor = next((item for item in store.anchors(workspace_id) if item.get("anchor_id") == target_id), None)
        if anchor is None:
            return {"code": "research_anchor_not_found", "workspace_id": workspace_id, "anchor_id": target_id}
        artifact = {
            "type": "research_anchor",
            "artifact_id": new_id("research-anchor"),
            "title": anchor.get("title") or anchor.get("source_title") or "Research anchor",
            "workspace_id": workspace_id,
            "anchor_id": target_id,
            "source_id": anchor.get("source_id", ""),
            "file": anchor.get("file_path", ""),
            "page_index": anchor.get("page_index"),
            "bbox": anchor.get("bbox"),
            "category": anchor.get("category", ""),
            "text": anchor.get("text", ""),
        }
        return {"code": "research_anchor_opened", "artifacts": [artifact], "model_context": {"anchor_id": target_id}}
    if target == "report":
        report = next((item for item in store.reports(workspace_id) if item.get("report_id") == target_id), None)
        if report is None:
            return {"code": "research_report_not_found", "workspace_id": workspace_id, "report_id": target_id}
        artifact = {
            "type": "research_report",
            "artifact_id": new_id("research-report"),
            "title": report.get("title") or "Research report",
            "workspace_id": workspace_id,
            "report_id": target_id,
            "markdown": report.get("markdown") or report.get("content") or "",
            "citations": report.get("citations") or [],
        }
        return {"code": "research_report_opened", "artifacts": [artifact], "model_context": {"report_id": target_id}}
    source = next((item for item in store.sources(workspace_id, include_inactive=True) if item.get("source_id") == target_id), None)
    if source is None:
        return {"code": "research_source_not_found", "workspace_id": workspace_id, "source_id": target_id}
    path = str(source.get("path") or "")
    if path and Path(path).is_file():
        artifact = {
            "type": "iframe",
            "artifact_id": new_id("research-source"),
            "title": source.get("title") or "Research source",
            "file": path,
            "height": "auto",
            "summary": f"Opened research source {source.get('title') or target_id}.",
        }
    else:
        artifact = {
            "type": "research_source",
            "artifact_id": new_id("research-source"),
            "title": source.get("title") or "Research source",
            "source": source,
        }
    return {"code": "research_source_opened", "artifacts": [artifact], "model_context": {"source_id": target_id}}


def _inspect(target: str, workspace_id: str, target_id: str) -> dict[str, Any]:
    store = get_store()
    if target == "workspace":
        if not workspace_id:
            return {"code": "research_board_error", "message": "workspace_id is required for inspect/workspace."}
        return {"code": "research_workspace_state", "state": store.workspace_state(workspace_id)}
    if not workspace_id:
        return {"code": "research_board_error", "message": "workspace_id is required for inspect."}
    if target == "source":
        return research_evidence(operation="source", workspace_id=workspace_id, target_id=target_id)
    if target == "anchor":
        return research_evidence(operation="anchor", workspace_id=workspace_id, target_id=target_id)
    if target == "quarantine":
        return research_evidence(operation="quarantine", workspace_id=workspace_id)
    if target == "report":
        reports = get_store().reports(workspace_id)
        if target_id:
            reports = [report for report in reports if report.get("report_id") == target_id]
        return {"code": "research_reports", "reports": reports, "count": len(reports)}
    return {"code": "research_board_error", "message": f"inspect is not supported for target {target}."}


def _prepare(
    workspace_id: str,
    target: str,
    target_id: str,
    payload: dict[str, Any],
    *,
    actor_id: str,
    idempotency_key: str,
    dots_ocr: DotsOCRConfiguration,
) -> dict[str, Any]:
    if target != "source":
        return {"code": "research_prepare_error", "message": "prepare currently targets source records."}
    store = get_store()
    sources = store.sources(workspace_id, include_inactive=False)
    selected = [source for source in sources if not target_id or source.get("source_id") == target_id]
    if not selected:
        return {"code": "research_prepare_error", "message": "No active sources matched the prepare request."}
    results = []
    for source in selected:
        source_id = str(source.get("source_id"))
        run_id = new_id("prep")
        run_event = store.append_event(
            action="insert",
            target="preparation_run",
            workspace_id=workspace_id,
            target_id=run_id,
            actor_id=actor_id,
            idempotency_key=f"{idempotency_key}:{source_id}" if idempotency_key else "",
            payload={
                "source_id": source_id,
                "status": "started",
                "backend": "dots_mocr",
                "backend_mode": dots_ocr.mode,
                "endpoint_configured": bool(dots_ocr.endpoint),
                "requested": payload,
            },
        )
        if run_event.get("idempotent_replay"):
            results.append({
                "source_id": source_id,
                "preparation_run": run_event,
                "result": {"code": "research_prepare_idempotent_replay", "message": "Preparation was already requested with this idempotency key."},
            })
            continue
        run_id = run_event["target_id"]
        try:
            result = _prepare_source(workspace_id, source, run_id, actor_id=actor_id, dots_ocr=dots_ocr)
        except Exception as exception:  # noqa: BLE001
            result = _quarantine(
                workspace_id,
                source_id,
                "preparation_exception",
                str(exception),
                actor_id=actor_id,
                preparation_run_id=run_id,
            )
        results.append({"source_id": source_id, "preparation_run": run_event, "result": result})
    return {"code": "research_prepare_completed", "workspace_id": workspace_id, "results": results}


def _prepare_source(
    workspace_id: str,
    source: dict[str, Any],
    run_id: str,
    *,
    actor_id: str,
    dots_ocr: DotsOCRConfiguration,
) -> dict[str, Any]:
    store = get_store()
    source_id = str(source.get("source_id"))
    source_kind = str(source.get("source_kind") or "document")
    if source_kind == "structured_record":
        return _prepare_structured_record(workspace_id, source, run_id, actor_id=actor_id)

    path = Path(str(source.get("path") or "")).expanduser()
    if not path or not path.is_file():
        return _quarantine(
            workspace_id,
            source_id,
            "missing_local_artifact",
            "The source has no readable local file artifact. Full text/PDF acquisition is required before evidence eligibility.",
            actor_id=actor_id,
            preparation_run_id=run_id,
        )
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown"}:
        return _prepare_text_document(workspace_id, source, path, run_id, actor_id=actor_id)
    if suffix in DOCUMENT_SUFFIXES:
        output_directory = research_database_path().parent / "research-artifacts" / workspace_id / source_id / run_id
        try:
            pages = parse_with_dots(path, output_directory, dots_ocr)
        except DotsBackendUnavailable as exception:
            return _quarantine(
                workspace_id,
                source_id,
                "dots_mocr_unavailable",
                str(exception),
                actor_id=actor_id,
                preparation_run_id=run_id,
            )
        except DotsParseError as exception:
            return _quarantine(
                workspace_id,
                source_id,
                "dots_mocr_parse_failed",
                str(exception),
                actor_id=actor_id,
                preparation_run_id=run_id,
            )
        return _index_dots_pages(workspace_id, source, path, pages, run_id, actor_id=actor_id)
    return _quarantine(
        workspace_id,
        source_id,
        "unsupported_file_type",
        f"Unsupported source artifact type: {suffix or '<none>'}",
        actor_id=actor_id,
        preparation_run_id=run_id,
    )


def _prepare_text_document(
    workspace_id: str,
    source: dict[str, Any],
    path: Path,
    run_id: str,
    *,
    actor_id: str,
) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    chunks = [chunk.strip() for chunk in text.split("\n\n") if chunk.strip()]
    if not chunks and text.strip():
        chunks = [text.strip()]
    created = []
    for index, chunk in enumerate(chunks[:200]):
        anchor_id = new_id("anchor")
        get_store().append_event(
            action="insert",
            target="anchor",
            workspace_id=workspace_id,
            target_id=anchor_id,
            actor_id=actor_id,
            parent_event_ids=[run_id],
            payload={
                "source_id": source["source_id"],
                "source_title": source.get("title", ""),
                "file_path": str(path),
                "page_index": None,
                "category": "Text",
                "evidence_modality": "text",
                "layout_cell_id": f"text-{index}",
                "char_range": None,
                "text": chunk,
            },
        )
        evidence_id = new_id("evidence")
        get_store().append_event(
            action="insert",
            target="evidence",
            workspace_id=workspace_id,
            target_id=evidence_id,
            actor_id=actor_id,
            parent_event_ids=[anchor_id],
            payload={
                "source_id": source["source_id"],
                "source_title": source.get("title", ""),
                "anchor_ids": [anchor_id],
                "evidence_modality": "text",
                "claim": compact_text(chunk, maximum=500),
                "text": chunk,
                "support_status": "unclear",
                "verification_status": "machine_extracted",
            },
        )
        created.append({"anchor_id": anchor_id, "evidence_card_id": evidence_id})
    return {"code": "research_source_prepared", "source_id": source["source_id"], "created": created, "count": len(created)}


def _index_dots_pages(
    workspace_id: str,
    source: dict[str, Any],
    path: Path,
    pages: list[dict[str, Any]],
    run_id: str,
    *,
    actor_id: str,
) -> dict[str, Any]:
    created = []
    for page in pages:
        for cell in page.get("cells", []):
            anchor_id = new_id("anchor")
            category = str(cell.get("category") or "Text")
            modality = modality_for_category(category)
            text = str(cell.get("text") or "")
            get_store().append_event(
                action="insert",
                target="anchor",
                workspace_id=workspace_id,
                target_id=anchor_id,
                actor_id=actor_id,
                parent_event_ids=[run_id],
                payload={
                    "source_id": source["source_id"],
                    "source_title": source.get("title", ""),
                    "file_path": str(path),
                    "page_index": page.get("page_index"),
                    "page_width": page.get("input_width"),
                    "page_height": page.get("input_height"),
                    "layout_cell_id": cell.get("layout_cell_id"),
                    "category": category,
                    "evidence_modality": modality,
                    "bbox": cell.get("bbox"),
                    "text": text,
                    "raw": cell.get("raw"),
                    "layout_info_path": page.get("layout_info_path", ""),
                    "layout_image_path": page.get("layout_image_path", ""),
                },
            )
            if modality in {"text", "table", "formula"} and text.strip():
                evidence_id = new_id("evidence")
                get_store().append_event(
                    action="insert",
                    target="evidence",
                    workspace_id=workspace_id,
                    target_id=evidence_id,
                    actor_id=actor_id,
                    parent_event_ids=[anchor_id],
                    payload={
                        "source_id": source["source_id"],
                        "source_title": source.get("title", ""),
                        "anchor_ids": [anchor_id],
                        "evidence_modality": modality,
                        "claim": compact_text(text, maximum=500),
                        "text": text,
                        "support_status": "unclear",
                        "verification_status": "machine_extracted",
                        "dots_category": category,
                    },
                )
                created.append({"anchor_id": anchor_id, "evidence_card_id": evidence_id})
            else:
                created.append({"anchor_id": anchor_id})
    return {"code": "research_source_prepared", "source_id": source["source_id"], "created": created, "count": len(created)}


def _prepare_structured_record(
    workspace_id: str,
    source: dict[str, Any],
    run_id: str,
    *,
    actor_id: str,
) -> dict[str, Any]:
    payload = source.get("record") or source.get("data") or source.get("metadata") or {}
    if not payload:
        return _quarantine(
            workspace_id,
            source["source_id"],
            "missing_structured_payload",
            "Structured record source has no record/data payload to index.",
            actor_id=actor_id,
            preparation_run_id=run_id,
        )
    anchor_id = new_id("anchor")
    get_store().append_event(
        action="insert",
        target="anchor",
        workspace_id=workspace_id,
        target_id=anchor_id,
        actor_id=actor_id,
        parent_event_ids=[run_id],
        payload={
            "source_id": source["source_id"],
            "source_title": source.get("title", ""),
            "category": "StructuredRecord",
            "evidence_modality": "structured_data",
            "record": payload,
        },
    )
    evidence_id = new_id("evidence")
    get_store().append_event(
        action="insert",
        target="evidence",
        workspace_id=workspace_id,
        target_id=evidence_id,
        actor_id=actor_id,
        parent_event_ids=[anchor_id],
        payload={
            "source_id": source["source_id"],
            "source_title": source.get("title", ""),
            "anchor_ids": [anchor_id],
            "evidence_modality": "structured_data",
            "claim": compact_text(json.dumps(payload, ensure_ascii=False), maximum=500),
            "data": payload,
            "support_status": "unclear",
            "verification_status": "machine_extracted",
        },
    )
    return {"code": "research_source_prepared", "source_id": source["source_id"], "created": [{"anchor_id": anchor_id, "evidence_card_id": evidence_id}], "count": 1}


def _quarantine(
    workspace_id: str,
    source_id: str,
    reason_code: str,
    message: str,
    *,
    actor_id: str,
    preparation_run_id: str,
) -> dict[str, Any]:
    event = get_store().append_event(
        action="insert",
        target="quarantine",
        workspace_id=workspace_id,
        target_id=new_id("quarantine"),
        actor_id=actor_id,
        parent_event_ids=[preparation_run_id],
        payload={
            "source_id": source_id,
            "preparation_run_id": preparation_run_id,
            "reason_code": reason_code,
            "message": message,
        },
    )
    return {"code": "research_source_quarantined", "event": event}


def _search_evidence(workspace_id: str, *, query: str, filters: dict[str, Any], limit: int) -> dict[str, Any]:
    cards = get_store().evidence(workspace_id)
    query_terms = {term.lower() for term in query.split() if term.strip()}
    def score(card: dict[str, Any]) -> int:
        haystack = " ".join(str(card.get(key, "")) for key in ("claim", "summary", "text", "source_title")).lower()
        base = sum(1 for term in query_terms if term in haystack)
        if filters.get("evidence_modality") and card.get("evidence_modality") != filters.get("evidence_modality"):
            return -1
        if filters.get("source_id") and card.get("source_id") != filters.get("source_id"):
            return -1
        return base
    ranked = [(score(card), card) for card in cards]
    ranked = [(card_score, card) for card_score, card in ranked if card_score >= 0]
    ranked.sort(key=lambda item: (item[0], item[1].get("created_at", "")), reverse=True)
    selected = []
    for card_score, card in ranked[: max(1, min(int(limit or 12), 50))]:
        selected.append({**card, "relevance_score": card_score})
    return {"code": "research_evidence_search", "query": query, "results": selected, "count": len(selected)}


def _validate_report(workspace_id: str, *, target_id: str = "", report: dict[str, Any] | None = None) -> dict[str, Any]:
    store = get_store()
    if report is None:
        reports = store.reports(workspace_id)
        report = next((item for item in reports if item.get("report_id") == target_id), None)
    if not report:
        return {"code": "research_report_validation_error", "message": "No report provided or found."}
    anchors = {anchor.get("anchor_id"): anchor for anchor in store.anchors(workspace_id)}
    quarantined_sources = {record.get("source_id") for record in store.quarantine(workspace_id)}
    citations = report.get("citations") or report.get("claim_citations") or []
    issues = []
    if not citations:
        issues.append({"severity": "error", "message": "Report has no claim-to-anchor citation mapping."})
    for index, citation in enumerate(citations):
        anchor_ids = citation.get("anchor_ids") if isinstance(citation, dict) else []
        if not anchor_ids:
            issues.append({"severity": "error", "citation_index": index, "message": "Citation mapping has no anchor_ids."})
            continue
        for anchor_id in anchor_ids:
            anchor = anchors.get(anchor_id)
            if anchor is None:
                issues.append({"severity": "error", "citation_index": index, "anchor_id": anchor_id, "message": "Anchor does not exist."})
            elif anchor.get("source_id") in quarantined_sources:
                issues.append({"severity": "error", "citation_index": index, "anchor_id": anchor_id, "message": "Anchor belongs to a quarantined source."})
    return {
        "code": "research_report_validation",
        "valid": not any(issue["severity"] == "error" for issue in issues),
        "issues": issues,
        "checked_citations": len(citations),
    }
