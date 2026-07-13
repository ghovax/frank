"""The web automation surface: the user's *own* Chrome, driven with Playwright over the Chrome
DevTools Protocol. One of the two surfaces built on ``surface.py`` — it shares that module's
worker, failure guard, indexed-element vocabulary, and result shapers, and adds only what is
genuinely the web's own: the DevTools connection, the aria-snapshot parse, and Playwright acting.

Why the real browser, not a copy. The point of the browser tool is to act as the user, with their
real logins and real session. Copying a profile cannot do that anymore: Google's Device Bound
Session Credentials (DBSC, on by default) tie a login to a non-exportable key in this device's
Secure Enclave, so a copied profile's short-lived cookies can never be refreshed and the session
dies within minutes. The only way to hold a real login is to use the real profile in place.

How the connection is made. Modern Chrome (M136+) refuses ``--remote-debugging-port`` on the
default profile and no longer serves the ``/json`` discovery endpoints for the permission-gated
remote-debugging path, so daisy neither launches, reopens, quits, copies, nor deletes anything.
Instead the user turns on Chrome's own switch once (chrome://inspect/#remote-debugging, "Allow
remote debugging for this browser instance") and approves the one-time permission prompt. That
starts a DevTools server on the user's live browser and writes a ``DevToolsActivePort`` file in
the profile directory holding the port and the browser-level WebSocket path
(``/devtools/browser/<uuid>``). daisy reads that file and hands the ``ws://`` URL straight to
Playwright's ``connect_over_cdp``. The direct-WebSocket form matters: the permission-gated
endpoint 404s the ``/json/*`` discovery that the ``http://`` form would need. daisy only ever
connects; the browser stays entirely the user's, and disconnecting leaves it running.

Why Playwright as the engine. The protocol transport, session juggling, frame handling (including
cross-origin iframes), actionability checks before every click, scrolling (a real hover-and-wheel
gesture routed by the browser's own scroll chaining), dialogs, downloads, file uploads, and
keyboard layouts are exactly the commodity layer Playwright maintains against Chrome's protocol
churn. daisy keeps only the model-facing observation vocabulary (parsed from Playwright's
ref-carrying accessibility snapshot into the shared ``Element``) and the connection policy above.

Threading. Playwright's sync API is thread-affine, so the ``SerialWorker`` this surface inherits is
mandatory: the Playwright instance, the connection, and the page registry are all touched only on
that one worker thread. The dispatch layer already calls into the surface off the event loop.
"""
from __future__ import annotations

import atexit
import os
import re
import tempfile
import time
from collections import deque
from itertools import count
from pathlib import Path
from typing import Any, Callable, Optional

from harness.computer.surface import Element, Surface, ToolFailure, message_loader

message = message_loader("browser")

# The user-visible Chromium browsers we can connect to and drive, mapped to the support directory
# that holds each one's DevToolsActivePort file (written when its remote-debugging switch is on).
_SUPPORT_ROOT = Path.home() / "Library" / "Application Support"
BROWSERS = {
    "chrome": {"data": _SUPPORT_ROOT / "Google" / "Chrome"},
    "edge": {"data": _SUPPORT_ROOT / "Microsoft Edge"},
    "brave": {"data": _SUPPORT_ROOT / "BraveSoftware" / "Brave-Browser"},
}

# Where the user turns on Chrome's remote-debugging switch. Surfaced to the UI so it can offer a
# one-click "open this page" button.
REMOTE_DEBUGGING_URL = "chrome://inspect/#remote-debugging"


def _not_connected_payload() -> dict:
    """The structured result the tool returns when Chrome's remote-debugging switch is off, so the
    UI can render an alert with the address and a one-click button to open it. daisy never enables
    the switch for the user; it grants full browser control, so it is their explicit choice."""
    return {
        "ok": False,
        "error": message("not_connected", enable_url=REMOTE_DEBUGGING_URL),
        "code": "browser_remote_debugging_off",
        "enable_url": REMOTE_DEBUGGING_URL,
    }


def _stale_endpoint_payload() -> dict:
    return {
        "ok": False,
        "error": message("endpoint_stale", enable_url=REMOTE_DEBUGGING_URL),
        "code": "browser_endpoint_stale",
        "enable_url": REMOTE_DEBUGGING_URL,
    }


def _devtools_websocket_url(browser: str) -> Optional[str]:
    """The browser-level DevTools WebSocket URL for the user's running Chrome, read from the
    ``DevToolsActivePort`` file its remote-debugging server writes into the profile directory (two
    lines: the port, then the ``/devtools/browser/<uuid>`` path). ``None`` when the user has not
    turned on Chrome's remote-debugging switch, so nothing wrote the file."""
    specification = BROWSERS.get(browser)
    if specification is None:
        return None
    try:
        lines = (specification["data"] / "DevToolsActivePort").read_text().splitlines()
    except OSError:
        return None
    port = lines[0].strip() if lines else ""
    path = lines[1].strip() if len(lines) > 1 else ""
    if not port or not path:
        return None
    return f"ws://127.0.0.1:{port}{path}"


class _Session:
    """Everything about the live connection, touched only from the worker thread."""

    def __init__(self, playwright_browser, context) -> None:
        self.browser = playwright_browser
        self.context = context
        self.page = None
        # Stable, model-facing tab ids for Playwright Page objects, which have no public id.
        self.tab_ids: dict[Any, str] = {}
        self.pages_by_id: dict[str, Any] = {}
        self._tab_counter = count(1)
        # A page's CDP targetId, cached once (it is stable for the page's lifetime), so tab
        # listings can join against cheap browser-level target metadata.
        self.target_ids: dict[Any, Optional[str]] = {}
        # Maps an element index from the most recent observe/find to its aria-ref, so the
        # model can act on elements by index.
        self.registry: dict[int, Optional[str]] = {}
        # The last snapshot text, for cheap "did anything change" comparisons.
        self.last_snapshot = ""
        # Dialogs auto-handled and downloads captured since the last result, drained into it.
        self.events: deque[dict] = deque(maxlen=8)

    def tab_id(self, page) -> str:
        if page not in self.tab_ids:
            identifier = f"tab{next(self._tab_counter)}"
            self.tab_ids[page] = identifier
            self.pages_by_id[identifier] = page
        return self.tab_ids[page]

    def target_id(self, page) -> Optional[str]:
        """The page's CDP targetId, read once from cached target metadata (Target.getTargetInfo
        runs no page script, so it never wakes a discarded background tab) and remembered."""
        if page not in self.target_ids:
            try:
                cdp = self.context.new_cdp_session(page)
                info = cdp.send("Target.getTargetInfo")
                cdp.detach()
                self.target_ids[page] = info.get("targetInfo", {}).get("targetId")
            except Exception:
                self.target_ids[page] = None
        return self.target_ids[page]

    def adopt(self, page) -> None:
        """Track a page and wire its dialog/download handling. Dialogs are answered immediately
        because an unanswered dialog freezes the page: alerts are acknowledged, anything asking
        a real question is declined, and both are reported in the next result so the model
        knows what happened."""
        self.tab_id(page)

        def on_dialog(dialog) -> None:
            accepted = dialog.type == "alert"
            self.events.append({
                "dialog": {"type": dialog.type, "message": dialog.message, "accepted": accepted},
            })
            try:
                if accepted:
                    dialog.accept()
                else:
                    dialog.dismiss()
            except Exception:
                pass

        def on_download(download) -> None:
            try:
                destination = os.path.join(
                    tempfile.mkdtemp(prefix="daisy-web-download-"), download.suggested_filename,
                )
                download.save_as(destination)
                self.events.append({"download": {"path": destination, "url": download.url}})
            except Exception as error:
                self.events.append({"download": {"url": download.url, "error": str(error)}})

        page.on("dialog", on_dialog)
        page.on("download", on_download)


# Observation: Playwright's ref-carrying accessibility snapshot, parsed into the shared indexed
# ``Element`` the model acts on.

# Roles that are interactive by definition. Combined with Playwright's own [cursor=pointer]
# hint this flags elements as `clickable`, so the model targets controls instead of prose.
_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch",
    "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "option", "slider", "spinbutton",
    "treeitem", "listbox", "menu", "menubar", "togglebutton", "scrollbar",
})

# Snapshot node states surfaced to the model as affordance signals.
_SURFACED_FLAGS = ("checked", "disabled", "expanded", "selected", "pressed", "active")

# One snapshot line: indentation, then `- role "name" [attr] [attr=value]: trailing text`.
_SNAPSHOT_LINE = re.compile(r"^(\s*)-\s+(?P<head>[^\s\[\":]+)(?P<rest>.*)$")
_SNAPSHOT_NAME = re.compile(r'"((?:[^"\\]|\\.)*)"')
_SNAPSHOT_ATTRS = re.compile(r"\[([a-zA-Z-]+)(?:=([^\]]*))?\]")

_MAXIMUM_ELEMENTS = 300

# A page overview with fewer readable elements than this right after a load is treated as
# "still rendering" and re-read briefly: a JS-heavy app reports itself loaded long before its
# framework has painted anything the accessibility tree can see.
_SETTLE_MINIMUM_ELEMENTS = 3
_SETTLE_WINDOW_SECONDS = 3.0

# Non-interactive text rows past this many are omitted from a full overview. They are the bulk
# of a large page's payload and rarely what an action needs; read and find reach all of them.
_PROSE_ELEMENT_BUDGET = 80

# Live-region roles: what a page announces after an action (a validation error, a status
# line). Always included in acting results, so the announcement is visible without a read.
_LIVE_REGION_ROLES = frozenset({"alert", "status"})

# Landmark container roles: the page's skeleton and the natural drill targets. There are only a
# handful per page, and omitting one (budgeting it out as if it were prose) would hide a whole
# branch the model might want to expand — so they are always kept in an overview, never budgeted.
_STRUCTURAL_ROLES = frozenset({
    "region", "navigation", "main", "complementary", "banner", "contentinfo", "form", "search",
})

# How many find matches come back at most: enough to disambiguate, small enough to stay readable.
_FIND_LIMIT = 25


def _parse_snapshot(
    snapshot: str, limit: int = _MAXIMUM_ELEMENTS, prose_budget: int = _PROSE_ELEMENT_BUDGET,
) -> tuple[list[Element], bool, int]:
    """Parse the ai-mode aria snapshot (YAML-shaped, one node per line, ``[ref=...]`` markers,
    iframe contents inlined with frame-scoped refs) into shared ``Element`` objects, each carrying
    its aria-ref as ``token``. Interactive elements always make the list; non-interactive text is
    kept up to ``prose_budget`` and counted past it. Returns (elements, truncated, omitted_text)."""
    elements: list[Element] = []
    truncated = False
    prose_kept = 0
    omitted_text = 0
    for line in snapshot.splitlines():
        match = _SNAPSHOT_LINE.match(line)
        if match is None:
            continue
        role = match.group("head")
        rest = match.group("rest")
        if role.startswith("/"):  # a property line like `- /url: ...`, not a node
            continue
        name_match = _SNAPSHOT_NAME.search(rest)
        name = name_match.group(1).replace('\\"', '"') if name_match else ""
        attributes = dict(_SNAPSHOT_ATTRS.findall(rest))
        # Trailing `: text` after the last bracket/name is the node's text content.
        tail = rest
        if name_match:
            tail = rest[name_match.end():]
        tail = _SNAPSHOT_ATTRS.sub("", tail)
        tail = tail.lstrip()
        value = tail[1:].strip() if tail.startswith(":") else ""
        if role == "text" and not name:
            name, value = value, ""

        reference = attributes.get("ref") or None
        clickable = role in _INTERACTIVE_ROLES or attributes.get("cursor") == "pointer"
        # Containers with nothing to say (no name, no text, not actable) are structure, not
        # content. Including them would bloat every listing with anonymous wrappers.
        if not (name or value or clickable):
            continue
        # Interactive elements, live-region announcements, and landmark containers always make
        # the listing; only plain text competes for the prose budget.
        if not clickable and role not in _LIVE_REGION_ROLES and role not in _STRUCTURAL_ROLES:
            if prose_kept >= prose_budget:
                omitted_text += 1
                continue
            prose_kept += 1
        if len(elements) >= limit:
            truncated = True
            break
        index = len(elements)
        element = Element(index=index, role=role, name=name, value=value or None, clickable=clickable, token=reference)
        for flag in _SURFACED_FLAGS:
            if flag in attributes:
                element.flags[flag] = attributes[flag] if attributes[flag] else True
        elements.append(element)
    return elements, truncated, omitted_text


def _snapshot(page, root_locator=None) -> str:
    """The ref-carrying accessibility snapshot: the whole page, or one element's subtree when a
    ``root_locator`` is given (refs are page-scoped either way, so subtree elements act
    normally). This is the tree-shaped progressive discovery the DOM affords: skim the page,
    then expand just the branch that matters."""
    target = root_locator if root_locator is not None else page.locator("body")
    return target.aria_snapshot(mode="ai", timeout=10_000)


# Icon fonts (Material Symbols, Font Awesome, and the like) render their ligatures as characters
# in Unicode's Private Use Areas, which leak into a text extraction as meaningless glyphs. Strip
# those, then collapse the blank lines they leave behind. The three ranges are the BMP PUA and
# the two supplementary PUA planes.
_PRIVATE_USE_CHARS = re.compile("[-\U000f0000-\U000ffffd\U00100000-\U0010fffd]")
_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def _clean_page_text(text: str) -> str:
    text = _PRIVATE_USE_CHARS.sub("", text)
    return _BLANK_LINES.sub("\n\n", text)


# One read window's worth of page text. Longer pages are read progressively via `offset`.
_READ_WINDOW_CHARS = 16_384  # Powers of 2 are nice, aren't they?

# Friendly lowercase key names accepted historically, mapped to Playwright's key vocabulary.
# Playwright key names are case-sensitive by design (a bare character's case selects the typed
# text), so this only canonicalizes the handful of named keys; anything else passes straight
# through, including chords like "Control+A" and names like "F5".
_KEY_ALIASES = {
    "enter": "Enter", "escape": "Escape", "tab": "Tab", "backspace": "Backspace",
    "delete": "Delete", "arrowdown": "ArrowDown", "arrowup": "ArrowUp",
    "arrowleft": "ArrowLeft", "arrowright": "ArrowRight", "pagedown": "PageDown",
    "pageup": "PageUp", "home": "Home", "end": "End", "space": "Space",
}

_SCROLL_DIRECTIONS = frozenset({"down", "up", "left", "right", "top", "bottom"})

# top/bottom are one huge fling of the same wheel gesture: a delta large enough to reach any end.
_SCROLL_JUMP = 1_000_000


def _safe_title(page) -> str:
    """The page's title, guarded. Reading document.title on a discarded or closed background
    tab throws (a real profile discards tabs to save memory); the page we actively drive is
    awake, so this only guards the edge cases."""
    try:
        return page.title()
    except Exception:
        return ""


def _safe_url(page) -> str:
    try:
        return page.url
    except Exception:
        return ""


def _await_quiet(page, timeout_ms: int = 8_000) -> None:
    """Wait for the page to reach a loaded state, tolerating pages that never settle."""
    try:
        page.wait_for_load_state("load", timeout=timeout_ms)
    except Exception:
        pass


class WebSurface(Surface):
    """The Chrome/Playwright implementation of the shared ``Surface``. Holds the Playwright
    instance and the live session (worker-thread-only), and exposes the browser actions."""

    live_region_roles = _LIVE_REGION_ROLES

    def __init__(self) -> None:
        super().__init__("daisy-playwright", message)
        self._playwright = None
        self._session: Optional[_Session] = None

    # Failure and recovery.

    def on_recover(self) -> dict:
        self._session = None
        return {}

    def recover(self, detail: str) -> dict:
        return {"ok": False, "error": message("connection_dropped", detail=detail)}

    def location_fields(self, page) -> dict:
        return {"url": _safe_url(page), "title": _safe_title(page)}

    # Connection, touched only on the worker thread.

    def session(self, browser: str = "chrome") -> _Session:
        """The live session, connecting if needed. Raises ``ToolFailure`` with the right payload
        when the user's browser is unreachable."""
        from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

        if self._session is not None:
            if self._session.browser.is_connected():
                return self._session
            self._session = None
        if self._playwright is None:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
        websocket_url = _devtools_websocket_url(browser)
        if websocket_url is None:
            raise ToolFailure(_not_connected_payload())
        try:
            connected = self._playwright.chromium.connect_over_cdp(websocket_url, timeout=10_000)
        except PlaywrightTimeout:
            # The port answers but the endpoint does not: the DevToolsActivePort file outlived
            # the debugging session (the infobar's Stop, or a toggle-off). The remedy differs
            # from "never enabled": the switch must be cycled to mint a fresh endpoint.
            raise ToolFailure(_stale_endpoint_payload())
        except PlaywrightError:
            raise ToolFailure(_not_connected_payload())
        context = connected.contexts[0] if connected.contexts else connected.new_context()
        session = _Session(connected, context)
        for page in context.pages:
            session.adopt(page)
        context.on("page", session.adopt)
        session.page = self._pick_page(session)
        self._session = session
        return session

    @staticmethod
    def _pick_page(session: _Session):
        """The user's current real web page. Prefers http(s) over chrome:// and blank surfaces."""
        pages = session.context.pages
        if not pages:
            return session.context.new_page()
        return next((page for page in pages if page.url.startswith("http")), pages[-1])

    def page(self, session: _Session):
        """The active page, healing if it was closed under us."""
        if session.page is None or session.page.is_closed():
            session.page = self._pick_page(session)
        return session.page

    def shutdown(self) -> None:
        def stop() -> dict:
            if self._session is not None and self._session.browser.is_connected():
                # For a connected (not launched) browser this only drops our connection;
                # the user's Chrome keeps running.
                self._session.browser.close()
            self._session = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None
            return {}

        try:
            self.worker.submit(stop, timeout=10.0)
        except Exception:
            pass
        self.worker.stop()

    # Reading and shaping, touched only on the worker thread.

    def _read_page(self, session: _Session, page, *, settle: bool, root_locator=None) -> tuple[list[dict], bool, int]:
        """Snapshot the page (or one subtree), refresh the session's registry and change-detection
        state, and return (payloads, truncated, omitted_text). ``settle`` re-reads a near-empty
        snapshot briefly, since a JS-heavy app reports itself loaded long before it has painted
        anything readable."""
        snapshot = _snapshot(page, root_locator)
        elements, truncated, omitted_text = _parse_snapshot(snapshot)
        if settle and len(elements) < _SETTLE_MINIMUM_ELEMENTS:
            deadline = time.monotonic() + _SETTLE_WINDOW_SECONDS
            while len(elements) < _SETTLE_MINIMUM_ELEMENTS and time.monotonic() < deadline:
                time.sleep(0.35)
                snapshot = _snapshot(page, root_locator)
                elements, truncated, omitted_text = _parse_snapshot(snapshot)
        session.registry = {element.index: element.token for element in elements}
        if root_locator is None:
            # A drilled subtree never stands in for the whole page in change detection.
            session.last_snapshot = snapshot
        return [element.payload() for element in elements], truncated, omitted_text

    @staticmethod
    def _drain(session: _Session) -> list[dict]:
        events: list[dict] = []
        while session.events:
            events.append(session.events.popleft())
        return events

    def _overview(self, session: _Session, page, *, before_url: Optional[str] = None, settle: bool = True, root_locator=None) -> dict:
        """The full page (or one drilled subtree) as indexed elements — what observe and the
        navigation actions return."""
        elements, truncated, omitted_text = self._read_page(session, page, settle=settle, root_locator=root_locator)
        notes = []
        if truncated:
            notes.append(message("overview_truncated", limit=str(_MAXIMUM_ELEMENTS)))
        if omitted_text:
            notes.append(message("text_omitted", count=str(omitted_text)))
        result = self.overview(
            context=page, elements=elements, events=self._drain(session), notes=notes,
            truncated=truncated, empty_hint=message("empty_page_hint"),
        )
        if before_url is not None:
            result["url_changed"] = _safe_url(page) != before_url
        return result

    def _digest(self, session: _Session, page, *, before_url: Optional[str]) -> dict:
        """The result an acting call returns: the page's complete actionable surface, with the
        bulk prose deferred to observe/read."""
        elements, truncated, _ = self._read_page(session, page, settle=True)
        result = self.digest(
            context=page, elements=elements, events=self._drain(session), truncated=truncated,
            truncated_note=message("overview_truncated", limit=str(_MAXIMUM_ELEMENTS)),
            prose_note_name="digest_prose", empty_hint=message("empty_page_hint"),
        )
        if before_url is not None:
            result["url_changed"] = _safe_url(page) != before_url
        return result

    def _locator(self, session: _Session, page, index: int):
        """The Playwright locator for an element index from the last observe/find. Raises a clean
        failure when the index is unknown or refers to plain text."""
        if index not in session.registry:
            raise ToolFailure({"ok": False, "error": f"No element at index {index}. Observe the page first."})
        reference = session.registry[index]
        if not reference:
            raise ToolFailure({
                "ok": False,
                "error": f"Element {index} is plain text with no actionable node. Target a clickable element instead.",
            })
        return page.locator(f"aria-ref={reference}")

    def _tab_summaries(self, session: _Session) -> list[dict]:
        """Every open tab with its title and url, read from the browser's cached target metadata in
        a single Target.getTargets call. A real profile can hold dozens of tabs, many discarded to
        save memory, and executing document.title on each would wake them one by one (slow) or throw
        on a closed one; the metadata call touches no renderer."""
        metadata: dict[str, dict] = {}
        try:
            cdp = session.browser.new_browser_cdp_session()
            for info in cdp.send("Target.getTargets").get("targetInfos", []):
                metadata[info.get("targetId", "")] = info
            cdp.detach()
        except Exception:
            pass
        active = session.page
        summaries = []
        for page in session.context.pages:
            info = metadata.get(session.target_id(page) or "", {})
            summaries.append({
                "tab": session.tab_id(page),
                "title": info.get("title", ""),
                "url": info.get("url") or _safe_url(page),
                "active": page == active,
            })
        return summaries

    # Actions.

    def navigate(self, url: str, browser: str = "chrome") -> dict:
        """Open a URL in the user's Chrome (connecting to it if needed) and return the overview."""

        def run() -> dict:
            session = self.session(browser)
            page = self.page(session)
            try:
                page.goto(url, wait_until="load", timeout=25_000)
            except Exception:
                # A page that never fires load (busy SPA, hanging resource) can still be usable;
                # the settle-aware overview decides what is actually there.
                pass
            return self._overview(session, page)

        return self.guard(run)

    def observe(self, element: Optional[int] = None) -> dict:
        """The current page's semantic tree as indexed elements (iframes included). With
        ``element``, just that element's subtree in full detail — the tree-shaped way to expand one
        region of a large page without paying for the rest. Indices then refer to the drilled
        subtree until the next observe or find."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            root_locator = self._locator(session, page, element) if element is not None else None
            result = self._overview(session, page, settle=element is None, root_locator=root_locator)
            if element is not None:
                result["did"] = f"Expanded element {element}"
            return result

        return self.guard(run)

    def find(self, query: str) -> dict:
        """Search the whole page (iframes included) for elements whose name or value contains
        ``query``, case-insensitively. Clickable matches first; every match is registered so it can
        be acted on by index."""

        def run() -> dict:
            needle = query.strip().lower()
            if not needle:
                return {"ok": False, "error": "The find action needs non-empty text to look for."}
            session = self.session()
            page = self.page(session)
            snapshot = _snapshot(page)
            # Search the whole page, not just what an overview would list. A match must never
            # hide behind the listing cap or the prose budget.
            elements, _, _ = _parse_snapshot(snapshot, limit=100_000, prose_budget=100_000)
            session.last_snapshot = snapshot
            matches = [
                element for element in elements
                if needle in element.name.lower()
                or (isinstance(element.value, str) and needle in element.value.lower())
            ]
            matches.sort(key=lambda element: (0 if element.clickable else 1, element.index))
            truncated = len(matches) > _FIND_LIMIT
            matches = matches[:_FIND_LIMIT]
            registry: dict[int, Optional[str]] = {}
            listed: list[dict] = []
            for position, element in enumerate(matches):
                registry[position] = element.token
                payload = element.payload()
                payload["index"] = position
                listed.append(payload)
            session.registry = registry
            result: dict[str, Any] = {
                "ok": True, "url": page.url, "title": _safe_title(page),
                "query": query, "count": len(listed), "elements": listed,
            }
            if truncated:
                result["truncated"] = True
                result["note"] = message("find_truncated", limit=str(_FIND_LIMIT))
            if not listed:
                result["note"] = message("find_no_match")
            return result

        return self.guard(run)

    def click(self, index: int) -> dict:
        """Click an element with Playwright's full actionability pipeline (visibility, stability,
        hit-target checks), then return the resulting page with an honest ``changed`` flag."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            before_snapshot = session.last_snapshot
            locator = self._locator(session, page, index)
            try:
                locator.click(timeout=5_000)
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not click element {index}: {str(error).splitlines()[0]}"})
            _await_quiet(page)
            result: dict[str, Any] = {"ok": True, "did": f"Clicked element {index}"}
            overview = self._digest(session, self.page(session), before_url=before_url)
            changed = overview.get("url_changed") or session.last_snapshot != before_snapshot
            result["changed"] = bool(changed)
            if not changed:
                result["note"] = message("click_no_change")
            result.update(overview)
            return result

        return self.guard(run)

    def type_text(self, index: int, text: str, submit: bool = False) -> dict:
        """Fill a field (input, textarea, or contenteditable) with Playwright's fill: focus, clear,
        insert, real input events. With ``submit``, press Enter and return the resulting page."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            locator = self._locator(session, page, index)
            try:
                locator.fill(text, timeout=5_000)
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not type into element {index}: {str(error).splitlines()[0]}"})
            if not submit:
                return {"ok": True, "did": f"Typed into element {index}"}
            locator.press("Enter", timeout=5_000)
            _await_quiet(page)
            result: dict[str, Any] = {"ok": True, "did": f"Typed into element {index} and pressed Enter"}
            result.update(self._digest(session, self.page(session), before_url=before_url))
            return result

        return self.guard(run)

    def press(self, key: str) -> dict:
        """Press a key (or chord like Control+A) on the focused element and return the overview."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            resolved = _KEY_ALIASES.get(key.strip().lower(), key.strip())
            try:
                page.keyboard.press(resolved)
            except Exception as error:
                return {"ok": False, "error": f"Could not press {key!r}: {str(error).splitlines()[0]}"}
            _await_quiet(page, timeout_ms=3_000)
            return self._digest(session, self.page(session), before_url=before_url)

        return self.guard(run)

    def hover(self, index: int) -> dict:
        """Move the pointer over an element (revealing hover menus and tooltips) without clicking."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            locator = self._locator(session, page, index)
            try:
                locator.hover(timeout=5_000)
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not hover element {index}: {str(error).splitlines()[0]}"})
            time.sleep(0.25)
            return self._digest(session, page, before_url=None)

        return self.guard(run)

    def scroll(self, direction: str = "down", element: Optional[int] = None) -> dict:
        """Scroll exactly the way a person does: point the mouse at the pane (over the ``element``,
        or the viewport centre for the page) and turn the wheel, trusted input that the browser's
        own scroll chaining routes to the right scroller. down/up/left/right move by most of a
        viewport; top/bottom fling to the ends. ``changed`` reports whether the page's content
        differs afterwards."""
        wanted = direction.strip().lower()
        if wanted not in _SCROLL_DIRECTIONS:
            return {"ok": False, "error": f"Unknown scroll direction {direction!r}. Use down, up, left, right, top, or bottom."}

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            before_snapshot = session.last_snapshot
            size = page.viewport_size or {"width": 1280, "height": 720}
            if element is not None:
                # Aim the wheel at the element's pane. Take the element's current box and clamp the
                # point into the viewport rather than hovering it to centre, so repeated paging keeps
                # the wheel over the same pane even once the anchor has scrolled out of sight. If the
                # element is not laid out yet, hover to bring it into view, then re-measure.
                locator = self._locator(session, page, element)
                box = locator.bounding_box()
                if box is None:
                    try:
                        locator.hover(timeout=5_000)
                        box = locator.bounding_box()
                    except Exception:
                        box = None
                if box is None:
                    raise ToolFailure({"ok": False, "error": f"Element {element} has no on-screen position to scroll at. Observe again."})
                point_x = min(max(box["x"] + box["width"] / 2, 1), size["width"] - 1)
                point_y = min(max(box["y"] + box["height"] / 2, 1), size["height"] - 1)
                page.mouse.move(point_x, point_y)
            else:
                page.mouse.move(size["width"] / 2, size["height"] / 2)
            step_x = int(size["width"] * 0.875)
            step_y = int(size["height"] * 0.875)
            deltas = {
                "down": (0, step_y), "up": (0, -step_y),
                "right": (step_x, 0), "left": (-step_x, 0),
                "top": (0, -_SCROLL_JUMP), "bottom": (0, _SCROLL_JUMP),
            }
            delta_x, delta_y = deltas[wanted]
            page.mouse.wheel(delta_x, delta_y)
            # Let the scroll land and lazily-rendered content (virtualized lists) paint.
            time.sleep(0.4)
            result: dict[str, Any] = {"ok": True, "did": f"Scrolled {wanted}"}
            overview = self._digest(session, page, before_url=before_url)
            result["changed"] = bool(overview.get("url_changed")) or session.last_snapshot != before_snapshot
            result.update(overview)
            return result

        return self.guard(run)

    def select_option(self, index: int, option: str) -> dict:
        """Choose an option in a native <select>. Playwright matches the given string against the
        option's value or its visible label."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            locator = self._locator(session, page, index)
            try:
                chosen = locator.select_option(option, timeout=5_000)
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not select {option!r} in element {index}: {str(error).splitlines()[0]}"})
            result: dict[str, Any] = {"ok": True, "did": f"Selected {option!r} in element {index}", "selected": chosen}
            result.update(self._digest(session, page, before_url=before_url))
            return result

        return self.guard(run)

    def upload(self, index: int, paths: list[str]) -> dict:
        """Attach local files to a file input (or a control that opens a file chooser)."""

        def run() -> dict:
            resolved = [str(Path(path).expanduser()) for path in paths]
            missing = [path for path in resolved if not os.path.isfile(path)]
            if missing:
                return {"ok": False, "error": f"No such file: {', '.join(missing)}"}
            session = self.session()
            page = self.page(session)
            before_url = page.url
            locator = self._locator(session, page, index)
            try:
                locator.set_input_files(resolved, timeout=5_000)
            except Exception:
                # Not a file input itself; it may be a button that opens the file chooser.
                try:
                    with page.expect_file_chooser(timeout=5_000) as chooser_info:
                        locator.click(timeout=5_000)
                    chooser_info.value.set_files(resolved)
                except Exception as error:
                    raise ToolFailure({"ok": False, "error": f"Could not upload to element {index}: {str(error).splitlines()[0]}"})
            result: dict[str, Any] = {"ok": True, "did": f"Attached {len(resolved)} file(s) to element {index}"}
            result.update(self._digest(session, page, before_url=before_url))
            return result

        return self.guard(run)

    def drag(self, index: int, to_element: int) -> dict:
        """Drag one element onto another (Playwright's full pointer sequence with hit checks)."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            source = self._locator(session, page, index)
            target = self._locator(session, page, to_element)
            try:
                source.drag_to(target, timeout=8_000)
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not drag element {index} to {to_element}: {str(error).splitlines()[0]}"})
            result: dict[str, Any] = {"ok": True, "did": f"Dragged element {index} onto {to_element}"}
            result.update(self._digest(session, page, before_url=before_url))
            return result

        return self.guard(run)

    def read(self, offset: int = 0, element: Optional[int] = None) -> dict:
        """The page's visible text, one bounded window at a time. With ``element``, only that
        element's subtree. ``offset`` continues a truncated read; the result says exactly which
        offset reaches the next window."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            if element is not None:
                source = self._locator(session, page, element).inner_text(timeout=10_000)
            else:
                source = page.inner_text("body", timeout=10_000)
            text = _clean_page_text(source)
            start = max(0, int(offset))
            window = text[start: start + _READ_WINDOW_CHARS]
            truncated = len(text) > start + _READ_WINDOW_CHARS
            result: dict[str, Any] = {
                "ok": True, "url": page.url, "title": _safe_title(page),
                "text": window, "truncated": truncated, "total_chars": len(text),
            }
            if start:
                result["offset"] = start
            if truncated:
                result["note"] = message("read_truncated", next_offset=str(start + _READ_WINDOW_CHARS))
            return result

        return self.guard(run)

    def screenshot(self) -> dict:
        """The visible viewport as pixels: the fallback for surfaces with no semantic tree at all
        (a canvas map, WebGL, a custom-drawn editor). Returns a temp PNG path for the caller to ship
        through the model-image side channel."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            handle, path = tempfile.mkstemp(prefix="daisy-web-capture-", suffix=".png")
            os.close(handle)
            page.screenshot(path=path, type="png", timeout=20_000)
            return {
                "ok": True, "image_path": path, "url": page.url, "title": _safe_title(page),
                "did": "Captured the visible viewport",
            }

        return self.guard(run)

    def history_back(self) -> dict:
        def run() -> dict:
            session = self.session()
            page = self.page(session)
            page.go_back(wait_until="load", timeout=15_000)
            return self._overview(session, page)

        return self.guard(run)

    def history_forward(self) -> dict:
        def run() -> dict:
            session = self.session()
            page = self.page(session)
            page.go_forward(wait_until="load", timeout=15_000)
            return self._overview(session, page)

        return self.guard(run)

    def reload(self) -> dict:
        """Reload the current page and return its fresh overview."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            page.reload(wait_until="load", timeout=25_000)
            return self._overview(session, page)

        return self.guard(run)

    def list_tabs(self) -> dict:
        """The open tabs of the user's browser, so the model can switch between them by id."""

        def run() -> dict:
            session = self.session()
            self.page(session)
            return {"ok": True, "tabs": self._tab_summaries(session)}

        return self.guard(run)

    def new_tab(self, url: str = "", browser: str = "chrome") -> dict:
        """Open a new tab (optionally at a URL), make it the active one, and return its overview."""

        def run() -> dict:
            session = self.session(browser)
            page = session.context.new_page()
            session.page = page
            page.bring_to_front()
            if url:
                try:
                    page.goto(url, wait_until="load", timeout=25_000)
                except Exception:
                    pass
            # A deliberately blank tab has nothing to settle for; don't make the caller wait.
            result = self._overview(session, page, settle=bool(url))
            result["tab"] = session.tab_id(page)
            return result

        return self.guard(run)

    def switch_tab(self, tab: str) -> dict:
        """Make a given tab (by id from the tabs action) the active one and return its overview."""

        def run() -> dict:
            session = self.session()
            page = session.pages_by_id.get(tab)
            if page is None or page.is_closed():
                return {"ok": False, "error": f"No tab with id {tab!r}. List tabs to get current ids.", "tabs": self._tab_summaries(session)}
            session.page = page
            page.bring_to_front()
            return self._overview(session, page)

        return self.guard(run)

    def close_tab(self, tab: str) -> dict:
        """Close a tab by id. When it was the active one, fall back to whatever tab remains."""

        def run() -> dict:
            session = self.session()
            page = session.pages_by_id.get(tab)
            if page is None or page.is_closed():
                return {"ok": False, "error": f"No tab with id {tab!r} (it may already be closed).", "tabs": self._tab_summaries(session)}
            page.close()
            session.pages_by_id.pop(tab, None)
            session.tab_ids.pop(page, None)
            if session.page == page:
                session.page = None
                self.page(session)
            return {"ok": True, "tabs": self._tab_summaries(session)}

        return self.guard(run)

    # Dispatch.

    #: Actions whose result carries an ``image_path`` for the model-image side channel.
    screenshot_actions = frozenset({"screenshot"})

    def preflight(self, action: str) -> Optional[dict]:
        """The web surface gates nothing up front — an unreachable browser surfaces as a payload
        from the operation itself."""
        return None

    def dispatch(self, action: str, arguments: dict) -> dict:
        url = str(arguments.get("url", ""))
        element = arguments.get("element")
        text = str(arguments.get("text", ""))
        key = str(arguments.get("key", ""))
        direction = str(arguments.get("direction") or "down")
        tab = str(arguments.get("tab", ""))
        browser_name = str(arguments.get("browser_name") or "chrome")
        index = int(element) if element is not None else None
        if action == "navigate":
            if not url:
                return {"ok": False, "error": "The navigate action needs a url."}
            return self.navigate(url, browser=browser_name)
        if action == "observe":
            return self.observe(element=index)
        if action == "find":
            query = str(arguments.get("query", ""))
            if not query.strip():
                return {"ok": False, "error": "The find action needs a query — the text to look for on the page."}
            return self.find(query)
        if action == "screenshot":
            return self.screenshot()
        if action in ("click", "type", "hover", "select", "upload", "drag"):
            if index is None:
                return {"ok": False, "error": f"The {action} action needs an element index from the last observe."}
            if action == "click":
                return self.click(index)
            if action == "hover":
                return self.hover(index)
            if action == "select":
                option = str(arguments.get("option", ""))
                if not option:
                    return {"ok": False, "error": "The select action needs an option — the visible label (or value) to choose."}
                return self.select_option(index, option)
            if action == "upload":
                paths = arguments.get("paths") or []
                if isinstance(paths, str):
                    paths = [paths]
                if not paths:
                    return {"ok": False, "error": "The upload action needs paths — the local file(s) to attach."}
                return self.upload(index, [str(path) for path in paths])
            if action == "drag":
                to_element = arguments.get("to_element")
                if to_element is None:
                    return {"ok": False, "error": "The drag action needs to_element — the index to drop onto."}
                return self.drag(index, int(to_element))
            return self.type_text(index, text, submit=bool(arguments.get("submit", False)))
        if action == "press":
            if not key:
                return {"ok": False, "error": "The press action needs a key (e.g. Enter)."}
            return self.press(key)
        if action == "scroll":
            return self.scroll(direction, element=index)
        if action == "read":
            return self.read(offset=int(arguments.get("offset", 0) or 0), element=index)
        if action == "back":
            return self.history_back()
        if action == "forward":
            return self.history_forward()
        if action == "reload":
            return self.reload()
        if action == "tabs":
            return self.list_tabs()
        if action == "new_tab":
            return self.new_tab(url, browser=browser_name)
        if action in ("switch_tab", "close_tab"):
            if not tab:
                return {"ok": False, "error": f"The {action} action needs a tab id from the tabs action."}
            if action == "switch_tab":
                return self.switch_tab(tab)
            return self.close_tab(tab)
        return {"ok": False, "error": f"Unknown action {action!r}."}


SURFACE = WebSurface()


def close() -> None:
    """Drop our connection (e.g. on server stop). The user's Chrome is left running; daisy just
    reconnects to it next time."""
    SURFACE.shutdown()


atexit.register(close)
