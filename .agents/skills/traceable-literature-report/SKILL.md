---
name: traceable-literature-report
title: Write traceable literature reports from the research blackboard
enabled: true
description: >-
  Use when producing literature reports whose claims must be grounded in durable
  evidence cards, citation anchors, parsed PDFs/images, database records, and a
  quarantine appendix. The skill coordinates the append-only research blackboard
  tools: research_board, research_evidence, and research_open.
---

# Traceable Literature Report

This skill governs the workflow. The tools provide state, retrieval, validation,
and rendering; the model writes and revises the report.

## Core Model

There are two contexts:

- **Chat context**: the normal conversation and reasoning trace.
- **Research blackboard**: a persistent append-only workspace holding objectives,
  sources, preparation runs, anchors, evidence cards, quarantine records, reports,
  and publication/supersession events.

Never treat the blackboard as CRUD state. Do not delete or reset. Append events:
`insert`, `annotate`, `exclude`, `supersede`, `prepare`, and `publish`.

## Tool Surface

Use only three research tools:

- `research_board`: append blackboard events or inspect derived state.
- `research_evidence`: retrieve evidence cards, anchors, source details,
  quarantine records, and report validation.
- `research_open`: render an anchor, source, or report for visual inspection.

The mutating tool is `research_board`. `research_evidence` is read-oriented.
`research_open` only renders UI artifacts.

## Required Workflow

1. **Open a workspace**
   - Call `research_board` with `action="insert", target="workspace"` unless the
     user named an existing `workspace_id`.
   - Payload should include `objective`, optional `scope`, and optional `notes`.

2. **Insert sources**
   - Call `research_board` with `action="insert", target="source"` for each PDF,
     Zotero item, DOI, literature record, web page, upload, database query, or
     manual source.
   - Source payloads must use:
     - `origin_channel`: `zotero`, `literature`, `web_page`, `database`, `upload`,
       or `manual`.
     - `source_kind`: `document`, `structured_record`, or `annotation`.
   - Include source-specific metadata in `metadata`, not in the type name.

3. **Prepare sources**
   - Call `research_board` with `action="prepare", target="source"`.
   - Preparation is strict. A source that lacks full text/artifact or cannot be
     parsed is quarantined and must not support report claims.
   - PDFs/images are prepared through the configured Dots/MOCR parser endpoint.
     If Dots/MOCR is disabled, unreachable, or returns unusable output, the
     source is quarantined.

4. **Inspect quarantine**
   - Call `research_evidence` with `operation="quarantine"` before drafting the
     report.
   - Reports must include a quarantine appendix when any relevant source was
     excluded or could not be evaluated.

5. **Retrieve evidence**
   - Call `research_evidence` with `operation="search"` repeatedly while drafting.
   - Use `operation="source"` for source-level inspection and `operation="anchor"`
     for exact citation context.
   - Evidence cards are inputs, not final prose. Read them and reason over them.

6. **Write the report**
   - The model writes the markdown report itself.
   - Every material claim needs one or more anchor ids in the citation mapping.
   - Do not cite quarantined sources as support.
   - If a claim is speculative or unclear, say so explicitly.

7. **Save and validate**
   - Save by appending a report event with `research_board`:
     `action="insert", target="report"`.
   - Payload should include `title`, `markdown`, and `citations`.
   - Validate using `research_evidence(operation="validate_report")`.
   - Revise unsupported or unmapped claims, then append a superseding report
     rather than editing the old one.

8. **Publish**
   - When the report is ready, call `research_board` with
     `action="publish", target="report", target_id=<report_id>`.

9. **Open for inspection**
   - Use `research_open(target="anchor")` when the user wants to inspect a cited
     PDF/page/layout cell/figure/table/formula.
   - Use `research_open(target="report")` to show the saved report.

## Dots/MOCR Categories

Preserve the original Dots/MOCR category in anchors:

`Caption`, `Footnote`, `Formula`, `List-item`, `Page-footer`, `Page-header`,
`Picture`, `Section-header`, `Table`, `Text`, `Title`.

Map them only for coarse retrieval:

- text: `Caption`, `Footnote`, `List-item`, `Page-footer`, `Page-header`,
  `Section-header`, `Text`, `Title`
- image: `Picture`
- table: `Table`
- formula: `Formula`

## Report Discipline

- No uncited material claims.
- No evidence from quarantined sources.
- No claim should be stronger than its evidence.
- Include contradictions and uncertainty rather than smoothing them away.
- Keep database records citable through the same anchor system as papers.
- Use `exclude` for off-topic or invalid sources, not deletion.
- Use `supersede` for revised reports, anchors, or interpretations.
