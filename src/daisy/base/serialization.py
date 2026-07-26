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

# No padding, and real UTF-8 rather than escapes. Both are purely a matter of encoding: the
# value that parses back out is identical either way.
_SEPARATORS = (",", ":")


def compact(payload: Any, **kwargs: Any) -> str:
    """`json.dumps` with nothing spent on whitespace or escapes."""
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(payload, separators=_SEPARATORS, **kwargs)
