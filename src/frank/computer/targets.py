"""What a script can be pointed at: one window, or one browser tab.

A target is a *place* — a thing with a tree inside it, a title, a focus state and a lifetime.
An application is not a place: it has zero windows or five, and naming one addresses none of them
in particular. That distinction is the whole of this module, and it exists because naming a place
by its application's display name resolved to the wrong process the first time two copies of one
application were open, and cost an investigation before anybody suspected the address rather than
the thing addressed.

**Accessibility says what exists; the window server says what it is called and whether you can see
it.** Both halves are needed and neither is sufficient. CoreGraphics numbers every compositing
layer — title bars, shadows, sheet backdrops — so it reported nineteen entries for RStudio's single
window and twenty-one for Finder's five; no size or layer filter separates those from real windows,
which is why the pixel floor this module used to apply removed nothing on screen and a hundred and
forty-four nameless strips off it. Accessibility publishes exactly the real windows, one entry each.
So AX decides what exists, and :func:`accessibility.windows_of` carries the window-server id across
for identity.

**Every window, on every Space.** ``kCGWindowListOptionOnScreenOnly`` means *on the current Space
and not minimized*, which made fifty-six of seventy-four windows unaddressable and reported them as
closed — a window on the next desktop is not a window that never opened. Synthesized input has never
needed a window to be on screen (every event goes through ``CGEventPostToPid``), so the restriction
bought nothing and cost the model most of the machine. What survives is ``visible``, a fact rather
than a filter.

The model never sees which surface a target belongs to. That is the point: ``win-10337`` and
``tab-3`` are both places to act, and choosing the machinery behind them is this module's job
rather than the model's. What it does see is ``can`` — the vocabulary a place answers to, which is
a fact about the place rather than about the implementation underneath it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from frank.computer import accessibility, permissions

logger = logging.getLogger(__name__)

NATIVE_PREFIX = "win"
BROWSER_PREFIX = "tab"

# The two vocabularies a place can answer to. A window and a page differ in what they can be told
# to do — a page can be navigated and scripted, a window cannot — and a script cannot be written
# without knowing which. This is not the surface name: it says what this place *is*, which is why
# it is safe to show where "computer" and "browser" were not.
WINDOW_VOCABULARY = "window"
PAGE_VOCABULARY = "page"

# Applications whose windows are pages: their real interface is reachable over the DevTools
# protocol, which reads structure the accessibility tree flattens or omits entirely.
BROWSER_OWNERS = frozenset({"Google Chrome", "Chromium", "Google Chrome Canary", "Google Chrome Beta"})

# Processes that own windows nobody addresses. Named rather than inferred, because the
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
    can: str = WINDOW_VOCABULARY
    focused: bool = False
    visible: bool = True
    addressable: bool = True
    main: bool = False
    document: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    url: str = ""
    note: str = ""
    address: dict[str, Any] = field(default_factory=dict)   # how the surface finds it again

    def described(self) -> dict[str, Any]:
        """The form handed to the model: a place, its owner, what it says, what it answers to.

        A key is absent when there is nothing to say rather than present and false, so the model
        learns one shape and reads whatever it finds. ``visible`` is the exception and is always
        stated when false, because a window on another Space is a thing worth telling a person
        about before acting in it."""
        described: dict[str, Any] = {"id": self.id, "app": self.app, "title": self.title, "can": self.can}
        if self.focused:
            described["focused"] = True
        if self.main:
            described["main"] = True
        # What this window holds, when it holds a file or a page. The strongest discriminator
        # there is: two Finder windows called "Applications" are told apart by nothing else, and
        # a window showing a PDF is better named by the PDF than by its own title.
        if self.document:
            described["document"] = self.document
        if not self.visible:
            described["visible"] = False
        if not self.addressable:
            described["addressable"] = False
        if self.url:
            described["url"] = self.url
        # Where it is and how big, so two otherwise identical windows can still be told apart —
        # and so "the one on the left" is a thing the model can answer rather than guess at.
        if self.bounds and any(self.bounds):
            left, top, width, height = self.bounds
            described["bounds"] = {"x": left, "y": top, "width": width, "height": height}
        if self.note:
            described["note"] = self.note
        return described


def _window_server_windows() -> tuple[dict[int, dict[str, Any]], set[int]]:
    """Every window the system has numbered, and the subset currently on screen.

    Two listings rather than one: ``kCGWindowListOptionAll`` is the world, and the on-screen
    listing is what ``visible`` means. Asking for the world and marking the difference is the
    whole change from filtering the world down to whatever happens to be in front."""
    try:
        import Quartz
    except Exception:  # noqa: BLE001 — a machine without Quartz simply has no native targets
        return {}, set()
    try:
        everything = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        on_screen = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
    except Exception:  # noqa: BLE001 — never let enumeration take the tool down
        logger.debug("Could not enumerate windows", exc_info=True)
        return {}, set()

    numbered: dict[int, dict[str, Any]] = {}
    for window in everything:
        owner = str(window.get("kCGWindowOwnerName") or "")
        number, process_id = window.get("kCGWindowNumber"), window.get("kCGWindowOwnerPID")
        if owner in FURNITURE_OWNERS or number is None or process_id is None:
            continue
        numbered[int(number)] = {"app": owner, "pid": int(process_id)}
    visible = {
        int(window["kCGWindowNumber"]) for window in on_screen
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
    """Every window worth addressing, named by accessibility and numbered by the window server.

    When Accessibility is not granted there is nothing to join against, and the honest answer is
    neither an empty list — which reads as "you have no windows open" — nor the raw window-server
    listing, which is nineteen nameless rows for one RStudio. Each application collapses to a
    single unaddressable entry carrying the reason, so the model can see what is running, cannot
    pretend to address a window it has no name for, and knows which permission would fix it."""
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
            logger.debug("Could not read the windows of pid %s", pid, exc_info=True)
            published = []
        if not published:
            # Running, and publishing nothing addressable: a menu-bar agent, or a window it draws
            # without describing. Kept as one row so it is seen rather than silently missing.
            silent_pids.add(pid)
            continue
        for record in published:
            entry = numbered.get(record.window_id)
            app = entry["app"] if entry else accessibility.app_name_for_pid(pid)
            on_screen = record.window_id in visible_ids
            is_page = app in BROWSER_OWNERS
            targets.append(Target(
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
            ))
    if silent_pids:
        # Two things are deliberately not reported.
        #
        # Background services, because they are not places anybody means. Twelve of twenty-eight
        # rows were `coreautha`, `loginwindow`, `TextInputSwitcher`, `PressAndHold` and their
        # kind, each carrying a twenty-word note about accessibility — nearly half the listing
        # spent on windows no task will ever address. Dock-visible applications are the ones a
        # person would name, and that is a fact the system reports rather than one we guess.
        #
        # And an application that already has addressable windows, because its silent *other*
        # process is not news. RStudio runs several: one publishes the real window, another
        # publishes nothing, and both were listed as "RStudio" — the second saying this
        # application does not publish its windows to accessibility, which was flatly untrue of
        # the application the model had just been told it could drive.
        answered_apps = {target.app for target in targets}
        withheld = {
            number: entry for number, entry in numbered.items()
            if entry["pid"] in silent_pids
            and entry["app"] not in answered_apps
            and _is_ordinary_application(entry["pid"])
        }
        targets.extend(_collapsed(
            withheld, visible_ids, frontmost, note=(
                "This application does not publish its windows to accessibility, so they cannot "
                "be addressed individually."
            ),
        ))
    return targets


def _is_ordinary_application(pid: int) -> bool:
    """Whether this process is a Dock-visible application rather than a background service."""
    try:
        from AppKit import NSApplicationActivationPolicyRegular, NSRunningApplication

        application = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return application is not None and application.activationPolicy() == NSApplicationActivationPolicyRegular
    except Exception:  # noqa: BLE001 — an unanswerable question is not a reason to hide a window
        return True


def _readable_document(document: str) -> str:
    """A window's document as a person would write it: a plain path, or the url as it stands.

    ``AXDocument`` gives a ``file://`` URL, percent-encoded. Handing that to a model that is about
    to compare it against a path it read from the filesystem makes it do URL decoding to answer
    "is this the same file", which is a question the answer should not depend on."""
    if not document:
        return ""
    if document.startswith("file://"):
        from urllib.parse import unquote, urlparse

        return unquote(urlparse(document).path)
    return document


def _collapsed(numbered: dict[int, dict[str, Any]], visible_ids: set[int],
               frontmost: int, *, note: str) -> list[Target]:
    """One row per application, for windows that cannot be named. Never addressable, always seen."""
    by_app: dict[str, dict[str, Any]] = {}
    for number, entry in sorted(numbered.items()):
        seen = by_app.setdefault(entry["app"], {"pid": entry["pid"], "count": 0, "visible": False, "number": number})
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
            note=f"{seen['count']} window(s). {note}".strip() if note else f"{seen['count']} window(s)",
            address={"window_number": seen["number"], "pid": seen["pid"]},
        )
        for app, seen in sorted(by_app.items())
    ]


def _browser_targets() -> list[Target]:
    """Every open tab of an already-connected browser.

    Not connecting on demand: enumerating targets happens on every turn, and a listing must never
    be the thing that starts a browser session — Chrome asks the user to approve a debugging
    connection, so an enumeration that connected would put a consent dialog in front of them once
    per turn. Until something has deliberately opened a session, a browser contributes its
    *windows*, which the window server knows about for free, and not its tabs."""
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
            can=PAGE_VOCABULARY,
            focused=bool(tab.get("active")),
            url=str(tab.get("url") or ""),
            address={"tab_id": identifier, "window_number": tab.get("window_number")},
        ))
    return targets


def vocabularies(mutating_allowed: bool = True) -> dict[str, dict[str, str]]:
    """What each kind of place can be told to do, with the shape of every call, read off the
    surfaces themselves.

    Computed rather than written down, because a written list is a second statement of a fact the
    code already holds, and the two drift: the model was handed a description promising ``hover``
    on every target while a window implemented eleven primitives, and promising
    ``evaluate(javascript, …)`` where the code took ``expression``. The only things that knew
    better used the difference to raise. One source, so the promise and the enforcement cannot
    disagree.

    A read-only session is shown only what it may actually run. Offering a primitive that the
    permission layer will refuse is the same defect one level up — a capability advertised and
    then denied — and it had a real cost: the guidance recommends ``evaluate`` as the efficient
    way to read a page, while the classifier treats it as mutating, so the investigating agents
    the system prompt tells you to create with ``read_only`` were being pointed at the one
    technique they cannot use."""
    from frank.computer import engine, web
    from frank.runtime.permissions import MUTATING_SCREEN_PRIMITIVES

    def offered(surface) -> dict[str, str]:
        signatures = surface.signatures()
        if mutating_allowed:
            return signatures
        return {name: shape for name, shape in signatures.items() if name not in MUTATING_SCREEN_PRIMITIVES}

    return {
        WINDOW_VOCABULARY: offered(engine.SURFACE),
        PAGE_VOCABULARY: offered(web.SURFACE),
    }


def list_windows() -> list[Target]:
    """Native windows only, from accessibility and the window server. Never touches a browser."""
    return _native_targets()


def list_tabs() -> list[Target]:
    """Browser tabs only, and only for a browser something has already connected to."""
    return _browser_targets()


def list_targets() -> list[Target]:
    """Every place a script can be pointed at, windows and tabs together, in one list.

    Order is stable — browser tabs after native windows, each in the platform's own order — so
    that a diff between two listings reflects the world changing rather than the enumeration."""
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


def context_block(mutating_allowed: bool = True) -> dict[str, Any]:
    """What the model is told about the screen, once per turn.

    Structured rather than prose, and carrying the vocabularies beside the places, because those
    are the two questions a script must answer before it can be written at all: *where am I
    acting*, and *what may I call there*. Both were already computed; neither was ever handed
    over, so the first call of every screen task had to fail to find them out.

    The signatures come with the names. A description that lists them separately is a second
    statement of the same fact, and it drifted three times over before anyone noticed."""
    from frank.computer.surface import message_loader

    targets = list_targets()
    block: dict[str, Any] = {"targets": describe_all(targets), "primitives": vocabularies(mutating_allowed)}
    # Said once, as a condition, at the top. It used to be said only as a note repeated on every
    # collapsed row — twenty-five identical sentences hanging off twenty-five entries that
    # otherwise looked like places to act. A fact stated that way reads as a footnote about each
    # application rather than as the state of the whole screen, and it was ignored: a model saw a
    # list of targets and tried to use one.
    if not permissions.accessibility_granted():
        block["blocked"] = message_loader("computer")("screen_blocked")
    return block


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
