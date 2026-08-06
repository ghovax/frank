"""A project's own conventions, as values rather than as a wire format."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass
class Instruction:
    """One body of convention and where it came from, so the model can say which file a rule is in."""

    source: str
    content: str
    scope: str = ""


def as_instructions(value: str | Iterable[Instruction] | None) -> list[Instruction]:
    """Take the shorthand or the long form, so one body of conventions can be written as a string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [Instruction(source="supplied", content=value)] if value.strip() else []
    return list(value)


def instructions_payload(instructions: Sequence[Instruction]) -> list[dict[str, str]]:
    """The instruction data for the system context, shallowest scope first so precedence reads in order."""
    payload = [
        {
            "source": instruction.source,
            # Absent rather than filled: an instruction supplied in code has no directory to invent one for.
            **({"scope": instruction.scope} if instruction.scope else {}),
            "content": instruction.content,
        }
        for instruction in instructions
        if instruction.content.strip()
    ]
    # Shallowest first, and an unscoped instruction sorts before everything that overrides it.
    return sorted(payload, key=lambda entry: entry.get("scope", "").count("/"))


__all__ = ["Instruction", "as_instructions", "instructions_payload"]
