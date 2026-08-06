"""Identifier minting: a short kind prefix, a hyphen, and a UUID4, so every id says what it is."""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    """Return ``"{prefix}-{canonical-uuid4}"``."""
    return f"{prefix}-{uuid.uuid4()}"


def is_id(value: str, prefix: str) -> bool:
    """Whether a value is one of ours of that kind, asked where a value comes from outside this process."""
    head, _, tail = value.partition("-")
    if head != prefix or not tail:
        return False
    try:
        return str(uuid.UUID(tail)) == tail
    except ValueError:
        return False
