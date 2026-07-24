"""TCC permission checks + deep-links for the computer-use tool.

One grant matters: **Accessibility** (reads the AX tree *and* authorizes synthesized
input — the one that actually gates control). It is user-granted in System Settings; we
can detect state and open the right pane, but never flip it ourselves.

Attribution note: in the packaged app this grant attaches to the *responsible* parent
process (Daisy.app), which the spawned server inherits — the same model the Full Disk
Access flow already relies on.
"""
from __future__ import annotations

import subprocess
from contextlib import suppress

import ApplicationServices as AS

from xeac.base.tuning import Limit, active_tuning


def accessibility_granted() -> bool:
    """Whether this process may read the AX tree and post synthesized input."""
    return bool(AS.AXIsProcessTrusted())


def request_accessibility() -> bool:
    """Check trust and, if untrusted, surface the system prompt that deep-links to the
    Accessibility pane. Returns current trust (the grant itself is async)."""
    options = {AS.kAXTrustedCheckOptionPrompt: True}
    return bool(AS.AXIsProcessTrustedWithOptions(options))


def open_accessibility_settings() -> None:
    _open("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")


def _open(url: str) -> None:
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["open", url], check=False, timeout=active_tuning().duration(Limit.OPEN_URL_SECONDS))
