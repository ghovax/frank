Open a research artifact in the UI.

Use this only when the user needs to inspect a citation, source, or report visually. Normal evidence retrieval should use `research_evidence`.

Arguments:
- `target`: `anchor`, `source`, or `report`.
- `workspace_id`: research workspace id.
- `target_id`: anchor/source/report id.

For anchors, the result is a specialized research artifact carrying the source file, page, bounding box, category, and extracted text. The frontend renders this as a citation preview. For reports, the result renders the saved markdown report.
