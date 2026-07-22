"""Canonical identifier minting for the daisy.

Every identifier we generate ourselves follows one shape: a short kind prefix,
a hyphen, then the canonical textual form of a UUID4 (e.g.
``agent-a0b47aff-c515-45c6-884f-86c6d82b879f``). This keeps every id in the
system instantly recognizable by kind and trivially parseable, instead of the
grab bag of bare hex, truncated hex, and underscore-joined variants that grew up
across the codebase.

Identifiers minted by external systems (model-provided tool-call ids, A2A
protocol message ids) are left untouched — those belong to their owners.
"""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    """Return ``"{prefix}-{canonical-uuid4}"``."""
    return f"{prefix}-{uuid.uuid4()}"
