# What the person asked for

The exchange below has just finished. What the person asked for in it outlives the turns that carried it: an instruction holds after its message is gone, and a correction made once must not have to be made twice.

**Answer by calling the `DirectiveBatch` tool, putting each instruction in its `directives` list.** That is the only way to answer: prose is not read, and their instructions are lost.

Each field's own description says what belongs in it. This says what the task is.

## Their meaning, not their words

Do not paste a message back. "Don't use canvas, I want the stations selectable" becomes one requirement — that station selection works, which rules out canvas rendering — together with the reason, because the reason is what lets a later model apply it to a case they never mentioned.

Several messages often carry one instruction between them. Record the instruction once. Write in English whatever language the conversation was held in, because a record in two languages cannot be checked against itself for what it already holds.

This record is append-only: an instruction they changed is a new entry naming the old one, never an edit.

## What is not an instruction

This record holds what governs work **after** these turns. A request that these turns already satisfied governs nothing; it is finished, and writing it down leaves the next reader with a list of settled questions to read past.

- **A question that was answered** — "How many tests are there?", "Does the frontend build?", "Where does the port come from?" — because it is asked, answered and done, and nothing about it binds the next model.
- **The next step of the same request** — "Try the type checker too", "and check how it is started" — because a follow-up widens or narrows the enquiry already under way and is answered inside it, so it is neither a new standing rule nor a correction of the one recorded.
- **Anything you were told here** — these instructions, the tool you must call, the shape of an entry — because they came from this prompt rather than from them.

A one-line ask can still be an instruction where it sets a rule: "always write in Italian", "never use inline styles in that file", "check the BBC rather than other sources". The test is not how it was phrased but whether it still constrains work they have not described yet.

Before writing any entry, ask the one question: **if the work stopped here and somebody else picked it up tomorrow, what would they still be bound by?** Whatever answers that is an instruction. A question asked and answered in these turns answers it with nothing, however plainly it was phrased as a request.

Most exchanges carry no such instruction, and an empty list is then the whole correct answer. Recording a settled question is not a harmless extra: it is read later as work still owed, and the reader goes looking for what is missing.

Where you are unsure whether a **rule** was meant seriously or said in passing, record it: a preference recorded needlessly costs one entry, and a preference lost is discovered by doing the work the wrong way. That licence covers rules only, never a request these turns already answered.

## The record so far

Read it before you write. `learned` is when each entry was recorded; that time is given to you, and you never write one yourself.

```jsonl
{{ existing_directives }}
```
