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
    """

    source: str
    content: str


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
    """The structured instruction data injected into an agent's system context."""
    return [
        {"source": instruction.source, "content": instruction.content}
        for instruction in instructions
        if instruction.content.strip()
    ]


__all__ = ["Instruction", "as_instructions", "instructions_payload"]
