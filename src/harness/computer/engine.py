"""The native macOS automation surface: any running app, driven through its accessibility tree.

One of the two surfaces built on ``surface.py`` — it shares that module's serial worker, its
stuck/readiness tracking, the ref-addressed element vocabulary, and the result shapers, and adds
only what is genuinely native: the accessibility-tree walk, AX actions, synthesized input, and the
permission gate.

The model works the way a person does: **look** at the screen (``observe``), and use the two
things a person has — a **pointer** (``pointer``) and a **keyboard** (``press``/``type``, with
``select``/``caret`` for placing the cursor and selecting text by content, and ``scroll`` to bring
things into view). A ``screenshot`` is the last resort, and only for *seeing* — when an app exposes
nothing to read — never for clicking blind. Everything else a person does at a Mac (launch or switch
apps, run a script) is a one-liner through the ``bash`` tool, not an action here.

Reading and acting on the accessibility tree is the accurate way to drive UI; each action re-reads
the app and reports what changed, so the model always sees the effect of what it did.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import ApplicationServices as AS

from harness.computer import accessibility, capture, input_synthesis, permissions
from harness.computer.surface import (
    Element, ElementRegistry, Surface, ToolFailure, diff_elements, message_loader,
    resolve_caret, resolve_range,
)
from harness.core.tuning import Limit, active_tuning, settle

message = message_loader("computer")

# Subroles of the standard window title-bar controls. When a read finds only these (or nothing),
# the app — typically a backgrounded Chromium/Electron app — has not built its real tree yet, so
# the read is not trustworthy and we wait for it to fill.
_WINDOW_CHROME_SUBROLES = frozenset({
    "AXCloseButton", "AXMinimizeButton", "AXFullScreenButton", "AXZoomButton",
})

# Semantic AX actions, tried before any synthesized input, split by the model's click count so a
# click maps to the macOS convention: one click activates (AXPress), a double click opens (AXOpen).
_ACTIVATE_ACTIONS = ("AXPress",)
_OPEN_ACTIONS = ("AXOpen", "AXConfirm", "AXPick")

# The actions that read the tree or synthesize input, and so need the Accessibility grant;
# screenshot gates on Screen Recording instead.
_ACCESSIBILITY_ACTIONS = frozenset({
    "observe", "pointer", "press", "type", "select", "caret", "scroll",
})

# A query/menu walk goes deep, well past the shallow overview depth.
_FIND_DEPTH = 64


@dataclass
class RegistryEntry:
    pid: int
    name: str  # a readable name for action messages, not part of the returned data
    handle: Any
    path: tuple[int, ...]
    center: Optional[tuple[float, float]]


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_ref(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value).strip()


def _element_name(element: accessibility.Element) -> str:
    return element.title or element.description or element.help or element.role


def _app_signature(pid: int) -> int:
    """A cheap, comparable signature of an app's current UI — the number of elements in a shallow
    focused-window snapshot — used to wait out a change (a menu appearing) until it settles."""
    try:
        return len(accessibility.snapshot_app(pid, window="focused").elements)
    except Exception:
        return -1


def _is_incomplete(snapshot: accessibility.Snapshot) -> bool:
    """Whether a read produced nothing usable: an empty tree, or only window-chrome controls (a
    Chromium/Electron app whose real tree has not built yet). Such a read is not acted on."""
    if not snapshot.elements:
        return True
    return all(ax.subrole in _WINDOW_CHROME_SUBROLES for ax in snapshot.elements)


def _to_element(ax: accessibility.Element, token: RegistryEntry) -> Element:
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
    """The macOS accessibility implementation of the shared ``Surface``."""

    def __init__(self) -> None:
        super().__init__("daisy-accessibility", message)
        self.registry = ElementRegistry()
        self._last_pid: Optional[int] = None
        self._last_elements: dict[int, list[dict]] = {}

    def recover(self, detail: str) -> dict:
        return {"ok": False, "error": message("action_failed", detail=detail)}

    def location_fields(self, snapshot: accessibility.Snapshot) -> dict:
        return {"app": snapshot.app_name, "window": snapshot.window_title}

    # Target and element resolution.

    def _resolve_pid(self, target: str) -> int:
        if target:
            pid = accessibility.find_app_pid(target)
            if pid is None:
                raise ToolFailure({"ok": False, "error": f"App {target!r} is not running. Open it first (via the bash tool), then observe."})
            return pid
        if self._last_pid is not None:
            return self._last_pid
        pid = accessibility.frontmost_pid()
        if pid is None:
            raise ToolFailure({"ok": False, "error": "No target app given and no frontmost app found."})
        return pid

    def _resolve_element(self, ref: str) -> tuple[RegistryEntry, Optional[Any]]:
        entry = self.registry.token(ref)
        if entry is None:
            raise ToolFailure({"ok": False, "error": f"No element {ref!r}. Observe first to get current refs."})
        if accessibility.handle_is_live(entry.handle):
            return entry, entry.handle
        rebuilt = accessibility.resolve_from_path(entry.pid, entry.path)
        if rebuilt is not None and accessibility.handle_is_live(rebuilt):
            return entry, rebuilt
        return entry, None

    def _element_center(self, ref: str) -> tuple[int, float, float]:
        entry, _ = self._resolve_element(ref)
        if entry.center is None:
            raise ToolFailure({"ok": False, "error": f"Element {entry.name!r} has no on-screen position to point at."})
        return entry.pid, entry.center[0], entry.center[1]

    def _text_target(self, ref: Optional[str]) -> tuple[int, Any]:
        if ref is not None:
            entry, handle = self._resolve_element(ref)
            if handle is None:
                raise ToolFailure({"ok": False, "error": f"Element {entry.name!r} is no longer live; observe again."})
            return entry.pid, handle
        if self._last_pid is None:
            raise ToolFailure({"ok": False, "error": "No app to act on. Observe one first, or give an element."})
        handle = accessibility.focused_element(self._last_pid)
        if handle is None:
            raise ToolFailure({"ok": False, "error": "No text field is focused. Give the element ref of the field."})
        return self._last_pid, handle

    # Observation and shaping.

    def _bind(self, pid: int, raw_elements: list[accessibility.Element]) -> list[dict]:
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
        if not track:
            return None
        previous = self._last_elements.get(pid)
        self._last_elements[pid] = payloads
        return diff_elements(previous, payloads) if previous is not None else None

    def _act_result(self, pid: int, result: dict) -> dict:
        """Finish an acting call: re-read the app and lead with the diff of what changed."""
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

    def _ready_snapshot(self, pid: int, window: str, maximum_depth: Optional[int]) -> accessibility.Snapshot:
        """Read the app's tree, waiting for it to actually build. Chromium/Electron apps expose
        only window chrome until their tree is asked for and then built asynchronously, so a cold
        first read catches an empty shell; poll until it fills or the settle ceiling elapses."""
        kwargs: dict[str, Any] = {"window": window}
        if maximum_depth is not None:
            kwargs["maximum_depth"] = maximum_depth
        snapshot = accessibility.snapshot_app(pid, **kwargs)
        if not _is_incomplete(snapshot):
            return snapshot
        deadline = time.monotonic() + active_tuning().settle_ceiling()
        interval = active_tuning().settle_interval()
        while time.monotonic() < deadline:
            time.sleep(interval)
            snapshot = accessibility.snapshot_app(pid, **kwargs)
            if not _is_incomplete(snapshot):
                return snapshot
        return snapshot

    def _environment(self, pid: int) -> dict:
        """The situational awareness a person gets from a glance: this app's other windows (so a
        reader window is not mistaken for the main one), and what else is open to switch to."""
        env: dict[str, Any] = {}
        windows = accessibility.window_titles(pid)
        if len(windows) > 1:
            env["windows"] = windows
        running = accessibility.running_app_names()
        if running:
            env["running_apps"] = running
        frontmost = accessibility.frontmost_pid()
        if frontmost is not None:
            name = accessibility.app_name_for_pid(frontmost)
            if name:
                env["frontmost"] = name
        return env

    # Actions.

    def observe(self, target: str = "", window: str = "focused", element: Optional[str] = None, query: str = "") -> dict:
        """Look at an app. With no arguments, the app's UI shallow-first (controls and text near the
        surface, deep containers as addressable regions). ``element`` drills into a region; ``query``
        searches the whole tree for matching text; ``window`` picks a window ("focused", "all", the
        special "menu" for the menu bar, or a window's title). Each read reports the app, its
        windows, what else is open, and — versus the last look — what changed."""

        def run() -> dict:
            if element is not None:
                return self._observe_region(element)
            pid = self._resolve_pid(target)
            if query.strip():
                return self._search(pid, query, window)
            maximum_depth = _FIND_DEPTH if window == "menu" else None
            snapshot = self._ready_snapshot(pid, window, maximum_depth)
            if _is_incomplete(snapshot):
                return self.incomplete("not_ready", app=(snapshot.app_name or target or "the app"))
            payloads = self._bind(pid, snapshot.elements)
            changes = self._record(pid, payloads, track=True)
            result = self.overview(context=snapshot, elements=payloads, empty_hint=message("empty_observation"))
            environment = self._environment(pid)
            if environment:
                result["environment"] = environment
            if changes:
                result["changes_since_last_observe"] = changes
            return result

        return self.guard(run)

    def _observe_region(self, ref: str) -> dict:
        entry = self.registry.token(ref)
        if entry is None:
            return {"ok": False, "error": f"No element {ref!r}. Observe first to get current refs."}
        root_handle = entry.handle if accessibility.handle_is_live(entry.handle) else \
            accessibility.resolve_from_path(entry.pid, entry.path)
        if root_handle is None:
            return {"ok": False, "error": f"Region {ref!r} is no longer available; observe the app again."}
        snapshot = accessibility.snapshot_app(entry.pid, root_handle=root_handle, root_path=entry.path)
        payloads = self._bind(entry.pid, snapshot.elements)
        result = self.overview(context=snapshot, elements=payloads, empty_hint=message("empty_observation"))
        result["did"] = f"Expanded element {ref}"
        return result

    def _search(self, pid: int, query: str, window: str) -> dict:
        needle = query.strip().lower()
        snapshot = accessibility.snapshot_app(pid, window=window, maximum_depth=_FIND_DEPTH)
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
        self._reset_progress()
        self._readable = bool(elements)
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

    def pointer(self, element: Optional[str], *, gesture: str = "click", to_element: Optional[str] = None,
                clicks: int = 1, button: str = "left") -> dict:
        """Point at an element the way a mouse does: click it (once or twice), right-click it, hover
        over it, or drag it onto another element. Targets an element by ref — never a raw screen
        point (looking at pixels and guessing coordinates is not how this works)."""

        def run() -> dict:
            if element is None:
                return {"ok": False, "error": "pointer needs an element ref to act on."}
            if gesture == "hover":
                pid, point_x, point_y = self._element_center(element)
                input_synthesis.move(pid, point_x, point_y)
                settle(lambda: _app_signature(pid))
                return self._act_result(pid, {"ok": True, "did": f"Hovered element {element}"})
            if gesture == "drag":
                if to_element is None:
                    return {"ok": False, "error": "A drag needs to_element — the element to drop onto."}
                pid, start_x, start_y = self._element_center(element)
                _, end_x, end_y = self._element_center(to_element)
                input_synthesis.drag(pid, start_x, start_y, end_x, end_y, button=button)
                return self._act_result(pid, {"ok": True, "did": f"Dragged {element} onto {to_element}"})
            # A click. Prefer the semantic AX action (no pointer movement): right-click opens the
            # element's context menu, a single left click activates, a double click opens.
            entry, handle = self._resolve_element(element)
            hint = None
            if handle is not None:
                available = set(accessibility.action_names(handle))
                if button == "right" and "AXShowMenu" in available:
                    if AS.AXUIElementPerformAction(handle, "AXShowMenu") == 0:
                        return self._act_result(entry.pid, {"ok": True, "did": f"Opened context menu on {entry.name!r}", "via": "ax"})
                elif button == "left":
                    wanted = _OPEN_ACTIONS if clicks >= 2 else _ACTIVATE_ACTIONS
                    action = next((name for name in wanted if name in available), "")
                    if action and AS.AXUIElementPerformAction(handle, action) == 0:
                        did = f"Opened {entry.name!r}" if clicks >= 2 else f"Clicked {entry.name!r}"
                        return self._act_result(entry.pid, {"ok": True, "did": did, "via": "ax"})
                    if clicks == 1 and available & set(_OPEN_ACTIONS):
                        hint = message("click_openable")
            if entry.center is None:
                return {"ok": False, "error": f"Element {entry.name!r} exposes no action and has no on-screen position to click."}
            input_synthesis.click(entry.pid, entry.center[0], entry.center[1], clicks=clicks, button=button)
            result: dict[str, Any] = {
                "ok": True, "did": f"Clicked {entry.name!r}", "via": "synthesized", "note": message("click_positional"),
            }
            if hint:
                result["hint"] = hint
            return self._act_result(entry.pid, result)

        return self.guard(run, acting=True)

    def press_key(self, key: str, modifiers: Optional[list[str]] = None, target: str = "") -> dict:
        """Press a key or chord — a named key (return, tab, escape, arrows, f-keys) or a shortcut
        (a letter/digit with Cmd/Option/Ctrl/Shift). This is how the model copies (Cmd+C), selects
        all (Cmd+A), finds (Cmd+F), switches apps (Cmd+Tab), and so on."""

        def run() -> dict:
            pid = self._resolve_pid(target)
            keys = modifiers or []
            if not input_synthesis.press_key(pid, key, keys):
                return {"ok": False, "error": f"{key!r} is not a key I can press. Use a named key (return, tab, escape, arrows, f1…) or a single letter/digit, with optional modifiers."}
            combo = " ".join([*keys, key])
            return self._act_result(pid, {"ok": True, "did": f"Pressed {combo}"})

        return self.guard(run, acting=True)

    def set_text(self, ref: Optional[str], text: str, *, mode: str = "replace") -> dict:
        """Enter text. ``replace`` rewrites the whole field; ``insert`` inserts at the caret
        (replacing any selection). To change part of a field, select the text and type over it."""

        def run() -> dict:
            if mode == "insert":
                pid, handle = self._text_target(ref)
                if accessibility.set_selected_text(handle, text):
                    return self._text_result(f"Inserted {len(text)} chars", via="ax")
                AS.AXUIElementSetAttributeValue(handle, accessibility.FOCUSED, True)
                time.sleep(active_tuning().duration(Limit.FOCUS_SETTLE_SECONDS))
                input_synthesis.type_text(pid, text)
                return self._text_result(f"Typed {len(text)} chars", via="synthesized", note=message("type_synthesized"))
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

        return self.guard(run, acting=True)

    def select(self, ref: Optional[str] = None, *, text: Optional[str] = None, to_text: Optional[str] = None,
               select_all: bool = False, occurrence: int = 1) -> dict:
        """Select text in a field, addressed by content: a substring (``text``), a range from
        ``text`` through ``to_text``, or the whole field (``select_all``). Uses the accessible
        selection range; if the field doesn't support it, press Cmd+A / use the shortcuts instead."""

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
            return {"ok": False, "error": message("select_unsupported")}

        return self.guard(run, acting=True)

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

        return self.guard(run, acting=True)

    def _text_result(self, did: str, *, via: str = "ax", **extra: Any) -> dict:
        return {"ok": True, "did": did, "via": via, **extra}

    def scroll(self, direction: str = "", element: Optional[str] = None, target: str = "") -> dict:
        """Reveal content: bring an ``element`` into view (the reliable way), or scroll the app in a
        ``direction`` (up, down, left, right)."""

        def run() -> dict:
            if element is not None:
                entry, handle = self._resolve_element(element)
                if handle is not None and AS.AXUIElementPerformAction(handle, "AXScrollToVisible") == 0:
                    return self._act_result(entry.pid, {"ok": True, "did": f"Scrolled {entry.name!r} into view", "via": "ax"})
                pid = entry.pid
            else:
                pid = self._resolve_pid(target)
            step = active_tuning().amount(Limit.SCROLL_AMOUNT_PIXELS)
            vectors = {"up": (0, step), "down": (0, -step), "left": (step, 0), "right": (-step, 0)}
            if direction not in vectors:
                return {"ok": False, "error": "Give an element to bring into view, or a direction (up, down, left, right)."}
            delta_x, delta_y = vectors[direction]
            input_synthesis.scroll(pid, delta_x, delta_y)
            return self._act_result(pid, {"ok": True, "did": f"Scrolled {direction}"})

        return self.guard(run, acting=True)

    def screenshot(self, target: str = "") -> dict:
        """See an app as pixels — the last resort, only when it exposes nothing to read. This is
        for *looking* (to understand, and to tell the user what to do); it is not a way to act."""

        def run() -> dict:
            if not self.pixels_allowed():
                return {"ok": False, "error": message("screenshot_gated")}
            pid = self._resolve_pid(target)
            if not permissions.screen_recording_granted():
                return {"ok": False, "error": message("screen_recording_needed"), "needs_permission": "screen_recording"}
            path = capture.capture_window(pid)
            if path is None:
                return {"ok": False, "error": "Could not capture the app's window."}
            return {"ok": True, "image_path": path, "app": accessibility.app_name_for_pid(pid), "note": message("screenshot_observe_only")}

        return self.guard(run)

    # Dispatch.

    screenshot_actions = frozenset({"screenshot"})

    def preflight(self, action: str) -> Optional[dict]:
        if action in _ACCESSIBILITY_ACTIONS and not permissions.accessibility_granted():
            return {"ok": False, "error": message("accessibility_needed"), "needs_permission": "accessibility"}
        return None

    def dispatch(self, action: str, arguments: dict) -> dict:
        app = str(arguments.get("app", ""))
        window = str(arguments.get("window", "focused") or "focused")
        ref = _as_ref(arguments.get("element"))
        if action == "observe":
            return self.observe(app, window, element=ref, query=str(arguments.get("query", "")))
        if action == "pointer":
            return self.pointer(
                ref, gesture=str(arguments.get("gesture", "click") or "click"),
                to_element=_as_ref(arguments.get("to_element")),
                clicks=int(arguments.get("clicks", 1) or 1),
                button=str(arguments.get("button", "left") or "left"),
            )
        if action == "press":
            key = str(arguments.get("key", ""))
            if not key:
                return {"ok": False, "error": "The press action needs a key or chord."}
            return self.press_key(key, arguments.get("modifiers") or [], app)
        if action == "type":
            return self.set_text(ref, str(arguments.get("text", "")), mode=str(arguments.get("mode", "replace") or "replace"))
        if action == "select":
            return self.select(
                ref, text=str(arguments.get("text", "")) or None, to_text=str(arguments.get("to_text", "")) or None,
                select_all=bool(arguments.get("select_all", False)), occurrence=int(arguments.get("occurrence", 1) or 1),
            )
        if action == "caret":
            return self.caret(
                ref, before=str(arguments.get("before", "")) or None, after=str(arguments.get("after", "")) or None,
                at_offset=_as_int(arguments.get("at_offset")), edge=str(arguments.get("edge", "")),
                occurrence=int(arguments.get("occurrence", 1) or 1),
            )
        if action == "scroll":
            return self.scroll(str(arguments.get("direction", "")), element=ref, target=app)
        if action == "screenshot":
            return self.screenshot(app)
        return {"ok": False, "error": f"Unknown action {action!r}."}


SURFACE = NativeSurface()
