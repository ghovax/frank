# Hand this work over

The exchange below has just finished. Record what it established, now, while its turns are whole — not because the context is full, but because this is the moment the work is freshest and nothing is rushed. What you write is kept; the turns themselves will not be.

**Answer by calling the `ObservationBatch` tool, putting each finding in its `observations` list.** That is the only way to answer: prose is not read, and the work is handed over with nothing.

## The record is append-only

Entries already in it are immutable. You never edit one and you never delete one. Where a prior entry is now wrong, incomplete, or overtaken by what these turns established, write a **new** entry and name the old one's id in `supersedes`. The next reader sees only what nothing supersedes, but the whole chain is kept, so a correction has to be stated as one rather than made quietly.

Do not repeat an entry that already holds. Add what these turns added, and revise what these turns changed.

## How much to write

**Dozens of entries is normal.** A long stretch of work establishes many things, and one entry per subject loses most of them. Write one entry per finding.

**Every `detail` is two to three full sentences, never one clause.** Say what the thing is, how it came to be known, and what it means for whoever continues. An entry whose `detail` is as short as its `claim` has failed this task.

**`claim` is judged alone.** A later pass sees only the claim line when deciding whether an entry still holds, so it must stand without its detail.

**`standing` is a fact about your knowledge, not decoration.** `verified` means you saw the proof. `reported` means something claimed it and you did not check. `inferred` means you concluded it. Something that ran without an error is `reported`, and a number you did not watch being measured is `reported`.

**`evidence` names the proof**: the path, the command, the output, the message. Leave it empty only where genuinely nothing establishes the entry.

## What to keep

- Concrete identifiers exactly as they appear: paths, ids, names, commands, numbers, versions, error codes. A measurement stays a measurement, because a number is evidence and "it was slow" is not.
- What was ruled out and how it failed. A failure costs as much to establish as a success, and without it the next reader tries it again and reads the second failure as new information.
- The reasoning that led to a decision, not only the decision itself.
- What is still open, and the next concrete step it implies.

Write state, not narration: "The port is read from `runtime_directory()/port`" beats "I looked for where the port comes from".

## What is not a finding

The record is append-only, so an entry written needlessly is carried for the rest of the conversation. Before writing one, ask whether somebody resuming this work would be worse off without it. These never pass that test:

- **Anything about yourself.** That you answered, complied, followed an instruction, chose a tool, or read a reminder. Your own conduct is not a finding about the work.
- **Furniture you happened to see.** A file you did not act on, a directory listing, a size or a timestamp that nothing turned on. Noticing something is not establishing it.
- **The obvious restated.** That a file exists because you just read it, or that a command ran because you just ran it. The finding is what it *said*, not that it happened.
- **What the person asked for.** Their instructions are kept in their own record, and duplicating them here lets the two drift apart.

An exchange that established nothing durable deserves no entries at all, and returning none is the right answer. Where a real detail is borderline, keep it: a redundant finding costs one entry, and a lost one costs the work that produced it.

## The record so far

Each entry is shown by its id, its claim, and `learned` — when it was recorded. The time is given to you; never write one yourself. Name an id in `supersedes` to revise it.

```jsonl
{{ existing_observations }}
```
