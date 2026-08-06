"""One spelling of JSON, everywhere it is produced."""

from __future__ import annotations

import json
from typing import Any

from frank.base.tuning import Tunable, active_tuning, clip_to_tokens

# No padding, and real UTF-8 rather than escapes.
_SEPARATORS = (",", ":")


def compact(payload: Any, **kwargs: Any) -> str:
    """`json.dumps` with nothing spent on whitespace or escapes."""
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(payload, separators=_SEPARATORS, **kwargs)


def upstream_detail(body: str) -> str:
    """An upstream service's error body, as it should appear inside a failure we raise."""
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    text = body.strip() if payload is None else compact(payload)
    clipped, was_clipped = clip_to_tokens(
        text, active_tuning().amount(Tunable.upstream_error_detail_tokens)
    )
    return f"{clipped}…" if was_clipped else clipped
