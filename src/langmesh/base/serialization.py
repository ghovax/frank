r"""One spelling of JSON everywhere: no padding, and real UTF-8 rather than `\uXXXX` escapes."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from langmesh.base.tuning import Tunable, active_tuning, clip_to_tokens

# Purely encoding: the value that parses back out is identical either way.
_SEPARATORS = (",", ":")


def compact(payload: Any, **kwargs: Any) -> str:
    """`json.dumps` with nothing spent on whitespace or escapes."""
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(payload, separators=_SEPARATORS, **kwargs)


def lines(records: list) -> str:
    """Records as JSONL: one object per line, which is what an append-only log actually is."""
    return "\n".join(compact(record) for record in records)


def content_address(payload: Any) -> str:
    """A stable content address for anything serializable: canonical JSON, so identical content is one identity."""
    return sha256(compact(payload, sort_keys=True).encode("utf-8")).hexdigest()


def conversation_snapshot_id(messages: list[dict[str, Any]]) -> str:
    """A stable content address for a serialized model conversation."""
    return content_address(messages)


def upstream_detail(body: str) -> str:
    """An upstream service's error body as it should appear in a failure we raise, in one shared answer."""
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    text = body.strip() if payload is None else compact(payload)
    clipped, was_clipped = clip_to_tokens(
        text, active_tuning().amount(Tunable.upstream_error_detail_tokens)
    )
    return f"{clipped}…" if was_clipped else clipped
