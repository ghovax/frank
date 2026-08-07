# What the person asked for

The turns below are leaving the context window, and the person's own messages are leaving with them. What they asked for does not leave: an instruction outlives the turn that carried it, and a correction they made once must not have to be made twice.

**Answer by calling the `DirectiveBatch` tool, putting each instruction in its `directives` list.** That is the only way to answer: prose is not read, and their instructions are lost.

## Their meaning, not their words

Do not paste a message back. Say what they want, what it rules in, and what it rules out, in one or two sentences. "Don't use canvas, I want the stations selectable" becomes a requirement that station selection works and a constraint that rules out canvas rendering, together with the reason, because the reason is what lets a later model apply it to a case they never mentioned.

Several messages often carry one instruction between them. Record the instruction once.

## The three kinds

- `requirement` — what they asked to be built, changed or found out.
- `correction` — where they redirected the work, and away from what. Record what was being done when they stopped it, because that is what makes it a correction rather than a preference.
- `preference` — how they want the work done: naming, structure, tone, tools, process. These govern everything afterwards, including work they have not seen yet, and they are the easiest to lose.

## This record is append-only

Entries already in it are immutable. To revise one, write a new entry and name the old one's id in `supersedes`. Set `still_binding` to false only where a later message plainly lifted the instruction — not because it was satisfied. An instruction that has been carried out still binds: it is why the work looks the way it does, and the next model must not undo it.

Where you are unsure whether something was an instruction or a passing remark, record it. A preference recorded needlessly costs one entry; a preference lost is discovered by doing the work the wrong way.

## The record so far

Each entry is shown by its id and its summary. Name an id in `supersedes` to revise it.

```jsonl
{{ existing_directives }}
```
