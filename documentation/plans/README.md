# Plans

Design plans for the Frank harness, built sequentially — each plan is a self-contained Markdown document describing one body of work and is associated with the commit it landed in. There is no manifest and no folder nesting: the plans are a flat, append-only sequence, read in creation order.

The plans themselves are left as they were written, which is why several of them say `xeac`: the harness carried that name for a stretch, and the plans from that stretch are records of the work as it was done. A record edited to match the present is not a record. Only this index, which describes the convention rather than any particular piece of work, tracks the current name.

Each plan carries a small YAML frontmatter block as its metadata (the title is the document's H1, not repeated here):

```yaml
---
created: 2026-07-19T16:49:03Z   # when the plan entered the sequence (RFC 3339, UTC)
updated: 2026-07-19T17:52:23Z   # when it was last substantively edited
commit: 88a99ab                 # the commit the plan is associated with
---
```

`created` defines the sequence: plans are ordered oldest-first by it. `updated` tracks later edits (equal to `created` if never edited). `commit` ties the plan to the point in history it landed. When adding a plan, create the file with its frontmatter; when revising one, bump `updated`.
