# Hand this work over

The exchange below has just finished. Record what it established, now, while its turns are whole — not because the context is full, but because this is the moment the work is freshest and nothing is rushed. What you write is kept; the turns themselves will not be.

**Answer by calling the `ObservationBatch` tool, putting each finding in its `observations` list.** That is the only way to answer: prose is not read, and the work is handed over with nothing.

Each field's own description says what belongs in it. This says what the task is.

## What the record is

Append-only. Nothing is edited and nothing is deleted; an entry these turns proved wrong, incomplete or finished is replaced by a new one naming it, so a correction has to be stated as a correction rather than made quietly. The next reader sees only what nothing supersedes, and the chain is kept behind that.

So: add what these turns added, revise what these turns changed, leave alone what still holds. An entry repeating one that already holds adds nothing. An entry contradicting one without naming it leaves two live entries disagreeing, which is worse than either alone.

## What counts as a finding

Write one entry per finding. What decides the number is how many distinct things these turns established — not how long they were, not how many turns they took, and not a count you are aiming for. One entry per subject loses most of them; one entry per turn invents them.

**One finding is one entry.** A result and the command that produced it are one entry, not a `fact` and an `artifact`. A failure and the fact that it is unresolved are one entry, not a `failure` and an `open`. Split them and the record holds half-findings whose parts have to be reassembled by somebody who never saw the turns.

Keep:

- Concrete identifiers exactly as they appear: paths, ids, names, commands, numbers, versions, error codes.
- What was ruled out and how it failed, because a failure costs as much to establish as a success and without it the next reader tries it again.
- The reasoning that led to a decision, not only the decision itself.
- What is still open, and the next concrete step it implies.

Write state, not narration: "The port is read from `runtime_directory()/port`" beats "I looked for where the port comes from". Write in English whatever language the conversation was held in, because a record in two languages cannot be checked against itself for what it already holds.

## What is not a finding

An entry written needlessly is carried for the rest of the conversation. Before writing one, ask whether somebody resuming this work would be worse off without it. These never pass that test:

- **Anything about yourself** — that you answered, complied, followed an instruction, chose a tool or read a reminder — because your own conduct is not a finding about the work.
- **Furniture you happened to see** — a file you did not act on, a directory listing, a size or timestamp nothing turned on — because noticing something is not establishing it.
- **The obvious restated**, such as that a file exists because you just read it, since the finding is what it *said* rather than that it happened.
- **What the person asked for**, since a rule they stated — a language to write in, a library not to use, a file not to touch — belongs in their own record below, however much it reads like a constraint, while what belongs here is a limit the environment imposes rather than one they imposed.
- **The content of an answer** — what a search returned, what a page said, the figures in a report you summarised — because that is the work's output, which they have already read, rather than knowledge about the work.
- **The record itself** — that a request was fulfilled, that an earlier entry no longer applies, that something is resolved — so supersede the entry instead, because an entry about the ledger tells the next reader nothing about the work.

Record instead what the *doing* established: that a source is reachable, that a tool behaves a certain way, that a number had to be derived rather than looked up. A headline is not a finding, though the fact that it could be fetched at all may be.

An exchange that established nothing durable deserves no entries, and returning none is the right answer. Where a real detail is borderline, keep it: a redundant finding costs one entry, and a lost one costs the work that produced it.

## The record so far

Read it before you write. `learned` is when each entry was recorded; that time is given to you, and you never write one yourself.

```jsonl
{{ existing_observations }}
```

## What they have already asked for

Held in the other record and not yours to restate. Shown so you can tell an instruction of theirs from a finding of the work's.

```jsonl
{{ existing_directives }}
```
