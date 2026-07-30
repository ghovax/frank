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

from frank.base.tuning import Tunable, active_tuning


def _resolve_symbols_before_any_thread_exists() -> None:
    """Touch every AX function this module calls, once, at import — on one thread.

    ``ApplicationServices`` is a pyobjc *lazy* module: an attribute is looked up in a metadata map
    and cached into the module on first access, and that path is not thread-safe. Two threads
    first-touching the same symbol race, and the loser gets ``KeyError`` — which surfaces as
    ``The action failed ('AXUIElementCreateApplication')``, because ``str(KeyError('X'))`` is
    ``"'X'"``, quotes and all.

    This module builds exactly that race and builds it once per process: ``_ready_snapshot`` calls
    ``start_prewarm()``, which spawns a thread that immediately calls ``prime_accessibility`` ->
    ``AXUIElementCreateApplication``, and then walks into ``snapshot_app`` -> the same symbol. It
    is a first-use-only failure, which is why it struck once in a recorded session and never
    again, and why it looked like the application was not ready — it had nothing to do with the
    application. Measured at 2 failures in 60 fresh processes.

    Resolving here means the cache is warm before any thread exists, so there is no first touch
    left to race."""
    for symbol in (
        "AXUIElementCreateApplication", "AXUIElementCreateSystemWide",
        "AXUIElementCopyAttributeValue", "AXUIElementCopyAttributeValues",
        "AXUIElementCopyMultipleAttributeValues", "AXUIElementCopyAttributeNames",
        "AXUIElementSetAttributeValue", "AXUIElementIsAttributeSettable",
        "AXUIElementPerformAction", "AXUIElementCopyActionNames",
        "AXUIElementSetMessagingTimeout", "AXUIElementGetPid",
        "AXIsProcessTrusted", "AXIsProcessTrustedWithOptions", "AXValueGetValue",
    ):
        with suppress(AttributeError, KeyError):
            getattr(AS, symbol)


_resolve_symbols_before_any_thread_exists()

# Attribute names. The kAX* symbols resolve to exactly these strings.
ROLE = "AXRole"
SUBROLE = "AXSubrole"
TITLE = "AXTitle"
DESCRIPTION = "AXDescription"
# What the application calls this kind of control, in prose the system itself writes:
# "increment arrow button", "close button", "disclosure triangle". Present on every element,
# and the only words some controls have — see the fallback in `engine._element_name`.
ROLE_DESCRIPTION = "AXRoleDescription"
# The prompt text shown inside an empty field ("Search", "Filter"). Rare across all elements, but
# text fields are disproportionately what a query looks for, and an empty one has nothing else.
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
    ROLE, SUBROLE, TITLE, DESCRIPTION, HELP, ROLE_DESCRIPTION, PLACEHOLDER, VALUE, ENABLED, SELECTED,
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
    # What the system calls this kind of control, in its own prose ("increment arrow button").
    # Defaulted because it is the newest attribute here and every existing construction site
    # predates it; it is the last fallback for a name, never a first choice.
    role_description: str = ""
    placeholder: str = ""
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


def rectangle(frame: Any) -> Optional[dict[str, int]]:
    """A CGRect as plain integers, or ``None`` when there is no rectangle worth reporting.

    Whole points, because the question this answers is "which of these two is the one I meant" and
    no one distinguishes controls by a third of a pixel. An empty rect answers ``None`` rather than
    four zeros: an element the application never laid out has no position, and saying so as
    ``0, 0, 0, 0`` would read as the top-left corner of the screen."""
    if frame is None or Quartz.CGRectIsEmpty(frame):
        return None
    return {
        "x": round(Quartz.CGRectGetMinX(frame)), "y": round(Quartz.CGRectGetMinY(frame)),
        "width": round(Quartz.CGRectGetWidth(frame)), "height": round(Quartz.CGRectGetHeight(frame)),
    }


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


def _pids_showing_a_window() -> set[int]:
    """Which processes are actually displaying something, according to the window server.

    Asked because an application can be running more than once. macOS reports each instance
    separately, and only one of them may own the window a person is looking at — the others are
    alive with nothing on screen. Accessibility is per process, so reading the wrong instance
    returns an empty tree that looks exactly like an app refusing to expose itself.
    """
    import Quartz

    try:
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
    except Exception:  # noqa: BLE001 — a preference must never be the thing that fails
        return set()
    showing = set()
    for window in windows:
        bounds = window.get("kCGWindowBounds") or {}
        # Layer 0 and a real size: a document window, not a menu-bar strip or an overlay.
        if window.get("kCGWindowLayer") == 0 and int(bounds.get("Width", 0)) > 200 and int(bounds.get("Height", 0)) > 200:
            showing.add(window.get("kCGWindowOwnerPID"))
    return showing


def find_app_pid(name: str) -> Optional[int]:
    """Resolve a running app to its PID by localized name or bundle id (case-insensitive;
    substring on the name). Returns None if it is not running.

    When several instances match, the one showing a window wins. It used to be whichever macOS
    listed first, which for an app left running with no window and then reopened was the empty
    one — and the empty one reads as zero elements, indistinguishable from an app that will not
    expose itself at all. That mistake cost several wrong theories about RStudio, whose real
    interface was 144 elements deep in the process nobody was asking.
    """
    needle = name.strip().lower()
    running_apps = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
    matches = [
        app.processIdentifier()
        for app in running_apps
        if needle in (_string(app.bundleIdentifier()).lower(), _string(app.localizedName()).lower())
    ] or [
        app.processIdentifier()
        for app in running_apps
        if needle in _string(app.localizedName()).lower() or needle in _string(app.bundleIdentifier()).lower()
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
    """The regular (Dock-visible) apps currently running, by name — the situational awareness a
    person gets from glancing at the Dock, so the model knows what it can switch to."""
    regular = AppKit.NSApplicationActivationPolicyRegular
    apps = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
    names = [_string(app.localizedName()) for app in apps if app.activationPolicy() == regular]
    return [name for name in names if name]


# The only bridge between the two things that describe a window. Accessibility knows what a
# window *is* — apps publish exactly their real windows, one entry each — while CoreGraphics
# knows what the system has *numbered*, which includes every compositing layer: title bars,
# shadows, sheet backdrops. Measured on one ordinary machine, CoreGraphics reported 19 layers
# for RStudio's single window and 21 for Finder's five. So AX decides what exists and CG
# supplies the identity, and `_AXUIElementGetWindow` is the only call that joins them. It is
# unprefixed-private but ABI-stable and has been the standard answer for a decade; there is no
# public equivalent, and the alternative — matching by frame — breaks on two stacked windows.
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
                        "ApplicationServices", {},
                        bundle_path="/System/Library/Frameworks/ApplicationServices.framework",
                    )
                    loaded: dict[str, Any] = {}
                    objc.loadBundleFunctions(bundle, loaded, [("_AXUIElementGetWindow", _WINDOW_ID_SIGNATURE)])
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
    """One window an application publishes, as accessibility describes it.

    ``document`` and ``main`` are here because a title is not an identity: two Finder windows
    were both called "Applications", neither was main, and nothing else was reported — so the
    model had no way to choose between them and no way to say which one it had chosen. A window
    that holds a file says which file, and an application says which of its windows is the main
    one; both are free, and together they separate almost everything a title cannot."""

    window_id: int
    title: str
    minimized: bool
    document: str = ""
    main: bool = False
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)   # x, y, width, height


def application_root(pid: int) -> Any:
    """An application's AX root, with the messaging timeout set and its rich tree asked for.

    Every read of an application goes through here, because every read needs the same three
    things and the one path that skipped the third was wrong in a way nothing caught: a fresh
    ``windows_of`` asked Electron apps for their windows *before* setting ``AXManualAccessibility``,
    and an Electron app publishes nothing until it is asked. RStudio — eleven real windows — came
    back with none, was reported as "does not publish its windows to accessibility", and became
    unaddressable, which is the exact opposite of true."""
    root = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(root, active_tuning().duration(Tunable.ax_messaging_seconds))
    enable_rich_accessibility(root)
    return root


def windows_of(pid: int) -> list[WindowRecord]:
    """Every real window an application publishes, with the id the window server minted for it.

    This is what makes a window addressable. The title comes from ``AXTitle`` rather than from
    ``kCGWindowName`` deliberately: the CoreGraphics name requires the Screen Recording grant,
    which this harness has never asked for, while ``AXTitle`` needs only Accessibility — the
    grant computer control already requires and already checks. One permission instead of two,
    for the same words.

    An empty list means the app publishes nothing readable *or* Accessibility is not granted.
    The caller distinguishes them; this function does not guess."""
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
                bounds = (int(rectangle.origin.x), int(rectangle.origin.y),
                          int(rectangle.size.width), int(rectangle.size.height))
        records.append(WindowRecord(
            window_id=window_id,
            title=_string(_single(window, TITLE)) or _string(_single(window, VALUE)),
            minimized=bool(_single(window, "AXMinimized")),
            document=_string(_single(window, "AXDocument")),
            main=bool(_single(window, "AXMain")),
            bounds=bounds,
        ))
    return records


def _published_windows(root: Any) -> list[Any]:
    """Every window an application exposes, however it chooses to expose them.

    ``AXWindows`` is the usual answer and it is not the only one: RStudio does not publish that
    attribute at all — its application element offers ``AXChildren``, ``AXMainWindow`` and
    ``AXFocusedWindow`` instead. Reading the one attribute and treating its absence as *this
    application has no windows* turned five real windows into a row saying RStudio "does not
    publish its windows to accessibility", which was both wrong and the reason a whole task could
    not be started. Ask every way, keep whatever answers, and let the window-server id decide
    what is a duplicate."""
    candidates: list[Any] = list(_single(root, WINDOWS) or [])
    candidates.extend(
        child for child in (_single(root, CHILDREN) or [])
        if _string(_single(child, ROLE)) == "AXWindow"
    )
    candidates.extend(
        window for window in (_single(root, MAIN_WINDOW), _single(root, FOCUSED_WINDOW))
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

# AXMenuItemCmdModifiers is a bitfield, and Command is inverted: the bit means *not* Command,
# because Command is the default for a menu shortcut. Everything downstream reads a chord string,
# so the inversion is undone exactly here rather than being a fact every caller has to remember.
_MENU_MODIFIER_BITS = ((1, "shift"), (2, "option"), (4, "control"))

# The virtual key codes macOS uses for menu items that have no character (function keys, arrows).
_MENU_VIRTUAL_KEYS = {
    0x7A: "f1", 0x78: "f2", 0x63: "f3", 0x76: "f4", 0x60: "f5", 0x61: "f6",
    0x62: "f7", 0x64: "f8", 0x65: "f9", 0x6D: "f10", 0x67: "f11", 0x6F: "f12",
    0x7B: "left", 0x7C: "right", 0x7D: "down", 0x7E: "up",
    0x24: "return", 0x30: "tab", 0x33: "delete", 0x35: "escape", 0x31: "space",
    0x73: "home", 0x77: "end", 0x74: "pageup", 0x79: "pagedown",
}


def _menu_chord(item: Any) -> str:
    """The chord a menu item advertises, spelled the way ``press`` accepts it, or ``""``.

    Read attribute by attribute rather than out of :func:`_read`'s batch. The batch exists to make
    a tree walk one round-trip per node and its attribute list is fixed; the menu-command
    attributes are not in it, so every item looked like it had no shortcut and the whole menu
    pruned itself to nothing — an empty answer that looked exactly like an application with no
    shortcuts to advertise.

    A character in the private-use range is AppKit's glyph for a key that has no character at all
    (the arrows, the function keys, Home, Page Up). Spelling it verbatim would hand back
    ``cmd+``, which ``press`` cannot parse and no one can read, so the virtual key code is
    the answer for those."""
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
    if not bits & 8:      # the inverted Command bit: unset means Command *is* held
        modifiers.insert(0, "cmd")
    return "+".join([*modifiers, character])


def shortcuts_of(pid: int, *, limit: int = 400) -> list[dict[str, Any]]:
    """The application's menu bar, as a tree, with the chord each item advertises.

    This exists because a model asked to reach RStudio's Help pane pressed ``Control+3`` from
    memory, got the Environment pane, and had no way to find out what the real shortcut was — the
    menu bar was never part of what a read returned, so the application's own published answer to
    "how do I get there" was the one thing it could not see. Guessing was not carelessness; it was
    the only option on offer.

    A menu *is* a tree, so it is returned as one: ``{"title": "View", "items": [{"title": "Panes",
    "items": [{"title": "Help", "keys": "cmd+shift+3"}]}]}``. Flattening it to ``"View > Panes >
    Help"`` would invent a path syntax nobody else here speaks and make the caller parse it back
    out of punctuation to learn what contains what — the same trade that turned a window's list of
    lines into one newline-joined blob.

    Branches with no shortcut anywhere beneath them are dropped: the whole menu bar is hundreds of
    items, and what makes this answerable is that only a fraction of them advertise a chord. Each
    ``keys`` is spelled the way ``press`` accepts it, so acting on one needs no translation step
    where a mistake could enter."""
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
            # A submenu is a child *menu* holding the items, so the walk goes through it either
            # way; an item with neither a chord nor a chord below it is not an answer to anything.
            items = branch(child, depth + 1)
            if not chord and not items:
                continue
            node: dict[str, Any] = {"title": title} if title else {}
            if chord:
                node["keys"] = chord
            if items:
                node["items"] = items
            nodes.append(node if node.get("title") or node.get("keys") else {"items": items})
        # An untitled wrapper (the anonymous AXMenu under every menu-bar item) is not a level
        # anybody means; lift its contents so the tree matches the menus a person sees.
        lifted: list[dict[str, Any]] = []
        for node in nodes:
            if set(node) == {"items"}:
                lifted.extend(node["items"])
            else:
                lifted.append(node)
        return lifted

    return branch(menu_bar, 0)


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
                self._thread = threading.Thread(target=self._run, name="frank-ax-prewarm", daemon=True)
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


# There is no depth limit. There was one — four levels — and it was measured to hide most of
# every interface: named elements went up 3.8x in Finder, 10x in System Settings and 59x in
# Chrome when it was removed, because the things a person names sit below the things that
# merely contain them. A window's own rows were stubs.
#
# It was also a guard on the wrong quantity. Depth does not predict what a walk costs: Photos
# bottoms out at depth 6 and takes 0.41s, while Claude reaches depth 35 in 0.19s — twice as
# deep, half the time. Cost tracks how quickly an app answers accessibility queries, so the
# budget below is in seconds, which is the thing actually being protected. Any fixed depth
# would be a number that truncates some app: 16 looked generous until Claude needed 35.
#
# The walk is already protected from a tree that does not end: a visited set breaks reference
# cycles, and anything off screen is skipped with its subtree.
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
    """Push a node's children so the stack yields them in document order (reversed on the
    way in), tagging each with its next depth and child-index path."""
    stack.extend(
        (children[index], depth + 1, path + (index,))
        for index in range(len(children) - 1, -1, -1)
    )


def _collect(
    seeds: list[tuple[Any, int, tuple[int, ...]]],
    window_rect: Any,
    budget_seconds: float,
) -> tuple[list[Element], int, bool]:
    """Walk the seed nodes into a flat element list, shallow-first, until the tree ends or the
    budget does.

    Shallow-first matters when the budget runs out: what is kept is the top of the tree, which
    is what a person would name first. A structural container still holding unwalked children
    when the walk stops becomes a region stand-in carrying its child count, so a truncated read
    says it is truncated instead of looking merely empty. Iterative traversal keeps it fast; a
    visited set breaks AX reference cycles."""
    elements: list[Element] = []
    seen: set[Any] = set()
    deadline = time.perf_counter() + budget_seconds
    exhausted = False
    stack: list[tuple[Any, int, tuple[int, ...]]] = list(reversed(seeds))
    while stack:
        if time.perf_counter() > deadline:
            # Everything still queued is unread. Each becomes a stand-in carrying its child
            # count, so a truncated read is visibly truncated rather than quietly short.
            exhausted = True
            for pending_node, pending_depth, pending_path in reversed(stack):
                pending = _read(pending_node)
                if pending is None:
                    continue
                pending_children = _child_nodes(pending)
                elements.append(_make_element(
                    pending_node, pending, _string(pending.get(ROLE)), _frame_of(pending),
                    pending_depth, pending_path,
                    as_region=True, child_count=len(pending_children) or 1,
                ))
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
        # A real rectangle that does not intersect the window is off screen: skip it and its
        # subtree. A frameless or empty-rect node is a container we still descend.
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
        # Text carries no control subtree: its content is its value, and anything nested inside
        # is presentation. Everything else is descended into, however far down it goes.
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
    """Walk one app's AX tree into a flat, shallow-first list of on-screen elements, each
    carrying its raw AX attributes and actions. ``window`` scopes it ("focused", "main",
    "all"); ``root_handle``/``root_path`` re-root the walk at a previously-seen region so the
    model can drill into it."""
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

    budget = budget_seconds if budget_seconds is not None else active_tuning().duration(
        Tunable.ax_walk_budget_seconds)
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

