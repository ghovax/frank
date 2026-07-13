"""The system clipboard (macOS NSPasteboard), for the copy/cut/paste actions.

This reads and writes the one real pasteboard the whole machine shares, so text the tool copies
can be pasted into the user's own apps, moved between the browser and native surfaces, and picked
up by anything else on the system — exactly what a person expects copy and paste to do. It is not
a private buffer.
"""
from __future__ import annotations

from contextlib import suppress
from typing import Optional

from AppKit import NSPasteboard, NSPasteboardTypeString


def read_text() -> Optional[str]:
    """The clipboard's current text, or None when it holds no string (or is empty)."""
    with suppress(Exception):
        return NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
    return None


def write_text(text: str) -> bool:
    """Replace the clipboard's contents with ``text``. Returns whether the write succeeded."""
    with suppress(Exception):
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        return bool(pasteboard.setString_forType_(text, NSPasteboardTypeString))
    return False
