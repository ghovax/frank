# Consolidate the record

The record below has grown large enough that reading it costs real context. Make it smaller — but the record is append-only, so you make it smaller by **superseding**, never by rewriting and never by deleting.

**Answer by calling the `ObservationBatch` tool, putting the consolidating entries in its `observations` list.** That is the only way to answer: prose is not read, and the record stays as it was.

## How consolidation works here

Each entry you write names, in `supersedes`, every id it replaces. Those entries leave the live view; they stay in the store, so nothing is lost and the chain shows how the record got here.

- **Collapse a value that changed into its final state.** Three entries tracking a value that moved twice become one entry stating what it is now, naming all three. Keep the history only where how it got there is itself the finding.
- **Merge entries that say one thing between them**, naming each. Keep the merged entry as specific as its most specific part: merging is not generalising, and two exact paths do not become "several files".
- **Supersede what no longer bears on the work.** A detail that mattered to a settled question, and cannot come up again, is finished — but supersede it explicitly rather than passing over it in silence.

An entry you leave alone stays live. You do not need to restate it, and restating it without superseding it puts the same finding in the record twice.

## What consolidation may never cost

Keep every concrete identifier — a path, an id, a name, a command, a number, a version. Keep every approach that was ruled out, and why it failed. Keep every measurement. Keep every open thread. These cost real work to establish and no amount of thinking recovers them, so an entry carrying one is merged rather than dropped.

Where an entry is borderline, leave it live. Carrying it costs one entry. Losing it costs work that somebody discovers much later, when the turns that would explain it are long gone.

## The record

Each entry is shown by its id, its claim and its detail.

```jsonl
{{ observations }}
```
