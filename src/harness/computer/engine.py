"""The computer-use orchestrator: one entry point the ``computer`` tool calls, which
drives whichever approach gives the most accurate result the fastest.

The model works in an observe/act loop. `observe` reads the accessibility tree of a named
app and returns a structured list of its controls, each with an `index`; the model then
acts by that index. An index is resolved through a small registry that prefers the live AX
handle, falls back to re-resolving the element from its tree path (handles go stale across
relayout), and finally to a contained coordinate click. Every action is delivered to a
specific process, so the user's cursor and keyboard are never disturbed.

Accuracy, not "tiers", decides the approach. Scripting a cooperative app (`run_script`)
returns exact structured data and is the most accurate and fastest way to answer a
question about its contents. Reading and acting on the accessibility tree
(`observe`/`click`/`type`/`key`/`menu`/`scroll`) is the accurate way to drive UI. A
`screenshot` is the least accurate option — pixels the model has to interpret — used only
when an app exposes no accessible structure at all.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import ApplicationServices as AS

from harness.computer import accessibility, capture, input_synthesis, permissions, scripting

_registry_lock = threading.Lock()

# Length bounds on the free-text a single element contributes, so one verbose node (a web
# app that stuffs a whole email body into an AXDescription) cannot flood the observation.
# A control's own contents get more room than its label; both are powers of two, and any
# clip is flagged so the model knows there is more behind it.
_LABEL_LENGTH = 256   # title / description / help
_VALUE_LENGTH = 512   # AXValue: a field's own contents

# Subroles of the standard window title-bar controls. When an observation contains only
# these, the app (typically a backgrounded Electron app) has not built its real tree yet.
_WINDOW_CHROME_SUBROLES = frozenset({
    "AXCloseButton", "AXMinimizeButton", "AXFullScreenButton", "AXZoomButton",
})


@dataclass
class RegistryEntry:
    pid: int
    name: str  # a readable name for action messages, not part of the returned data
    handle: Any
    path: tuple[int, ...]
    center: Optional[tuple[float, float]]


# The registry is replaced on every observe: an index always refers to the most recent
# observation, matching how the model reasons ("observe, then act on what I saw").
_registry: dict[int, RegistryEntry] = {}
_last_pid: Optional[int] = None
# Last element payloads per pid, so a repeat observe can report just what changed.
_last_elements: dict[int, list[dict]] = {}


def _element_name(element: accessibility.Element) -> str:
    return element.title or element.description or element.help or element.role


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    """Clip text to a length bound, reporting whether anything was cut."""
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _element_payload(index: int, element: accessibility.Element) -> dict:
    """The element's real AX attributes, taken as returned and kept when populated. An
    ``index`` (its position, so the model can reference it) is added; free-text is length
    bounded with a ``truncated`` flag when clipped; a region carries its ``children`` count;
    a control carries its ``actions``; unremarkable defaults (enabled, unselected) are
    dropped as noise."""
    payload: dict[str, Any] = {"index": index, "role": element.role}
    if element.subrole:
        payload["subrole"] = element.subrole
    truncated = False
    for name, text in (("title", element.title), ("description", element.description), ("help", element.help)):
        if not text:
            continue
        payload[name], clipped = _bounded(text, _LABEL_LENGTH)
        truncated = truncated or clipped
    value = element.value
    if isinstance(value, str):
        if value:
            payload["value"], clipped = _bounded(value, _VALUE_LENGTH)
            truncated = truncated or clipped
    elif value is not None:
        payload["value"] = value
    if element.enabled is False:
        payload["enabled"] = False
    if element.selected:
        payload["selected"] = True
    if element.actions:
        payload["actions"] = element.actions
    if element.child_count is not None:
        payload["children"] = element.child_count
    if truncated:
        payload["truncated"] = True
    return payload


def _diff(previous: list[dict], current: list[dict]) -> Optional[dict]:
    """A structured diff between two observations of the same app, keyed by the element's
    identity (role + name fields) so a shifted index is not mistaken for a change. Returns
    appeared/disappeared/changed element objects, or None when nothing moved."""
    def identity(element: dict) -> tuple:
        return (element["role"], element.get("title", ""), element.get("description", ""), element.get("help", ""))

    def mutable(element: dict) -> tuple:
        return (element.get("value"), element.get("enabled"), element.get("selected"))

    previous_by_identity = {identity(element): element for element in previous}
    current_by_identity = {identity(element): element for element in current}
    appeared = [element for key, element in current_by_identity.items() if key not in previous_by_identity]
    disappeared = [element for key, element in previous_by_identity.items() if key not in current_by_identity]
    changed = [
        element for key, element in current_by_identity.items()
        if key in previous_by_identity and mutable(element) != mutable(previous_by_identity[key])
    ]
    diff = {
        name: value
        for name, value in (("appeared", appeared), ("disappeared", disappeared), ("changed", changed))
        if value
    }
    return diff or None


def _resolve_pid(target: str) -> tuple[Optional[int], str]:
    """Resolve a target app string to a pid. Empty target reuses the last observed app,
    else the frontmost. Returns (pid, error)."""
    global _last_pid
    if target:
        pid = accessibility.find_app_pid(target)
        if pid is None:
            return None, f"App {target!r} is not running. Launch it first with action='launch'."
        return pid, ""
    if _last_pid is not None:
        return _last_pid, ""
    pid = accessibility.frontmost_pid()
    if pid is None:
        return None, "No target app given and no frontmost app found."
    return pid, ""


def observe(target: str = "", window: str = "focused", element: Optional[int] = None) -> dict:
    """Read an app's UI shallow-first. With no ``element``, return the top-level overview of
    the app (controls and text near the surface, with deep containers as addressable regions
    carrying a child count). With ``element`` set to a region's index from the last observe,
    drill in and expand that region. Either way each element carries its raw AX attributes,
    its actions, and — versus the previous same-scope observe — a diff."""
    if element is not None:
        return _observe_region(element)
    pid, error = _resolve_pid(target)
    if error:
        return {"ok": False, "error": error}
    snapshot = accessibility.snapshot_app(pid, window=window)
    return _finish_observe(pid, snapshot, track=True)


def _observe_region(index: int) -> dict:
    """Re-root the walk at a previously-seen region and expand it, again shallow-first."""
    with _registry_lock:
        entry = _registry.get(index)
    if entry is None:
        return {"ok": False, "error": f"No element at index {index}. Run action='observe' first."}
    root_handle = entry.handle if accessibility.handle_is_live(entry.handle) else \
        accessibility.resolve_from_path(entry.pid, entry.path)
    if root_handle is None:
        return {"ok": False, "error": f"Region {index} is no longer available; observe the app again."}
    snapshot = accessibility.snapshot_app(entry.pid, root_handle=root_handle, root_path=entry.path)
    return _finish_observe(entry.pid, snapshot, track=False)


def _finish_observe(pid: int, snapshot: accessibility.Snapshot, *, track: bool) -> dict:
    """Turn a snapshot into the payload, rebuild the index registry, and — only for a full
    app observe (``track``) — diff against and record the previous observation. A drill is a
    fresh sub-view, so it neither diffs nor overwrites the app's diff baseline."""
    global _last_pid
    elements = [_element_payload(index, element) for index, element in enumerate(snapshot.elements)]
    with _registry_lock:
        _registry.clear()
        for index, element in enumerate(snapshot.elements):
            _registry[index] = RegistryEntry(
                pid=pid, name=_element_name(element),
                handle=element.handle, path=element.path, center=element.center,
            )
        _last_pid = pid
        previous = _last_elements.get(pid) if track else None
        if track:
            _last_elements[pid] = elements
    result: dict[str, Any] = {
        "ok": True,
        "app": snapshot.app_name,
        "window": snapshot.window_title,
        "count": len(elements),
        "duration_ms": snapshot.duration_milliseconds,
        "elements": elements,
    }
    if not elements:
        result["hint"] = ("No accessible elements. If this is a browser or Electron app in the background, bring it "
                          "to the front with action='launch' and observe again; otherwise it draws its own UI, so "
                          "use action='screenshot'.")
    elif all(element.subrole in _WINDOW_CHROME_SUBROLES for element in snapshot.elements):
        result["hint"] = ("Only the window controls are exposed — an Electron app that has not built its accessibility "
                          "tree yet. Use action='launch' on it to bring it forward, then observe again for its full UI.")
    if previous is not None:
        changes = _diff(previous, elements)
        if changes:
            result["changes_since_last_observe"] = changes
    return result


def _resolve_element(index: int) -> tuple[Optional[RegistryEntry], Optional[Any], str]:
    """Return (entry, live_handle_or_None, error). The live handle is the current AX
    element to act on; None means fall back to the entry's coordinates."""
    with _registry_lock:
        entry = _registry.get(index)
    if entry is None:
        return None, None, f"No element at index {index}. Run action='observe' first."
    if accessibility.handle_is_live(entry.handle):
        return entry, entry.handle, ""
    # Handle went stale (the app relayouted). Re-resolve from the tree path.
    rebuilt = accessibility.resolve_from_path(entry.pid, entry.path)
    if rebuilt is not None and accessibility.handle_is_live(rebuilt):
        return entry, rebuilt, ""
    # Last resort: coordinates (a contained click still lands on the right spot).
    return entry, None, ""


_CLICK_ACTIONS = ("AXPress", "AXOpen", "AXConfirm", "AXPick")


def click(index: int, *, clicks: int = 1, button: str = "left") -> dict:
    entry, handle, error = _resolve_element(index)
    if error:
        return {"ok": False, "error": error}
    # Prefer a semantic AX action (no pointer movement at all) for a plain left click.
    if handle is not None and clicks == 1 and button == "left":
        available = set(accessibility.action_names(handle))
        action = next((name for name in _CLICK_ACTIONS if name in available), "")
        if action:
            code = AS.AXUIElementPerformAction(handle, action)
            if code == 0:
                return {"ok": True, "did": f"Clicked {entry.name!r}", "via": "ax"}
    # Fall back to a contained synthesized click at the element's center.
    if entry.center is None:
        return {"ok": False, "error": f"Element {entry.name!r} exposes no press action and has no on-screen position to click."}
    center_x, center_y = entry.center
    input_synthesis.click(entry.pid, center_x, center_y, clicks=clicks, button=button)
    # No semantic AX action existed, so this was a blind positional click: it lands on the
    # spot but cannot confirm the element did anything (a non-interactive label would swallow
    # it silently). Tell the model to verify rather than read a bare success as "it worked".
    return {
        "ok": True,
        "did": f"Clicked {entry.name!r} at ({round(center_x)}, {round(center_y)})",
        "via": "synthesized",
        "note": "This element had no AX press action, so the click was positional and unconfirmed; observe again to check it did what you intended.",
    }


def set_text(index: int, text: str, *, replace: bool = True) -> dict:
    entry, handle, error = _resolve_element(index)
    if error:
        return {"ok": False, "error": error}
    if handle is not None and replace:
        settable_error, settable = AS.AXUIElementIsAttributeSettable(handle, accessibility.VALUE, None)
        if settable_error == 0 and settable:
            code = AS.AXUIElementSetAttributeValue(handle, accessibility.VALUE, text)
            if code == 0:
                return {"ok": True, "did": f"Set {entry.name!r} to {text[:60]!r}", "via": "ax"}
    # Focus then type into the target process (contained). Focus via AX if we can.
    if handle is not None:
        AS.AXUIElementSetAttributeValue(handle, accessibility.FOCUSED, True)
    elif entry.center is not None:
        center_x, center_y = entry.center
        input_synthesis.click(entry.pid, center_x, center_y)
    time.sleep(0.03)
    input_synthesis.type_text(entry.pid, text)
    return {
        "ok": True,
        "did": f"Typed into {entry.name!r}",
        "via": "synthesized",
        "note": "Typed by focusing the field and synthesizing keystrokes; observe again to confirm the value landed.",
    }


def press_key(key: str, modifiers: Optional[list[str]] = None, target: str = "") -> dict:
    pid, error = _resolve_pid(target)
    if error:
        return {"ok": False, "error": error}
    modifiers = modifiers or []
    if input_synthesis.press_key(pid, key, modifiers):
        combo = " ".join([*modifiers, key])
        return {"ok": True, "did": f"Pressed {combo}"}
    return {
        "ok": False,
        "error": (
            f"{key!r} is not a named key. Named keys are: {', '.join(input_synthesis.NAMED_KEYS)}. "
            "For a command shortcut like copy or select-all, invoke the app's menu item with action='menu'."
        ),
    }


def scroll(direction: str, amount: int = 300, target: str = "") -> dict:
    pid, error = _resolve_pid(target)
    if error:
        return {"ok": False, "error": error}
    vectors = {
        "up": (0, amount), "down": (0, -amount), "left": (amount, 0), "right": (-amount, 0),
    }
    if direction not in vectors:
        return {"ok": False, "error": "Direction must be one of up, down, left, or right."}
    delta_x, delta_y = vectors[direction]
    input_synthesis.scroll(pid, delta_x, delta_y)
    return {"ok": True, "did": f"Scrolled {direction}"}


def open_menu(menu_path: list[str], target: str = "") -> dict:
    """Navigate an app's menu bar by a list of item titles (e.g. ["File", "New Window"])
    and press the final item, via the accessibility menu tree (no visible menu flicker)."""
    pid, error = _resolve_pid(target)
    if error:
        return {"ok": False, "error": error}
    segments = [segment.strip() for segment in (menu_path or []) if segment and segment.strip()]
    if not segments:
        return {"ok": False, "error": "Menu path is empty."}
    root = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(root, accessibility.MESSAGING_TIMEOUT_SECONDS)
    menu_bar = accessibility._single(root, "AXMenuBar")
    if menu_bar is None:
        return {"ok": False, "error": "App exposes no menu bar."}

    def child_titled(element: Any, title: str) -> Optional[Any]:
        children = accessibility._single(element, accessibility.CHILDREN) or []
        target_title = title.lower()
        return next(
            (
                child for child in children
                if accessibility._string(accessibility._single(child, accessibility.TITLE)).lower() == target_title
            ),
            None,
        )

    current = menu_bar
    for depth, segment in enumerate(segments):
        item = child_titled(current, segment)
        if item is None:
            return {"ok": False, "error": f"Menu item {segment!r} not found under {' > '.join(segments[:depth]) or 'menu bar'}."}
        if depth < len(segments) - 1:
            # Descend into this item's submenu (its single AXMenu child).
            submenu = accessibility._single(item, accessibility.CHILDREN) or []
            current = submenu[0] if submenu else item
        else:
            code = AS.AXUIElementPerformAction(item, "AXPress")
            if code == 0:
                return {"ok": True, "did": f"Opened menu {' > '.join(segments)}"}
            return {"ok": False, "error": f"Could not press menu item {segment!r} (AX error {code})."}
    return {"ok": False, "error": "Menu navigation failed."}


def screenshot(target: str = "") -> dict:
    """Capture the target app's window to a PNG when it has no accessible structure to
    read. Returns the path; the tool layer attaches the pixels for a vision-capable model."""
    pid, error = _resolve_pid(target)
    if error:
        return {"ok": False, "error": error}
    if not permissions.screen_recording_granted():
        return {"ok": False, "error": "Screen Recording permission is not granted.", "needs_permission": "screen_recording"}
    path = capture.capture_window(pid)
    if path is None:
        return {"ok": False, "error": "Could not capture the app's window."}
    return {"ok": True, "image_path": path, "app": accessibility.app_name_for_pid(pid)}


def run_script(source: str, language: str = "applescript") -> dict:
    return scripting.run_script(source, language=language)


def launch(name: str, arguments: Optional[list[str]] = None) -> dict:
    result = scripting.launch_app(name, args=arguments, new_instance=bool(arguments))
    if result.get("ok"):
        time.sleep(0.6)  # give the app a moment to create its window before an observe
        return {"ok": True, "did": f"Launched {name}"}
    return {"ok": False, "error": result.get("error") or "Launch failed."}
