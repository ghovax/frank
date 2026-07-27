"""The macOS Accessibility (AX) tree — the accurate way to read and drive app UI.

This is the workhorse. It reads any running app's semantic UI (every button, field,
menu item, its role, name, value, state and on-screen frame) and acts on elements
directly (press a button, set a text field's value). Because AX actions target a
specific element in a specific process, they never move the user's cursor or steal the
keyboard: that is the containment guarantee.

The data is taken as the system returns it: each element's real AX attributes (AXRole,
AXTitle, AXValue, …) are read and passed through, filtered only by which elements are
worth including and which attributes are populated. No parallel vocabulary, no
renamed roles, no synthesized element ids — the caller references an element by its
position in the returned list.

Speed comes from two levers. One batched IPC round-trip per node
(AXUIElementCopyMultipleAttributeValues) reads every attribute at once instead of a
call each. And the app itself is asked what is on screen — AXVisibleChildren /
AXVisibleRows return only the currently-visible descendants — so a scrolled list of ten
thousand rows costs the handful actually shown. Geometry uses CoreGraphics (CGRect*),
and the only bound is cycle detection (a visited set, since AX trees can contain
reference cycles) plus the per-message timeout that guards against a hung app.

Interactivity is universal: there is no allowlist of "clickable" roles. Anything that is
not a pure structural container is included, so custom and third-party controls come
through too; whether an element can be pressed is decided at act-time from its real AX
action list.
"""
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

from daisy.base.tuning import Tunable, active_tuning

# Attribute names. The kAX* symbols resolve to exactly these strings.
ROLE = "AXRole"
SUBROLE = "AXSubrole"
TITLE = "AXTitle"
DESCRIPTION = "AXDescription"
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

# The text attributes an editable element exposes: its own contents (AXValue), the current
# selection as a substring, and the selection as a (location, length) range. Setting the range
# moves the caret or selects text; setting the selected text inserts at the caret or replaces the
# selection. These are the accessible, VoiceOver-grade way to edit text without synthesizing keys.
SELECTED_TEXT = "AXSelectedText"
SELECTED_TEXT_RANGE = "AXSelectedTextRange"
NUMBER_OF_CHARACTERS = "AXNumberOfCharacters"

# One batched read pulls all of these in a single IPC round-trip per node. AXFrame is the
# element's rectangle in one value; AXVisibleChildren/AXVisibleRows let the app report
# what is on screen so we never descend into scrolled-away content.
BATCH_ATTRIBUTES = [
    ROLE, SUBROLE, TITLE, DESCRIPTION, HELP, VALUE, ENABLED, SELECTED,
    FRAME, POSITION, SIZE, VISIBLE_CHILDREN, VISIBLE_ROWS, CHILDREN,
]

# Pure containers: not included on their own (they carry no action or information), but
# always descended through to reach the real controls inside them. Table rows and cells
# are structure here too — we descend through them and include the text/controls they
# hold, one line per item instead of row+cell+text triples.
STRUCTURAL_ROLES = frozenset({
    "AXGroup", "AXSplitGroup", "AXScrollArea", "AXLayoutArea", "AXLayoutItem",
    "AXUnknown", "AXToolbar", "AXTabGroup", "AXList", "AXOutline", "AXTable",
    "AXWebArea", "AXBrowser", "AXBox", "AXGenericElement", "AXScrollBar",
    "AXSplitter", "AXGrowArea", "AXRow", "AXCell", "AXColumn", "AXOutlineRow",
    "AXApplication", "AXWindow",
})

# Text nodes carry their content in AXValue and have no control subtree, so they are
# included when they have text and not descended into. Decorative nodes are included only
# when they carry a real name (a labeled image can be a button; an unlabeled one is chrome).
TEXT_ROLES = frozenset({"AXStaticText", "AXHeading", "AXText"})
DECORATIVE_ROLES = frozenset({
    "AXImage", "AXProgressIndicator", "AXBusyIndicator", "AXValueIndicator",
    "AXRelevanceIndicator", "AXRulerMarker", "AXRuler",
})

# AXValue geometry types (symbol names have drifted across SDKs, so resolve once).
POINT_TYPE = getattr(AS, "kAXValueCGPointType", getattr(AS, "kAXValueTypeCGPoint", 1))
SIZE_TYPE = getattr(AS, "kAXValueCGSizeType", getattr(AS, "kAXValueTypeCGSize", 2))
RECT_TYPE = getattr(AS, "kAXValueCGRectType", getattr(AS, "kAXValueTypeCGRect", 3))
ERROR_VALUE_TYPE = getattr(AS, "kAXValueAXErrorType", getattr(AS, "kAXValueTypeAXError", 5))
RANGE_TYPE = getattr(AS, "kAXValueCFRangeType", getattr(AS, "kAXValueTypeCFRange", 4))

# A single message to a wedged app must not block the walk forever. This is a safety valve against
# a hung process, not an accuracy cap: a healthy element answers in well under a millisecond, so a
# generous ceiling never drops a real one. The ceiling lives in the central tuning policy
# (``ax_messaging_seconds``, scaled by the timeout knob), read at each call site.


@dataclass
class Element:
    """One included node, holding the raw AX attribute values the system returned plus
    the handle and geometry needed to act on it. Empty attributes stay as-is (empty
    string / None); the caller filters them when presenting."""
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
    # The real AX actions this node supports (AXPress, AXConfirm, …), so the caller can tell
    # a clickable control from an inert label without a separate act-time round-trip. Empty
    # for text and pure containers, which are never queried for actions.
    actions: list[str] = field(default_factory=list)
    # A region is a container the shallow walk chose not to expand: it stands in for its
    # subtree and carries how many on-screen children wait inside, so the caller can drill
    # into it. None on ordinary leaf elements.
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
    """A value fit to hand back as-is: real AX values that are strings/numbers/bools pass
    through; anything else (an element reference, an unbridged object) becomes None."""
    if isinstance(value, bool) or isinstance(value, (str, int, float)):
        return value
    return None


def _geometry(value: Any) -> Any:
    """Unwrap an AXValue carrying a CGRect/CGPoint/CGSize into its CoreGraphics struct, or
    None for error placeholders and other types."""
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
    """Batched single-round-trip read of every attribute we care about, with AX error
    placeholders normalized to None."""
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
    """The element's rectangle as a CGRect: AXFrame when the app provides it (one value),
    otherwise composed from AXPosition + AXSize when a batch is missing AXFrame."""
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


def _child_nodes(attributes: dict[str, Any]) -> list[Any]:
    """The descendants the app considers on screen. AXVisibleRows (tables/outlines) and
    AXVisibleChildren (scroll areas) collapse huge scrolled lists to what is shown; the
    full child list is used when the app does not report visibility."""
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


def find_app_pid(name: str) -> Optional[int]:
    """Resolve a running app to its PID by localized name or bundle id (case-insensitive;
    substring on the name). Returns None if it is not running."""
    needle = name.strip().lower()
    running_apps = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
    exact = next(
        (
            app.processIdentifier()
            for app in running_apps
            if needle in (_string(app.bundleIdentifier()).lower(), _string(app.localizedName()).lower())
        ),
        None,
    )
    if exact is not None:
        return exact
    return next(
        (
            app.processIdentifier()
            for app in running_apps
            if needle and (needle in _string(app.localizedName()).lower() or needle in _string(app.bundleIdentifier()).lower())
        ),
        None,
    )


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
    """The regular (Dock-visible) apps currently running, by name — the situational awareness a
    person gets from glancing at the Dock, so the model knows what it can switch to."""
    regular = AppKit.NSApplicationActivationPolicyRegular
    apps = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
    names = [_string(app.localizedName()) for app in apps if app.activationPolicy() == regular]
    return [name for name in names if name]


def window_titles(pid: int) -> list[str]:
    """Every window title of an app — so the model can tell one window from another (a document
    or reader window vs. the main window) and target it by name, instead of assuming the focused
    window is the one it wants."""
    root = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(root, active_tuning().duration(Tunable.ax_messaging_seconds))
    windows = _single(root, WINDOWS) or []
    titles = [_string(_single(window, TITLE)) for window in windows]
    return [title for title in titles if title]


def enable_rich_accessibility(root: Any) -> None:
    """Ask an app that gates its accessibility tree to build the full one.

    Chromium-based apps (every Electron app — VS Code, Obsidian, Slack, …) expose only
    their window chrome until a client sets AXManualAccessibility; setting it lights up
    their entire tree (VS Code goes from 3 elements to hundreds). The build is
    asynchronous — a background app finishes it the next time it pumps its run loop —
    but once built it persists, so later background reads see the full tree.

    Only AXManualAccessibility is set, deliberately: the other AT handshake,
    AXEnhancedUserInterface, makes some apps reposition their windows, which would
    disturb the user. This attribute has no visual effect. Browsers enable their web
    content on their own once they see sustained accessibility queries. Best-effort:
    apps that don't implement the attribute return an error we ignore."""
    with suppress(Exception):
        AS.AXUIElementSetAttributeValue(root, "AXManualAccessibility", kCFBooleanTrue)


def prime_accessibility(pid: int) -> None:
    """Switch on an app's rich accessibility tree ahead of a read, so a later read meets a tree that
    is already built instead of racing its asynchronous construction. Idempotent and cheap (setting
    AXManualAccessibility again is a no-op once the tree is up); called by the pre-warm watcher when
    an app comes to the front. Best-effort: an app that ignores the handshake is left as it was."""
    root = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(root, active_tuning().duration(Tunable.ax_messaging_seconds))
    enable_rich_accessibility(root)


class _Prewarmer:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_pid: Optional[int] = None

    def start(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="daisy-ax-prewarm", daemon=True)
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
            time.sleep(active_tuning().duration(Tunable.ax_prewarm_interval_seconds))


_prewarmer = _Prewarmer()


def start_prewarm() -> None:
    """Start the pre-warm watcher if it is not already running. Idempotent; the native surface calls
    it on first use, so the daemon only runs when computer control is actually exercised."""
    _prewarmer.start()


def _window_roots(root: Any, window: str) -> list[Any]:
    if window == "all":
        return list(_single(root, WINDOWS) or [])
    if window == "main":
        main = _single(root, MAIN_WINDOW) or _single(root, FOCUSED_WINDOW)
        return [main] if main else list(_single(root, WINDOWS) or [])
    if window and window != "focused":
        # Anything else is a window title (or a substring of one): target the matching window,
        # so the model can pick one window from another by name rather than by its role.
        needle = window.strip().lower()
        matched = [window for window in (_single(root, WINDOWS) or []) if needle in _string(_single(window, TITLE)).lower()]
        if matched:
            return matched
    focused = _single(root, FOCUSED_WINDOW) or _single(root, MAIN_WINDOW)
    return [focused] if focused else list(_single(root, WINDOWS) or [])


def _includes(role: str, has_name: bool, has_value: bool) -> bool:
    """Whether an element is worth returning on its own. Structural containers never are;
    text is when it has content; a decorative node is when it is named; everything else
    (any control, standard or custom) is."""
    if role in STRUCTURAL_ROLES:
        return False
    if role in DECORATIVE_ROLES:
        return has_name
    if role in TEXT_ROLES:
        return has_value
    return True


# The shallow overview expands this many levels before turning any still-deeper container
# into an addressable region stand-in. A drill (observing one region) applies the same depth
# from that region's root, so exploration stays progressive at every level rather than
# dumping the whole tree at once.
DEFAULT_OVERVIEW_DEPTH = 4


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
    """Build an Element from an already-read attribute batch. Actions are queried only for
    real controls — never for text, or for a region that only stands in for its subtree."""
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
    """Push a node's children so the stack yields them in document order (reversed on the
    way in), tagging each with its next depth and child-index path."""
    stack.extend(
        (children[index], depth + 1, path + (index,))
        for index in range(len(children) - 1, -1, -1)
    )


def _collect(
    seeds: list[tuple[Any, int, tuple[int, ...]]],
    window_rect: Any,
    maximum_depth: int,
) -> tuple[list[Element], int]:
    """Walk the seed nodes into a flat element list, shallow-first. Structural containers
    reached at or beyond ``maximum_depth`` become region stand-ins (carrying their child count)
    instead of being expanded; everything shallower is walked as usual. Iterative traversal
    keeps it fast and immune to deep trees; a visited set breaks AX reference cycles."""
    elements: list[Element] = []
    seen: set[Any] = set()
    stack: list[tuple[Any, int, tuple[int, ...]]] = list(reversed(seeds))
    while stack:
        node, depth, path = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        attributes = _read(node)
        if attributes is None:
            continue

        role = _string(attributes.get(ROLE))
        frame = _frame_of(attributes)
        # A real rectangle that does not intersect the window is off screen: skip it and its
        # subtree. A frameless or empty-rect node is a container we still descend.
        if frame is not None and window_rect is not None and not Quartz.CGRectIsEmpty(frame):
            if not Quartz.CGRectIntersectsRect(frame, window_rect):
                continue

        children = _child_nodes(attributes)
        beyond_depth = depth >= maximum_depth

        if role in STRUCTURAL_ROLES:
            # Deep container: stand it in for its subtree so the caller can drill instead of
            # inlining hundreds of descendants. Shallow container: descend, as before.
            if beyond_depth and children:
                elements.append(_make_element(
                    node, attributes, role, frame, depth, path,
                    as_region=True, child_count=len(children),
                ))
                continue
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
        # Text carries no control subtree; past the depth bound we also stop descending so
        # an included leaf stands alone rather than dragging its whole subtree into view.
        if role in TEXT_ROLES or beyond_depth:
            continue
        _push_children(stack, children, depth, path)

    return elements, len(seen)


def snapshot_app(
    pid: int,
    *,
    window: str = "focused",
    maximum_depth: int = DEFAULT_OVERVIEW_DEPTH,
    root_handle: Any = None,
    root_path: tuple[int, ...] = (),
) -> Snapshot:
    """Walk one app's AX tree into a flat, shallow-first list of on-screen elements, each
    carrying its raw AX attributes and actions. ``window`` scopes it ("focused", "main",
    "all"); ``root_handle``/``root_path`` re-root the walk at a previously-seen region so the
    model can drill into it."""
    started = time.perf_counter()
    root = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(root, active_tuning().duration(Tunable.ax_messaging_seconds))
    enable_rich_accessibility(root)
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

    elements, visited = _collect(seeds, window_rect, maximum_depth)
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
    """The AX actions an element actually supports (e.g. AXPress, AXConfirm, AXPick), read
    at act-time so the walk stays one round-trip per node."""
    error, names = AS.AXUIElementCopyActionNames(element, None)
    if error != 0 or names is None:
        return []
    return [str(name) for name in names]


def resolve_from_path(pid: int, path: tuple[int, ...]) -> Any:
    """Re-resolve an element from a fresh app root by its child-index path. Handles go
    stale when an app relayouts or relaunches; this rebuilds a live handle so an action a
    beat after the observe still lands on the right control. Uses the same visible-child
    traversal that produced the path."""
    root = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(root, active_tuning().duration(Tunable.ax_messaging_seconds))
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
    """Set the selection (or, with length 0, place the caret). Returns False when the element does
    not support a settable selection range, so the caller can fall back."""
    if not attribute_settable(element, SELECTED_TEXT_RANGE):
        return False
    value = AS.AXValueCreate(RANGE_TYPE, NSMakeRange(location, length))
    if value is None:
        return False
    return AS.AXUIElementSetAttributeValue(element, SELECTED_TEXT_RANGE, value) == 0


def set_selected_text(element: Any, text: str) -> bool:
    """Replace the current selection with ``text`` (inserting at the caret when the selection is
    empty). Returns False when the element does not support it, so the caller can fall back."""
    if not attribute_settable(element, SELECTED_TEXT):
        return False
    return AS.AXUIElementSetAttributeValue(element, SELECTED_TEXT, text) == 0

