"""The native macOS surface: any running app, read and driven through its accessibility tree."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import ApplicationServices as AS

from langmesh.computer import accessibility, input_synthesis, permissions
from langmesh.computer.retrieval import Document, element_text, text_or_fallback
from langmesh.computer.surface import (
    Element,
    Glance,
    Surface,
    ToolFailure,
    message_loader,
    resolve_caret,
    resolve_range,
)
from langmesh.base.tuning import Tunable, active_tuning

message = message_loader("computer")

# The standard window title-bar controls; a read finding only these has met a tree that has not built yet.
_WINDOW_CHROME_SUBROLES = frozenset(
    {
        "AXCloseButton",
        "AXMinimizeButton",
        "AXFullScreenButton",
        "AXZoomButton",
    }
)

# Semantic actions tried before any synthesized input, split so one click activates and two open.
_ACTIVATE_ACTIONS = ("AXPress",)
_OPEN_ACTIONS = ("AXOpen", "AXConfirm", "AXPick")

# Every action reads the tree or synthesizes input, and so needs the Accessibility grant.
_NEEDS_ACCESSIBILITY = frozenset(
    {
        "documents",
        "click",
        "type",
        "press",
        "scroll",
        "select",
        "caret",
        "drag",
        "read",
        "focus",
        "shortcuts",
    }
)


@dataclass
class RegistryEntry:
    pid: int
    name: str  # a readable name for action messages, not part of the returned data
    handle: Any
    path: tuple[int, ...]
    center: Optional[tuple[float, float]]


@dataclass
class _WindowState:
    """Everything the surface knows about one target window, keyed by that target so reads cannot re-point each other."""

    pid: int
    window_id: int
    elements: dict[str, RegistryEntry] = field(default_factory=dict)


def _name_containers_from_their_contents(documents: list[Document]) -> None:
    """Give a container the text of what it contains when it has none of its own, as a Finder row does."""
    children: dict[str, list[Document]] = {}
    for document in documents:
        if document.parent:
            children.setdefault(document.parent, []).append(document)
    if not children:
        return

    def contents_of(document: Document) -> list[str]:
        """Every piece of text inside this element, nearest first, in the order it appears."""
        parts: list[str] = []
        for child in children.get(document.id, ()):
            parts.append(child.text) if child.text else parts.extend(contents_of(child))
        return parts

    for document in documents:
        if document.text or document.id not in children:
            continue
        parts = list(dict.fromkeys(part for part in contents_of(document) if part))
        if not parts:
            continue
        document.payload["name"] = parts[0]
        document.payload["contains"] = parts
        # Ranking searches `text` so it holds every part, while the structure above is what a caller reads.
        document.text = " ".join(parts)


# Roles that name a region rather than a control, which is what a person means by "in the sidebar".
_SECTION_ROLES = frozenset(
    {
        "AXWindow",
        "AXGroup",
        "AXToolbar",
        "AXTabGroup",
        "AXSplitGroup",
        "AXScrollArea",
        "AXOutline",
        "AXTable",
        "AXList",
        "AXHeading",
        "AXRadioGroup",
        "AXDrawer",
        "AXSheet",
        "AXPopover",
        "AXDisclosureTriangle",
        "AXTabPanel",
    }
)


def _sections_in(snapshot: accessibility.Snapshot) -> dict[tuple[int, ...], str]:
    """Every element that names a region, by its path, read before unnamed containers borrow their contents' words."""
    named: dict[tuple[int, ...], str] = {}
    for element in snapshot.elements:
        if element.role not in _SECTION_ROLES:
            continue
        label = element.title or element.description or element.help
        if label:
            named[tuple(element.path)] = label
    return named


def _context_for(path: tuple[int, ...], sections: dict[tuple[int, ...], str]) -> str:
    """The nearest named region enclosing this element, rather than a whole trail that would blur its descendants."""
    for depth in range(len(path) - 1, 0, -1):
        label = sections.get(tuple(path[:depth]))
        if label:
            return label
    return ""


def _element_name(element: accessibility.Element) -> str:
    # The system's own prose for the role comes before the raw role, which the embedding has never usefully seen.
    value = element.value if isinstance(element.value, str) else ""
    return (
        element.title
        or element.description
        or element.help
        or element.placeholder
        or value
        or element.role_description
        or element.role
    )


def _displayed_window(pid: int) -> Optional[tuple[int, int]]:
    """The size of a real window this process is showing, asked of the window server rather than of accessibility."""
    import Quartz

    try:
        windows = (
            Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
            or []
        )
    except Exception:  # noqa: BLE001 — a diagnosis must never be the thing that fails
        return None
    for window in windows:
        if window.get("kCGWindowOwnerPID") != pid or window.get("kCGWindowLayer"):
            continue
        bounds = window.get("kCGWindowBounds") or {}
        width, height = int(bounds.get("Width", 0)), int(bounds.get("Height", 0))
        if width > 200 and height > 200:
            return width, height
    return None


def _is_incomplete(snapshot: accessibility.Snapshot) -> bool:
    """Whether a read produced nothing usable: an empty tree, or only window chrome."""
    if not snapshot.elements:
        return True
    return all(accessible.subrole in _WINDOW_CHROME_SUBROLES for accessible in snapshot.elements)


def _to_element(accessible: accessibility.Element, token: RegistryEntry) -> Element:
    flags: dict[str, Any] = {}
    if accessible.enabled is False:
        flags["enabled"] = False
    if accessible.selected:
        flags["selected"] = True
    if accessible.placeholder:
        flags["placeholder"] = accessible.placeholder
    if accessible.role_description:
        flags["role_description"] = accessible.role_description
    return Element(
        role=accessible.role,
        # The role description last, because it names the kind of control rather than this one, but is present on every element.
        name=accessible.title
        or accessible.description
        or accessible.help
        or accessible.placeholder,
        value=accessible.value,
        clickable=bool(accessible.actions),
        flags=flags,
        children=accessible.child_count,
        actions=accessible.actions,
        token=token,
    )


class NativeSurface(Surface):
    """The macOS accessibility implementation of the shared `Surface`."""

    def __init__(self) -> None:
        super().__init__("langmesh-accessibility", message)
        # One entry per target, keyed by the element's path in the tree, which is the platform's own re-resolvable address.
        self._windows: dict[str, _WindowState] = {}

    def recover(self, detail: str) -> dict:
        return {"ok": False, "error": message("action_failed", detail=detail)}

    # Target and element resolution.

    def _state_for(self, target: str) -> _WindowState:
        """The live state of one window, resolved on first touch, with no current window and no fallback to the last."""
        from langmesh.computer import targets as target_registry

        state = self._windows.get(target)
        if state is not None:
            return state
        found = target_registry.find_window(target)
        if found is None:
            raise ToolFailure(
                {
                    "ok": False,
                    "error": message("no_such_window", target=target),
                    "targets": {"current": target_registry.describe_windows()},
                }
            )
        if not found.addressable:
            raise ToolFailure(
                {
                    "ok": False,
                    "error": message(
                        "not_a_window", app=found.app, target=target, detail=found.note
                    ),
                    "targets": {"current": target_registry.describe_windows()},
                }
            )
        state = _WindowState(
            pid=int(found.address["pid"]), window_id=int(found.address["window_number"])
        )
        self._windows[target] = state
        return state

    @staticmethod
    def _entry(state: _WindowState, ref: str) -> RegistryEntry:
        entry = state.elements.get(ref)
        if entry is None:
            raise ToolFailure(
                {
                    "ok": False,
                    "error": f"No element {ref!r}. Find the element first (find_one or find_many) to get current element ids.",
                }
            )
        return entry

    def _live_handle(self, entry: RegistryEntry) -> Optional[Any]:
        if accessibility.handle_is_live(entry.handle):
            return entry.handle
        rebuilt = accessibility.resolve_from_path(entry.pid, entry.path)
        if rebuilt is not None and accessibility.handle_is_live(rebuilt):
            return rebuilt
        return None

    def _ready_snapshot(self, pid: int, window: str) -> accessibility.Snapshot:
        """Read the app's tree, waiting out the asynchronous build an Electron app does when accessibility is first switched on."""
        accessibility.start_prewarm()
        kwargs: dict[str, Any] = {"window": window}
        snapshot = accessibility.snapshot_app(pid, **kwargs)
        if not _is_incomplete(snapshot):
            return snapshot
        deadline = time.monotonic() + active_tuning().settle_give_up()
        delay = active_tuning().settle_poll()
        while time.monotonic() < deadline:
            time.sleep(delay)
            delay = min(delay * 2, active_tuning().duration(Tunable.accessibility_ready_backoff))
            if self._tree_ready(pid, window):
                return accessibility.snapshot_app(pid, **kwargs)
        return accessibility.snapshot_app(pid, **kwargs)

    def _tree_ready(self, pid: int, window: str) -> bool:
        """A cheap read answering whether the real tree has built, bounded by a short time budget rather than a shallow depth."""
        probe = accessibility.snapshot_app(
            pid,
            window=window,
            budget_seconds=active_tuning().duration(Tunable.accessibility_ready_probe),
        )
        return not _is_incomplete(probe)

    def _environment(self, pid: int) -> dict:
        """The situational awareness a glance gives: this app's other windows, and what else is open to switch to."""
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
            return {
                "ok": False,
                "error": message("accessibility_needed"),
                "needs_permission": "accessibility",
            }
        return None

    def documents(self, target: str = "") -> dict:
        """Read the app into retrieval documents, one per element keyed by its tree path, rebuilding the map `perform` acts through."""

        def run() -> dict:
            state = self._state_for(target)
            pid = state.pid
            snapshot = self._ready_snapshot(pid, "focused")
            if _is_incomplete(snapshot):
                name = snapshot.app_name or target or "the app"
                # Not ready and never going to be ready are different facts, and the window server is what tells them apart.
                displayed = _displayed_window(pid)
                if displayed is not None:
                    width, height = displayed
                    return self.incomplete(
                        "withholds_accessibility",
                        app=name,
                        width=width,
                        height=height,
                    )
                return self.incomplete("not_ready", app=name)
            state.elements = {}
            documents: list[Document] = []
            sections = _sections_in(snapshot)
            for accessible in snapshot.elements:
                entry = RegistryEntry(
                    pid=pid,
                    name=_element_name(accessible),
                    handle=accessible.handle,
                    path=accessible.path,
                    center=accessible.center,
                )
                ref = ".".join(str(step) for step in accessible.path) or "root"
                state.elements[ref] = entry
                element = _to_element(accessible, entry)
                # `shown` is the element's own words for a reader, while `key` is what is embedded.
                shown = element_text(name=element.name or "", value=element.value)
                # What the element is called, falling back to what it says, since two thirds of native elements have no name.
                said = text_or_fallback(
                    element_text(name=element.name or ""),
                    element.value if isinstance(element.value, str) else "",
                )
                # And then the kind of control in the application's own words, which is what makes a nameless control reachable.
                kind = str(element.flags.get("role_description") or "")
                key = f"{said} {kind}".strip() if said and kind else text_or_fallback(said, kind)
                payload: dict[str, Any] = {"role": element.role}
                if ax_placeholder := element.flags.get("placeholder"):
                    payload["placeholder"] = ax_placeholder
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
                if shown:
                    payload["text"] = shown
                # Which region of the window this sits in, so a caller can narrow with `context=` here as on a page.
                context = _context_for(accessible.path, sections)
                if context:
                    payload["context"] = context
                    element.context = context
                parent = (
                    ".".join(str(step) for step in accessible.path[:-1])
                    if len(accessible.path) > 1
                    else ""
                )
                if parent:
                    payload["parent"] = parent
                # Where it is, from the same rectangle a click already uses and the target listing already reports.
                where = accessibility.rectangle(accessible.frame)
                if where is not None:
                    payload["bounds"] = where
                documents.append(Document(id=ref, text=key, payload=payload, parent=parent))
            _name_containers_from_their_contents(documents)
            result: dict[str, Any] = {
                "ok": True,
                "app": snapshot.app_name,
                "window": snapshot.window_title,
                "documents": documents,
            }
            environment = self._environment(pid)
            if environment:
                result["environment"] = environment
            return result

        return self.guard(run)

    # Acting — control_screen. ``perform`` routes one primitive call to its handler.

    def perform(self, target: str, operation: str, arguments: list, keywords: dict) -> dict:
        handler = getattr(self, f"_primitive_{operation}", None)
        if handler is None:
            from langmesh.computer import targets as target_registry

            available = ", ".join(target_registry.vocabularies()[target_registry.WINDOW_VOCABULARY])
            return {
                "ok": False,
                "error": f"A window has no {operation!r} action. It has: {available}.",
            }
        gate = self.preflight(operation)
        if gate is not None:
            return gate
        try:
            state = self._state_for(target)
        except ToolFailure as failure:
            return failure.payload
        return self.call_primitive(operation, handler, state, arguments, keywords)

    def _primitive_click(
        self, state: _WindowState, element: str, *, button: str = "left", count: int = 1, **_: Any
    ) -> dict:
        def run() -> dict:
            entry = self._entry(state, element)
            handle = self._live_handle(entry)
            if handle is not None:
                available_actions = set(accessibility.action_names(handle))
                if button == "right" and "AXShowMenu" in available_actions:
                    if AS.AXUIElementPerformAction(handle, "AXShowMenu") == 0:
                        return {
                            "ok": True,
                            "did": f"Opened context menu on {entry.name!r}",
                            "via": "accessible",
                        }
                elif button == "left":
                    preferred_actions = _OPEN_ACTIONS if count >= 2 else _ACTIVATE_ACTIONS
                    action = next(
                        (name for name in preferred_actions if name in available_actions), ""
                    )
                    if action and AS.AXUIElementPerformAction(handle, action) == 0:
                        did = f"Opened {entry.name!r}" if count >= 2 else f"Clicked {entry.name!r}"
                        return {"ok": True, "did": did, "via": "accessible"}
            if entry.center is None:
                return {
                    "ok": False,
                    "error": f"Element {entry.name!r} exposes no action and has no on-screen position to click.",
                }
            input_synthesis.click(
                entry.pid, entry.center[0], entry.center[1], clicks=count, button=button
            )
            return {"ok": True, "did": f"Clicked {entry.name!r}", "via": "synthesized"}

        return self.guard(run)

    def _primitive_type(
        self,
        state: _WindowState,
        element: str,
        text: str,
        *,
        submit: bool = False,
        mode: str = "replace",
        **_: Any,
    ) -> dict:
        """Put text into a field and, with `submit`, post the Return that commits it."""

        def run() -> dict:
            entry = self._entry(state, element)
            handle = self._live_handle(entry)
            if handle is None:
                return {
                    "ok": False,
                    "error": f"Element {entry.name!r} is no longer available; search again.",
                }
            result = self._enter_text(entry, handle, text, mode=mode)
            if result.get("ok") and submit:
                # Return goes to the process rather than the element, since a form is committed by the focused control.
                AS.AXUIElementSetAttributeValue(handle, accessibility.FOCUSED, True)
                time.sleep(active_tuning().duration(Tunable.focus_settle))
                if input_synthesis.press_key(entry.pid, "return", []):
                    result["did"] = f"{result.get('did', 'Typed')}, then submitted"
                    result["submitted"] = True
                else:
                    result["submitted"] = False
                    result["note"] = (
                        "The text went in but Return could not be posted, so nothing was submitted."
                    )
            return result

        return self.guard(run)

    def _enter_text(self, entry: RegistryEntry, handle: Any, text: str, *, mode: str) -> dict:
        """The text itself: set through accessibility where the field allows it, typed where not."""
        if mode == "insert":
            if accessibility.set_selected_text(handle, text):
                return {"ok": True, "did": f"Inserted {len(text)} chars", "via": "accessible"}
            AS.AXUIElementSetAttributeValue(handle, accessibility.FOCUSED, True)
            time.sleep(active_tuning().duration(Tunable.focus_settle))
            input_synthesis.type_text(entry.pid, text)
            return {"ok": True, "did": f"Typed {len(text)} chars", "via": "synthesized"}
        if (
            accessibility.attribute_settable(handle, accessibility.VALUE)
            and AS.AXUIElementSetAttributeValue(handle, accessibility.VALUE, text) == 0
        ):
            landed = accessibility.text_value(handle)
            result: dict[str, Any] = {"ok": True, "did": f"Set {entry.name!r}", "via": "accessible"}
            if landed is not None:
                result["value"] = landed
                if landed != text:
                    result["note"] = message("type_clamped")
            return result
        AS.AXUIElementSetAttributeValue(handle, accessibility.FOCUSED, True)
        time.sleep(active_tuning().duration(Tunable.focus_settle))
        input_synthesis.type_text(entry.pid, text)
        return {"ok": True, "did": f"Typed into {entry.name!r}", "via": "synthesized"}

    def _primitive_press(
        self, state: _WindowState, key: str, *, modifiers: Optional[list[str]] = None, **_: Any
    ) -> dict:
        def run() -> dict:
            keys = modifiers or []
            if not input_synthesis.press_key(state.pid, key, keys):
                return {
                    "ok": False,
                    "error": f'{key!r} is not a key. Use a named key, a letter, or a chord: press("cmd+shift+g").',
                }
            return {"ok": True, "did": f"Pressed {' '.join([*keys, key])}"}

        return self.guard(run)

    def _primitive_scroll(
        self,
        state: _WindowState,
        element: Optional[str] = None,
        *,
        direction: str = "down",
        **_: Any,
    ) -> dict:
        def run() -> dict:
            if element is not None:
                entry = self._entry(state, element)
                handle = self._live_handle(entry)
                if (
                    handle is not None
                    and AS.AXUIElementPerformAction(handle, "AXScrollToVisible") == 0
                ):
                    return {
                        "ok": True,
                        "did": f"Scrolled {entry.name!r} into view",
                        "via": "accessible",
                    }
                pid = entry.pid
            else:
                pid = state.pid
            step = active_tuning().amount(Tunable.scroll_amount_pixels)
            vectors = {"up": (0, step), "down": (0, -step), "left": (step, 0), "right": (-step, 0)}
            if direction not in vectors:
                return {
                    "ok": False,
                    "error": "Give an element to bring into view, or a direction (up, down, left, right).",
                }
            delta_x, delta_y = vectors[direction]
            input_synthesis.scroll(pid, delta_x, delta_y)
            return {"ok": True, "did": f"Scrolled {direction}"}

        return self.guard(run)

    def _primitive_select(
        self,
        state: _WindowState,
        element: str,
        *,
        text: Optional[str] = None,
        to_text: Optional[str] = None,
        select_all: bool = False,
        occurrence: int = 1,
        **_: Any,
    ) -> dict:
        def run() -> dict:
            entry = self._entry(state, element)
            handle = self._live_handle(entry)
            if handle is None:
                return {
                    "ok": False,
                    "error": f"Element {entry.name!r} is no longer available; search again.",
                }
            content = accessibility.text_value(handle)
            if content is None:
                return {"ok": False, "error": "This element holds no editable text to select."}
            if select_all:
                start, length = resolve_range(content, select_all=True)
            elif to_text is not None:
                start, length = resolve_range(
                    content, anchor_from=text, anchor_to=to_text, occurrence=occurrence
                )
            else:
                start, length = resolve_range(content, text=text, occurrence=occurrence)
            if accessibility.set_selected_range(handle, start, length):
                return {"ok": True, "did": f"Selected {length} chars", "via": "accessible"}
            return {"ok": False, "error": message("select_unsupported")}

        return self.guard(run)

    def _primitive_caret(
        self,
        state: _WindowState,
        element: str,
        *,
        before: Optional[str] = None,
        after: Optional[str] = None,
        at_offset: Optional[int] = None,
        edge: str = "",
        occurrence: int = 1,
        **_: Any,
    ) -> dict:
        def run() -> dict:
            entry = self._entry(state, element)
            handle = self._live_handle(entry)
            if handle is None:
                return {
                    "ok": False,
                    "error": f"Element {entry.name!r} is no longer available; search again.",
                }
            content = accessibility.text_value(handle)
            if content is None:
                return {"ok": False, "error": "This element holds no editable text."}
            offset = resolve_caret(
                content,
                before=before,
                after=after,
                at_offset=at_offset,
                to_start=edge == "start",
                to_end=edge == "end",
                occurrence=occurrence,
            )
            if accessibility.set_selected_range(handle, offset, 0):
                return {"ok": True, "did": f"Caret at {offset}", "via": "accessible"}
            return {"ok": False, "error": message("select_unsupported")}

        return self.guard(run)

    def _primitive_drag(
        self,
        state: _WindowState,
        element: str,
        onto: Optional[str] = None,
        *,
        button: str = "left",
        **_: Any,
    ) -> dict:
        def run() -> dict:
            if onto is None:
                return {"ok": False, "error": "drag needs onto — the element to drop onto."}
            source = self._entry(state, element)
            target = self._entry(state, onto)
            if source.center is None or target.center is None:
                return {
                    "ok": False,
                    "error": "Both elements need an on-screen position to drag between.",
                }
            input_synthesis.drag(
                source.pid,
                source.center[0],
                source.center[1],
                target.center[0],
                target.center[1],
                button=button,
            )
            return {"ok": True, "did": f"Dragged {source.name!r} onto {target.name!r}"}

        return self.guard(run)

    def glance(self, target: str) -> Glance:
        """Title, focus, selection and the elements present, from one walk, with the keys a window cannot fill absent."""
        try:
            state = self._windows.get(target)
            pid = state.pid if state is not None else self._state_for(target).pid
            snapshot = accessibility.snapshot_app(pid, budget_seconds=1.0)
        except Exception:  # noqa: BLE001 — an observation must never be the thing that fails
            return Glance()
        focused = next(
            (
                _element_name(accessible)
                for accessible in snapshot.elements
                if getattr(accessible, "focused", False)
            ),
            None,
        )
        selected = [
            _element_name(accessible) for accessible in snapshot.elements if accessible.selected
        ]
        return Glance(
            facts={
                "title": snapshot.window_title or "",
                "focus": focused,
                "selection": selected[0] if selected else None,
            },
            ids=frozenset(
                ".".join(str(step) for step in accessible.path) or "root"
                for accessible in snapshot.elements
            ),
        )

    def _primitive_focus(
        self, state: _WindowState, element: Optional[str] = None, **_: Any
    ) -> dict:
        """Give keyboard focus to this window or one control inside it, without raising the app over what the user is doing."""

        def run() -> dict:
            if element:
                entry = self._entry(state, element)
                handle = self._live_handle(entry)
                if handle is None:
                    return {
                        "ok": False,
                        "error": f"Element {entry.name!r} is no longer available; search again.",
                    }
                if AS.AXUIElementSetAttributeValue(handle, accessibility.FOCUSED, True) != 0:
                    return {"ok": False, "error": f"{entry.name!r} does not accept keyboard focus."}
                return {"ok": True, "focused": entry.name}
            window = accessibility.window_handle(state.pid, state.window_id)
            if window is None:
                return {"ok": False, "error": "That window is no longer available."}
            AS.AXUIElementSetAttributeValue(window, "AXMain", True)
            AS.AXUIElementSetAttributeValue(window, accessibility.FOCUSED, True)
            return {"ok": True, "focused": True}

        return self.guard(run)

    def _primitive_shortcuts(self, state: _WindowState, **_: Any) -> dict:
        """Every keyboard shortcut this application publishes, from its own menu bar rather than from memory."""

        def run() -> dict:
            found = accessibility.shortcuts_of(state.pid)
            if not found:
                return {
                    "ok": True,
                    "shortcuts": [],
                    "note": message(
                        "no_shortcuts",
                        app=accessibility.app_name_for_pid(state.pid) or "This application",
                    ),
                }
            return {"ok": True, "shortcuts": found}

        return self.guard(run)

    def _primitive_read(self, state: _WindowState, element: Optional[str] = None, **_: Any) -> dict:
        """Read one element's text, or the whole target's when no element is named, as `read()` means on a page."""

        def run() -> dict:
            if not element:
                # The whole target's text, so one name means one thing whichever surface is answering.
                snapshot = self._ready_snapshot(state.pid, "focused")
                # Every line the window says, as a list, because a window's text is discrete labels rather than a document.
                return {
                    "ok": True,
                    "lines": [
                        text
                        for text in (_element_name(element) for element in snapshot.elements)
                        if text
                    ],
                }
            entry = self._entry(state, element)
            handle = self._live_handle(entry)
            if handle is None:
                return {
                    "ok": False,
                    "error": f"Element {entry.name!r} is no longer available; search again.",
                }
            return {"ok": True, "lines": (accessibility.text_value(handle) or "").splitlines()}

        return self.guard(run)


SURFACE = NativeSurface()
