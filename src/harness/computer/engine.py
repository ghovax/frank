"""The native macOS automation surface: any running app, driven through the most accurate
approach available. One of the two surfaces built on ``surface.py`` — it shares that module's
serial worker, failure guard, indexed-element vocabulary, and result shapers, and adds only what
is genuinely native: the accessibility-tree walk, AX actions, synthesized input, scripting, and
the TCC permission gate.

The model works in an observe/act loop identical to the web surface's. ``observe`` reads the
accessibility tree of a named app and returns the shared indexed elements; the model then acts by
index. An index is resolved through a registry that prefers the live AX handle, falls back to
re-resolving the element from its tree path (handles go stale across relayout), and finally to a
contained coordinate click. Every action is delivered to a specific process, so the user's cursor
and keyboard are never disturbed. After an action the surface re-observes and returns the app's
actionable surface plus a ``changed`` flag — the same honest result the web surface gives.

Accuracy, not "tiers", decides the approach. Scripting a cooperative app (``run_script``) returns
exact structured data and is the most accurate and fastest way to answer a question about its
contents. Reading and acting on the accessibility tree (``observe``/``click``/``type``/``press``/
``menu``/``scroll``/``find``) is the accurate way to drive UI. A ``screenshot`` is the least
accurate option — pixels the model has to interpret — used only when an app exposes no accessible
structure at all.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import ApplicationServices as AS

from harness.computer import accessibility, capture, input_synthesis, permissions, scripting
from harness.computer.surface import Element, Surface, ToolFailure, message_loader

message = message_loader("computer")

# Subroles of the standard window title-bar controls. When an observation contains only these,
# the app (typically a backgrounded Electron app) has not built its real tree yet.
_WINDOW_CHROME_SUBROLES = frozenset({
    "AXCloseButton", "AXMinimizeButton", "AXFullScreenButton", "AXZoomButton",
})

# AX actions that stand in for a plain left click, tried in order before any synthesized input.
_CLICK_ACTIONS = ("AXPress", "AXOpen", "AXConfirm", "AXPick")

# The actions that read or drive the accessibility tree, and so require the Accessibility grant.
# Scripting, launch, and screenshot do not (screenshot gates on Screen Recording separately).
_ACCESSIBILITY_ACTIONS = frozenset({"observe", "click", "type", "press", "menu", "scroll", "find"})

# A find walks the whole tree, so it expands far past the shallow overview depth.
_FIND_DEPTH = 64
_FIND_LIMIT = 25


@dataclass
class RegistryEntry:
    pid: int
    name: str  # a readable name for action messages, not part of the returned data
    handle: Any
    path: tuple[int, ...]
    center: Optional[tuple[float, float]]


def _element_name(element: accessibility.Element) -> str:
    return element.title or element.description or element.help or element.role


def _to_element(index: int, ax: accessibility.Element) -> Element:
    """Convert a raw accessibility node into the shared, cross-surface ``Element``. The three AX
    name attributes collapse into one ``name``; a node is clickable when it exposes any AX action
    (text and pure regions expose none); a region carries its on-screen child count so the model
    can drill; only the notable states become flags."""
    flags: dict[str, Any] = {}
    if ax.enabled is False:
        flags["enabled"] = False
    if ax.selected:
        flags["selected"] = True
    return Element(
        index=index,
        role=ax.role,
        name=ax.title or ax.description or ax.help,
        value=ax.value,
        clickable=bool(ax.actions),
        flags=flags,
        children=ax.child_count,
        actions=ax.actions,
    )


def _diff(previous: list[dict], current: list[dict]) -> Optional[dict]:
    """A structured diff between two observations of the same app, keyed by the element's identity
    (role + name) so a shifted index is not mistaken for a change. Returns appeared/disappeared/
    changed element objects, or None when nothing moved."""
    def identity(element: dict) -> tuple:
        return (element["role"], element.get("name", ""))

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


class NativeSurface(Surface):
    """The macOS accessibility implementation of the shared ``Surface``. Holds the index registry
    and change-detection baselines (worker-thread-only) and exposes the computer actions."""

    def __init__(self) -> None:
        super().__init__("daisy-accessibility", message)
        # An index always refers to the most recent observation, matching how the model reasons
        # ("observe, then act on what I saw"). Replaced on every observe.
        self._registry: dict[int, RegistryEntry] = {}
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
                raise ToolFailure({"ok": False, "error": f"App {target!r} is not running. Launch it first with action='launch'."})
            return pid
        if self._last_pid is not None:
            return self._last_pid
        pid = accessibility.frontmost_pid()
        if pid is None:
            raise ToolFailure({"ok": False, "error": "No target app given and no frontmost app found."})
        return pid

    def _resolve_element(self, index: int) -> tuple[RegistryEntry, Optional[Any]]:
        """Return (entry, live_handle_or_None); the live handle is the current AX element to act
        on, None means fall back to the entry's coordinates. Raises ``ToolFailure`` for an unknown
        index."""
        entry = self._registry.get(index)
        if entry is None:
            raise ToolFailure({"ok": False, "error": f"No element at index {index}. Run action='observe' first."})
        if accessibility.handle_is_live(entry.handle):
            return entry, entry.handle
        # Handle went stale (the app relayouted). Re-resolve from the tree path.
        rebuilt = accessibility.resolve_from_path(entry.pid, entry.path)
        if rebuilt is not None and accessibility.handle_is_live(rebuilt):
            return entry, rebuilt
        # Last resort: coordinates (a contained click still lands on the right spot).
        return entry, None

    # Observation and shaping.

    def _register(self, pid: int, raw_elements: list[accessibility.Element]) -> None:
        self._registry = {
            index: RegistryEntry(
                pid=pid, name=_element_name(ax), handle=ax.handle, path=ax.path, center=ax.center,
            )
            for index, ax in enumerate(raw_elements)
        }
        self._last_pid = pid

    @staticmethod
    def _payloads(raw_elements: list[accessibility.Element]) -> list[dict]:
        return [_to_element(index, ax).payload() for index, ax in enumerate(raw_elements)]

    def _record(self, pid: int, payloads: list[dict], *, track: bool) -> Optional[dict]:
        """Update the per-pid change-detection baseline and return the diff against the previous
        observation, or None. A drill is a fresh sub-view, so it never becomes the baseline."""
        if not track:
            return None
        previous = self._last_elements.get(pid)
        self._last_elements[pid] = payloads
        return _diff(previous, payloads) if previous is not None else None

    def _reobserve(self, pid: int, *, window: str = "focused", track: bool = True) -> tuple[accessibility.Snapshot, list[dict], Optional[dict]]:
        snapshot = accessibility.snapshot_app(pid, window=window)
        self._register(pid, snapshot.elements)
        payloads = self._payloads(snapshot.elements)
        changes = self._record(pid, payloads, track=track)
        return snapshot, payloads, changes

    def _act_result(self, pid: int, result: dict) -> dict:
        """Finish an acting call the way the web surface does: re-observe the app, attach its
        actionable surface, and set an honest ``changed`` flag."""
        snapshot, payloads, changes = self._reobserve(pid, track=True)
        digest = self.digest(
            context=snapshot, elements=payloads, prose_note_name="digest_prose",
            empty_hint=message("empty_observation"),
        )
        result.update(digest)
        result["changed"] = changes is not None
        if changes is None and "note" not in result:
            result["note"] = message("no_change")
        return result

    # Actions.

    def observe(self, target: str = "", window: str = "focused", element: Optional[int] = None) -> dict:
        """Read an app's UI shallow-first. With no ``element``, the top-level overview (controls
        and text near the surface, deep containers as addressable regions with a child count). With
        ``element`` set to a region's index from the last observe, drill in and expand that region.
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

    def _observe_region(self, index: int) -> dict:
        """Re-root the walk at a previously-seen region and expand it, again shallow-first. A drill
        is a fresh sub-view: it neither diffs nor overwrites the app's diff baseline."""
        entry = self._registry.get(index)
        if entry is None:
            return {"ok": False, "error": f"No element at index {index}. Run action='observe' first."}
        root_handle = entry.handle if accessibility.handle_is_live(entry.handle) else \
            accessibility.resolve_from_path(entry.pid, entry.path)
        if root_handle is None:
            return {"ok": False, "error": f"Region {index} is no longer available; observe the app again."}
        snapshot = accessibility.snapshot_app(entry.pid, root_handle=root_handle, root_path=entry.path)
        self._register(entry.pid, snapshot.elements)
        payloads = self._payloads(snapshot.elements)
        result = self.overview(
            context=snapshot, elements=payloads, empty_hint=message("empty_observation"),
        )
        result["did"] = f"Expanded element {index}"
        return result

    def find(self, query: str, target: str = "") -> dict:
        """Search the target app's on-screen tree for elements whose name or value contains
        ``query``, case-insensitively. Clickable matches first; every match is registered so it can
        be acted on by index."""

        def run() -> dict:
            needle = query.strip().lower()
            if not needle:
                return {"ok": False, "error": "The find action needs non-empty text to look for."}
            pid = self._resolve_pid(target)
            snapshot = accessibility.snapshot_app(pid, window="focused", maximum_depth=_FIND_DEPTH)
            raw = snapshot.elements
            matched = [
                (index, ax) for index, ax in enumerate(raw)
                if needle in _element_name(ax).lower()
                or (isinstance(ax.value, str) and needle in ax.value.lower())
            ]
            matched.sort(key=lambda pair: (0 if pair[1].actions else 1, pair[0]))
            truncated = len(matched) > _FIND_LIMIT
            matched = matched[:_FIND_LIMIT]
            self._registry = {}
            listed: list[dict] = []
            for position, (_, ax) in enumerate(matched):
                self._registry[position] = RegistryEntry(
                    pid=pid, name=_element_name(ax), handle=ax.handle, path=ax.path, center=ax.center,
                )
                payload = _to_element(position, ax).payload()
                listed.append(payload)
            self._last_pid = pid
            result: dict[str, Any] = {
                "ok": True, "app": snapshot.app_name, "window": snapshot.window_title,
                "query": query, "count": len(listed), "elements": listed,
            }
            if truncated:
                result["truncated"] = True
                result["note"] = message("find_truncated", limit=str(_FIND_LIMIT))
            if not listed:
                result["note"] = message("find_no_match")
            return result

        return self.guard(run)

    def click(self, index: int, *, clicks: int = 1, button: str = "left") -> dict:
        def run() -> dict:
            entry, handle = self._resolve_element(index)
            # Prefer a semantic AX action (no pointer movement at all) for a plain left click.
            if handle is not None and clicks == 1 and button == "left":
                available = set(accessibility.action_names(handle))
                action = next((name for name in _CLICK_ACTIONS if name in available), "")
                if action and AS.AXUIElementPerformAction(handle, action) == 0:
                    return self._act_result(entry.pid, {"ok": True, "did": f"Clicked {entry.name!r}", "via": "ax"})
            # Fall back to a contained synthesized click at the element's center.
            if entry.center is None:
                return {"ok": False, "error": f"Element {entry.name!r} exposes no press action and has no on-screen position to click."}
            center_x, center_y = entry.center
            input_synthesis.click(entry.pid, center_x, center_y, clicks=clicks, button=button)
            # No semantic AX action existed, so this was a blind positional click: it lands on the
            # spot but cannot confirm the element did anything. The re-observed surface below lets
            # the model check; the note flags that the click itself was unconfirmed.
            return self._act_result(entry.pid, {
                "ok": True,
                "did": f"Clicked {entry.name!r} at ({round(center_x)}, {round(center_y)})",
                "via": "synthesized",
                "note": message("click_positional"),
            })

        return self.guard(run)

    def set_text(self, index: int, text: str, *, replace: bool = True) -> dict:
        def run() -> dict:
            entry, handle = self._resolve_element(index)
            if handle is not None and replace:
                settable_error, settable = AS.AXUIElementIsAttributeSettable(handle, accessibility.VALUE, None)
                if settable_error == 0 and settable and AS.AXUIElementSetAttributeValue(handle, accessibility.VALUE, text) == 0:
                    return self._act_result(entry.pid, {"ok": True, "did": f"Set {entry.name!r} to {text[:60]!r}", "via": "ax"})
            # Focus then type into the target process (contained). Focus via AX if we can.
            if handle is not None:
                AS.AXUIElementSetAttributeValue(handle, accessibility.FOCUSED, True)
            elif entry.center is not None:
                center_x, center_y = entry.center
                input_synthesis.click(entry.pid, center_x, center_y)
            time.sleep(0.03)
            input_synthesis.type_text(entry.pid, text)
            return self._act_result(entry.pid, {
                "ok": True, "did": f"Typed into {entry.name!r}", "via": "synthesized",
                "note": message("type_synthesized"),
            })

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

    def scroll(self, direction: str, amount: int = 300, target: str = "") -> dict:
        def run() -> dict:
            pid = self._resolve_pid(target)
            vectors = {"up": (0, amount), "down": (0, -amount), "left": (amount, 0), "right": (-amount, 0)}
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

    def run_script(self, source: str, language: str = "applescript") -> dict:
        return self.guard(lambda: scripting.run_script(source, language=language))

    def launch(self, name: str, arguments: Optional[list[str]] = None) -> dict:
        def run() -> dict:
            result = scripting.launch_app(name, args=arguments, new_instance=bool(arguments))
            if result.get("ok"):
                time.sleep(0.6)  # give the app a moment to create its window before an observe
                return {"ok": True, "did": f"Launched {name}"}
            return {"ok": False, "error": result.get("error") or "Launch failed."}

        return self.guard(run)

    # Dispatch.

    #: Actions whose result carries an ``image_path`` for the model-image side channel.
    screenshot_actions = frozenset({"screenshot"})

    def preflight(self, action: str) -> Optional[dict]:
        """The AX-tree actions need the Accessibility grant; a clear, actionable error beats a
        silent empty result. Scripting, launch, and screenshot gate elsewhere (screenshot checks
        Screen Recording inside itself)."""
        if action in _ACCESSIBILITY_ACTIONS and not permissions.accessibility_granted():
            return {
                "ok": False,
                "error": message("accessibility_needed"),
                "needs_permission": "accessibility",
            }
        return None

    def dispatch(self, action: str, arguments: dict) -> dict:
        app = str(arguments.get("app", ""))
        element = arguments.get("element")
        text = str(arguments.get("text", ""))
        window = str(arguments.get("window", "focused") or "focused")
        language = str(arguments.get("language", "applescript") or "applescript")
        clicks = int(arguments.get("clicks", 1) or 1)
        button = str(arguments.get("button", "left") or "left")
        key = str(arguments.get("key", ""))
        modifiers = arguments.get("modifiers") or []
        menu_path = arguments.get("menu_path") or []
        direction = str(arguments.get("direction", ""))
        launch_arguments = arguments.get("arguments") or []
        index = int(element) if element is not None else None
        if action == "observe":
            return self.observe(app, window, element=index)
        if action == "find":
            query = str(arguments.get("query", ""))
            if not query.strip():
                return {"ok": False, "error": "The find action needs a query — the text to look for."}
            return self.find(query, app)
        if action in ("click", "type"):
            if index is None:
                return {"ok": False, "error": f"The {action} action needs an element index from the last observe."}
            if action == "click":
                return self.click(index, clicks=clicks, button=button)
            return self.set_text(index, text)
        if action == "press":
            if not key:
                return {"ok": False, "error": "The press action needs a key (e.g. return, tab, or a chord)."}
            return self.press_key(key, modifiers, app)
        if action == "menu":
            return self.open_menu(menu_path, app)
        if action == "scroll":
            return self.scroll(direction or "down", target=app)
        if action == "screenshot":
            return self.screenshot(app)
        if action == "run_script":
            return self.run_script(text, language)
        if action == "launch":
            return self.launch(app, launch_arguments)
        return {"ok": False, "error": f"Unknown action {action!r}."}


SURFACE = NativeSurface()
