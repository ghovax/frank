---
name: traceable-literature-report
title: Write a traceable literature report from an evidence blackboard
enabled: true
description: >-
  Use when producing a literature report whose every material claim must be
  traceable to exact evidence — a verbatim anchor, page, figure, or a resolved
  citation — rather than asserted from memory. This is the write-up discipline that
  turns an existing `evidence-blackboard` board into a defensible report: retrieve
  evidence with SQL, write prose where each claim resolves to an anchor, and record
  conclusions back onto the board. Load `evidence-blackboard` to build and query the
  board; load `literature-search` to fetch any missing sources.
---

# Traceable Literature Report

This skill is the **write-up phase**, not the graph-building phase. Building the knowledge graph — adding sources, ingesting, resolving references, asserting claims, classifying — is the **`evidence-blackboard`** skill's loop; do that first (or alongside). Here you turn an already-populated board into a written report where **every material claim resolves to a queryable anchor or claim**, not to memory. The report is your normal chat deliverable; it is traceable because each assertion is backed by a row you can produce with SQL.

## Two planes

- **Chat**: your reasoning and the final written report.
- **Blackboard** (`~/.blackboard`, append-only, driven from `uv run python`): `works`, `anchors`, `claims`/`claim_support`, `citations`, `figures`, `bibliography`, `topics`, `quarantine`, and your `notes`. You only ever append. See `evidence-blackboard` for the functions and the full table list.

## Workflow

1. **Confirm the board is built.** Reopen it with `open_board(board=<id>)`. If sources for the question are missing, go build/extend the board with the `evidence-blackboard` loop (fetch via `literature-search`, `add_source`, `ingest`, `link_reference`, `add_claim`) before drafting.

2. **Check quarantine first.** `query("SELECT source_id, reason_code, message FROM quarantine WHERE board_id = ?", [board])`. If a relevant source was quarantined, the report needs a short appendix listing what could not be evaluated — never quietly drop it.

3. **Retrieve evidence with SQL — repeatedly, while drafting.**
   - Free-text: join `anchors_fts` to `anchors` and `MATCH`.
   - By information type: filter `anchors` by `aspect` (`result`, `limitation`, `method`, `metric`, …) to gather every passage of one kind across the corpus — then narrow with an FTS `MATCH`. Also filter by `category` / `evidence_modality` / `section_hierarchy_json`; read a figure's `description` from `figures`.
   - Facts: read `claims` and their `claim_support` edges (grouped by `stance`) to see what is supported, contested, or refuted, and across which works.
   - Graph: trace `citations` → `works` to ground "who cites whom" and surface the most-cited works you have not yet read.
   Evidence rows are inputs to reason over, not finished prose.

4. **Write the report.** You write the markdown. Every material claim carries the `anchor_id`(s) (or `claim_id`) that support it — cite them inline, e.g. `[anchor-…]` — and each must resolve to a real, non-quarantined row; verify with a query if unsure. State contradictions and speculation plainly (lean on `claim_support` stances); no claim should be stronger than its evidence.

5. **Record conclusions back onto the board.** As you settle a finding, `add_claim` it and `cite_evidence` it to its anchors, and set the claim `status` (`supported`/`contested`/`refuted`) — so the next session and other agents inherit the synthesis instead of re-deriving it. `note(...)` durable observations; `exclude`/`supersede` to retract, never delete.

6. **Let the user inspect a citation.** Query the anchor's `file_path` and `page_index`, then use the core `open_preview` tool on that PDF (e.g. `#page=N`) so the user sees the cited page.

## Discipline

- No uncited material claims; no evidence from quarantined sources.
- Content is stored verbatim — do not paraphrase an anchor's `text`/`html` when the exact wording matters for the claim.
- A claim's strength is its `claim_support`: report agreement and contradiction as the edges show them, do not smooth them away.
- Figures and structured records are citable through the same anchor system as passages.
