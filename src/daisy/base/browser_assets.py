"""Files that are shipped with the harness and served to a browser.

These sit in `base` rather than beside the tool that first produced them because two very
different callers need them: a session's tools, and the daemon's GUI surface. Keeping them
here is what lets the daemon serve an artifact page without importing the runtime — the
import the pre-forked worker exists to carry, and the one thing the daemon must stay clear
of if it is to remain cheap to start.
"""

from __future__ import annotations

import re
from pathlib import Path

ASSETS_DIRECTORY = Path(__file__).resolve().parent / "assets"

# Read once at import: these are packaged files that cannot change under a running process.
ARTIFACT_RUNTIME = (ASSETS_DIRECTORY / "artifact_runtime.html").read_text(encoding="utf-8")
PROXY_RUNTIME = (ASSETS_DIRECTORY / "proxy_runtime.js").read_text(encoding="utf-8")


def inject_artifact_runtime(html: str) -> str:
    """Place the artifact runtime (error and resize reporting) as early as possible in the
    document, so it catches failures in the model's own scripts and can size the artifact.

    Falls back to prepending when there is no recognizable `<head>`/`<body>`/`<html>`
    insertion point — a fragment is still a document worth instrumenting."""
    for pattern in (r"<head[^>]*>", r"<body[^>]*>", r"<html[^>]*>"):
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return html[: match.end()] + ARTIFACT_RUNTIME + html[match.end():]
    return ARTIFACT_RUNTIME + html
