# Consolidate memory

The memory below grew large enough to deserve a rewrite. Write a smaller version. A model that resumes this work must not be able to tell it from the original.

**Answer by calling the `ObservationBatch` tool, putting the rewritten memory in its `observations` list.** That is the only way to answer: prose is not read, and the memory stays as it was.

- **Collapse superseded state into its final form.** Three entries that track a value which changed twice become one entry that states the value now. Keep the history only where how it got there is itself the finding.
- **Merge entries that say one thing between them.** Keep the merged entry as specific as its most specific part. To merge is not to generalise: two exact paths do not become "several files".
- **Drop what no longer bears on the work.** A detail that mattered to a settled question, and cannot come up again, is finished.
- **Never drop five things.** Keep every concrete identifier — a path, an id, a name, a command, a number, a version. Keep every constraint and preference the user set. Keep every approach you ruled out, and why. Keep every measurement. Keep every open thread. These entries cost real work, and no amount of thinking recovers them.
- **Keep the order where the order carries meaning.** A decision must still follow the fact that forced it.

Somebody rewrote this memory before, and somebody will rewrite it again. Each pass can lose something quietly. So where an entry is borderline, keep it. To carry it costs one line. To drop it costs work that somebody discovers much later, when they no longer have the turns that would explain it.

## Current memory

```json
{{ observations }}
```
