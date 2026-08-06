"""Canonical identifier minting for the harness."""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    """Return ``"{prefix}-{canonical-uuid4}"``."""
    return f"{prefix}-{uuid.uuid4()}"


def is_id(value: str, prefix: str) -> bool:
    """Whether a value is one of ours, of that kind — a `{prefix}-{uuid4}` and nothing else."""
    head, _, tail = value.partition("-")
    if head != prefix or not tail:
        return False
    try:
        return str(uuid.UUID(tail)) == tail
    except ValueError:
        return False
