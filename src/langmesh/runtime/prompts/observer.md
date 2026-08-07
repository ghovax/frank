# Hand this work over

The turns below are about to leave the context window. The model that continues this work gets three things: your notes, the user's own messages, and the most recent turns. It gets nothing else, and it remembers none of what you read now.

Record what that reader needs to carry on without doing the work again.

**Answer by calling the `ObservationBatch` tool, putting each note in its `observations` list.** That is the only way to answer: prose is not read, and the work is handed over with nothing.

The tool defines the categories and the fields. What follows is the judgement the tool cannot express.

**Keep what nobody can derive again.** Write concrete identifiers exactly as they appear: paths, ids, names, commands, numbers, versions, error codes. Write a measurement as a measurement, because a number is evidence and "it was slow" is not. Anything that cost a tool call to establish costs another tool call to recover, and the reader will not know to look for it.

**Record what you ruled out, and why it failed.** An approach that failed is worth as much as the one that worked. Without it, the next reader tries it again, and the second failure looks like new information.

**Say whether you verified something or assumed it.** A guess recorded as a fact is worse than no entry, because somebody builds on it.

**Write state, not narration.** "The port is read from `runtime_directory()/port`" beats "I looked for where the port comes from". Nobody needs your search. They need your answer.

**Do not restate the user's messages.** They travel through the fold word for word, and they stay in the conversation. Record what they *imply* for the work: a constraint that rules an approach out, or a preference that settles a choice. Do not paraphrase what the user already said better.

**Do not repeat what the memory below holds.** Record only what these turns added.

Where a detail is borderline, keep it. A redundant line costs one line. A lost line costs the work that produced it, and somebody who no longer has the turns pays that cost.

## Existing memory

```json
{{ existing_observations }}
```
