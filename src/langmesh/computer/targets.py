"""What a script can be pointed at: one window, or one browser tab, each a place with a tree and a lifetime."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from langmesh.computer import accessibility, permissions

logger = logging.getLogger(__name__)

NATIVE_PREFIX = "win"
BROWSER_PREFIX = "tab"

# The two vocabularies a place can answer to, since a page can be navigated and scripted where a window cannot.
WINDOW_VOCABULARY = "window"
PAGE_VOCABULARY = "page"

# Applications whose windows are pages, reachable over the DevTools protocol rather than the accessibility tree.
BROWSER_OWNERS = frozenset(
    {"Google Chrome", "Chromium", "Google Chrome Canary", "Google Chrome Beta"}
)

# Processes that own windows nobody addresses, named rather than inferred so small real windows still show.
FURNITURE_OWNERS = frozenset(
    {"Control Center", "Window Server", "Dock", "Spotlight", "Notification Center"}
)


@dataclass(frozen=True)
class Target:
    """One addressable place, in the vocabulary the model reads, with the surface kept off what it is shown."""

    id: str
    app: str
    title: str
    surface: str  # "computer" or "browser" — for routing, never for the model
    can: str = WINDOW_VOCABULARY
    focused: bool = False
    visible: bool = True
    addressable: bool = True
    main: bool = False
    document: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    url: str = ""
    note: str = ""
    address: dict[str, Any] = field(default_factory=dict)  # how the surface finds it again

    def described(self) -> dict[str, Any]:
        """The form handed to the model, where a key is absent rather than present and false."""
        described: dict[str, Any] = {
            "id": self.id,
            "app": self.app,
            "title": self.title,
            "can": self.can,
        }
        if self.focused:
            described["focused"] = True
        if self.main:
            described["main"] = True
        # What this window holds, which is the strongest discriminator between two otherwise identical windows.
        if self.document:
            described["document"] = self.document
        if not self.visible:
            described["visible"] = False
        if not self.addressable:
            described["addressable"] = False
        if self.url:
            described["url"] = self.url
        # Where it is and how big, so "the one on the left" is a thing the model can answer.
        if self.bounds and any(self.bounds):
            left, top, width, height = self.bounds
            described["bounds"] = {"x": left, "y": top, "width": width, "height": height}
        if self.note:
            described["note"] = self.note
        return described


def _window_server_windows() -> tuple[dict[int, dict[str, Any]], set[int]]:
    """Every window the system has numbered, and the subset on screen, as two listings because they mean different things."""
    try:
        import Quartz
    except Exception:  # noqa: BLE001 — a machine without Quartz simply has no native targets
        return {}, set()
    try:
        everything = (
            Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
            or []
        )
        on_screen = (
            Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
            or []
        )
    except Exception:  # noqa: BLE001 — never let enumeration take the tool down
        logger.debug("could not enumerate windows", exc_info=True)
        return {}, set()

    numbered: dict[int, dict[str, Any]] = {}
    for window in everything:
        owner = str(window.get("kCGWindowOwnerName") or "")
        number, process_id = window.get("kCGWindowNumber"), window.get("kCGWindowOwnerPID")
        if owner in FURNITURE_OWNERS or number is None or process_id is None:
            continue
        numbered[int(number)] = {"app": owner, "pid": int(process_id)}
    visible = {
        int(window["kCGWindowNumber"])
        for window in on_screen
        if window.get("kCGWindowNumber") is not None
    }
    return numbered, visible


def _frontmost_process_id() -> int:
    """The process the user is currently in, or 0. Only used to mark a target as focused."""
    try:
        from AppKit import NSWorkspace

        application = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(application.processIdentifier()) if application is not None else 0
    except Exception:  # noqa: BLE001 — a missing answer means "focused is unknown", not a failure
        return 0


def _native_targets() -> list[Target]:
    """Every window worth addressing, named by accessibility and numbered by the window server."""
    numbered, visible_ids = _window_server_windows()
    if not numbered:
        return []
    frontmost = _frontmost_process_id()
    if not permissions.accessibility_granted():
        return _collapsed(numbered, visible_ids, frontmost, note="")

    targets: list[Target] = []
    silent_pids: set[int] = set()
    for pid in sorted({entry["pid"] for entry in numbered.values()}):
        try:
            published = accessibility.windows_of(pid)
        except Exception:  # noqa: BLE001 — one unresponsive application must not empty the list
            logger.debug("could not read the windows of pid %s", pid, exc_info=True)
            published = []
        if not published:
            # Running and publishing nothing addressable, kept as one row so it is seen rather than silently missing.
            silent_pids.add(pid)
            continue
        for record in published:
            entry = numbered.get(record.window_id)
            app = entry["app"] if entry else accessibility.app_name_for_pid(pid)
            on_screen = record.window_id in visible_ids
            is_page = app in BROWSER_OWNERS
            targets.append(
                Target(
                    id=f"{NATIVE_PREFIX}-{record.window_id}",
                    app=app,
                    title=record.title,
                    surface="browser" if is_page else "computer",
                    can=PAGE_VOCABULARY if is_page else WINDOW_VOCABULARY,
                    focused=pid == frontmost and on_screen,
                    visible=on_screen and not record.minimized,
                    main=record.main,
                    document=_readable_document(record.document),
                    bounds=record.bounds,
                    note="minimized" if record.minimized else "",
                    address={"window_number": record.window_id, "pid": pid},
                )
            )
    if silent_pids:
        # Background services and unclaimed layers are withheld, because neither is a place anybody means.
        answered_apps = {target.app for target in targets}
        withheld = {
            number: entry
            for number, entry in numbered.items()
            if entry["pid"] in silent_pids
            and entry["app"] not in answered_apps
            and _is_ordinary_application(entry["pid"])
        }
        targets.extend(
            _collapsed(
                withheld,
                visible_ids,
                frontmost,
                note=(
                    "This application does not publish its windows to accessibility, so they cannot be addressed individually."
                ),
            )
        )
    return targets


def _is_ordinary_application(pid: int) -> bool:
    """Whether this process is a Dock-visible application rather than a background service."""
    try:
        from AppKit import NSApplicationActivationPolicyRegular, NSRunningApplication

        application = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return (
            application is not None
            and application.activationPolicy() == NSApplicationActivationPolicyRegular
        )
    except Exception:  # noqa: BLE001 — an unanswerable question is not a reason to hide a window
        return True


def _readable_document(document: str) -> str:
    """A window's document as a person would write it: a plain path, or the url as it stands."""
    if not document:
        return ""
    if document.startswith("file://"):
        from urllib.parse import unquote, urlparse

        return unquote(urlparse(document).path)
    return document


def _collapsed(
    numbered: dict[int, dict[str, Any]], visible_ids: set[int], frontmost: int, *, note: str
) -> list[Target]:
    """One row per application, for windows that cannot be named. Never addressable, always seen."""
    by_app: dict[str, dict[str, Any]] = {}
    for number, entry in sorted(numbered.items()):
        seen = by_app.setdefault(
            entry["app"], {"pid": entry["pid"], "count": 0, "visible": False, "number": number}
        )
        seen["count"] += 1
        seen["visible"] = seen["visible"] or number in visible_ids
    return [
        Target(
            id=f"{NATIVE_PREFIX}-{seen['number']}",
            app=app,
            title="",
            surface="browser" if app in BROWSER_OWNERS else "computer",
            can=PAGE_VOCABULARY if app in BROWSER_OWNERS else WINDOW_VOCABULARY,
            focused=seen["pid"] == frontmost,
            visible=seen["visible"],
            addressable=False,
            note=f"{seen['count']} window(s). {note}".strip()
            if note
            else f"{seen['count']} window(s)",
            address={"window_number": seen["number"], "pid": seen["pid"]},
        )
        for app, seen in sorted(by_app.items())
    ]


def _browser_targets() -> list[Target]:
    """Every open tab of an already-connected browser, never connecting, since a listing must not start a session."""
    try:
        from langmesh.computer import web
    except Exception:  # noqa: BLE001
        return []
    surface = getattr(web, "SURFACE", None)
    if surface is None:
        return []
    try:
        listing = surface.open_tabs()
    except Exception:  # noqa: BLE001 — a browser that is not connected simply offers no targets
        logger.debug("could not enumerate browser tabs", exc_info=True)
        return []
    targets = []
    for tab in listing:
        identifier = str(tab.get("id") or "")
        if not identifier:
            continue
        targets.append(
            Target(
                id=identifier
                if identifier.startswith(BROWSER_PREFIX)
                else f"{BROWSER_PREFIX}-{identifier}",
                app=str(tab.get("app") or "Browser"),
                title=str(tab.get("title") or ""),
                surface="browser",
                can=PAGE_VOCABULARY,
                focused=bool(tab.get("active")),
                url=str(tab.get("url") or ""),
                address={"tab_id": identifier, "window_number": tab.get("window_number")},
            )
        )
    return targets


def vocabularies() -> dict[str, dict[str, str]]:
    """What each kind of place can be told to do, with every call's shape, read off the surfaces themselves."""
    from langmesh.computer import engine, web

    return {
        WINDOW_VOCABULARY: engine.SURFACE.signatures(),
        PAGE_VOCABULARY: web.SURFACE.signatures(),
    }


def _worth_naming(target: Target) -> tuple:
    """Sort key: the window a person would mean, first."""
    width, height = target.bounds[2], target.bounds[3]
    return (
        not target.focused,
        not target.visible,
        not target.main,
        not target.document,
        -(width * height),
        target.app.lower(),
    )


def list_windows() -> list[Target]:
    """Native windows only, ordered so the likeliest is first."""
    return sorted(_native_targets(), key=_worth_naming)


def list_tabs() -> list[Target]:
    """Browser tabs only, and only for a browser something has already connected to."""
    return _browser_targets()


def list_targets() -> list[Target]:
    """Every place a script can be pointed at, in a stable order so a diff reflects the world rather than the ordering."""
    global _warmed
    targets = _native_targets() + _browser_targets()
    # Set on the way out, so nothing checking `warm()` mid-listing is told it is cheap and then waits.
    _warmed = True
    return targets


def _find(target_id: str, among: list[Target]) -> Optional[Target]:
    wanted = (target_id or "").strip()
    return next((target for target in among if target.id == wanted), None) if wanted else None


def find_window(target_id: str) -> Optional[Target]:
    """The native window with this id, or ``None``. Browser-free, so it cannot block on Chrome."""
    return _find(target_id, list_windows())


def find_tab(target_id: str) -> Optional[Target]:
    return _find(target_id, list_tabs())


def find_target(target_id: str) -> Optional[Target]:
    """The target with this id on either surface, re-enumerated rather than read from a cache that may be stale."""
    found = find_window(target_id)
    return found if found is not None else find_tab(target_id)


def describe_windows() -> list[dict[str, Any]]:
    """Every native window, as the model reads it. Browser-free."""
    return [target.described() for target in list_windows()]


# The keys whose meaning cannot be read off the name, which is why the obvious ones are left out.
LEGEND_KEYS = ("visible", "can", "main", "addressable")


def legend() -> dict[str, str]:
    """What the non-obvious keys of a listing mean, for the model that has to read them."""
    from langmesh.computer.surface import message_loader

    message = message_loader("computer")
    return {key: message(f"legend_{key}") for key in LEGEND_KEYS}


def describe_all(targets: Optional[list[Target]] = None) -> list[dict[str, Any]]:
    """The whole listing, as the model reads it."""
    return [target.described() for target in (targets if targets is not None else list_targets())]


# Whether this process has completed an enumeration, the first of which pays for the accessibility connections.
_warmed = False


def warm() -> bool:
    """Whether reading the screen is cheap in this process yet."""
    return _warmed


def prewarm() -> None:
    """Open this process's accessibility connections before a turn needs them, throwing the enumeration away."""
    global _warmed
    try:
        _native_targets()
    except Exception:  # noqa: BLE001 — warming is an optimisation, never a reason to fail
        logger.debug("could not warm the screen listing", exc_info=True)
    _warmed = True


def context_block() -> dict[str, Any]:
    """What the model is told about the screen once per turn: the places, and the vocabularies they answer to."""
    from langmesh.computer.surface import message_loader

    targets = list_targets()
    block: dict[str, Any] = {
        "targets": describe_all(targets),
        "primitives": vocabularies(),
        "legend": legend(),
    }
    # Said once as a condition at the top rather than repeated as a note on every row.
    if not permissions.accessibility_granted():
        block["blocked"] = message_loader("computer")("screen_blocked")
    return block


def difference(before: list[Target], after: list[Target]) -> dict[str, Any]:
    """What changed between two listings, sent instead of the whole list so unchanged rows are not repeated."""
    before_by_id = {target.id: target for target in before}
    after_by_id = {target.id: target for target in after}
    added = [
        target.described()
        for identifier, target in after_by_id.items()
        if identifier not in before_by_id
    ]
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
