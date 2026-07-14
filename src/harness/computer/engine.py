"""The native macOS automation surface: any running app, driven through the most accurate
approach available. One of the two surfaces built on ``surface.py`` — it shares that module's
serial worker, failure guard, ref-addressed element vocabulary, and result shapers, and adds only
what is genuinely native: the accessibility-tree walk, AX actions, synthesized input, and the TCC
permission gate. Shelling out — scripting an app with ``osascript``, launching one with ``open`` —
is not a tool action here; the model does that through the ``bash`` tool on the same Mac.

The model works in an observe/act loop identical to the web surface's. ``observe`` reads the
accessibility tree of a named app and returns the shared elements, each with a stable ``ref``; the
model then acts by ref. A ref is resolved through the durable registry to a live AX handle,
falling back to re-resolving the element from its tree path (handles go stale across relayout),
and finally to a contained coordinate click. Every action is delivered to a specific process, so
the user's cursor and keyboard are never disturbed. After an action the surface re-observes and
returns a diff-first digest of what changed — the same honest, closed-loop result the web surface
gives.

Accuracy decides the approach. Reading and acting on the accessibility tree
(``observe``/``click``/``type``/``press``/``menu``/``scroll``/``find``) is the accurate way to
drive UI. A ``screenshot`` is the least accurate option — pixels the model has to interpret — used
only when an app exposes no accessible structure at all. Scripting a cooperative app (AppleScript
or JXA) is not a tool action here: it is a plain ``osascript`` invocation, so the model runs it
through the ``bash`` tool on the same Mac rather than routing it through this surface.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import ApplicationServices as AS

from harness.computer import accessibility, capture, clipboard, input_synthesis, permissions
from harness.computer.surface import (
    Element, ElementRegistry, Surface, ToolFailure, diff_elements, message_loader,
    resolve_caret, resolve_range,
)
# Listing caps, physical pacing, and settlement come from the one central tuning policy.
from harness.core.tuning import Limit, active_tuning, settle
# The verbatim-first matcher and compact diff window from the file-edit tool: the `edit` action
# changes a field's text exactly the way edit_file changes a file, reusing that tested logic.
from harness.tools.file_tools import _context_diff_window as context_diff_window, _match_find_text as match_find_text

message = message_loader("computer")

# Subroles of the standard window title-bar controls. When an observation contains only these,
# the app (typically a backgrounded Electron app) has not built its real tree yet.
_WINDOW_CHROME_SUBROLES = frozenset({
    "AXCloseButton", "AXMinimizeButton", "AXFullScreenButton", "AXZoomButton",
})

# Semantic AX actions, tried before any synthesized input, split by the model's click count so a
# click maps to the macOS convention: one click activates (AXPress — a button, link, checkbox),
# a double click opens (AXOpen — a Finder folder/file, a list row). AXOpen is a stronger action
# than AXPress, so firing it on a single click would surprise (a Finder single-click only selects).
_ACTIVATE_ACTIONS = ("AXPress",)
_OPEN_ACTIONS = ("AXOpen", "AXConfirm", "AXPick")

# The actions that read the accessibility tree or synthesize input, and so require the
# Accessibility grant; screenshot gates on Screen Recording instead.
_ACCESSIBILITY_ACTIONS = frozenset({
    "observe", "find", "click", "type", "edit", "press", "menu", "scroll",
    "select", "caret", "copy", "cut", "paste", "hover", "drag",
})

# A find walks the whole tree, so it expands far past the shallow overview depth. The depth is a
# structural walk bound, not a token budget, so it stays fixed here; how many matches come back is
# a listing budget and comes from the central tuning policy.
_FIND_DEPTH = 64


@dataclass
class RegistryEntry:
    pid: int
    name: str  # a readable name for action messages, not part of the returned data
    handle: Any
    path: tuple[int, ...]
    center: Optional[tuple[float, float]]


def _as_int(value: Any) -> Optional[int]:
    """A tool argument coerced to int, or None when it is absent or blank."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_ref(value: Any) -> Optional[str]:
    """An element argument coerced to a ref string, or None when absent/blank. A bare integer is
    stringified so it still routes to a lookup that fails cleanly with the current-refs hint."""
    if value is None or value == "":
        return None
    return str(value).strip()


def _element_name(element: accessibility.Element) -> str:
    return element.title or element.description or element.help or element.role


def _app_signature(pid: int) -> int:
    """A cheap, comparable signature of an app's current UI — the number of elements in a shallow
    focused-window snapshot. Fed to ``settle`` so an action that reveals content (a hover menu)
    is waited out until the tree stops changing, instead of a blind fixed sleep. Returns -1 on a
    transient failure so a hiccup reads as 'still moving'."""
    try:
        return len(accessibility.snapshot_app(pid, window="focused").elements)
    except Exception:
        return -1


def _to_element(ax: accessibility.Element, token: "RegistryEntry") -> Element:
    """Convert a raw accessibility node into the shared, cross-surface ``Element``, carrying the
    registry entry that will act on it as its ``token``. The three AX name attributes collapse into
    one ``name``; a node is clickable when it exposes any AX action (text and pure regions expose
    none); a region carries its on-screen child count so the model can drill; only the notable
    states become flags."""
    flags: dict[str, Any] = {}
    if ax.enabled is False:
        flags["enabled"] = False
    if ax.selected:
        flags["selected"] = True
    return Element(
        role=ax.role,
        name=ax.title or ax.description or ax.help,
        value=ax.value,
        clickable=bool(ax.actions),
        flags=flags,
        children=ax.child_count,
        actions=ax.actions,
        token=token,
    )


class NativeSurface(Surface):
    """The macOS accessibility implementation of the shared ``Surface``. Holds the durable ref
    registry and change-detection baselines (worker-thread-only) and exposes the computer actions."""

    def __init__(self) -> None:
        super().__init__("daisy-accessibility", message)
        # The durable ref↔entry map: a stable ref per element identity, rebound to a fresh AX
        # registry entry on every observe. A ref the model saw survives across calls, and a
        # no-match find never wipes it — the same closed-loop contract the web surface has.
        self.registry = ElementRegistry()
        self._last_pid: Optional[int] = None
        # Last element payloads per pid, so a repeat observe (or a post-action re-observe) can
        # report just what changed.
        self._last_elements: dict[int, list[dict]] = {}

    def recover(self, detail: str) -> dict:
        return {"ok": False, "error": message("action_failed", detail=detail)}

    def location_fields(self, snapshot: accessibility.Snapshot) -> dict:
        return {"app": snapshot.app_name, "window": snapshot.window_title}

    # Target and element resolution.

    def _resolve_pid(self, target: str) -> int:
        """Resolve a target app string to a pid. Empty target reuses the last observed app, else
        the frontmost. Raises ``ToolFailure`` when nothing resolves, so ``guard`` shapes it."""
        if target:
            pid = accessibility.find_app_pid(target)
            if pid is None:
                raise ToolFailure({"ok": False, "error": f"App {target!r} is not running. Open it first by running `open -a {target!r}` through the bash tool, then observe."})
            return pid
        if self._last_pid is not None:
            return self._last_pid
        pid = accessibility.frontmost_pid()
        if pid is None:
            raise ToolFailure({"ok": False, "error": "No target app given and no frontmost app found."})
        return pid

    def _resolve_element(self, ref: str) -> tuple[RegistryEntry, Optional[Any]]:
        """Return (entry, live_handle_or_None); the live handle is the current AX element to act
        on, None means fall back to the entry's coordinates. Raises ``ToolFailure`` for a ref that
        was never seen."""
        entry = self.registry.token(ref)
        if entry is None:
            raise ToolFailure({"ok": False, "error": f"No element {ref!r}. Run action='observe' first to get current refs."})
        if accessibility.handle_is_live(entry.handle):
            return entry, entry.handle
        # Handle went stale (the app relayouted). Re-resolve from the tree path.
        rebuilt = accessibility.resolve_from_path(entry.pid, entry.path)
        if rebuilt is not None and accessibility.handle_is_live(rebuilt):
            return entry, rebuilt
        # Last resort: coordinates (a contained click still lands on the right spot).
        return entry, None

    # Observation and shaping.

    def _bind(self, pid: int, raw_elements: list[accessibility.Element]) -> list[dict]:
        """Turn raw AX nodes into shared elements, assign each its stable ref in the durable
        registry (rebinding its live handle), and return the model-facing payloads."""
        elements = [
            _to_element(ax, RegistryEntry(
                pid=pid, name=_element_name(ax), handle=ax.handle, path=ax.path, center=ax.center,
            ))
            for ax in raw_elements
        ]
        self.registry.bind(elements)
        self._last_pid = pid
        return [element.payload() for element in elements]

    def _record(self, pid: int, payloads: list[dict], *, track: bool) -> Optional[dict]:
        """Update the per-pid change-detection baseline and return the diff against the previous
        observation, or None. A drill is a fresh sub-view, so it never becomes the baseline."""
        if not track:
            return None
        previous = self._last_elements.get(pid)
        self._last_elements[pid] = payloads
        return diff_elements(previous, payloads) if previous is not None else None

    def _reobserve(self, pid: int, *, window: str = "focused", track: bool = True) -> tuple[accessibility.Snapshot, list[dict], Optional[dict]]:
        snapshot = accessibility.snapshot_app(pid, window=window)
        payloads = self._bind(pid, snapshot.elements)
        changes = self._record(pid, payloads, track=track)
        return snapshot, payloads, changes

    def _act_result(self, pid: int, result: dict) -> dict:
        """Finish an acting call the way the web surface does: re-observe the app and return the
        diff-first digest against the app as it stood before the action (the last baseline, which
        this action has not yet overwritten)."""
        previous = self._last_elements.get(pid)
        snapshot = accessibility.snapshot_app(pid, window="focused")
        current = self._bind(pid, snapshot.elements)
        self._last_elements[pid] = current
        digest = self.digest(
            context=snapshot, current=current, previous=previous, prose_note_name="digest_prose",
            empty_hint=message("empty_observation"), no_change_note=message("no_change"),
        )
        result.update(digest)
        return result

    # Actions.

    def observe(self, target: str = "", window: str = "focused", element: Optional[str] = None) -> dict:
        """Read an app's UI shallow-first. With no ``element``, the top-level overview (controls
        and text near the surface, deep containers as addressable regions with a child count). With
        ``element`` set to a region's ref from an earlier observe, drill in and expand that region.
        Each element carries the shared vocabulary plus, versus the previous same-scope observe, a
        diff."""

        def run() -> dict:
            if element is not None:
                return self._observe_region(element)
            pid = self._resolve_pid(target)
            snapshot, payloads, changes = self._reobserve(pid, window=window, track=True)
            result = self.overview(
                context=snapshot, elements=payloads, empty_hint=message("empty_observation"),
            )
            if payloads and all(ax.subrole in _WINDOW_CHROME_SUBROLES for ax in snapshot.elements):
                result["hint"] = message("chrome_only")
            if changes:
                result["changes_since_last_observe"] = changes
            return result

        return self.guard(run)

    def _observe_region(self, ref: str) -> dict:
        """Re-root the walk at a previously-seen region and expand it, again shallow-first. A drill
        is a fresh sub-view: it neither diffs nor overwrites the app's diff baseline."""
        entry = self.registry.token(ref)
        if entry is None:
            return {"ok": False, "error": f"No element {ref!r}. Run action='observe' first to get current refs."}
        root_handle = entry.handle if accessibility.handle_is_live(entry.handle) else \
            accessibility.resolve_from_path(entry.pid, entry.path)
        if root_handle is None:
            return {"ok": False, "error": f"Region {ref!r} is no longer available; observe the app again."}
        snapshot = accessibility.snapshot_app(entry.pid, root_handle=root_handle, root_path=entry.path)
        payloads = self._bind(entry.pid, snapshot.elements)
        result = self.overview(
            context=snapshot, elements=payloads, empty_hint=message("empty_observation"),
        )
        result["did"] = f"Expanded element {ref}"
        return result

    def find(self, query: str, target: str = "") -> dict:
        """Search the target app's on-screen tree for elements whose name or value contains
        ``query``, case-insensitively. Clickable matches first; matches are bound into the durable
        registry (merged, never replacing it), so a no-match find cannot brick a paging loop."""

        def run() -> dict:
            needle = query.strip().lower()
            if not needle:
                return {"ok": False, "error": "The find action needs non-empty text to look for."}
            pid = self._resolve_pid(target)
            snapshot = accessibility.snapshot_app(pid, window="focused", maximum_depth=_FIND_DEPTH)
            matched = [
                ax for ax in snapshot.elements
                if needle in _element_name(ax).lower()
                or (isinstance(ax.value, str) and needle in ax.value.lower())
            ]
            matched.sort(key=lambda ax: 0 if ax.actions else 1)
            find_limit = active_tuning().amount(Limit.FIND_LIMIT)
            truncated = len(matched) > find_limit
            matched = matched[:find_limit]
            elements = [
                _to_element(ax, RegistryEntry(
                    pid=pid, name=_element_name(ax), handle=ax.handle, path=ax.path, center=ax.center,
                ))
                for ax in matched
            ]
            self.registry.bind(elements)
            self._last_pid = pid
            listed = [element.payload() for element in elements]
            result: dict[str, Any] = {
                "ok": True, "app": snapshot.app_name, "window": snapshot.window_title,
                "query": query, "count": len(listed), "elements": listed,
            }
            if truncated:
                result["truncated"] = True
                result["note"] = message("find_truncated", limit=str(find_limit))
            if not listed:
                result["note"] = message("find_no_match")
            return result

        return self.guard(run)

    def _point_target(self, ref: Optional[str], x: Optional[int], y: Optional[int]) -> tuple[int, float, float]:
        """Resolve a pointer operation to (pid, x, y): the given element's centre, or an explicit
        screen point in the last-observed (or frontmost) app. Raises ToolFailure otherwise."""
        if ref is not None:
            entry, _ = self._resolve_element(ref)
            if entry.center is None:
                raise ToolFailure({"ok": False, "error": f"Element {entry.name!r} has no on-screen position to point at."})
            return entry.pid, entry.center[0], entry.center[1]
        if x is not None and y is not None:
            pid = self._last_pid if self._last_pid is not None else accessibility.frontmost_pid()
            if pid is None:
                raise ToolFailure({"ok": False, "error": "No app to act on. Observe one first, or give an element."})
            return pid, float(x), float(y)
        raise ToolFailure({"ok": False, "error": "This action needs an element ref or an x/y point."})

    def _text_target(self, ref: Optional[str]) -> tuple[int, Any]:
        """Resolve a text operation to (pid, live AX handle): the given element, or the app's
        currently focused element. Raises ToolFailure when there is no editable handle."""
        if ref is not None:
            entry, handle = self._resolve_element(ref)
            if handle is None:
                raise ToolFailure({"ok": False, "error": f"Element {entry.name!r} is no longer live; observe the app again."})
            return entry.pid, handle
        if self._last_pid is None:
            raise ToolFailure({"ok": False, "error": "No app to act on. Observe one first, or give an element."})
        handle = accessibility.focused_element(self._last_pid)
        if handle is None:
            raise ToolFailure({"ok": False, "error": "No text field is focused. Give the element ref of the field."})
        return self._last_pid, handle

    def _text_result(self, did: str, *, via: str = "ax", **extra: Any) -> dict:
        """The light result a text operation returns: just what it did and how (the accessible API
        or a synthesized fallback). It echoes no field content and does not re-observe, so the model
        can chain edits on one field without churning refs or filling the context."""
        return {"ok": True, "did": did, "via": via, **extra}

    def click(self, ref: Optional[str] = None, *, x: Optional[int] = None, y: Optional[int] = None,
              clicks: int = 1, button: str = "left") -> dict:
        def run() -> dict:
            # An element left click prefers the semantic AX action (no pointer movement): AXPress on
            # a single click, AXOpen on a double click. A right click, a point click, or an element
            # with no matching action falls back to a synthesized click.
            hint = None
            if ref is not None and x is None and y is None:
                entry, handle = self._resolve_element(ref)
                if handle is not None and button == "left":
                    available = set(accessibility.action_names(handle))
                    wanted = _OPEN_ACTIONS if clicks >= 2 else _ACTIVATE_ACTIONS
                    action = next((name for name in wanted if name in available), "")
                    if action and AS.AXUIElementPerformAction(handle, action) == 0:
                        did = f"Opened {entry.name!r}" if clicks >= 2 else f"Clicked {entry.name!r}"
                        return self._act_result(entry.pid, {"ok": True, "did": did, "via": "ax"})
                    # A single click on an item that can only be opened (a Finder folder cell) just
                    # selects it; point the model at the double click that opens it.
                    if clicks == 1 and available & set(_OPEN_ACTIONS):
                        hint = message("click_openable")
                if entry.center is None:
                    return {"ok": False, "error": f"Element {entry.name!r} exposes no action and has no on-screen position to click."}
                pid, point_x, point_y, name = entry.pid, entry.center[0], entry.center[1], repr(entry.name)
            else:
                pid, point_x, point_y = self._point_target(ref, x, y)
                name = f"({round(point_x)}, {round(point_y)})"
            input_synthesis.click(pid, point_x, point_y, clicks=clicks, button=button)
            # A synthesized click lands on the spot but cannot confirm the target did anything; the
            # re-observed surface lets the model check, and the note flags it as unconfirmed.
            result: dict[str, Any] = {
                "ok": True, "did": f"Clicked {name} at ({round(point_x)}, {round(point_y)})",
                "via": "synthesized", "note": message("click_positional"),
            }
            if hint:
                result["hint"] = hint
            return self._act_result(pid, result)

        return self.guard(run)

    def set_text(self, ref: Optional[str], text: str, *, mode: str = "replace") -> dict:
        """Enter text. ``replace`` rewrites the whole field; ``insert`` inserts at the caret,
        replacing the current selection if there is one. Insert uses the accessible text API and
        falls back to synthesized typing. To change part of a field, prefer ``edit`` (find and
        replace) over rewriting the whole thing."""
        def run() -> dict:
            if mode == "insert":
                pid, handle = self._text_target(ref)
                if accessibility.set_selected_text(handle, text):
                    return self._text_result(f"Inserted {len(text)} chars", via="ax")
                # The field does not expose settable selected text; type at its focus instead.
                AS.AXUIElementSetAttributeValue(handle, accessibility.FOCUSED, True)
                time.sleep(active_tuning().duration(Limit.FOCUS_SETTLE_SECONDS))
                input_synthesis.type_text(pid, text)
                return self._text_result(f"Typed {len(text)} chars", via="synthesized", note=message("type_synthesized"))
            # replace: set the whole value, or focus and type.
            entry, handle = self._resolve_element(ref) if ref is not None else (None, None)
            if entry is not None and handle is not None and accessibility.attribute_settable(handle, accessibility.VALUE) \
                    and AS.AXUIElementSetAttributeValue(handle, accessibility.VALUE, text) == 0:
                landed = accessibility.text_value(handle)
                result: dict[str, Any] = {"ok": True, "did": f"Set {entry.name!r} to {text[:60]!r}", "via": "ax"}
                if landed is not None:
                    result["value"] = landed
                    if landed != text:
                        result["note"] = message("type_clamped")
                return self._act_result(entry.pid, result)
            if handle is not None:
                AS.AXUIElementSetAttributeValue(handle, accessibility.FOCUSED, True)
            elif entry is not None and entry.center is not None:
                input_synthesis.click(entry.pid, entry.center[0], entry.center[1])
            pid = entry.pid if entry is not None else self._resolve_pid("")
            time.sleep(active_tuning().duration(Limit.FOCUS_SETTLE_SECONDS))
            input_synthesis.type_text(pid, text)
            did = f"Typed into {entry.name!r}" if entry is not None else f"Typed {len(text)} chars"
            return self._act_result(pid, {"ok": True, "did": did, "via": "synthesized", "note": message("type_synthesized")})

        return self.guard(run)

    def edit_text(self, ref: Optional[str], find: str, replace: str, *, replace_all: bool = False) -> dict:
        """Change part of a field's text by replacing ``find`` with ``replace`` — the same
        verbatim-first, must-be-unique matching as the file-edit tool, so the model edits a field
        the way it edits a file. Returns a compact before/after diff, not the whole field."""
        def run() -> dict:
            pid, handle = self._text_target(ref)
            content = accessibility.text_value(handle)
            if content is None:
                return {"ok": False, "error": "This element holds no editable text to change."}
            if find == replace:
                return {"ok": False, "error": "find and replace are identical; nothing to change."}
            matched = match_find_text(content, find, replace)
            if matched is None:
                return {"ok": False, "error": "The text to change is not in the field. Copy it exactly as it appears."}
            effective, found, replacement, occurrences = matched
            if occurrences > 1 and not replace_all:
                return {"ok": False, "error": f"{occurrences} matches for that text; make it unique or set replace_all."}
            after = effective.replace(found, replacement, -1 if replace_all else 1)
            applied = False
            # Prefer setting the whole value (mirrors the file-edit tool). If the field is not
            # value-settable, apply surgically to a single verbatim match's range instead.
            if accessibility.attribute_settable(handle, accessibility.VALUE) \
                    and AS.AXUIElementSetAttributeValue(handle, accessibility.VALUE, after) == 0:
                applied = True
            elif not replace_all and content.count(find) == 1:
                offset = content.find(find)
                applied = accessibility.set_selected_range(handle, offset, len(find)) \
                    and accessibility.set_selected_text(handle, replace)
            if not applied:
                return {"ok": False, "error": message("select_unsupported")}
            before_window, after_window = context_diff_window(effective, after)
            count = occurrences if replace_all else 1
            return {"ok": True, "did": f"Changed {count} occurrence(s)", "via": "ax", "before": before_window, "after": after_window}

        return self.guard(run)

    def select(self, ref: Optional[str] = None, *, text: Optional[str] = None, to_text: Optional[str] = None,
               select_all: bool = False, occurrence: int = 1) -> dict:
        """Select text in a field, addressed by content: a substring (``text``), a range from
        ``text`` through ``to_text``, or the whole field (``select_all``). Uses the accessible
        selection range, with synthesized select-all as the only fallback."""
        def run() -> dict:
            pid, handle = self._text_target(ref)
            content = accessibility.text_value(handle)
            if content is None:
                return {"ok": False, "error": "This element holds no editable text to select."}
            if select_all:
                start, length = resolve_range(content, select_all=True)
            elif to_text is not None:
                start, length = resolve_range(content, anchor_from=text, anchor_to=to_text, occurrence=occurrence)
            else:
                start, length = resolve_range(content, text=text, occurrence=occurrence)
            if accessibility.set_selected_range(handle, start, length):
                return self._text_result(f"Selected {length} chars", via="ax")
            if select_all and input_synthesis.press_key(pid, "a", ["command"]):
                return self._text_result("Selected all", via="synthesized", note=message("select_synthesized"))
            return {"ok": False, "error": message("select_unsupported")}

        return self.guard(run)

    def caret(self, ref: Optional[str] = None, *, before: Optional[str] = None, after: Optional[str] = None,
              at_offset: Optional[int] = None, edge: str = "", occurrence: int = 1) -> dict:
        """Place the insertion point in a field: ``before``/``after`` a substring, ``at_offset`` a
        character offset, or at the ``start``/``end`` edge."""
        def run() -> dict:
            pid, handle = self._text_target(ref)
            content = accessibility.text_value(handle)
            if content is None:
                return {"ok": False, "error": "This element holds no editable text."}
            offset = resolve_caret(
                content, before=before, after=after, at_offset=at_offset,
                to_start=edge == "start", to_end=edge == "end", occurrence=occurrence,
            )
            if accessibility.set_selected_range(handle, offset, 0):
                return self._text_result(f"Caret at {offset}", via="ax")
            return {"ok": False, "error": message("select_unsupported")}

        return self.guard(run)

    def copy_selection(self, ref: Optional[str] = None, *, cut: bool = False) -> dict:
        """Copy the current selection to the system clipboard (``cut`` also deletes it). Reads the
        selection through the accessibility API, falling back to a synthesized shortcut."""
        def run() -> dict:
            pid, handle = self._text_target(ref)
            selected = accessibility.selected_text(handle)
            if selected:
                clipboard.write_text(selected)
                if cut:
                    accessibility.set_selected_text(handle, "")
                verb = "Cut" if cut else "Copied"
                return self._text_result(f"{verb} {len(selected)} chars", via="ax")
            # No accessible selection text; let the app copy whatever it has selected.
            if input_synthesis.press_key(pid, "x" if cut else "c", ["command"]):
                return self._text_result("Cut selection" if cut else "Copied selection", via="synthesized")
            return {"ok": False, "error": "Nothing is selected to copy. Select text first."}

        return self.guard(run)

    def paste(self, ref: Optional[str] = None) -> dict:
        """Insert the system clipboard's text at the caret (replacing any selection)."""
        def run() -> dict:
            pid, handle = self._text_target(ref)
            text = clipboard.read_text()
            if not text:
                return {"ok": False, "error": "The clipboard is empty; copy something first."}
            if accessibility.set_selected_text(handle, text):
                return self._text_result(f"Pasted {len(text)} chars", via="ax")
            if input_synthesis.press_key(pid, "v", ["command"]):
                return self._text_result(f"Pasted {len(text)} chars", via="synthesized", note=message("type_synthesized"))
            return {"ok": False, "error": message("select_unsupported")}

        return self.guard(run)

    def hover(self, ref: Optional[str] = None, *, x: Optional[int] = None, y: Optional[int] = None) -> dict:
        """Move the pointer over an element or point (revealing hover menus and tooltips) without
        clicking, then re-read the app."""
        def run() -> dict:
            pid, point_x, point_y = self._point_target(ref, x, y)
            input_synthesis.move(pid, point_x, point_y)
            # Let a hover menu or tooltip appear, waiting only until the app's tree stops changing
            # rather than a blind fixed pause.
            settle(lambda: _app_signature(pid))
            return self._act_result(pid, {"ok": True, "did": f"Hovered ({round(point_x)}, {round(point_y)})"})

        return self.guard(run)

    def drag(self, ref: Optional[str] = None, to_element: Optional[str] = None, *,
             x: Optional[int] = None, y: Optional[int] = None, to_x: Optional[int] = None,
             to_y: Optional[int] = None, button: str = "left") -> dict:
        """Press at a source element/point, drag to a target element/point, and release — for drag
        and drop and drag-to-select."""
        def run() -> dict:
            pid, start_x, start_y = self._point_target(ref, x, y)
            _, end_x, end_y = self._point_target(to_element, to_x, to_y)
            input_synthesis.drag(pid, start_x, start_y, end_x, end_y, button=button)
            return self._act_result(pid, {"ok": True, "did": f"Dragged to ({round(end_x)}, {round(end_y)})"})

        return self.guard(run)

    def press_key(self, key: str, modifiers: Optional[list[str]] = None, target: str = "") -> dict:
        def run() -> dict:
            pid = self._resolve_pid(target)
            keys = modifiers or []
            if not input_synthesis.press_key(pid, key, keys):
                return {
                    "ok": False,
                    "error": (
                        f"{key!r} is not a named key. Named keys are: {', '.join(input_synthesis.NAMED_KEYS)}. "
                        "For a command shortcut like copy or select-all, invoke the app's menu item with action='menu'."
                    ),
                }
            combo = " ".join([*keys, key])
            return self._act_result(pid, {"ok": True, "did": f"Pressed {combo}"})

        return self.guard(run)

    def scroll(self, direction: str, amount: Optional[int] = None, target: str = "") -> dict:
        def run() -> dict:
            pid = self._resolve_pid(target)
            step = active_tuning().amount(Limit.SCROLL_AMOUNT_PIXELS) if amount is None else amount
            vectors = {"up": (0, step), "down": (0, -step), "left": (step, 0), "right": (-step, 0)}
            if direction not in vectors:
                return {"ok": False, "error": "Direction must be one of up, down, left, or right."}
            delta_x, delta_y = vectors[direction]
            input_synthesis.scroll(pid, delta_x, delta_y)
            return self._act_result(pid, {"ok": True, "did": f"Scrolled {direction}"})

        return self.guard(run)

    def open_menu(self, menu_path: list[str], target: str = "") -> dict:
        """Navigate an app's menu bar by a list of item titles (e.g. ["File", "New Window"]) and
        press the final item, via the accessibility menu tree (no visible menu flicker)."""

        def run() -> dict:
            pid = self._resolve_pid(target)
            segments = [segment.strip() for segment in (menu_path or []) if segment and segment.strip()]
            if not segments:
                return {"ok": False, "error": "Menu path is empty."}
            root = AS.AXUIElementCreateApplication(pid)
            AS.AXUIElementSetMessagingTimeout(root, active_tuning().duration(Limit.AX_MESSAGING_SECONDS))
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
                    if AS.AXUIElementPerformAction(item, "AXPress") == 0:
                        return self._act_result(pid, {"ok": True, "did": f"Opened menu {' > '.join(segments)}"})
                    return {"ok": False, "error": f"Could not press menu item {segment!r}."}
            return {"ok": False, "error": "Menu navigation failed."}

        return self.guard(run)

    def screenshot(self, target: str = "") -> dict:
        """Capture the target app's window to a PNG when it has no accessible structure to read.
        Returns the path; the dispatch layer attaches the pixels for a vision-capable model."""

        def run() -> dict:
            pid = self._resolve_pid(target)
            if not permissions.screen_recording_granted():
                return {"ok": False, "error": message("screen_recording_needed"), "needs_permission": "screen_recording"}
            path = capture.capture_window(pid)
            if path is None:
                return {"ok": False, "error": "Could not capture the app's window."}
            return {"ok": True, "image_path": path, "app": accessibility.app_name_for_pid(pid)}

        return self.guard(run)

    # Dispatch.

    #: Actions whose result carries an ``image_path`` for the model-image side channel.
    screenshot_actions = frozenset({"screenshot"})

    def preflight(self, action: str) -> Optional[dict]:
        """The AX-tree actions need the Accessibility grant; a clear, actionable error beats a
        silent empty result. Screenshot gates elsewhere (it checks Screen Recording inside
        itself)."""
        if action in _ACCESSIBILITY_ACTIONS and not permissions.accessibility_granted():
            return {
                "ok": False,
                "error": message("accessibility_needed"),
                "needs_permission": "accessibility",
            }
        return None

    def dispatch(self, action: str, arguments: dict) -> dict:
        app = str(arguments.get("app", ""))
        text = str(arguments.get("text", ""))
        window = str(arguments.get("window", "focused") or "focused")
        clicks = int(arguments.get("clicks", 1) or 1)
        button = str(arguments.get("button", "left") or "left")
        key = str(arguments.get("key", ""))
        modifiers = arguments.get("modifiers") or []
        menu_path = arguments.get("menu_path") or []
        direction = str(arguments.get("direction", ""))
        ref = _as_ref(arguments.get("element"))
        to_ref = _as_ref(arguments.get("to_element"))
        point_x = _as_int(arguments.get("x"))
        point_y = _as_int(arguments.get("y"))
        occurrence = int(arguments.get("occurrence", 1) or 1)
        if action == "observe":
            return self.observe(app, window, element=ref)
        if action == "find":
            query = str(arguments.get("query", ""))
            if not query.strip():
                return {"ok": False, "error": "The find action needs a query — the text to look for."}
            return self.find(query, app)
        if action == "click":
            return self.click(ref, x=point_x, y=point_y, clicks=clicks, button=button)
        if action == "type":
            if ref is None and self._last_pid is None:
                return {"ok": False, "error": "The type action needs an element ref (the field) or a focused app."}
            return self.set_text(ref, text, mode=str(arguments.get("mode", "replace") or "replace"))
        if action == "edit":
            find = str(arguments.get("find", ""))
            if not find:
                return {"ok": False, "error": "The edit action needs find (the exact text to change) and replace."}
            return self.edit_text(ref, find, str(arguments.get("replace", "")), replace_all=bool(arguments.get("replace_all", False)))
        if action == "select":
            return self.select(
                ref, text=text or None, to_text=str(arguments.get("to_text", "")) or None,
                select_all=bool(arguments.get("select_all", False)), occurrence=occurrence,
            )
        if action == "caret":
            return self.caret(
                ref, before=str(arguments.get("before", "")) or None,
                after=str(arguments.get("after", "")) or None, at_offset=_as_int(arguments.get("at_offset")),
                edge=str(arguments.get("edge", "")), occurrence=occurrence,
            )
        if action in ("copy", "cut"):
            return self.copy_selection(ref, cut=action == "cut")
        if action == "paste":
            return self.paste(ref)
        if action == "press":
            if not key:
                return {"ok": False, "error": "The press action needs a key (e.g. return, tab, or a chord)."}
            return self.press_key(key, modifiers, app)
        if action == "hover":
            return self.hover(ref, x=point_x, y=point_y)
        if action == "drag":
            return self.drag(
                ref, to_ref,
                x=point_x, y=point_y, to_x=_as_int(arguments.get("to_x")), to_y=_as_int(arguments.get("to_y")),
                button=button,
            )
        if action == "menu":
            return self.open_menu(menu_path, app)
        if action == "scroll":
            return self.scroll(direction or "down", target=app)
        if action == "screenshot":
            return self.screenshot(app)
        return {"ok": False, "error": f"Unknown action {action!r}."}


SURFACE = NativeSurface()
