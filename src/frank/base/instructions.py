"""A project's own conventions, as values rather than as a wire format.

This exists because `Catalogue.instructions()` used to return a JSON string, which made it the
only seam that asked a caller to hand-write a serialisation. Its siblings do not: `skills()`
answers with `Skill` values and `memories()` with `Memory` values, and the runtime encodes both
where it assembles the prompt. Instructions were encoding a layer too early, and the shape
leaked into the documented example, where a reader met a hand-written JSON array and asked —
reasonably — what it was.

`source` rather than `path`, because a file-backed catalogue puts a real path there and an
in-code one puts a label. Calling a label a path is how the confusion started.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass
class Instruction:
    """One body of project convention, and where it came from.

    The model sees `source`, so it can say which file or which label a convention came from
    rather than asserting it without provenance.

    It also sees `scope`: the directory this document governs, which is the directory the
    document sits in. Without it the prompt could state a precedence rule and the model had
    nothing to apply it to — a home-wide `CLAUDE.md` and a project `AGENTS.md` arrived as two
    unlabelled bodies of text, and which one won a disagreement was left to whichever the model
    read last.
    """

    source: str
    content: str
    scope: str = ""


def as_instructions(value: str | Iterable[Instruction] | None) -> list[Instruction]:
    """Take the shorthand or the long form.

    A caller with one body of conventions writes a string and means the obvious thing; a caller
    with several passes values. Accepting both here keeps that convenience out of every
    catalogue that wants it.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [Instruction(source="supplied", content=value)] if value.strip() else []
    return list(value)


def instructions_payload(instructions: Sequence[Instruction]) -> list[dict[str, str]]:
    """The structured instruction data injected into an agent's system context.

    Ordered shallowest scope first, so the documents arrive in the order the precedence rule
    reads them: the general ones, then the ones that override them. A model applying "the more
    deeply nested one wins" against a list in arbitrary order has to sort it first, and a rule
    that needs work before it can be applied is a rule that will sometimes not be."""
    payload = [
        {
            "source": instruction.source,
            # Absent rather than filled with a word. An instruction supplied in code has no
            # directory, and to invent one — "everywhere" — would put a value in the field that
            # reads like a path and is not one. A model applying the precedence rule to it has
            # nothing to compare. An absent key says the same thing and cannot be misread.
            **({"scope": instruction.scope} if instruction.scope else {}),
            "content": instruction.content,
        }
        for instruction in instructions
        if instruction.content.strip()
    ]
    # Shallowest scope first, so the documents arrive in the order the precedence rule reads
    # them. An instruction with no scope sorts first: it is the most general thing there is,
    # and everything with a directory overrides it.
    return sorted(payload, key=lambda entry: entry.get("scope", "").count("/"))


__all__ = ["Instruction", "as_instructions", "instructions_payload"]
