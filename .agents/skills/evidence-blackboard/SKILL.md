---
name: evidence-blackboard
title: Build a durable knowledge graph of works, claims, and citations you can query with SQL
enabled: true
description: >-
  Use when research must compound over time — accumulating evidence across many
  documents into a durable, queryable knowledge graph rather than reasoning from
  memory each session. A blackboard under ~/.blackboard turns PDFs (and DOIs, URLs,
  records) into canonical works, verbatim anchors each tagged with an information
  aspect (method/result/limitation/…), a cross-document citation graph, first-class
  claims backed by evidence, and a topic classification — all read back with
  read-only SQL, so you can pull every method or limitation across the corpus in one
  query. Self-contained in the importable `blackboard` Python package;
  composes with a discovery engine like `scholar` but reimplements none of it. The
  write-up discipline that turns a board into a report is `traceable-literature-report`.
---

# Evidence Blackboard

A **blackboard** is a durable, append-only knowledge base you scratch findings onto and query freely, backed by SQLite at **`~/.blackboard/board.db`** (created automatically; override the home with `BLACKBOARD_HOME`). It lives entirely **outside** any skill folder and any repo — the skill folder is reference/execution only, and `~/.blackboard` is gitignored by living in the home directory. Several agents can share one board because every write is an append-only event and every id is random and unique. You only ever append — `insert`, or via a targeted action `annotate`/`exclude`/`supersede`; never mutate or delete.

A board is meant to be **long-lived**: reuse one board for an ongoing research area (reopen it by id) so knowledge compounds across sessions, rather than starting a fresh board per question.

## The model — three durable structures

Knowing which structure holds what is the whole point:

- **works** — canonical scholarly identity. A paper is **one** node whether you read it (an ingested source) or merely see it cited (a resolved reference), deduped by DOI/arXiv/title. This is what turns a pile of parsed PDFs into a citation graph that spans papers. A work has a `status`: `ingested` (read), `cited` (referenced by something you read), or `frontier` (worth chasing, not yet read).
- **claims** + **claim_support** — first-class facts. A claim is an assertion; `claim_support` edges link it to the exact **anchors** (verbatim passages) that support, contradict, or qualify it, across works. Reading compounds into knowledge here, not in chat history.
- **topics** + **work_topics** — a durable classification dimension over the corpus.

Under these sit the raw layers: **sources** (artifacts you added), **anchors** (verbatim layout blocks), **bibliography** (parsed reference entries), **citations** (anchor→work edges), **figures** (vision-described images), **quarantine** (unreadable sources).

**Every anchor also carries an `aspect`** — a normalized information type assigned automatically at ingest. This is the index that makes the board more than keyword search: you can pull *every* method, *every* limitation, *every* reported metric across the whole corpus in one query, then narrow by keyword. The controlled vocabulary is: `background`, `definition`, `hypothesis`, `method`, `dataset`, `result`, `metric`, `limitation`, `comparison`, `conclusion`, `future_work`, `other`. A figure's aspect is inferred from its description; a passage's from its text and section.

## Engine and how to run it

The blackboard is the **`blackboard` Python package** (in `scripts/blackboard/`) — self-contained, no dependency on any other skill. Run it with **`uv run python`** from `scripts/blackboard/` (installed editable, so `import blackboard` just works):

```bash
cd scripts/blackboard
uv run python - <<'PY'
import json, blackboard
board = blackboard.open_board(objective="...")["board"]
print(json.dumps(blackboard.board_state(board), default=str))
PY
```

### Configuration (environment or a project `.env`)

- **`DATALAB_API_KEY`** — the hosted Datalab (Marker) parser. Without it, ingestion cannot parse and sources fall through to **quarantine**.
- **`BLACKBOARD_MODEL`** (plus optional `BLACKBOARD_MODEL_API_KEY` / `BLACKBOARD_MODEL_BASE_URL`, defaulting to `OPENCODE_API_KEY`) — the **text** model for enrichment (citation markers, aspects, bibliography). Without it, ingest still parses and indexes; it just skips the LLM enrichments.
- **`BLACKBOARD_VISION_MODEL`** (plus optional `…_API_KEY` / `…_BASE_URL`) — a **vision-capable** model for figure description, which sends the actual figure image. Falls back to `BLACKBOARD_MODEL` when unset, so set this whenever `BLACKBOARD_MODEL` is text-only, or figures go undescribed.
- **`BLACKBOARD_HOME`** — override the `~/.blackboard` data directory.

## The functions

- **`open_board(objective="", board="")`** — open/create a board (reopen by id). Returns the `board` id.
- **`add_source(board, title=, path=, uri=, doi=, year=, origin_channel=, source_kind=, record=)`** — register a source and bind it to a canonical **work**. `origin_channel` ∈ `zotero | literature | webpage | database | upload | manual`; `source_kind` ∈ `document | structured_record | annotation`. Returns `source_id` and `work_id`.
- **`ingest(board, source="")`** — parse the source's document(s) via Datalab (Marker) into verbatim **anchors**, then enrich: citation markers per passage, figure descriptions, an **information aspect** per block, and the bibliography split into numbered entries (stored **unresolved**). Writes anchors (aspect-tagged), `figures`, `bibliography`, and `citations`. **Slow** — run it as a **background bash command** so the harness wakes you with the result. Safe to re-run (parses cached by content hash). Unreadable or unparseable sources are **quarantined** and can never back a claim.
- **`add_claim(board, statement, topic=, status=)`** — assert a first-class claim. `status` ∈ `open | supported | contested | refuted`.
- **`cite_evidence(board, claim, anchor, stance=, note=)`** — link a claim to an anchor. `stance` ∈ `supports | contradicts | qualifies`. The anchor's work rides on the edge, so a claim accumulates evidence across papers.
- **`link_reference(board, reference, doi=, arxiv=, title=, year=)`** — attach a **resolved cited work** to a bibliography entry and point its citation edges at that work, closing the cross-document graph. Resolving the raw string to these identifiers is discovery — do it with `scholar`; this records the result.
- **`classify(board, work, topic=, origin=)`** — tag a work with a topic (created if new).
- **`note(board, body="", target="note", action="insert", target_id="", **fields)`** — a free observation, or a targeted `annotate`/`exclude`/`supersede` on any record.
- **`query(sql, parameters=None, maximum_rows=200)`** — **read-only SQL** (SELECT/WITH only, `?` placeholders) over the read model.
- **`board_state(board)`** — compact per-table counts and quarantine.

### Read model tables

`works`, `sources`, `anchors`, `bibliography`, `citations`, `figures`, `claims`, `claim_support`, `topics`, `work_topics`, `notes`, `quarantine`, and `anchors_fts` (FTS5 — join `anchors_fts.anchor_id` to `anchors.anchor_id` and `MATCH`). An anchor row carries `page_index`, `category`, `evidence_modality`, `aspect`, `text`, `html`, `bbox_json`, `section_hierarchy_json`, `work_id`.

Example — *every limitation passage about g-C₃N₄ across the corpus* (the headline use of the aspect index):

```python
blackboard.query(
    "SELECT anchor.text, anchor.work_id, anchor.page_index "
    "FROM anchors AS anchor "
    "JOIN anchors_fts AS fts ON anchor.anchor_id = fts.anchor_id "
    "WHERE anchor.board_id = ? AND anchor.aspect = 'limitation' AND fts.text MATCH ?",
    [board, "g-C3N4 OR carbon nitride"],
)
```

Example — *where is reference 21 discussed, and in which work?*

```python
blackboard.query(
    "SELECT anchor.page_index, anchor.text, citation.cited_work_id "
    "FROM citations AS citation "
    "JOIN anchors AS anchor ON citation.source_anchor_id = anchor.anchor_id "
    "WHERE citation.board_id = ? AND citation.marker_number = ?",
    [board, 21],
)
```

## The loop — how the graph gets built (compose with `scholar`)

The graph does not build itself; this is the repeatable loop, and the scholar seam is explicit. Do it every time so the graph actually connects:

1. **Find and fetch** with the `literature-search` skill's `scholar` engine (`search`, `fulltext`, `zotero_items`), preferring the user's Zotero library first.
2. **`add_source`** each fetched PDF/DOI/record (this creates/dedups its work).
3. **`ingest`** in the background — each anchor is tagged with its aspect as it lands. Check `quarantine` before trusting anything.
4. **Resolve the bibliography — do this yourself; there is no tool for it, and you don't need one.** Query the unresolved entries: `SELECT reference_id, raw_string FROM bibliography WHERE board_id = ? AND resolution_status = 'unresolved'`. For each, get the cited work's identifiers with `scholar`: if the `raw_string` already contains a DOI, take it directly; otherwise `scholar.search(raw_string)` and read the top result's `ids.doi`, `title`, and `year`. Then `link_reference(board, reference_id, doi=, title=, year=)` to attach the cited work and close its citation edges. It's one discovery query per reference, so batch it and run it as a **background bash command**. Cited works you have not read become `cited`/`frontier` nodes — your reading frontier.
5. **Assert claims**: as you read anchors, `add_claim` the facts and `cite_evidence` each to its supporting/contradicting anchors across works. Set the claim `status` as evidence accumulates.
6. **`classify`** works by topic (seed labels from OpenAlex topics via `scholar`, extend by hand).
7. **Query** the graph for synthesis: slice by aspect across papers (`WHERE aspect = 'result'` / `'limitation'` / `'method'`, joined to `anchors_fts` for a keyword), consensus and contradiction (`claim_support` grouped by stance), the most-cited unread work (`citations` → `works WHERE status != 'ingested'`), a theme's structure (`work_topics` + `citations`).

## Discipline

- **Append-only.** Correct or retract with `annotate` / `exclude` / `supersede`, never by deleting. Every id is random and unique.
- **Verbatim content.** Datalab output is stored as received — never truncated or paraphrased (the enrichment prompts also send full text, no slicing). Quote an anchor's `text`/`html` when exact wording matters.
- **Claims are deliberate.** Anchors are the evidence substrate; a claim is a considered assertion you make and back — never auto-generate one per passage.
- **Quarantine is a hard filter.** A quarantined source can never support a claim. Tell the user what could not be evaluated.
- **Evidence rows are inputs, not prose.** Query them, reason over them, then write.
- **Keep the boundary.** The blackboard stores and queries; it never does discovery. Resolving references and finding papers is `scholar`'s job — compose, do not reimplement.

See `references/blackboard.mmd` for how ingestion and querying flow around the store.
