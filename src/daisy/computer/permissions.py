"""TCC permission checks + deep-links for the computer-use tool.

Two grants matter: **Accessibility** (reads the AX tree *and* authorizes synthesized
input — the one that actually gates control) and **Screen Recording** (only needed for
the Tier-2 screenshot path). Both are user-granted in System Settings; we can detect
state and open the right pane, but never flip them ourselves.

Attribution note: in the packaged app these grants attach to the *responsible* parent
process (Daisy.app), which the spawned server inherits — the same model the Full Disk
Access flow already relies on.
"""
from __future__ import annotations

import subprocess
from contextlib import suppress

import ApplicationServices as AS

from daisy.core.tuning import Limit, active_tuning

with suppress(Exception):  # Quartz screen-capture preflight lives in the Quartz umbrella
    import Quartz


def accessibility_granted() -> bool:
    """Whether this process may read the AX tree and post synthesized input."""
    return bool(AS.AXIsProcessTrusted())


def request_accessibility() -> bool:
    """Check trust and, if untrusted, surface the system prompt that deep-links to the
    Accessibility pane. Returns current trust (the grant itself is async)."""
    options = {AS.kAXTrustedCheckOptionPrompt: True}
    return bool(AS.AXIsProcessTrustedWithOptions(options))


def screen_recording_granted() -> bool:
    """Whether this process may capture screen pixels (Tier-2 only). Preflight does not
    prompt."""
    try:
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return False


def request_screen_recording() -> bool:
    """Trigger the Screen Recording prompt if not yet granted."""
    try:
        return bool(Quartz.CGRequestScreenCaptureAccess())
    except Exception:
        return False


def open_accessibility_settings() -> None:
    _open("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")


def open_screen_recording_settings() -> None:
    _open("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")


def _open(url: str) -> None:
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["open", url], check=False, timeout=active_tuning().duration(Limit.OPEN_URL_SECONDS))
