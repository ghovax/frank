"""Web automation that connects to the user's *own* Chrome and drives it with Playwright over the
Chrome DevTools Protocol.

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
churn. daisy keeps only what is genuinely its own: the model-facing observation vocabulary
(indexed elements parsed from Playwright's ref-carrying accessibility snapshot) and the
connection policy above.

Threading model. Playwright's sync API is thread-affine: every object must be used from the
thread that created it. All public functions here therefore marshal their work onto one dedicated
worker thread that owns the Playwright instance, the connection, and the page registry. The tool
executor already calls these functions off the event loop, so blocking on the worker is safe.
"""
from __future__ import annotations

import atexit
import concurrent.futures
import os
import queue
import re
import tempfile
import threading
import time
from collections import deque
from itertools import count
from pathlib import Path
from typing import Any, Callable, Optional

from harness.core.configuration import PromptLoader

# User- and model-facing guidance prose lives in messages/*.md, loaded at runtime like every
# other prompt in the harness, never inlined in code. Bundled by the freeze spec.
_MESSAGE_LOADER = PromptLoader(Path(__file__).parent / "messages")


def _message(name: str, **variables: str) -> str:
    return _MESSAGE_LOADER.load(name, variables).strip()


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
        "error": _message("browser_not_connected", enable_url=REMOTE_DEBUGGING_URL),
        "code": "browser_remote_debugging_off",
        "enable_url": REMOTE_DEBUGGING_URL,
    }


def _stale_endpoint_payload() -> dict:
    return {
        "ok": False,
        "error": _message("browser_endpoint_stale", enable_url=REMOTE_DEBUGGING_URL),
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


class _ToolFailure(Exception):
    """A structured tool result raised as control flow inside the worker; carries the payload."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("error", ""))
        self.payload = payload


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


class _Worker:
    """The dedicated thread that owns Playwright. Public tool functions submit closures and
    block on the result; they never touch Playwright objects themselves."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # Worker-thread-only state:
        self._playwright = None
        self._session: Optional[_Session] = None

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="daisy-playwright", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, future = item
            try:
                future.set_result(fn())
            except BaseException as error:  # noqa: BLE001 (marshalled to the caller)
                future.set_exception(error)

    def submit(self, fn: Callable[[], dict], timeout: float = 120.0) -> dict:
        self._ensure_thread()
        future: "concurrent.futures.Future[dict]" = concurrent.futures.Future()
        self._queue.put((fn, future))
        return future.result(timeout=timeout)

    # Everything below runs on the worker thread.

    def session(self, browser: str = "chrome") -> _Session:
        """The live session, connecting if needed. Raises _ToolFailure with the right payload
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
            raise _ToolFailure(_not_connected_payload())
        try:
            connected = self._playwright.chromium.connect_over_cdp(websocket_url, timeout=10_000)
        except PlaywrightTimeout:
            # The port answers but the endpoint does not: the DevToolsActivePort file outlived
            # the debugging session (the infobar's Stop, or a toggle-off). The remedy differs
            # from "never enabled": the switch must be cycled to mint a fresh endpoint.
            raise _ToolFailure(_stale_endpoint_payload())
        except PlaywrightError:
            raise _ToolFailure(_not_connected_payload())
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

    def drop_session(self) -> None:
        self._session = None

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
            self.submit(stop, timeout=10.0)
        except Exception:
            pass
        self._queue.put(None)


_worker = _Worker()


def close() -> None:
    """Drop our connection (e.g. on server stop). The user's Chrome is left running; daisy just
    reconnects to it next time."""
    _worker.shutdown()


atexit.register(close)


def _run_tool(operation: Callable[[], dict], *, browser: str = "chrome") -> dict:
    """Submit one tool operation to the worker and shape every failure into an honest payload.
    Connection-level losses drop the session so the next call reconnects."""

    def guarded() -> dict:
        try:
            return operation()
        except _ToolFailure as failure:
            return failure.payload

    try:
        return _worker.submit(guarded)
    except Exception as error:  # Playwright errors, timeouts, dead browser
        message = str(error).splitlines()[0] if str(error) else error.__class__.__name__

        def drop() -> dict:
            _worker.drop_session()
            return {}

        try:
            _worker.submit(drop, timeout=5.0)
        except Exception:
            pass
        return {
            "ok": False,
            "error": f"The connection to your Chrome dropped ({message}). It may have been closed; try again and daisy will reconnect.",
        }


# Observation: Playwright's ref-carrying accessibility snapshot, parsed into the indexed-element
# vocabulary the model acts on.

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

_MAX_ELEMENTS = 300

# A page overview with fewer readable elements than this right after a load is treated as
# "still rendering" and re-read briefly: a JS-heavy app reports itself loaded long before its
# framework has painted anything the accessibility tree can see.
_SETTLE_MINIMUM_ELEMENTS = 3
_SETTLE_WINDOW_SECONDS = 3.0


def _parse_snapshot(snapshot: str, limit: int = _MAX_ELEMENTS) -> tuple[list[dict], dict[int, Optional[str]], bool]:
    """Parse the ai-mode aria snapshot (YAML-shaped, one node per line, ``[ref=...]`` markers,
    iframe contents inlined with frame-scoped refs) into flat indexed elements. Returns
    (elements, index-to-ref registry, truncated)."""
    elements: list[dict] = []
    registry: dict[int, Optional[str]] = {}
    truncated = False
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
        if len(elements) >= limit:
            truncated = True
            break
        index = len(elements)
        element: dict[str, Any] = {"index": index, "role": role}
        if name:
            element["name"] = name[:256]
        if value:
            element["value"] = value[:512]
        for flag in _SURFACED_FLAGS:
            if flag in attributes:
                element[flag] = attributes[flag] if attributes[flag] else True
        if clickable:
            element["clickable"] = True
        elements.append(element)
        registry[index] = reference
    return elements, registry, truncated


def _snapshot(page) -> str:
    return page.locator("body").aria_snapshot(mode="ai", timeout=10_000)


def _overview(session: _Session, page, *, before_url: Optional[str] = None, settle: bool = True) -> dict:
    """The current page as indexed elements: the shared read every action's result is built on.
    ``settle`` re-reads a near-empty snapshot briefly (a JS app finishing its first paint);
    ``before_url`` adds an explicit ``url_changed`` flag so an action that silently moved the
    page (an SPA route change, a map viewport rewrite) is impossible to miss."""
    snapshot = _snapshot(page)
    elements, registry, truncated = _parse_snapshot(snapshot)
    if settle and len(elements) < _SETTLE_MINIMUM_ELEMENTS:
        deadline = time.monotonic() + _SETTLE_WINDOW_SECONDS
        while len(elements) < _SETTLE_MINIMUM_ELEMENTS and time.monotonic() < deadline:
            time.sleep(0.35)
            snapshot = _snapshot(page)
            elements, registry, truncated = _parse_snapshot(snapshot)
    session.registry = registry
    session.last_snapshot = snapshot
    result: dict[str, Any] = {
        "ok": True,
        "url": page.url,
        "title": _safe_title(page),
        "count": len(elements),
        "elements": elements,
    }
    if truncated:
        result["truncated"] = True
    if before_url is not None:
        result["url_changed"] = page.url != before_url
    if not elements:
        result["hint"] = _message("browser_empty_page_hint")
    while session.events:
        result.update(session.events.popleft())
    return result


def _locator(session: _Session, page, index: int):
    """The Playwright locator for an element index from the last observe/find. Raises a clean
    failure when the index is unknown or refers to plain text."""
    if index not in session.registry:
        raise _ToolFailure({"ok": False, "error": f"No element at index {index}. Observe the page first."})
    reference = session.registry[index]
    if not reference:
        raise _ToolFailure({
            "ok": False,
            "error": f"Element {index} is plain text with no actionable node. Target a clickable element instead.",
        })
    return page.locator(f"aria-ref={reference}")


def _await_quiet(page, timeout_ms: int = 8_000) -> None:
    """Wait for the page to reach a loaded state, tolerating pages that never settle."""
    try:
        page.wait_for_load_state("load", timeout=timeout_ms)
    except Exception:
        pass


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


def navigate(url: str, browser: str = "chrome") -> dict:
    """Open a URL in the user's Chrome (connecting to it if needed) and return the page overview."""

    def run() -> dict:
        session = _worker.session(browser)
        page = _worker.page(session)
        try:
            page.goto(url, wait_until="load", timeout=25_000)
        except Exception:
            # A page that never fires load (busy SPA, hanging resource) can still be usable;
            # the settle-aware overview decides what is actually there.
            pass
        return _overview(session, page)

    return _run_tool(run, browser=browser)


def observe() -> dict:
    """The current page's semantic tree as indexed elements (iframes included)."""

    def run() -> dict:
        session = _worker.session()
        return _overview(session, _worker.page(session))

    return _run_tool(run)


# How many find matches come back at most: enough to disambiguate, small enough to stay readable.
_FIND_LIMIT = 25


def find(query: str) -> dict:
    """Search the whole page (iframes included) for elements whose name or value contains
    ``query``, case-insensitively. Clickable matches first; every match is registered so it can
    be acted on by index."""

    def run() -> dict:
        needle = query.strip().lower()
        if not needle:
            return {"ok": False, "error": "The find action needs non-empty text to look for."}
        session = _worker.session()
        page = _worker.page(session)
        snapshot = _snapshot(page)
        # Search the whole page, not just what an overview would list. A match must never
        # hide behind the listing cap.
        elements, registry, _ = _parse_snapshot(snapshot, limit=100_000)
        session.last_snapshot = snapshot
        matches = [
            element for element in elements
            if needle in element.get("name", "").lower() or needle in element.get("value", "").lower()
        ]
        matches.sort(key=lambda element: (0 if element.get("clickable") else 1, element["index"]))
        truncated = len(matches) > _FIND_LIMIT
        matches = matches[:_FIND_LIMIT]
        new_registry: dict[int, Optional[str]] = {}
        listed: list[dict] = []
        for position, element in enumerate(matches):
            new_registry[position] = registry.get(element["index"])
            reindexed = dict(element)
            reindexed["index"] = position
            listed.append(reindexed)
        session.registry = new_registry
        result: dict[str, Any] = {
            "ok": True,
            "url": page.url,
            "title": _safe_title(page),
            "query": query,
            "count": len(listed),
            "elements": listed,
        }
        if truncated:
            result["truncated"] = True
        if not listed:
            result["note"] = _message("browser_find_no_match")
        return result

    return _run_tool(run)


def click(index: int) -> dict:
    """Click an element with Playwright's full actionability pipeline (visibility, stability,
    hit-target checks), then return the resulting page with an honest ``changed`` flag."""

    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        before_url = page.url
        before_snapshot = session.last_snapshot
        locator = _locator(session, page, index)
        try:
            locator.click(timeout=5_000)
        except Exception as error:
            raise _ToolFailure({
                "ok": False,
                "error": f"Could not click element {index}: {str(error).splitlines()[0]}",
            })
        _await_quiet(page)
        result: dict[str, Any] = {"ok": True, "did": f"Clicked element {index}"}
        overview = _overview(session, _worker.page(session), before_url=before_url)
        changed = overview.get("url_changed") or session.last_snapshot != before_snapshot
        result["changed"] = bool(changed)
        if not changed:
            result["note"] = _message("browser_click_no_change")
        result.update(overview)
        return result

    return _run_tool(run)


def type_text(index: int, text: str, submit: bool = False) -> dict:
    """Fill a field (input, textarea, or contenteditable) with Playwright's fill: focus, clear,
    insert, real input events. With ``submit``, press Enter and return the resulting page."""

    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        before_url = page.url
        locator = _locator(session, page, index)
        try:
            locator.fill(text, timeout=5_000)
        except Exception as error:
            raise _ToolFailure({
                "ok": False,
                "error": f"Could not type into element {index}: {str(error).splitlines()[0]}",
            })
        if not submit:
            return {"ok": True, "did": f"Typed into element {index}"}
        locator.press("Enter", timeout=5_000)
        _await_quiet(page)
        result: dict[str, Any] = {"ok": True, "did": f"Typed into element {index} and pressed Enter"}
        result.update(_overview(session, _worker.page(session), before_url=before_url))
        return result

    return _run_tool(run)


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


def press(key: str) -> dict:
    """Press a key (or chord like Control+A) on the focused element and return the overview."""

    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        before_url = page.url
        resolved = _KEY_ALIASES.get(key.strip().lower(), key.strip())
        try:
            page.keyboard.press(resolved)
        except Exception as error:
            return {"ok": False, "error": f"Could not press {key!r}: {str(error).splitlines()[0]}"}
        _await_quiet(page, timeout_ms=3_000)
        return _overview(session, _worker.page(session), before_url=before_url)

    return _run_tool(run)


def hover(index: int) -> dict:
    """Move the pointer over an element (revealing hover menus and tooltips) without clicking."""

    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        locator = _locator(session, page, index)
        try:
            locator.hover(timeout=5_000)
        except Exception as error:
            raise _ToolFailure({
                "ok": False,
                "error": f"Could not hover element {index}: {str(error).splitlines()[0]}",
            })
        time.sleep(0.25)
        return _overview(session, page)

    return _run_tool(run)


_SCROLL_DIRECTIONS = frozenset({"down", "up", "left", "right", "top", "bottom"})

# top/bottom are one huge fling of the same wheel gesture: a delta large enough to reach any end.
_SCROLL_JUMP = 1_000_000


def scroll(direction: str = "down", element: Optional[int] = None) -> dict:
    """Scroll exactly the way a person does: point the mouse at the pane (over the ``element``,
    or the viewport centre for the page) and turn the wheel, trusted input that the browser's
    own scroll chaining routes to the right scroller. down/up/left/right move by most of a
    viewport; top/bottom fling to the ends. ``changed`` reports whether the page's content
    differs afterwards. The overview reads the whole page regardless of scroll position, so
    scrolling matters for virtualized feeds (new rows render) and app state, not for seeing more
    of a static document."""
    wanted = direction.strip().lower()
    if wanted not in _SCROLL_DIRECTIONS:
        return {"ok": False, "error": f"Unknown scroll direction {direction!r}. Use down, up, left, right, top, or bottom."}

    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        before_url = page.url
        before_snapshot = session.last_snapshot
        size = page.viewport_size or {"width": 1280, "height": 720}
        if element is not None:
            # Aim the wheel at the element's pane. Take the element's current box and clamp the
            # point into the viewport rather than hovering it to centre, so repeated paging keeps
            # the wheel over the same pane even once the anchor has scrolled out of sight. If the
            # element is not laid out yet, hover to bring it into view, then re-measure.
            locator = _locator(session, page, element)
            box = locator.bounding_box()
            if box is None:
                try:
                    locator.hover(timeout=5_000)
                    box = locator.bounding_box()
                except Exception:
                    box = None
            if box is None:
                raise _ToolFailure({
                    "ok": False,
                    "error": f"Element {element} has no on-screen position to scroll at. Observe again.",
                })
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
        overview = _overview(session, page, before_url=before_url)
        result["changed"] = bool(overview.get("url_changed")) or session.last_snapshot != before_snapshot
        result.update(overview)
        return result

    return _run_tool(run)


def select_option(index: int, option: str) -> dict:
    """Choose an option in a native <select>. Playwright matches the given string against the
    option's value or its visible label."""

    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        locator = _locator(session, page, index)
        try:
            chosen = locator.select_option(option, timeout=5_000)
        except Exception as error:
            raise _ToolFailure({
                "ok": False,
                "error": f"Could not select {option!r} in element {index}: {str(error).splitlines()[0]}",
            })
        result: dict[str, Any] = {"ok": True, "did": f"Selected {option!r} in element {index}", "selected": chosen}
        result.update(_overview(session, page))
        return result

    return _run_tool(run)


def upload(index: int, paths: list[str]) -> dict:
    """Attach local files to a file input (or a control that opens a file chooser)."""

    def run() -> dict:
        resolved = [str(Path(path).expanduser()) for path in paths]
        missing = [path for path in resolved if not os.path.isfile(path)]
        if missing:
            return {"ok": False, "error": f"No such file: {', '.join(missing)}"}
        session = _worker.session()
        page = _worker.page(session)
        locator = _locator(session, page, index)
        try:
            locator.set_input_files(resolved, timeout=5_000)
        except Exception:
            # Not a file input itself; it may be a button that opens the file chooser.
            try:
                with page.expect_file_chooser(timeout=5_000) as chooser_info:
                    locator.click(timeout=5_000)
                chooser_info.value.set_files(resolved)
            except Exception as error:
                raise _ToolFailure({
                    "ok": False,
                    "error": f"Could not upload to element {index}: {str(error).splitlines()[0]}",
                })
        result: dict[str, Any] = {"ok": True, "did": f"Attached {len(resolved)} file(s) to element {index}"}
        result.update(_overview(session, page))
        return result

    return _run_tool(run)


def drag(index: int, to_element: int) -> dict:
    """Drag one element onto another (Playwright's full pointer sequence with hit checks)."""

    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        source = _locator(session, page, index)
        target = _locator(session, page, to_element)
        try:
            source.drag_to(target, timeout=8_000)
        except Exception as error:
            raise _ToolFailure({
                "ok": False,
                "error": f"Could not drag element {index} to {to_element}: {str(error).splitlines()[0]}",
            })
        result: dict[str, Any] = {"ok": True, "did": f"Dragged element {index} onto {to_element}"}
        result.update(_overview(session, page))
        return result

    return _run_tool(run)


# Icon fonts (Material Symbols, Font Awesome, and the like) render their ligatures as characters
# in Unicode's Private Use Areas, which leak into a text extraction as meaningless glyphs. Strip
# those, then collapse the blank lines they leave behind. The three ranges are the BMP PUA and
# the two supplementary PUA planes.
_PRIVATE_USE_CHARS = re.compile("[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]")
_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def _clean_page_text(text: str) -> str:
    text = _PRIVATE_USE_CHARS.sub("", text)
    return _BLANK_LINES.sub("\n\n", text)


def read() -> dict:
    """The current page's visible text, bounded, for a quick read without the element tree."""

    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        text = _clean_page_text(page.inner_text("body", timeout=10_000))
        truncated = len(text) > 32_768 # Powers of 2 are nice, aren't they?
        return {
            "ok": True, "url": page.url, "title": _safe_title(page),
            "text": text[:32_768], "truncated": truncated,
        }

    return _run_tool(run)


def screenshot() -> dict:
    """The visible viewport as pixels: the fallback for surfaces with no semantic tree at all
    (a canvas map, WebGL, a custom-drawn editor). Returns a temp PNG path for the caller to ship
    through the model-image side channel."""

    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        handle, path = tempfile.mkstemp(prefix="daisy-web-capture-", suffix=".png")
        os.close(handle)
        page.screenshot(path=path, type="png", timeout=20_000)
        return {
            "ok": True, "image_path": path, "url": page.url, "title": _safe_title(page),
            "did": "Captured the visible viewport",
        }

    return _run_tool(run)


def history_back() -> dict:
    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        page.go_back(wait_until="load", timeout=15_000)
        return _overview(session, page)

    return _run_tool(run)


def history_forward() -> dict:
    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        page.go_forward(wait_until="load", timeout=15_000)
        return _overview(session, page)

    return _run_tool(run)


def reload() -> dict:
    """Reload the current page and return its fresh overview."""

    def run() -> dict:
        session = _worker.session()
        page = _worker.page(session)
        page.reload(wait_until="load", timeout=25_000)
        return _overview(session, page)

    return _run_tool(run)


def _tab_summaries(session: _Session) -> list[dict]:
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


def list_tabs() -> dict:
    """The open tabs of the user's browser, so the model can switch between them by id."""

    def run() -> dict:
        session = _worker.session()
        _worker.page(session)
        return {"ok": True, "tabs": _tab_summaries(session)}

    return _run_tool(run)


def new_tab(url: str = "", browser: str = "chrome") -> dict:
    """Open a new tab (optionally at a URL), make it the active one, and return its overview."""

    def run() -> dict:
        session = _worker.session(browser)
        page = session.context.new_page()
        session.page = page
        page.bring_to_front()
        if url:
            try:
                page.goto(url, wait_until="load", timeout=25_000)
            except Exception:
                pass
        # A deliberately blank tab has nothing to settle for; don't make the caller wait.
        result = _overview(session, page, settle=bool(url))
        result["tab"] = session.tab_id(page)
        return result

    return _run_tool(run, browser=browser)


def switch_tab(tab: str) -> dict:
    """Make a given tab (by id from the tabs action) the active one and return its overview."""

    def run() -> dict:
        session = _worker.session()
        page = session.pages_by_id.get(tab)
        if page is None or page.is_closed():
            return {"ok": False, "error": f"No tab with id {tab!r}. List tabs to get current ids.", "tabs": _tab_summaries(session)}
        session.page = page
        page.bring_to_front()
        return _overview(session, page)

    return _run_tool(run)


def close_tab(tab: str) -> dict:
    """Close a tab by id. When it was the active one, fall back to whatever tab remains."""

    def run() -> dict:
        session = _worker.session()
        page = session.pages_by_id.get(tab)
        if page is None or page.is_closed():
            return {"ok": False, "error": f"No tab with id {tab!r} (it may already be closed).", "tabs": _tab_summaries(session)}
        page.close()
        session.pages_by_id.pop(tab, None)
        session.tab_ids.pop(page, None)
        if session.page == page:
            session.page = None
            _worker.page(session)
        return {"ok": True, "tabs": _tab_summaries(session)}

    return _run_tool(run)
