"""The native macOS automation surface: any running app, driven through its accessibility tree.

The model drives it through one tool, ``control_screen``, whose script both reads and acts:
**``find_one``/``find_many``** read the app into a flat set of elements (via
:meth:`NativeSurface.documents`), each keyed by its position in the accessibility tree, and
retrieval ranks them against the model's plain-language query; the acting primitives then drive the
chosen elements (via :meth:`NativeSurface.perform`) with trusted input — semantic AX actions where
the element exposes them, synthesized mouse and keyboard where it does not.

Reading and acting through the accessibility tree is the accurate way to drive native UI; this
module keeps the tree walk, the AX actions, the synthesized input, and the permission gate, and
leaves perceiving (retrieval) and composing (the control sandbox) to their own layers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import ApplicationServices as AS

from frank.computer import accessibility, input_synthesis, permissions
from frank.computer.retrieval import Document, element_text
from frank.computer.surface import (
    Element, Surface, ToolFailure, message_loader, resolve_caret, resolve_range,
)
from frank.base.tuning import Tunable, active_tuning

message = message_loader("computer")

# Subroles of the standard window title-bar controls. When a read finds only these (or nothing),
# the app — typically a backgrounded Chromium/Electron app — has not built its real tree yet, so
# the read is not trustworthy and we wait for it to fill.
_WINDOW_CHROME_SUBROLES = frozenset({
    "AXCloseButton", "AXMinimizeButton", "AXFullScreenButton", "AXZoomButton",
})

# Semantic AX actions, tried before any synthesized input, split by the click count so a click maps
# to the macOS convention: one click activates (AXPress), a double click opens (AXOpen).
_ACTIVATE_ACTIONS = ("AXPress",)
_OPEN_ACTIONS = ("AXOpen", "AXConfirm", "AXPick")

# Every action reads the tree or synthesizes input, and so needs the Accessibility grant.
_NEEDS_ACCESSIBILITY = frozenset({
    "documents", "click", "type", "press", "scroll", "select", "caret", "drag", "read",
})


@dataclass
class RegistryEntry:
    pid: int
    name: str  # a readable name for action messages, not part of the returned data
    handle: Any
    path: tuple[int, ...]
    center: Optional[tuple[float, float]]


def _name_containers_from_their_contents(documents: list[Document]) -> None:
    """Give a container the text of what it contains, when it has none of its own.

    A Finder row carries no title, description or help: the file name lives in the static text
    inside it. So a listing came back as a wall of `{'id': ..., 'role': 'AXRow'}` with nothing to
    tell one row from another, and an agent asked to list a folder could see that there were
    eleven things and not what any of them were.

    The children are already in this snapshot — the tree is flattened, and a child's id is its
    parent's id with one more step — so this needs no second read of the screen.

    What a row contains is a record, not a sentence: a file name, a date, a size, a kind. They
    stay separate in ``contains``, and the first becomes the row's ``name``, because that is the
    one a person would call it. Flattening them into one string would ask every caller to guess
    where a file name ends and its modification date begins, and dropping the tail of a record
    would lose the columns without saying which.
    """
    with_text = [document for document in documents if document.text]
    if not with_text:
        return
    for document in documents:
        if document.text:
            continue
        prefix = f"{document.id}."
        parts = list(dict.fromkeys(
            other.text for other in with_text if other.id.startswith(prefix)
        ))
        if not parts:
            continue
        document.payload["name"] = parts[0]
        document.payload["contains"] = parts
        # Ranking searches `text`, so it holds every part; the structure above is what a caller
        # reads. The two serve different readers and neither has to compromise for the other.
        document.text = " ".join(parts)


def _element_name(element: accessibility.Element) -> str:
    return element.title or element.description or element.help or element.role


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
    """The macOS accessibility implementation of the shared ``Surface``. Snapshots an app into
    ranked-elsewhere documents, and performs trusted actions on the elements a search returned."""

    def __init__(self) -> None:
        super().__init__("frank-accessibility", message)
        self._last_pid: Optional[int] = None
        # id → element, rebuilt on every ``documents`` read. The id is the element's path in the
        # tree (``0.3.1``) — the platform's own address, stable within a snapshot and re-resolvable
        # afterwards — so ``control_screen`` acts on exactly what a ``find`` returned.
        self._targets: dict[str, RegistryEntry] = {}

    def recover(self, detail: str) -> dict:
        return {"ok": False, "error": message("action_failed", detail=detail)}


    # Target and element resolution.

    def _resolve_pid(self, target: str) -> int:
        if target:
            pid = accessibility.find_app_pid(target)
            if pid is None:
                raise ToolFailure({"ok": False, "error": f"App {target!r} is not running. Open it first (via the bash tool), then find it."})
            return pid
        if self._last_pid is not None:
            return self._last_pid
        pid = accessibility.frontmost_pid()
        if pid is None:
            raise ToolFailure({"ok": False, "error": "No target app given and no frontmost app found."})
        return pid

    def _entry(self, ref: str) -> RegistryEntry:
        entry = self._targets.get(ref)
        if entry is None:
            raise ToolFailure({"ok": False, "error": f"No element {ref!r}. Find the element first (find_one or find_many) to get current element ids."})
        return entry

    def _live_handle(self, entry: RegistryEntry) -> Optional[Any]:
        if accessibility.handle_is_live(entry.handle):
            return entry.handle
        rebuilt = accessibility.resolve_from_path(entry.pid, entry.path)
        if rebuilt is not None and accessibility.handle_is_live(rebuilt):
            return rebuilt
        return None

    def _ready_snapshot(self, pid: int, window: str, maximum_depth: Optional[int]) -> accessibility.Snapshot:
        """Read the app's tree, waiting out the asynchronous build a Chromium/Electron app does the
        first time its accessibility is switched on. The pre-warm watcher means the front app's tree
        is usually already up, so the common path is a single full read. When it is not — an app just
        launched, or one targeted by name that was never frontmost — a shallow readiness probe polls
        with a widening backoff until content appears past the window chrome, then the full read is
        taken. On timeout the last (possibly incomplete) read is returned for the caller to report."""
        accessibility.start_prewarm()
        kwargs: dict[str, Any] = {"window": window}
        if maximum_depth is not None:
            kwargs["maximum_depth"] = maximum_depth
        snapshot = accessibility.snapshot_app(pid, **kwargs)
        if not _is_incomplete(snapshot):
            return snapshot
        probe_depth = active_tuning().amount(Tunable.ax_ready_probe_depth) if maximum_depth is None else min(active_tuning().amount(Tunable.ax_ready_probe_depth), maximum_depth)
        deadline = time.monotonic() + active_tuning().settle_give_up()
        delay = active_tuning().settle_poll()
        while time.monotonic() < deadline:
            time.sleep(delay)
            delay = min(delay * 2, active_tuning().duration(Tunable.ax_ready_backoff_seconds))
            if self._tree_ready(pid, window, probe_depth):
                return accessibility.snapshot_app(pid, **kwargs)
        return accessibility.snapshot_app(pid, **kwargs)

    def _tree_ready(self, pid: int, window: str, probe_depth: int) -> bool:
        """A cheap, shallow read that answers one question: has the app's real tree built, or is only
        window chrome up? It reuses the overview walk at a shallow depth so readiness is judged by
        exactly the rule a full read uses (``_is_incomplete``), without paying for the deep walk on
        every poll while the tree is still building."""
        probe = accessibility.snapshot_app(pid, window=window, maximum_depth=probe_depth)
        return not _is_incomplete(probe)

    def _environment(self, pid: int) -> dict:
        """The situational awareness a person gets from a glance: this app's other windows, and
        what else is open to switch to."""
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

    # Perceiving — find.

    def preflight(self, operation: str) -> Optional[dict]:
        if operation in _NEEDS_ACCESSIBILITY and not permissions.accessibility_granted():
            return {"ok": False, "error": message("accessibility_needed"), "needs_permission": "accessibility"}
        return None

    def documents(self, target: str = "") -> dict:
        """Read the app into retrieval documents: one per element, keyed by its tree path, its text
        the element's own words. Rebuilds the id→element map that ``perform`` acts through."""

        def run() -> dict:
            pid = self._resolve_pid(target)
            snapshot = self._ready_snapshot(pid, "focused", None)
            if _is_incomplete(snapshot):
                return self.incomplete("not_ready", app=(snapshot.app_name or target or "the app"))
            self._last_pid = pid
            self._targets = {}
            documents: list[Document] = []
            for ax in snapshot.elements:
                entry = RegistryEntry(pid=pid, name=_element_name(ax), handle=ax.handle, path=ax.path, center=ax.center)
                ref = ".".join(str(step) for step in ax.path) or "root"
                self._targets[ref] = entry
                element = _to_element(ax, entry)
                text = element_text(name=element.name or "", value=element.value)
                payload: dict[str, Any] = {"role": element.role}
                if element.name:
                    payload["name"] = element.name
                if isinstance(element.value, str):
                    if element.value:
                        payload["value"] = element.value
                elif element.value is not None:
                    payload["value"] = element.value
                payload.update(element.flags)
                if element.clickable:
                    payload["clickable"] = True
                if text:
                    payload["text"] = text
                documents.append(Document(id=ref, text=text, payload=payload))
            _name_containers_from_their_contents(documents)
            result: dict[str, Any] = {
                "ok": True, "app": snapshot.app_name, "window": snapshot.window_title,
                "documents": documents,
            }
            environment = self._environment(pid)
            if environment:
                result["environment"] = environment
            return result

        return self.guard(run)

    # Acting — control_screen. ``perform`` routes one primitive call to its handler.

    def perform(self, operation: str, arguments: list, keywords: dict) -> dict:
        handler = getattr(self, f"_primitive_{operation}", None)
        if handler is None:
            return {"ok": False, "error": f"The computer surface has no '{operation}' action."}
        gate = self.preflight(operation)
        if gate is not None:
            return gate
        return handler(*arguments, **keywords)

    def _primitive_click(self, ref: str, *, button: str = "left", count: int = 1, **_: Any) -> dict:
        def run() -> dict:
            entry = self._entry(ref)
            handle = self._live_handle(entry)
            if handle is not None:
                available_actions = set(accessibility.action_names(handle))
                if button == "right" and "AXShowMenu" in available_actions:
                    if AS.AXUIElementPerformAction(handle, "AXShowMenu") == 0:
                        return {"ok": True, "did": f"Opened context menu on {entry.name!r}", "via": "ax"}
                elif button == "left":
                    preferred_actions = _OPEN_ACTIONS if count >= 2 else _ACTIVATE_ACTIONS
                    action = next((name for name in preferred_actions if name in available_actions), "")
                    if action and AS.AXUIElementPerformAction(handle, action) == 0:
                        did = f"Opened {entry.name!r}" if count >= 2 else f"Clicked {entry.name!r}"
                        return {"ok": True, "did": did, "via": "ax"}
            if entry.center is None:
                return {"ok": False, "error": f"Element {entry.name!r} exposes no action and has no on-screen position to click."}
            input_synthesis.click(entry.pid, entry.center[0], entry.center[1], clicks=count, button=button)
            return {"ok": True, "did": f"Clicked {entry.name!r}", "via": "synthesized"}

        return self.guard(run)

    def _primitive_type(self, ref: str, text: str, *, mode: str = "replace", **_: Any) -> dict:
        def run() -> dict:
            entry = self._entry(ref)
            handle = self._live_handle(entry)
            if handle is None:
                return {"ok": False, "error": f"Element {entry.name!r} is no longer available; search again."}
            if mode == "insert":
                if accessibility.set_selected_text(handle, text):
                    return {"ok": True, "did": f"Inserted {len(text)} chars", "via": "ax"}
                AS.AXUIElementSetAttributeValue(handle, accessibility.FOCUSED, True)
                time.sleep(active_tuning().duration(Tunable.focus_settle_seconds))
                input_synthesis.type_text(entry.pid, text)
                return {"ok": True, "did": f"Typed {len(text)} chars", "via": "synthesized"}
            if accessibility.attribute_settable(handle, accessibility.VALUE) \
                    and AS.AXUIElementSetAttributeValue(handle, accessibility.VALUE, text) == 0:
                landed = accessibility.text_value(handle)
                result: dict[str, Any] = {"ok": True, "did": f"Set {entry.name!r}", "via": "ax"}
                if landed is not None:
                    result["value"] = landed
                    if landed != text:
                        result["note"] = message("type_clamped")
                return result
            AS.AXUIElementSetAttributeValue(handle, accessibility.FOCUSED, True)
            time.sleep(active_tuning().duration(Tunable.focus_settle_seconds))
            input_synthesis.type_text(entry.pid, text)
            return {"ok": True, "did": f"Typed into {entry.name!r}", "via": "synthesized"}

        return self.guard(run)

    def _primitive_press(self, key: str, *, modifiers: Optional[list[str]] = None, **_: Any) -> dict:
        def run() -> dict:
            pid = self._last_pid if self._last_pid is not None else self._resolve_pid("")
            keys = modifiers or []
            if not input_synthesis.press_key(pid, key, keys):
                return {"ok": False, "error": f"{key!r} is not a key. Use a named key, a letter, or a chord: press(\"cmd+shift+g\")."}
            return {"ok": True, "did": f"Pressed {' '.join([*keys, key])}"}

        return self.guard(run)

    def _primitive_scroll(self, ref: Optional[str] = None, *, direction: str = "", **_: Any) -> dict:
        def run() -> dict:
            if ref is not None:
                entry = self._entry(ref)
                handle = self._live_handle(entry)
                if handle is not None and AS.AXUIElementPerformAction(handle, "AXScrollToVisible") == 0:
                    return {"ok": True, "did": f"Scrolled {entry.name!r} into view", "via": "ax"}
                pid = entry.pid
            else:
                pid = self._last_pid if self._last_pid is not None else self._resolve_pid("")
            step = active_tuning().amount(Tunable.scroll_amount_pixels)
            vectors = {"up": (0, step), "down": (0, -step), "left": (step, 0), "right": (-step, 0)}
            if direction not in vectors:
                return {"ok": False, "error": "Give an element to bring into view, or a direction (up, down, left, right)."}
            delta_x, delta_y = vectors[direction]
            input_synthesis.scroll(pid, delta_x, delta_y)
            return {"ok": True, "did": f"Scrolled {direction}"}

        return self.guard(run)

    def _primitive_select(self, ref: str, *, text: Optional[str] = None, to_text: Optional[str] = None,
                   select_all: bool = False, occurrence: int = 1, **_: Any) -> dict:
        def run() -> dict:
            entry = self._entry(ref)
            handle = self._live_handle(entry)
            if handle is None:
                return {"ok": False, "error": f"Element {entry.name!r} is no longer available; search again."}
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
                return {"ok": True, "did": f"Selected {length} chars", "via": "ax"}
            return {"ok": False, "error": message("select_unsupported")}

        return self.guard(run)

    def _primitive_caret(self, ref: str, *, before: Optional[str] = None, after: Optional[str] = None,
                  at_offset: Optional[int] = None, edge: str = "", occurrence: int = 1, **_: Any) -> dict:
        def run() -> dict:
            entry = self._entry(ref)
            handle = self._live_handle(entry)
            if handle is None:
                return {"ok": False, "error": f"Element {entry.name!r} is no longer available; search again."}
            content = accessibility.text_value(handle)
            if content is None:
                return {"ok": False, "error": "This element holds no editable text."}
            offset = resolve_caret(
                content, before=before, after=after, at_offset=at_offset,
                to_start=edge == "start", to_end=edge == "end", occurrence=occurrence,
            )
            if accessibility.set_selected_range(handle, offset, 0):
                return {"ok": True, "did": f"Caret at {offset}", "via": "ax"}
            return {"ok": False, "error": message("select_unsupported")}

        return self.guard(run)

    def _primitive_drag(self, ref: str, to_element: Optional[str] = None, *, button: str = "left", **_: Any) -> dict:
        def run() -> dict:
            if to_element is None:
                return {"ok": False, "error": "drag needs to_element — the element to drop onto."}
            source = self._entry(ref)
            target = self._entry(to_element)
            if source.center is None or target.center is None:
                return {"ok": False, "error": "Both elements need an on-screen position to drag between."}
            input_synthesis.drag(source.pid, source.center[0], source.center[1], target.center[0], target.center[1], button=button)
            return {"ok": True, "did": f"Dragged {source.name!r} onto {target.name!r}"}

        return self.guard(run)

    def _primitive_read(self, ref: Optional[str] = None, **_: Any) -> dict:
        """Read one element's text.

        `ref` is optional here because it is optional on the browser surface, and a script is
        written against `read()` — not against whichever surface happens to be answering. It
        used to be required, so `read()` on this surface raised a bare
        `TypeError: missing 1 required positional argument` out of the primitive dispatcher and
        into the transcript: a Python signature shown to someone who never called a Python
        function. A surface that cannot do something says so in words.
        """
        def run() -> dict:
            if not ref:
                return {
                    "ok": False,
                    "error": "read() needs the id of an element on this surface. Call find_one "
                             "or find_many first and pass the id you want to read.",
                }
            entry = self._entry(ref)
            handle = self._live_handle(entry)
            if handle is None:
                return {"ok": False, "error": f"Element {entry.name!r} is no longer available; search again."}
            return {"ok": True, "text": accessibility.text_value(handle) or ""}

        return self.guard(run)


SURFACE = NativeSurface()
