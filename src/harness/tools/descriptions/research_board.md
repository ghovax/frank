Append to or inspect the durable research blackboard.

This is the only research tool that mutates durable state. Mutations are append-only events: never delete or reset. Use `exclude` to mark something inactive, `supersede` to replace an interpretation/report with a newer one, and `annotate` to add notes or decisions.

Arguments:
- `action`: `insert`, `annotate`, `exclude`, `supersede`, `prepare`, `publish`, or `inspect`.
- `target`: `workspace`, `source`, `preparation_run`, `evidence`, `anchor`, `report`, `note`, or `quarantine`.
- `workspace_id`: required except for `action="insert", target="workspace"`.
- `target_id`: existing target id, or caller-chosen id for insertions.
- `payload`: structured data for the event.
- `parent_event_ids`: event ids this event builds on.
- `expected_revision`: optional optimistic concurrency guard.
- `idempotency_key`: optional retry key; repeated calls with the same key return the original event.

Typical sequence:
1. `insert/workspace` with an objective.
2. `insert/source` for PDFs, Zotero items, DOIs, web pages, uploads, or database descriptors.
3. `prepare/source` to run local acquisition/parsing/indexing. PDFs/images use local Dots/MOCR when installed; otherwise they are quarantined.
4. `inspect/workspace` to see current derived state.

Source payloads should use:
- `origin_channel`: `zotero`, `literature`, `web_page`, `database`, `upload`, or `manual`.
- `source_kind`: `document`, `structured_record`, or `annotation`.
- `title`, plus one or more of `path`, `uri`, `doi`, `record`, `metadata`.
