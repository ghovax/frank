Retrieve evidence from a research workspace.

This tool is read-oriented. It does not write reports and does not decide what the report should say. It returns compact evidence cards, source details, anchors, quarantine records, or mechanical validation results so the model can reason and write.

Arguments:
- `operation`: `search`, `source`, `anchor`, `quarantine`, or `validate_report`.
- `workspace_id`: research workspace id.
- `query`: search query for `operation="search"`.
- `target_id`: source id, anchor id, or report id depending on operation.
- `filters`: optional filters such as `source_id` or `evidence_modality`.
- `limit`: maximum results to return.
- `report`: inline report payload for `validate_report` when the report has not been saved.

Use `search` while drafting, `anchor` when you need exact citation context, `quarantine` before reporting limitations, and `validate_report` after drafting to check citation mappings.
