"""One spelling of JSON, everywhere it is produced.

`json.dumps` defaults to `", "` and `": "` separators and to `\\uXXXX` escapes for anything
outside ASCII. Both are wasteful in the place it matters most: nearly every JSON string this
harness builds is either handed to a model as a tool result or printed for something to parse.
A model pays for the padding by the token, and an escaped non-ASCII character costs several
tokens where the character itself costs one — so a tool result full of accented text or CJK
was being billed at a multiple of its real size.

`compact` is therefore the default spelling and the only one worth reaching for. The one place
it is deliberately *not* used is a file a person edits by hand — an agent sidecar, the remote
agent list — where readability is the whole point and no model is paying for the whitespace.
"""

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
    """An upstream service's error body, as it should appear inside a failure we raise.

    Three things a raw ``body[:500]`` gets wrong, and this is why every provider client now
    shares one answer instead of each picking its own number. A JSON error body — which is what
    all of these are — spends most of its characters on structure and whitespace, so a fixed cut
    lands mid-object and shows *less* of the provider's actual message than the same budget
    spent on `compact` output would. A cut that leaves no mark reads as the whole answer, so a
    reader cannot tell a terse upstream error from a truncated verbose one. And a character
    count is not what costs anything downstream; tokens are, which is what every other length in
    this harness is measured in.

    Left as text when the body is not JSON, because then there is nothing to re-encode and the
    bytes are already the message."""
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    text = body.strip() if payload is None else compact(payload)
    clipped, was_clipped = clip_to_tokens(
        text, active_tuning().amount(Tunable.upstream_error_detail_tokens)
    )
    return f"{clipped}…" if was_clipped else clipped
