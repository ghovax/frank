"""What a script can be pointed at: one window, or one browser tab.

A target is a *place* — a thing with a tree inside it, a title, a focus state and a lifetime.
An application is not a place: it has zero windows or five, and naming one addresses none of them
in particular. That distinction is the whole of this module, and it exists because naming a place
by its application's display name resolved to the wrong process the first time two copies of one
application were open, and cost an investigation before anybody suspected the address rather than
the thing addressed.

Neither platform makes us invent an identifier. macOS assigns every window a ``kCGWindowNumber``
— system-generated, unique, stable for as long as the window lives — and Chrome assigns every tab
a DevTools target id, which :mod:`frank.computer.web` already keeps a registry of. Using the
platform's own identity is what makes two windows of one application as distinguishable as two
windows of different ones.

The model never sees which surface a target belongs to. That is the point: ``win-10337`` and
``tab-3`` are both places to act, and choosing the machinery behind them is this module's job
rather than the model's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

NATIVE_PREFIX = "win"
BROWSER_PREFIX = "tab"

# A window smaller than this in either direction is furniture — a menu-bar extra, a shadow, a
# tooltip. The same threshold `engine._displayed_window` already applies, for the same reason:
# twenty-two on-screen windows on an ordinary machine are mostly Control Center items nobody
# means when they say "that window".
MINIMUM_WINDOW_EDGE = 120

# Processes that own on-screen windows nobody addresses. Named rather than inferred, because the
# alternative — guessing from size alone — also hides small real windows.
FURNITURE_OWNERS = frozenset({"Control Center", "Window Server", "Dock", "Spotlight", "Notification Center"})


@dataclass(frozen=True)
class Target:
    """One addressable place, in the vocabulary the model reads.

    ``surface`` is present for the dispatcher and deliberately absent from what the model is
    shown: it is the implementation detail this whole design exists to stop leaking.
    """

    id: str
    app: str
    title: str
    surface: str                      # "computer" or "browser" — for routing, never for the model
    focused: bool = False
    url: str = ""
    address: dict[str, Any] = field(default_factory=dict)   # how the surface finds it again

    def described(self) -> dict[str, Any]:
        """The form handed to the model: a place, its owner, what it says, and whether it is live."""
        described: dict[str, Any] = {"id": self.id, "app": self.app, "title": self.title}
        if self.focused:
            described["focused"] = True
        if self.url:
            described["url"] = self.url
        return described


def _native_targets() -> list[Target]:
    """Every on-screen window worth addressing, from the window server rather than accessibility.

    The window server is asked because it is the only thing that knows what is actually *shown*:
    an application can publish an accessibility tree for a window it is not displaying, and can
    display one it publishes nothing about."""
    try:
        import Quartz
    except Exception:  # noqa: BLE001 — a machine without Quartz simply has no native targets
        return []
    try:
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
    except Exception:  # noqa: BLE001 — never let enumeration take the tool down
        logger.debug("Could not enumerate windows", exc_info=True)
        return []

    frontmost = _frontmost_process_id()
    targets: list[Target] = []
    for window in windows:
        owner = str(window.get("kCGWindowOwnerName") or "")
        if owner in FURNITURE_OWNERS:
            continue
        bounds = window.get("kCGWindowBounds") or {}
        width, height = float(bounds.get("Width", 0)), float(bounds.get("Height", 0))
        if width < MINIMUM_WINDOW_EDGE or height < MINIMUM_WINDOW_EDGE:
            continue
        number = window.get("kCGWindowNumber")
        process_id = window.get("kCGWindowOwnerPID")
        if number is None or process_id is None:
            continue
        targets.append(Target(
            id=f"{NATIVE_PREFIX}-{int(number)}",
            app=owner,
            title=str(window.get("kCGWindowName") or ""),
            surface="computer",
            focused=int(process_id) == frontmost,
            address={"window_number": int(number), "pid": int(process_id)},
        ))
    return targets


def _frontmost_process_id() -> int:
    """The process the user is currently in, or 0. Only used to mark a target as focused."""
    try:
        from AppKit import NSWorkspace

        application = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(application.processIdentifier()) if application is not None else 0
    except Exception:  # noqa: BLE001 — a missing answer means "focused is unknown", not a failure
        return 0


def _browser_targets() -> list[Target]:
    """Every open tab in the browser frank is connected to, or none if it is not connected.

    Not connecting on demand: enumerating targets is something that happens on every turn, and a
    listing must never be the thing that starts a browser session or raises because one is not
    running."""
    try:
        from frank.computer import web
    except Exception:  # noqa: BLE001
        return []
    surface = getattr(web, "SURFACE", None)
    if surface is None:
        return []
    try:
        listing = surface.open_tabs()
    except Exception:  # noqa: BLE001 — a browser that is not connected simply offers no targets
        logger.debug("Could not enumerate browser tabs", exc_info=True)
        return []
    targets = []
    for tab in listing:
        identifier = str(tab.get("id") or "")
        if not identifier:
            continue
        targets.append(Target(
            id=identifier if identifier.startswith(BROWSER_PREFIX) else f"{BROWSER_PREFIX}-{identifier}",
            app=str(tab.get("app") or "Browser"),
            title=str(tab.get("title") or ""),
            surface="browser",
            focused=bool(tab.get("active")),
            url=str(tab.get("url") or ""),
            address={"tab_id": identifier},
        ))
    return targets


def list_windows() -> list[Target]:
    """Native windows only, from the window server. Never touches a browser."""
    return _native_targets()


def list_tabs() -> list[Target]:
    """Browser tabs only. Connects to the browser, so it is not on any native path."""
    return _browser_targets()


def list_targets() -> list[Target]:
    """Every place a script can be pointed at, windows and tabs together, in one list.

    Order is stable — browser tabs after native windows, each in the platform's own order — so
    that a diff between two listings reflects the world changing rather than the enumeration.

    Enumerating tabs means *connecting to the browser*, which is why the two halves are also
    available separately and why anything on a native path must use :func:`list_windows`. Asking
    for the whole world in order to resolve one window number reached into the user's live Chrome
    over the DevTools protocol, and hung the process that asked."""
    return _native_targets() + _browser_targets()


def _find(target_id: str, among: list[Target]) -> Optional[Target]:
    wanted = (target_id or "").strip()
    return next((target for target in among if target.id == wanted), None) if wanted else None


def find_window(target_id: str) -> Optional[Target]:
    """The native window with this id, or ``None``. Browser-free, so it cannot block on Chrome."""
    return _find(target_id, list_windows())


def find_tab(target_id: str) -> Optional[Target]:
    return _find(target_id, list_tabs())


def find_target(target_id: str) -> Optional[Target]:
    """The target with this id on either surface, or ``None`` if it has gone.

    Re-enumerates rather than consulting a cache. A cached target is a promise about a window
    that may have closed since, and the failure it produces — acting into a place that no longer
    exists — is exactly the one this module is meant to make impossible to reach silently.

    Windows are searched first, so resolving a window id never enumerates tabs and never opens a
    browser connection."""
    found = find_window(target_id)
    return found if found is not None else find_tab(target_id)


def describe_windows() -> list[dict[str, Any]]:
    """Every native window, as the model reads it. Browser-free."""
    return [target.described() for target in list_windows()]


def describe_all(targets: Optional[list[Target]] = None) -> list[dict[str, Any]]:
    """The whole listing, as the model reads it."""
    return [target.described() for target in (targets if targets is not None else list_targets())]


def difference(before: list[Target], after: list[Target]) -> dict[str, Any]:
    """What changed between two listings: added, removed, and changed in place.

    Sent instead of the whole list because a full enumeration repeats twenty unchanged lines to
    report that one window's title changed. The same principle as an action's diff, applied to
    the world rather than to one window."""
    before_by_id = {target.id: target for target in before}
    after_by_id = {target.id: target for target in after}
    added = [target.described() for identifier, target in after_by_id.items() if identifier not in before_by_id]
    removed = [identifier for identifier in before_by_id if identifier not in after_by_id]
    changed = [
        target.described()
        for identifier, target in after_by_id.items()
        if identifier in before_by_id and before_by_id[identifier].described() != target.described()
    ]
    report: dict[str, Any] = {}
    if added:
        report["added"] = added
    if removed:
        report["removed"] = removed
    if changed:
        report["changed"] = changed
    return report
