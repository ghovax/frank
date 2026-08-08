"""Reads and drives running apps through the macOS accessibility tree, the accurate way to see and act on their interface."""

from __future__ import annotations

import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Optional

import AppKit
import ApplicationServices as AS
import Quartz
from CoreFoundation import kCFBooleanTrue
from Foundation import NSMakeRange

from langmesh.base.tuning import Tunable, active_tuning


def _resolve_symbols_before_any_thread_exists() -> None:
    """Touch every AX symbol once at import, on one thread, because pyobjc's lazy attribute lookup is not thread-safe."""
    for symbol in (
        "AXUIElementCreateApplication",
        "AXUIElementCreateSystemWide",
        "AXUIElementCopyAttributeValue",
        "AXUIElementCopyAttributeValues",
        "AXUIElementCopyMultipleAttributeValues",
        "AXUIElementCopyAttributeNames",
        "AXUIElementSetAttributeValue",
        "AXUIElementIsAttributeSettable",
        "AXUIElementPerformAction",
        "AXUIElementCopyActionNames",
        "AXUIElementSetMessagingTimeout",
        "AXUIElementGetPid",
        "AXIsProcessTrusted",
        "AXIsProcessTrustedWithOptions",
        "AXValueGetValue",
    ):
        with suppress(AttributeError, KeyError):
            getattr(AS, symbol)


_resolve_symbols_before_any_thread_exists()

# Attribute names. The kAX* symbols resolve to exactly these strings.
ROLE = "AXRole"
SUBROLE = "AXSubrole"
TITLE = "AXTitle"
DESCRIPTION = "AXDescription"
# What the application calls this kind of control, in the system's own prose, and the only words some controls have.
ROLE_DESCRIPTION = "AXRoleDescription"
# The prompt text inside an empty field, which is often all a text field has to be found by.
PLACEHOLDER = "AXPlaceholderValue"
HELP = "AXHelp"
VALUE = "AXValue"
ENABLED = "AXEnabled"
FOCUSED = "AXFocused"
SELECTED = "AXSelected"
FRAME = "AXFrame"
POSITION = "AXPosition"
SIZE = "AXSize"
IDENTIFIER = "AXIdentifier"
CHILDREN = "AXChildren"
VISIBLE_CHILDREN = "AXVisibleChildren"
VISIBLE_ROWS = "AXVisibleRows"
WINDOWS = "AXWindows"
MAIN_WINDOW = "AXMainWindow"
FOCUSED_WINDOW = "AXFocusedWindow"
FOCUSED_ELEMENT = "AXFocusedUIElement"

# The text attributes an editable element exposes: its contents, its selection, and that selection's range.
SELECTED_TEXT = "AXSelectedText"
SELECTED_TEXT_RANGE = "AXSelectedTextRange"
NUMBER_OF_CHARACTERS = "AXNumberOfCharacters"

# One batched read pulls all of these per node, including the frame and whatever the app reports as on screen.
BATCH_ATTRIBUTES = [
    ROLE,
    SUBROLE,
    TITLE,
    DESCRIPTION,
    HELP,
    ROLE_DESCRIPTION,
    PLACEHOLDER,
    VALUE,
    ENABLED,
    SELECTED,
    FRAME,
    POSITION,
    SIZE,
    VISIBLE_CHILDREN,
    VISIBLE_ROWS,
    CHILDREN,
]

# Pure containers, never included on their own but always descended through to reach the controls inside.
STRUCTURAL_ROLES = frozenset(
    {
        "AXGroup",
        "AXSplitGroup",
        "AXScrollArea",
        "AXLayoutArea",
        "AXLayoutItem",
        "AXUnknown",
        "AXToolbar",
        "AXTabGroup",
        "AXList",
        "AXOutline",
        "AXTable",
        "AXWebArea",
        "AXBrowser",
        "AXBox",
        "AXGenericElement",
        "AXScrollBar",
        "AXSplitter",
        "AXGrowArea",
        "AXRow",
        "AXCell",
        "AXColumn",
        "AXOutlineRow",
        "AXApplication",
        "AXWindow",
    }
)

# Text nodes are included when they have content and never descended into; decorative nodes only when named.
TEXT_ROLES = frozenset({"AXStaticText", "AXHeading", "AXText"})
DECORATIVE_ROLES = frozenset(
    {
        "AXImage",
        "AXProgressIndicator",
        "AXBusyIndicator",
        "AXValueIndicator",
        "AXRelevanceIndicator",
        "AXRulerMarker",
        "AXRuler",
    }
)

# AXValue geometry types used by the declared macOS SDK.
POINT_TYPE = AS.kAXValueCGPointType
SIZE_TYPE = AS.kAXValueCGSizeType
RECT_TYPE = AS.kAXValueCGRectType
ERROR_VALUE_TYPE = AS.kAXValueAXErrorType
RANGE_TYPE = AS.kAXValueCFRangeType

# A ceiling on one message to a wedged app, generous enough that a healthy element is never dropped.


@dataclass
class Element:
    """One included node, holding the raw attribute values plus the handle and geometry needed to act on it."""

    role: str
    subrole: str
    title: str
    description: str
    help: str
    value: Any
    enabled: Optional[bool]
    selected: Optional[bool]
    center: Optional[tuple[float, float]]
    frame: Any  # Quartz.CGRect or None
    depth: int
    handle: Any  # AXUIElementRef: fast path for acting within the same observe cycle
    path: tuple[int, ...]  # child-index path from the app root, for re-resolution
    # The real actions this node supports, so a clickable control is distinguishable from a label without another round trip.
    actions: list[str] = field(default_factory=list)
    # What the system calls this kind of control, in its own prose; the last fallback for a name, never a first choice.
    role_description: str = ""
    placeholder: str = ""
    # A region stands in for a subtree the shallow walk did not expand, carrying how many children wait inside.
    child_count: Optional[int] = None


@dataclass
class Snapshot:
    pid: int
    app_name: str
    window_title: str
    elements: list[Element]
    duration_milliseconds: float
    visited: int
    root: Any = None


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _primitive(value: Any) -> Any:
    """A value fit to hand back as-is; anything that is not a string, number or bool becomes `None`."""
    if isinstance(value, bool) or isinstance(value, (str, int, float)):
        return value
    return None


def _geometry(value: Any) -> Any:
    """Unwrap an AXValue carrying a rectangle, point or size into its CoreGraphics struct, or `None`."""
    try:
        value_type = AS.AXValueGetType(value)
    except Exception:
        return None
    if value_type == RECT_TYPE:
        succeeded, rect = AS.AXValueGetValue(value, RECT_TYPE, None)
        return rect if succeeded else None
    if value_type == POINT_TYPE:
        succeeded, point = AS.AXValueGetValue(value, POINT_TYPE, None)
        return point if succeeded else None
    if value_type == SIZE_TYPE:
        succeeded, size = AS.AXValueGetValue(value, SIZE_TYPE, None)
        return size if succeeded else None
    return None


def _read(element: Any) -> Optional[dict[str, Any]]:
    """One batched read of every attribute we care about, with error placeholders normalized to `None`."""
    error, values = AS.AXUIElementCopyMultipleAttributeValues(element, BATCH_ATTRIBUTES, 0, None)
    if error != 0 or values is None:
        return None
    attributes: dict[str, Any] = {}
    for name, value in zip(BATCH_ATTRIBUTES, values):
        if value is None:
            attributes[name] = None
            continue
        try:
            is_error = AS.AXValueGetType(value) == ERROR_VALUE_TYPE
        except Exception:
            is_error = False
        attributes[name] = None if is_error else value
    return attributes


def _frame_of(attributes: dict[str, Any]) -> Any:
    """The element's rectangle, from `AXFrame` when the app provides it and composed from position and size otherwise."""
    frame_value = attributes.get(FRAME)
    if frame_value is not None:
        rect = _geometry(frame_value)
        if rect is not None:
            return rect
    position = attributes.get(POSITION)
    size = attributes.get(SIZE)
    point = _geometry(position) if position is not None else None
    extent = _geometry(size) if size is not None else None
    if point is not None and extent is not None:
        return Quartz.CGRectMake(point.x, point.y, extent.width, extent.height)
    return None


def rectangle(frame: Any) -> Optional[dict[str, int]]:
    """A rectangle as whole integers, or `None` when there is nothing worth reporting."""
    if frame is None or Quartz.CGRectIsEmpty(frame):
        return None
    return {
        "x": round(Quartz.CGRectGetMinX(frame)),
        "y": round(Quartz.CGRectGetMinY(frame)),
        "width": round(Quartz.CGRectGetWidth(frame)),
        "height": round(Quartz.CGRectGetHeight(frame)),
    }


def _child_nodes(attributes: dict[str, Any]) -> list[Any]:
    """The descendants the app considers on screen, falling back to the full child list when it reports no visibility."""
    rows = attributes.get(VISIBLE_ROWS)
    if rows:
        return list(rows)
    visible = attributes.get(VISIBLE_CHILDREN)
    if visible:
        return list(visible)
    children = attributes.get(CHILDREN)
    return list(children) if children else []


def _single(element: Any, attribute: str) -> Any:
    error, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
    return value if error == 0 else None


def _pids_showing_a_window() -> set[int]:
    """Which processes are actually displaying something, since an application can be running more than once."""
    import Quartz

    try:
        windows = (
            Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
            or []
        )
    except Exception:  # noqa: BLE001 — a preference must never be the thing that fails
        return set()
    showing = set()
    for window in windows:
        bounds = window.get("kCGWindowBounds") or {}
        # Layer 0 and a real size: a document window, not a menu-bar strip or an overlay.
        if (
            window.get("kCGWindowLayer") == 0
            and int(bounds.get("Width", 0)) > 200
            and int(bounds.get("Height", 0)) > 200
        ):
            showing.add(window.get("kCGWindowOwnerPID"))
    return showing


def find_app_pid(name: str) -> Optional[int]:
    """Resolve a running app to its process id by name or bundle id, preferring the instance showing a window."""
    needle = name.strip().lower()
    running_apps = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
    matches = [
        app.processIdentifier()
        for app in running_apps
        if needle in (_string(app.bundleIdentifier()).lower(), _string(app.localizedName()).lower())
    ] or [
        app.processIdentifier()
        for app in running_apps
        if needle in _string(app.localizedName()).lower()
        or needle in _string(app.bundleIdentifier()).lower()
    ]
    if not matches:
        return None
    if len(matches) > 1:
        showing = _pids_showing_a_window()
        for pid in matches:
            if pid in showing:
                return pid
    return matches[0]


def frontmost_pid() -> Optional[int]:
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.processIdentifier() if app else None


def app_name_for_pid(pid: int) -> str:
    running_apps = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
    return next(
        (_string(app.localizedName()) for app in running_apps if app.processIdentifier() == pid),
        "",
    )


def running_app_names() -> list[str]:
    """The Dock-visible apps currently running, by name, so the model knows what it can switch to."""
    regular = AppKit.NSApplicationActivationPolicyRegular
    apps = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
    names = [_string(app.localizedName()) for app in apps if app.activationPolicy() == regular]
    return [name for name in names if name]


# The only bridge between what accessibility calls a window and what the window server has numbered.
_WINDOW_ID_SIGNATURE = b"i^{__AXUIElement=}o^I"
_window_id_lock = threading.Lock()
_window_id_function: Optional[Any] = None


def _window_id_of(window: Any) -> Optional[int]:
    """The window-server id of an AX window, or ``None`` when the bridge is unavailable."""
    global _window_id_function
    if _window_id_function is None:
        with _window_id_lock:
            if _window_id_function is None:
                try:
                    import objc

                    bundle = objc.loadBundle(
                        "ApplicationServices",
                        {},
                        bundle_path="/System/Library/Frameworks/ApplicationServices.framework",
                    )
                    loaded: dict[str, Any] = {}
                    objc.loadBundleFunctions(
                        bundle, loaded, [("_AXUIElementGetWindow", _WINDOW_ID_SIGNATURE)]
                    )
                    _window_id_function = loaded.get("_AXUIElementGetWindow", False)
                except Exception:  # noqa: BLE001 — no bridge means no ids, never a crash
                    _window_id_function = False
    if not _window_id_function:
        return None
    try:
        error, identifier = _window_id_function(window, None)
    except Exception:  # noqa: BLE001
        return None
    return int(identifier) if error == 0 and identifier else None


@dataclass(frozen=True)
class WindowRecord:
    """One window an application publishes, carrying `document` and `main` because a title is not an identity."""

    window_id: int
    title: str
    minimized: bool
    document: str = ""
    main: bool = False
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, width, height


def application_root(pid: int) -> Any:
    """An application's accessibility root, with its messaging timeout set and its rich tree asked for."""
    root = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(
        root, active_tuning().duration(Tunable.accessibility_messaging)
    )
    enable_rich_accessibility(root)
    return root


def windows_of(pid: int) -> list[WindowRecord]:
    """Every real window an application publishes with its window-server id, titled from `AXTitle` so no screen-recording grant is needed."""
    records: list[WindowRecord] = []
    seen: set[int] = set()
    for window in _published_windows(application_root(pid)):
        window_id = _window_id_of(window)
        if window_id is None or window_id in seen:
            continue
        seen.add(window_id)
        rectangle = _frame_of(_read(window) or {})
        bounds = (0, 0, 0, 0)
        if rectangle is not None:
            with suppress(Exception):
                bounds = (
                    int(rectangle.origin.x),
                    int(rectangle.origin.y),
                    int(rectangle.size.width),
                    int(rectangle.size.height),
                )
        records.append(
            WindowRecord(
                window_id=window_id,
                title=_string(_single(window, TITLE)) or _string(_single(window, VALUE)),
                minimized=bool(_single(window, "AXMinimized")),
                document=_string(_single(window, "AXDocument")),
                main=bool(_single(window, "AXMain")),
                bounds=bounds,
            )
        )
    return records


def _published_windows(root: Any) -> list[Any]:
    """Every window an application exposes, however it chooses to expose them, since `AXWindows` is not always offered."""
    candidates: list[Any] = list(_single(root, WINDOWS) or [])
    candidates.extend(
        child
        for child in (_single(root, CHILDREN) or [])
        if _string(_single(child, ROLE)) == "AXWindow"
    )
    candidates.extend(
        window
        for window in (_single(root, MAIN_WINDOW), _single(root, FOCUSED_WINDOW))
        if window is not None
    )
    return candidates


def window_handle(pid: int, window_id: int) -> Optional[Any]:
    """The live AX element for one window of an app, found by the id the window server minted."""
    for window in _published_windows(application_root(pid)):
        if _window_id_of(window) == window_id:
            return window
    return None


def window_titles(pid: int) -> list[str]:
    """Every window title of an app, for the situational-awareness block."""
    return [record.title for record in windows_of(pid) if record.title]


MENU_BAR = "AXMenuBar"
MENU_ITEM_CHARACTER = "AXMenuItemCmdChar"
MENU_ITEM_MODIFIERS = "AXMenuItemCmdModifiers"
MENU_ITEM_VIRTUAL_KEY = "AXMenuItemCmdVirtualKey"

# The modifier bitfield inverts Command, and the inversion is undone here rather than by every caller.
_MENU_MODIFIER_BITS = ((1, "shift"), (2, "option"), (4, "control"))

# The virtual key codes macOS uses for menu items that have no character (function keys, arrows).
_MENU_VIRTUAL_KEYS = {
    0x7A: "f1",
    0x78: "f2",
    0x63: "f3",
    0x76: "f4",
    0x60: "f5",
    0x61: "f6",
    0x62: "f7",
    0x64: "f8",
    0x65: "f9",
    0x6D: "f10",
    0x67: "f11",
    0x6F: "f12",
    0x7B: "left",
    0x7C: "right",
    0x7D: "down",
    0x7E: "up",
    0x24: "return",
    0x30: "tab",
    0x33: "delete",
    0x35: "escape",
    0x31: "space",
    0x73: "home",
    0x77: "end",
    0x74: "pageup",
    0x79: "pagedown",
}


def _menu_chord(item: Any) -> str:
    """The chord a menu item advertises, spelled the way `press` accepts it, read outside the fixed batch."""
    character = _string(_single(item, MENU_ITEM_CHARACTER))
    if len(character) == 1 and 0xF700 <= ord(character) <= 0xF8FF:
        character = ""
    character = character.lower()
    if not character:
        virtual_key = _single(item, MENU_ITEM_VIRTUAL_KEY)
        character = _MENU_VIRTUAL_KEYS.get(int(virtual_key), "") if virtual_key is not None else ""
    if not character:
        return ""
    raw = _single(item, MENU_ITEM_MODIFIERS)
    bits = int(raw) if raw is not None else 0
    modifiers = [name for bit, name in _MENU_MODIFIER_BITS if bits & bit]
    if not bits & 8:  # the inverted Command bit: unset means Command *is* held
        modifiers.insert(0, "cmd")
    return "+".join([*modifiers, character])


def shortcuts_of(pid: int, *, limit: int = 400) -> list[dict[str, Any]]:
    """The application's menu bar as a tree, with the chord each item advertises, so shortcuts are found rather than guessed."""
    root = application_root(pid)
    menu_bar = _single(root, MENU_BAR)
    if menu_bar is None:
        return []
    counted = 0

    def branch(element: Any, depth: int) -> list[dict[str, Any]]:
        nonlocal counted
        if depth > 6 or counted >= limit:
            return []
        nodes: list[dict[str, Any]] = []
        for child in _single(element, CHILDREN) or []:
            if counted >= limit:
                break
            title = _string(_single(child, TITLE))
            chord = _menu_chord(child)
            if chord:
                counted += 1
            # A submenu is a child menu, and an item with neither a chord nor one below it answers nothing.
            items = branch(child, depth + 1)
            if not chord and not items:
                continue
            node: dict[str, Any] = {"title": title} if title else {}
            if chord:
                node["keys"] = chord
            if items:
                node["items"] = items
            nodes.append(node if node.get("title") or node.get("keys") else {"items": items})
        # An untitled wrapper is not a level anybody means, so its contents are lifted into the menu a person sees.
        lifted: list[dict[str, Any]] = []
        for node in nodes:
            if set(node) == {"items"}:
                lifted.extend(node["items"])
            else:
                lifted.append(node)
        return lifted

    return branch(menu_bar, 0)


def enable_rich_accessibility(root: Any) -> None:
    """Ask an app that gates its accessibility tree to build the full one, which every Electron app needs."""
    with suppress(Exception):
        AS.AXUIElementSetAttributeValue(root, "AXManualAccessibility", kCFBooleanTrue)


def prime_accessibility(pid: int) -> None:
    """Switch on an app's rich tree ahead of a read, so the read meets a built tree rather than racing its construction."""
    root = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(
        root, active_tuning().duration(Tunable.accessibility_messaging)
    )
    enable_rich_accessibility(root)


class _Prewarmer:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_pid: Optional[int] = None

    def start(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="langmesh-accessibility-prewarm", daemon=True
                )
                self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                pid = frontmost_pid()
                if pid is not None and pid != self._last_pid:
                    self._last_pid = pid
                    prime_accessibility(pid)
            except Exception:
                pass
            time.sleep(active_tuning().duration(Tunable.accessibility_prewarm_interval))


_prewarmer = _Prewarmer()


def start_prewarm() -> None:
    """Start the pre-warm watcher if it is not already running, so it runs only when computer control is exercised."""
    _prewarmer.start()


def _window_roots(root: Any, window: str) -> list[Any]:
    if window == "all":
        return list(_single(root, WINDOWS) or [])
    if window == "main":
        main = _single(root, MAIN_WINDOW) or _single(root, FOCUSED_WINDOW)
        return [main] if main else list(_single(root, WINDOWS) or [])
    if window and window != "focused":
        # Anything else is a window title or a substring of one, so the model can pick a window by name.
        needle = window.strip().lower()
        matched = [
            window
            for window in (_single(root, WINDOWS) or [])
            if needle in _string(_single(window, TITLE)).lower()
        ]
        if matched:
            return matched
    focused = _single(root, FOCUSED_WINDOW) or _single(root, MAIN_WINDOW)
    return [focused] if focused else list(_single(root, WINDOWS) or [])


def _includes(role: str, has_name: bool, has_value: bool) -> bool:
    """Whether an element is worth returning on its own: never a container, text with content, a decorative node when named."""
    if role in STRUCTURAL_ROLES:
        return False
    if role in DECORATIVE_ROLES:
        return has_name
    if role in TEXT_ROLES:
        return has_value
    return True


# There is no depth limit, because the things a person names sit below the things that merely contain them.
_WALK_BUDGET_EXCEEDED = "walk_budget_exceeded"


def _make_element(
    node: Any,
    attributes: dict[str, Any],
    role: str,
    frame: Any,
    depth: int,
    path: tuple[int, ...],
    *,
    as_region: bool = False,
    child_count: Optional[int] = None,
) -> Element:
    """Build an element from an already-read batch, querying actions only for real controls."""
    center = None
    if frame is not None and not Quartz.CGRectIsEmpty(frame):
        center = (Quartz.CGRectGetMidX(frame), Quartz.CGRectGetMidY(frame))
    actions = [] if as_region or role in TEXT_ROLES else action_names(node)
    return Element(
        role=role,
        subrole=_string(attributes.get(SUBROLE)),
        title=_string(attributes.get(TITLE)),
        description=_string(attributes.get(DESCRIPTION)),
        help=_string(attributes.get(HELP)),
        role_description=_string(attributes.get(ROLE_DESCRIPTION)),
        placeholder=_string(attributes.get(PLACEHOLDER)),
        value=_primitive(attributes.get(VALUE)),
        enabled=attributes.get(ENABLED),
        selected=attributes.get(SELECTED),
        center=center,
        frame=frame,
        depth=depth,
        handle=node,
        path=path,
        actions=actions,
        child_count=child_count,
    )


def _push_children(stack: list, children: list[Any], depth: int, path: tuple[int, ...]) -> None:
    """Push a node's children so the stack yields them in document order, tagged with depth and child-index path."""
    stack.extend(
        (children[index], depth + 1, path + (index,)) for index in range(len(children) - 1, -1, -1)
    )


def _collect(
    seeds: list[tuple[Any, int, tuple[int, ...]]],
    window_rect: Any,
    budget_seconds: float,
) -> tuple[list[Element], int, bool]:
    """Walk the seed nodes into a flat list, shallow-first, so a spent budget keeps the top of the tree."""
    elements: list[Element] = []
    seen: set[Any] = set()
    deadline = time.perf_counter() + budget_seconds
    exhausted = False
    stack: list[tuple[Any, int, tuple[int, ...]]] = list(reversed(seeds))
    while stack:
        if time.perf_counter() > deadline:
            # Everything still queued becomes a stand-in carrying its child count, so a truncated read is visibly truncated.
            exhausted = True
            for pending_node, pending_depth, pending_path in reversed(stack):
                pending = _read(pending_node)
                if pending is None:
                    continue
                pending_children = _child_nodes(pending)
                elements.append(
                    _make_element(
                        pending_node,
                        pending,
                        _string(pending.get(ROLE)),
                        _frame_of(pending),
                        pending_depth,
                        pending_path,
                        as_region=True,
                        child_count=len(pending_children) or 1,
                    )
                )
            break
        node, depth, path = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        attributes = _read(node)
        if attributes is None:
            continue

        role = _string(attributes.get(ROLE))
        frame = _frame_of(attributes)
        # A real rectangle that does not intersect the window is off screen, while a frameless node is still descended.
        if frame is not None and window_rect is not None and not Quartz.CGRectIsEmpty(frame):
            if not Quartz.CGRectIntersectsRect(frame, window_rect):
                continue

        children = _child_nodes(attributes)

        if role in STRUCTURAL_ROLES:
            # A container is not itself worth reporting — what it holds is. Descend.
            _push_children(stack, children, depth, path)
            continue

        title = _string(attributes.get(TITLE))
        description = _string(attributes.get(DESCRIPTION))
        help_text = _string(attributes.get(HELP))
        value = _primitive(attributes.get(VALUE))
        has_name = bool(title or description or help_text)
        has_value = value not in (None, "")
        if _includes(role, has_name, has_value):
            elements.append(_make_element(node, attributes, role, frame, depth, path))
        # Text carries no control subtree, so its content is its value and everything else is descended into.
        if role in TEXT_ROLES:
            continue
        _push_children(stack, children, depth, path)

    return elements, len(seen), exhausted


def snapshot_app(
    pid: int,
    *,
    window: str = "focused",
    budget_seconds: Optional[float] = None,
    root_handle: Any = None,
    root_path: tuple[int, ...] = (),
) -> Snapshot:
    """Walk one app's tree into a flat, shallow-first list of on-screen elements, scoped or re-rooted at a known region."""
    started = time.perf_counter()
    root = application_root(pid)
    app_name = app_name_for_pid(pid)

    roots = _window_roots(root, window)
    window_rect = None
    window_title = ""
    if roots and roots[0] is not None:
        first = _read(roots[0])
        if first is not None:
            window_rect = _frame_of(first)
            window_title = _string(first.get(TITLE)) or _string(first.get(VALUE))

    if root_handle is not None:
        seeds = [(root_handle, 0, root_path or (0,))]
    else:
        seeds = [(node, 0, (index,)) for index, node in enumerate(roots) if node is not None]

    budget = (
        budget_seconds
        if budget_seconds is not None
        else active_tuning().duration(Tunable.accessibility_walk_budget)
    )
    elements, visited, exhausted = _collect(seeds, window_rect, budget)
    return Snapshot(
        pid=pid,
        app_name=app_name,
        window_title=window_title,
        elements=elements,
        duration_milliseconds=round((time.perf_counter() - started) * 1000, 1),
        visited=visited,
        root=root,
    )


def action_names(element: Any) -> list[str]:
    """The actions an element actually supports, read at act time so the walk stays one round trip per node."""
    error, names = AS.AXUIElementCopyActionNames(element, None)
    if error != 0 or names is None:
        return []
    return [str(name) for name in names]


def resolve_from_path(pid: int, path: tuple[int, ...]) -> Any:
    """Re-resolve an element from a fresh app root by its child-index path, since handles go stale on relayout."""
    root = application_root(pid)
    if not path:
        return root
    windows = _single(root, WINDOWS) or []
    if path[0] >= len(windows):
        return None
    current = windows[path[0]]
    for index in path[1:]:
        children = _child_nodes(_read(current) or {})
        if index >= len(children):
            return None
        current = children[index]
    return current


def handle_is_live(handle: Any) -> bool:
    """Cheap liveness probe: a stale AXUIElementRef errors on any attribute read."""
    error, _ = AS.AXUIElementCopyAttributeValue(handle, ROLE, None)
    return error == 0


def attribute_settable(element: Any, attribute: str) -> bool:
    """Whether an attribute can be written on this element (a read-only field is not settable)."""
    error, settable = AS.AXUIElementIsAttributeSettable(element, attribute, None)
    return error == 0 and bool(settable)


def text_value(element: Any) -> Optional[str]:
    """The element's own text contents (AXValue), or None when it holds no string."""
    value = _single(element, VALUE)
    return value if isinstance(value, str) else None


def set_selected_range(element: Any, location: int, length: int) -> bool:
    """Set the selection, or place the caret with a zero length, returning false when the element does not support it."""
    if not attribute_settable(element, SELECTED_TEXT_RANGE):
        return False
    value = AS.AXValueCreate(RANGE_TYPE, NSMakeRange(location, length))
    if value is None:
        return False
    return AS.AXUIElementSetAttributeValue(element, SELECTED_TEXT_RANGE, value) == 0


def set_selected_text(element: Any, text: str) -> bool:
    """Replace the current selection with `text`, returning false when the element does not support it."""
    if not attribute_settable(element, SELECTED_TEXT):
        return False
    return AS.AXUIElementSetAttributeValue(element, SELECTED_TEXT, text) == 0
