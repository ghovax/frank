"""Screen pixels — the least accurate option, used only when there's no structure to read.

Only reached when the accessibility tree is empty or too thin to act on (custom-drawn
canvases, some Electron apps, games). A screenshot forces the model to interpret pixels,
which is both slower and less accurate than reading structured elements, so it is never
the default path.

Capture is scoped to a single window (by its CGWindowID for the owning process), not
the whole screen, so it stays contained: we never grab the user's other windows, and
capturing does not disturb what they are doing.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from contextlib import suppress
from typing import Optional

import Quartz

from harness.core.tuning import Limit, active_tuning


def window_id_for_pid(pid: int) -> Optional[int]:
    """The CGWindowID of the frontmost on-screen window owned by ``pid`` (layer 0, so we
    skip menus, shadows, and helper overlays)."""
    window_infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    ) or []
    owned = (
        info for info in window_infos
        if info.get("kCGWindowOwnerPID") == pid and info.get("kCGWindowLayer", 0) == 0
    )
    best = next(owned, None)
    return int(best["kCGWindowNumber"]) if best else None


def capture_window(pid: int) -> Optional[str]:
    """Capture the frontmost window of ``pid`` to a temporary PNG and return its path, or
    None if the window could not be found or capture failed. Uses the ``screencapture``
    CLI, which already wraps ScreenCaptureKit and requires the Screen Recording grant."""
    window_id = window_id_for_pid(pid)
    if window_id is None:
        return None
    handle, path = tempfile.mkstemp(prefix="daisy-capture-", suffix=".png")
    os.close(handle)
    try:
        completed = subprocess.run(
            ["screencapture", "-x", "-o", "-l", str(window_id), path],
            capture_output=True, timeout=active_tuning().duration(Limit.SCREENCAPTURE_SECONDS), check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _remove(path)
        return None
    if completed.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        _remove(path)
        return None
    return path


def _remove(path: str) -> None:
    with suppress(OSError):
        os.remove(path)
