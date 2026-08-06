"""A project's own conventions, as values rather than as a wire format."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass
class Instruction:
    """One body of project convention, and where it came from."""

    source: str
    content: str
    scope: str = ""


def as_instructions(value: str | Iterable[Instruction] | None) -> list[Instruction]:
    """Take the shorthand or the long form."""
    if value is None:
        return []
    if isinstance(value, str):
        return [Instruction(source="supplied", content=value)] if value.strip() else []
    return list(value)


def instructions_payload(instructions: Sequence[Instruction]) -> list[dict[str, str]]:
    """The structured instruction data injected into an agent's system context."""
    payload = [
        {
            "source": instruction.source,
            # Absent rather than filled with a word.
            **({"scope": instruction.scope} if instruction.scope else {}),
            "content": instruction.content,
        }
        for instruction in instructions
        if instruction.content.strip()
    ]
    # Shallowest scope first, so the documents arrive in the order the precedence rule reads them.
    return sorted(payload, key=lambda entry: entry.get("scope", "").count("/"))


__all__ = ["Instruction", "as_instructions", "instructions_payload"]
