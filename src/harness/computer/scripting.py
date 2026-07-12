"""Structured automation via Apple Events — the most accurate way to get an app's data.

When an app is scriptable (Chrome/Safari can run JS in a tab and return the result;
Calendar/Mail/Notes/Finder expose real object models), this is the most accurate and
fastest path — you send a typed command and get typed data straight back, with no UI
walking and no pixels. It is also inherently contained: an Apple Event is a message to a
specific target process; it never touches the user's mouse or keyboard.

This is where Calendar-class tasks belong. Calendar's AX tree is nearly empty (it
renders its grid with no accessible elements), so "what's on my calendar next week" is
answered by scripting it, not by reading UI.
"""
from __future__ import annotations

import subprocess
from contextlib import suppress
from typing import Optional


def run_script(source: str, *, language: str = "applescript", timeout: float = 25.0) -> dict:
    """Run an AppleScript or JXA (`language="javascript"`) snippet via ``osascript`` and
    return its result. The osascript child is the automation subject, so the target app
    must have granted Automation consent to whatever process tree runs the server."""
    argv = ["osascript"]
    if language.lower() in ("javascript", "jxa", "js"):
        argv += ["-l", "JavaScript"]
    argv += ["-e", source]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"script timed out after {timeout:.0f}s", "output": ""}
    except OSError as error:
        return {"ok": False, "error": str(error), "output": ""}
    output = completed.stdout.strip()
    error = completed.stderr.strip()
    if completed.returncode != 0:
        return {"ok": False, "error": error or f"exit code {completed.returncode}", "output": output}
    return {"ok": True, "output": output, "error": error}


def launch_app(name: str, *, args: Optional[list[str]] = None, new_instance: bool = False) -> dict:
    """Open or focus an app by name/bundle id. `new_instance` + `args` supports things
    like opening a specific Chrome profile (`--profile-directory="Profile 1"`)."""
    argv = ["open"]
    if new_instance:
        argv.append("-n")
    argv += ["-a", name]
    if args:
        argv.append("--args")
        argv += args
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": str(error)}
    if completed.returncode != 0:
        return {"ok": False, "error": completed.stderr.strip() or f"exit code {completed.returncode}"}
    return {"ok": True}


def frontmost_app_name() -> str:
    result = run_script(
        'tell application "System Events" to get name of first application process whose frontmost is true'
    )
    return result.get("output", "") if result.get("ok") else ""
