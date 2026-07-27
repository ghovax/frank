"""The web automation surface: the user's *own* Chrome, driven with Playwright over the Chrome
DevTools Protocol. The model drives it through one tool, ``control_screen``, whose script both reads
and acts: **``find_one``/``find_many``** read the page into retrieval documents
(:meth:`WebSurface.documents`) — one per element, plus one per recent network exchange, so the model
can find a control *or* the page's own API endpoint by describing it — and the acting primitives run
against the result (:meth:`WebSurface.perform`) with Playwright's trusted, actionability-checked
input, and can replay an authenticated request in-page with ``evaluate``.

Why the real browser, not a copy. The point of the browser tool is to act as the user, with their
real logins and real session. Copying a profile cannot do that anymore: Google's Device Bound
Session Credentials tie a login to a non-exportable key in this device's Secure Enclave, so a
copied profile's cookies die within minutes. The only way to hold a real login is to use the real
profile in place — which is also why ``evaluate`` (in-page ``fetch``) reaches exactly the endpoints
the page itself can, with its signing and its session.

How the connection is made. Modern Chrome refuses ``--remote-debugging-port`` on the default
profile, so daisy neither launches nor copies anything: the user turns on Chrome's own switch once
(chrome://inspect/#remote-debugging), which starts a DevTools server and writes a
``DevToolsActivePort`` file; daisy reads it and hands the ``ws://`` URL to Playwright's
``connect_over_cdp``. daisy only ever connects; disconnecting leaves the browser running.

Threading. Playwright's sync API is thread-affine, so the ``SerialWorker`` this surface inherits is
mandatory: the Playwright instance, the connection, and the page registry are touched only on that
one worker thread.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from collections import deque
from itertools import count
from pathlib import Path
from typing import Any, Optional

from daisy.computer.retrieval import Document, element_text
from daisy.computer.surface import Element, Surface, ToolFailure, message_loader, resolve_caret, resolve_range
from daisy.base.tuning import Tunable, active_tuning, settle

message = message_loader("browser")

# The DOM selection helper Playwright has no native API for (selecting an arbitrary substring or
# placing the caret at an offset). Kept as a real .js file and loaded at runtime, bundled by the
# freeze spec.
_APPLY_SELECTION_JS = (Path(__file__).parent / "scripts" / "apply_selection.js").read_text()



def _decode_body(text: str, content_type: str = "") -> Any:
    """Represent a captured body as structured data when it is JSON — so the model reads it as an
    object it can navigate rather than an escaped string — and otherwise as the plain string."""
    stripped = text.lstrip()
    if "json" in content_type or stripped[:1] in "{[":
        try:
            return json.loads(text)
        except Exception:
            pass
    return text

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
    """The structured result when Chrome's remote-debugging switch is off, so the UI can render an
    alert with a one-click button. daisy never enables the switch — it grants full browser control,
    so it is the user's explicit choice."""
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
    ``DevToolsActivePort`` file. ``None`` when the user has not turned the switch on."""
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
        # What to do with the next JavaScript dialog an action triggers ("accept"/"dismiss"); None
        # means the default (acknowledge alerts, decline questions).
        self.pending_dialog: Optional[str] = None
        # The page's recent network exchanges — full request/response, so a ``find`` can
        # surface the API endpoints behind a rendered view and ``evaluate`` can replay them. A
        # generous rolling window, bounded only so a long-lived page cannot grow the buffer forever.
        self.exchanges: deque[dict] = deque(maxlen=active_tuning().amount(Tunable.web_exchanges))
        self._exchange_counter = count(1)
        # Live WebSockets and their recent frames (chat, live data, trading feeds — the traffic that
        # never shows up as XHR). Keyed by a model-facing id, pruned oldest-first past the limit.
        self.websockets: dict[str, dict] = {}
        self._websocket_counter = count(1)
        # Dialogs auto-handled and downloads captured since the last result, drained into it.
        self.events: deque[dict] = deque(maxlen=8)
        # Frame id (``f1``) → the aria-ref of the ``iframe`` element that owns it, read out of the
        # last snapshot. The snapshot states this ownership itself, so it is recorded rather than
        # inferred from the order Playwright happens to list frames in.
        self.frame_owners: dict[str, str] = {}

    def tab_id(self, page) -> str:
        if page not in self.tab_ids:
            identifier = f"tab{next(self._tab_counter)}"
            self.tab_ids[page] = identifier
            self.pages_by_id[identifier] = page
        return self.tab_ids[page]

    def forget(self, page) -> None:
        """Drop a closed page from both directions of the registry."""
        identifier = self.tab_ids.pop(page, None)
        if identifier is not None:
            self.pages_by_id.pop(identifier, None)

    def live_pages(self) -> list:
        """The tabs still open, in the browser's own order, pruning the registry as it goes.

        Nothing removed a closed page before this existed, so both dictionaries grew for the life
        of the connection and every id they held had to be treated as a maybe."""
        for page in list(self.tab_ids):
            if page.is_closed():
                self.forget(page)
        pages = [page for page in self.context.pages if not page.is_closed()]
        for page in pages:
            # Should already be covered — every page is adopted at connect or by the context's
            # `page` event. A tab that slipped through gets adopted rather than merely numbered, so
            # it is never listed as reachable while its dialogs would freeze it unanswered.
            if page not in self.tab_ids:
                self.adopt(page)
        return pages

    def adopt(self, page) -> None:
        """Track a page and wire its dialog/download/network handling. Dialogs are answered
        immediately because an unanswered dialog freezes the page: an alert is acknowledged and a
        question declined by default, unless the acting call asked to ``accept``/``dismiss``."""
        self.tab_id(page)

        def on_dialog(dialog) -> None:
            intent = self.pending_dialog
            self.pending_dialog = None
            if intent == "accept":
                accepted = True
            elif intent == "dismiss":
                accepted = False
            else:
                accepted = dialog.type == "alert"
            self.events.append({"dialog": {"type": dialog.type, "message": dialog.message, "accepted": accepted}})
            try:
                dialog.accept() if accepted else dialog.dismiss()
            except Exception:
                pass

        def on_download(download) -> None:
            try:
                destination = os.path.join(tempfile.mkdtemp(prefix="daisy-web-download-"), download.suggested_filename)
                download.save_as(destination)
                self.events.append({"download": {"path": destination, "url": download.url}})
            except Exception as error:
                self.events.append({"download": {"url": download.url, "error": str(error)}})

        def on_response(response) -> None:
            # Capture each exchange in full for the exchange documents: eager but selective — only
            # the data-shaped requests (XHR/fetch with a text-ish body), size-clipped, so the junk
            # (images, fonts, media) never gets stored and one exchange cannot dominate. Best effort.
            try:
                request = response.request
                resource_type = request.resource_type
                entry: dict[str, Any] = {
                    "id": f"req{next(self._exchange_counter)}",
                    "method": request.method, "url": request.url,
                    "status": response.status, "type": resource_type,
                }
                request_headers: dict[str, str] = {}
                try:
                    request_headers = dict(request.headers)
                    entry["request_headers"] = request_headers
                except Exception:
                    pass
                headers: dict[str, str] = {}
                try:
                    headers = dict(response.headers)
                    entry["response_headers"] = headers
                except Exception:
                    pass
                try:
                    post = request.post_data
                    if post:
                        entry["request_body"] = _decode_body(post, request_headers.get("content-type", ""))
                except Exception:
                    pass
                content_type = headers.get("content-type", "")
                if resource_type in ("xhr", "fetch") and any(
                    marker in content_type for marker in ("json", "javascript", "text", "xml", "graphql", "urlencoded")
                ):
                    try:
                        entry["response_body"] = _decode_body(response.text(), content_type)
                    except Exception:
                        pass
                self.exchanges.append(entry)
            except Exception:
                pass

        def on_websocket(websocket) -> None:
            # Observe a WebSocket's frames — the model can search them like any exchange, and act on
            # the socket in-page with ``evaluate`` (the page's own socket, or a new one it opens).
            identifier = f"ws{next(self._websocket_counter)}"
            if len(self.websockets) >= active_tuning().amount(Tunable.web_websockets):
                self.websockets.pop(next(iter(self.websockets)))
            record: dict[str, Any] = {"id": identifier, "url": websocket.url, "frames": deque(maxlen=active_tuning().amount(Tunable.web_websocket_frames))}
            self.websockets[identifier] = record

            def note(direction: str):
                def handler(payload) -> None:
                    if isinstance(payload, (bytes, bytearray)):
                        record["frames"].append({"direction": direction, "binary_bytes": len(payload)})
                    else:
                        record["frames"].append({"direction": direction, "data": _decode_body(payload)})
                return handler

            websocket.on("framesent", note("sent"))
            websocket.on("framereceived", note("received"))

        page.on("dialog", on_dialog)
        page.on("download", on_download)
        page.on("response", on_response)
        page.on("websocket", on_websocket)

    def drain_events(self) -> list[dict]:
        events: list[dict] = []
        while self.events:
            events.append(self.events.popleft())
        return events


# Observation: Playwright's ref-carrying accessibility snapshot, parsed into the shared indexed
# ``Element`` — the unit a ``find`` ranks.

_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch",
    "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "option", "slider", "spinbutton",
    "treeitem", "listbox", "menu", "menubar", "togglebutton", "scrollbar",
})

_SURFACED_FLAGS = ("checked", "disabled", "expanded", "selected", "pressed", "active")

_SNAPSHOT_LINE = re.compile(r"^(\s*)-\s+(?P<head>[^\s\[\":]+)(?P<rest>.*)$")
_SNAPSHOT_NAME = re.compile(r'"((?:[^"\\]|\\.)*)"')
_SNAPSHOT_ATTRS = re.compile(r"\[([a-zA-Z-]+)(?:=([^\]]*))?\]")
# An aria-ref inside an iframe is prefixed with the frame it belongs to: ``f1e3`` is the third
# element of the first frame. The prefix is Playwright's, and it is reused as the frame's id rather
# than being paired with a second numbering of our own, which would only ever drift from it.
_FRAME_PREFIX = re.compile(r"^(f\d+)e\d+$")

_LIVE_REGION_ROLES = frozenset({"alert", "status"})
_STRUCTURAL_ROLES = frozenset({
    "region", "navigation", "main", "complementary", "banner", "contentinfo", "form", "search",
})
# Roles whose accessible name labels the section around them, becoming the `context` of the plainer
# controls inside it — what tells twenty identical "Add to Cart" buttons apart.
_LABEL_ROLES = _STRUCTURAL_ROLES | frozenset({"heading", "link"})


def _frame_of(reference: Optional[str]) -> str:
    """The frame an aria-ref belongs to (``f1e3`` → ``f1``), or ``""`` for the main document."""
    match = _FRAME_PREFIX.match(reference or "")
    return match.group(1) if match else ""


def _parse_snapshot(snapshot: str) -> tuple[list[Element], dict[str, str]]:
    """Parse the ai-mode aria snapshot (YAML-shaped, one node per line, ``[ref=...]`` markers,
    iframe contents inlined with frame-scoped refs) into shared ``Element`` objects, each carrying
    its aria-ref as ``token`` and the ``context`` of its nearest labelling ancestor. Every element
    is kept — a ``find`` ranks the whole surface, so nothing is capped or budgeted out.

    Also returns the frame ownership the snapshot states: contents of the first iframe are numbered
    ``f1…``, the second ``f2…``, and each frame's nodes are indented under the ``iframe`` element
    that holds them — so the innermost open iframe at the moment a new frame prefix first appears is
    the element that owns it. This is read rather than inferred because the alternative, matching
    ``page.frames`` order against prefix order, is an assumption about two orderings agreeing."""
    elements: list[Element] = []
    labels: dict[int, str] = {}
    frame_owners: dict[str, str] = {}
    open_iframes: list[tuple[int, str]] = []   # (depth, the iframe element's own ref)
    for line in snapshot.splitlines():
        match = _SNAPSHOT_LINE.match(line)
        if match is None:
            continue
        depth = len(match.group(1))
        role = match.group("head")
        rest = match.group("rest")
        if role.startswith("/"):  # a property line like `- /url: ...`, not a node
            continue
        name_match = _SNAPSHOT_NAME.search(rest)
        name = name_match.group(1).replace('\\"', '"') if name_match else ""
        attributes = dict(_SNAPSHOT_ATTRS.findall(rest))
        tail = rest[name_match.end():] if name_match else rest
        tail = _SNAPSHOT_ATTRS.sub("", tail).lstrip()
        value = tail[1:].strip() if tail.startswith(":") else ""
        if role == "text" and not name:
            name, value = value, ""

        for stale in [key for key in labels if key > depth]:
            del labels[stale]
        context = next((labels[key] for key in sorted(labels, reverse=True) if key <= depth), "")
        if name and role in _LABEL_ROLES:
            labels[depth] = name

        reference = attributes.get("ref") or None
        # Frame bookkeeping runs before the filter below, because an ``iframe`` node carries no
        # name, no value and no pointer cursor, so it is exactly the kind of node that filter drops.
        while open_iframes and open_iframes[-1][0] >= depth:
            open_iframes.pop()
        frame = _frame_of(reference)
        if frame and frame not in frame_owners:
            frame_owners[frame] = open_iframes[-1][1] if open_iframes else ""
        if role == "iframe" and reference:
            open_iframes.append((depth, reference))

        clickable = role in _INTERACTIVE_ROLES or attributes.get("cursor") == "pointer"
        if not (name or value or clickable):
            continue
        element = Element(role=role, name=name, value=value or None, clickable=clickable, context=context, token=reference)
        for flag in _SURFACED_FLAGS:
            if flag in attributes:
                element.flags[flag] = attributes[flag] if attributes[flag] else True
        elements.append(element)
    return elements, frame_owners


def _snapshot(page) -> str:
    """The ref-carrying accessibility snapshot of the whole page (iframes inlined)."""
    return page.locator("body").aria_snapshot(mode="ai", timeout=active_tuning().amount(Tunable.snapshot_timeout_ms))


# Icon fonts render ligatures as Private Use Area characters that leak into a text read as garbage.
_PRIVATE_USE_CHARS = re.compile("[-\U000f0000-\U000ffffd\U00100000-\U0010fffd]")
_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def _clean_page_text(text: str) -> str:
    return _BLANK_LINES.sub("\n\n", _PRIVATE_USE_CHARS.sub("", text))


_KEY_ALIASES = {
    "enter": "Enter", "escape": "Escape", "tab": "Tab", "backspace": "Backspace",
    "delete": "Delete", "arrowdown": "ArrowDown", "arrowup": "ArrowUp",
    "arrowleft": "ArrowLeft", "arrowright": "ArrowRight", "pagedown": "PageDown",
    "pageup": "PageUp", "home": "Home", "end": "End", "space": "Space",
}

_SCROLL_DIRECTIONS = frozenset({"down", "up", "left", "right", "top", "bottom"})
_SCROLL_JUMP = 1_000_000


def _element_signature(page) -> int:
    """A cheap signature of what is on the page (its element count) — fed to ``settle`` so an action
    that reveals content is waited out until the count stops changing, not a blind fixed sleep."""
    try:
        return len(_parse_snapshot(_snapshot(page))[0])
    except Exception:
        return -1


def _safe_title(page) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def _safe_url(page) -> str:
    try:
        return page.url
    except Exception:
        return ""


def _await_quiet(page) -> None:
    """Let the DOM parse after an action without ever blocking on one stalled resource:
    ``domcontentloaded`` is the cheap signal the new document exists, bounded and swallowed."""
    ceiling_ms = max(1, int(active_tuning().settle_give_up() * 1000))
    try:
        page.wait_for_load_state("domcontentloaded", timeout=ceiling_ms)
    except Exception:
        pass


def _actionability_error(error: Exception) -> str:
    """The honest reason an action could not complete, kept from Playwright's message: the headline
    plus the diagnostic lines that name *why* (an overlay that ``intercepts pointer events``, what
    it was ``waiting for``), so the model can dismiss the overlay rather than blindly retry."""
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    if not lines:
        return error.__class__.__name__
    headline = lines[0]
    diagnostics = [
        line for line in lines[1:]
        if ("intercepts pointer events" in line or line.startswith("waiting for")
            or "is not visible" in line or "is not enabled" in line or "is not stable" in line)
    ]
    seen: set[str] = set()
    unique = [line for line in diagnostics if not (line in seen or seen.add(line))]
    return " — ".join([headline, *unique[:3]]) if unique else headline


class WebSurface(Surface):
    """The Chrome/Playwright implementation of the shared ``Surface``. Snapshots a page into
    ranked-elsewhere documents, and performs trusted actions on the elements a search returned."""

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


    def preflight(self, operation: str) -> Optional[dict]:
        """Gate a read on the browser being reachable at all, so a Chrome with its remote-debugging
        switch off surfaces as the structured not-connected payload (with the one-click enable
        button) up front, rather than as a bare error raised mid-script. A connection that drops
        later still surfaces from the operation itself."""
        if operation == "documents" and _devtools_websocket_url("chrome") is None:
            return _not_connected_payload()
        return None

    # Connection, touched only on the worker thread.

    def session(self, browser: str = "chrome") -> _Session:
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
            connected = self._playwright.chromium.connect_over_cdp(websocket_url, timeout=active_tuning().amount(Tunable.connect_timeout_ms))
        except PlaywrightTimeout:
            raise ToolFailure(_stale_endpoint_payload())
        except PlaywrightError:
            raise ToolFailure(_not_connected_payload())
        context = connected.contexts[0] if connected.contexts else connected.new_context()
        context.set_default_timeout(active_tuning().amount(Tunable.action_timeout_ms))
        context.set_default_navigation_timeout(active_tuning().amount(Tunable.navigation_timeout_ms))
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

    def _live(self) -> tuple[_Session, Any]:
        session = self.session()
        return session, self.page(session)

    def shutdown(self) -> None:
        def stop() -> dict:
            if self._session is not None and self._session.browser.is_connected():
                self._session.browser.close()  # only drops our connection; the user's Chrome runs on
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

    def _locator(self, page, ref: Optional[str]):
        """The Playwright locator for an element id from a recent search.

        The id is Playwright's own aria-ref, and it survives re-reading: snapshotting an unchanged
        page yields the same refs, and new content is numbered around what is already there rather
        than renumbering it. A ref for an element that has *left* the page does not fail fast — the
        selector simply matches nothing and waits out the context's action timeout, which is right
        for an action whose target may be about to appear and wrong for anything enumerating, so a
        caller that is listing rather than acting passes a shorter timeout of its own."""
        if not ref:
            raise ToolFailure({"ok": False, "error": "This action needs an element id from a find (find_one or find_many)."})
        return page.locator(f"aria-ref={ref}")

    def _field_text(self, locator) -> str:
        try:
            return locator.input_value()
        except Exception:
            return locator.text_content() or ""

    def _frame(self, session: _Session, page, identifier: str):
        """The live Playwright ``Frame`` a frame id names.

        Resolved through the ``iframe`` element the snapshot said owns it, rather than by indexing
        ``page.frames`` — the snapshot states the ownership, and matching two orderings would be a
        guess that happens to be right most of the time."""
        if not session.frame_owners:
            _, session.frame_owners = _parse_snapshot(_snapshot(page))
        element_ref = session.frame_owners.get(identifier)
        if element_ref is None:
            known = ", ".join(sorted(session.frame_owners)) or "none"
            raise ToolFailure({"ok": False, "error": f"No frame {identifier!r} on this page (frames here: {known}). Call frames() for what is there."})
        if not element_ref:
            return page.main_frame
        frame = None
        try:
            handle = page.locator(f"aria-ref={element_ref}").element_handle(
                timeout=active_tuning().amount(Tunable.frame_resolve_timeout_ms)
            )
            frame = handle.content_frame() if handle is not None else None
        except Exception:
            frame = None
        if frame is None:
            raise ToolFailure({"ok": False, "error": f"Frame {identifier!r} is no longer on the page. Read the page again to get current frames."})
        return frame

    def _frame_or_page(self, session: _Session, page, identifier: str):
        return self._frame(session, page, identifier) if identifier else page

    # Perceiving — find.

    def documents(self, browser: str = "chrome") -> dict:
        """Read the page into retrieval documents: one per element (its own words, keyed by its
        aria-ref), plus one per recent network exchange (method + url, keyed by its id, with the
        full request/response as payload), so the model can find a control or an API endpoint."""

        def run() -> dict:
            session = self.session(browser)
            page = self.page(session)
            documents: list[Document] = []
            elements, session.frame_owners = _parse_snapshot(_snapshot(page))
            for element in elements:
                text = element_text(name=element.name, value=element.value, context=element.context)
                payload: dict[str, Any] = {"role": element.role}
                # Which frame the element sits in, so a model meeting `f1e3` for the first time is
                # not left to guess what `f1` is or to spend a call asking.
                frame = _frame_of(element.token if isinstance(element.token, str) else None)
                if frame:
                    payload["frame"] = frame
                if element.name:
                    payload["name"] = element.name
                if isinstance(element.value, str):
                    if element.value:
                        payload["value"] = element.value
                elif element.value is not None:
                    payload["value"] = element.value
                if element.context:
                    payload["context"] = element.context
                payload.update(element.flags)
                if element.clickable:
                    payload["clickable"] = True
                if text:
                    payload["text"] = text
                documents.append(Document(id=element.token or "", text=text, payload=payload))
            for exchange in list(session.exchanges):
                documents.append(Document(
                    id=exchange["id"], text=f"{exchange['method']} {exchange['url']}",
                    payload={"kind": "request", **exchange},
                ))
            for record in list(session.websockets.values()):
                documents.append(Document(
                    id=record["id"], text=f"websocket {record['url']}",
                    payload={"kind": "websocket", "url": record["url"], "frames": list(record["frames"])},
                ))
            return {"ok": True, "url": _safe_url(page), "title": _safe_title(page), "documents": documents}

        return self.guard(run)

    # Acting — control_screen. ``perform`` routes one primitive call to its handler.

    def perform(self, operation: str, arguments: list, keywords: dict) -> dict:
        handler = getattr(self, f"_primitive_{operation}", None)
        if handler is None:
            return {"ok": False, "error": f"The browser surface has no '{operation}' action."}
        return handler(*arguments, **keywords)

    def _primitive_click(self, ref: str, *, button: str = "left", count: int = 1, dialog: str = "", **_: Any) -> dict:
        def run() -> dict:
            session, page = self._live()
            session.pending_dialog = dialog or None
            try:
                self._locator(page, ref).click(button=button, click_count=count)
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not click {ref}: {_actionability_error(error)}"})
            _await_quiet(page)
            return self._acted(session, page, f"Clicked {ref}")

        return self.guard(run)

    def _primitive_type(self, ref: str, text: str, *, submit: bool = False, mode: str = "replace", **_: Any) -> dict:
        def run() -> dict:
            session, page = self._live()
            locator = self._locator(page, ref)
            try:
                if mode == "insert":
                    locator.focus()
                    page.keyboard.insert_text(text)
                else:
                    locator.fill(text)
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not type into {ref}: {_actionability_error(error)}"})
            landed = self._field_text(locator)
            if not submit:
                result: dict[str, Any] = {"ok": True, "did": f"Typed into {ref}", "value": landed}
                if mode == "replace" and landed != text:
                    result["note"] = message("type_clamped")
                return result
            session.pending_dialog = None
            locator.press("Enter")
            _await_quiet(page)
            result = self._acted(session, page, f"Typed into {ref} and pressed Enter")
            result["value"] = landed
            return result

        return self.guard(run)

    def _primitive_press(self, key: str, **_: Any) -> dict:
        def run() -> dict:
            session, page = self._live()
            resolved = _KEY_ALIASES.get(key.strip().lower(), key.strip())
            try:
                page.keyboard.press(resolved)
            except Exception as error:
                return {"ok": False, "error": f"Could not press {key!r}: {_actionability_error(error)}"}
            _await_quiet(page)
            return self._acted(session, page, f"Pressed {resolved}")

        return self.guard(run)

    def _primitive_hover(self, ref: str, **_: Any) -> dict:
        def run() -> dict:
            session, page = self._live()
            try:
                self._locator(page, ref).hover()
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not hover {ref}: {_actionability_error(error)}"})
            settle(lambda: _element_signature(page))
            return self._acted(session, page, f"Hovered {ref}")

        return self.guard(run)

    def _primitive_scroll(self, ref: Optional[str] = None, *, direction: str = "down", **_: Any) -> dict:
        normalized_direction = direction.strip().lower()
        if normalized_direction not in _SCROLL_DIRECTIONS:
            return {"ok": False, "error": f"Unknown scroll direction {direction!r}. Use down, up, left, right, top, or bottom."}

        def run() -> dict:
            session, page = self._live()
            size = page.viewport_size or {"width": 1280, "height": 720}
            if ref is not None:
                box = self._locator(page, ref).bounding_box()
                if box is None:
                    raise ToolFailure({"ok": False, "error": f"Element {ref!r} has no on-screen position to scroll at. Search again."})
                point_x = min(max(box["x"] + box["width"] / 2, 1), size["width"] - 1)
                point_y = min(max(box["y"] + box["height"] / 2, 1), size["height"] - 1)
                page.mouse.move(point_x, point_y)
            else:
                page.mouse.move(size["width"] / 2, size["height"] / 2)
            step_x, step_y = int(size["width"] * 0.875), int(size["height"] * 0.875)
            deltas = {
                "down": (0, step_y), "up": (0, -step_y), "right": (step_x, 0), "left": (-step_x, 0),
                "top": (0, -_SCROLL_JUMP), "bottom": (0, _SCROLL_JUMP),
            }
            delta_x, delta_y = deltas[normalized_direction]
            page.mouse.wheel(delta_x, delta_y)
            settle(lambda: _element_signature(page))
            return self._acted(session, page, f"Scrolled {normalized_direction}")

        return self.guard(run)

    def _primitive_choose(self, ref: str, option: str, **_: Any) -> dict:
        def run() -> dict:
            session, page = self._live()
            try:
                chosen = self._locator(page, ref).select_option(option)
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not choose {option!r} in {ref}: {_actionability_error(error)}"})
            result = self._acted(session, page, f"Chose {option!r} in {ref}")
            result["chosen"] = chosen
            return result

        return self.guard(run)

    def _primitive_upload(self, ref: str, paths: Any, **_: Any) -> dict:
        def run() -> dict:
            resolved = [str(Path(path).expanduser()) for path in ([paths] if isinstance(paths, str) else paths)]
            missing = [path for path in resolved if not os.path.isfile(path)]
            if missing:
                return {"ok": False, "error": f"No such file: {', '.join(missing)}"}
            session, page = self._live()
            locator = self._locator(page, ref)
            try:
                locator.set_input_files(resolved)
            except Exception:
                try:
                    with page.expect_file_chooser() as chooser:
                        locator.click()
                    chooser.value.set_files(resolved)
                except Exception as error:
                    raise ToolFailure({"ok": False, "error": f"Could not upload to {ref}: {_actionability_error(error)}"})
            return self._acted(session, page, f"Attached {len(resolved)} file(s) to {ref}")

        return self.guard(run)

    def _primitive_drag(self, ref: str, to_element: Optional[str] = None, **_: Any) -> dict:
        def run() -> dict:
            if to_element is None:
                return {"ok": False, "error": "drag needs to_element — the element to drop onto."}
            session, page = self._live()
            try:
                self._locator(page, ref).drag_to(self._locator(page, to_element), timeout=active_tuning().amount(Tunable.drag_timeout_ms))
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not drag {ref} to {to_element}: {_actionability_error(error)}"})
            return self._acted(session, page, f"Dragged {ref} onto {to_element}")

        return self.guard(run)

    def _primitive_select(self, ref: str, *, text: Optional[str] = None, to_text: Optional[str] = None,
                   select_all: bool = False, occurrence: int = 1, **_: Any) -> dict:
        def run() -> dict:
            _, page = self._live()
            locator = self._locator(page, ref)
            content = self._field_text(locator)
            if select_all:
                start, length = resolve_range(content, select_all=True)
            elif to_text is not None:
                start, length = resolve_range(content, anchor_from=text, anchor_to=to_text, occurrence=occurrence)
            else:
                start, length = resolve_range(content, text=text, occurrence=occurrence)
            if locator.evaluate(_APPLY_SELECTION_JS, [start, start + length]) is None:
                return {"ok": False, "error": message("select_unsupported")}
            return {"ok": True, "did": f"Selected {length} chars"}

        return self.guard(run)

    def _primitive_caret(self, ref: str, *, before: Optional[str] = None, after: Optional[str] = None,
                  at_offset: Optional[int] = None, edge: str = "", occurrence: int = 1, **_: Any) -> dict:
        def run() -> dict:
            _, page = self._live()
            locator = self._locator(page, ref)
            content = self._field_text(locator)
            offset = resolve_caret(content, before=before, after=after, at_offset=at_offset,
                                   to_start=edge == "start", to_end=edge == "end", occurrence=occurrence)
            if locator.evaluate(_APPLY_SELECTION_JS, [offset, offset]) is None:
                return {"ok": False, "error": message("select_unsupported")}
            return {"ok": True, "did": f"Caret at {offset}"}

        return self.guard(run)

    def _primitive_read(self, ref: Optional[str] = None, *, frame: str = "", **_: Any) -> dict:
        def run() -> dict:
            session, page = self._live()
            timeout = active_tuning().amount(Tunable.read_text_timeout_ms)
            if ref is not None:
                # An element id already names its own frame, so `frame` adds nothing here.
                source = self._locator(page, ref).inner_text(timeout=timeout)
            else:
                source = self._frame_or_page(session, page, frame).inner_text("body", timeout=timeout)
            return {"ok": True, "text": _clean_page_text(source), "url": _safe_url(page)}

        return self.guard(run)

    def _primitive_evaluate(self, expression: str, argument: Any = None, *, frame: str = "", **_: Any) -> dict:
        def run() -> dict:
            session, page = self._live()
            expression_text = expression.strip()
            if not expression_text:
                return {"ok": False, "error": "evaluate needs a JavaScript expression to run."}
            # A frame is its own origin with its own session. Running in one is what lets the
            # embedded checkout, consent screen or document viewer be scripted through the
            # credentials it actually holds, instead of the top document's.
            target = self._frame_or_page(session, page, frame)
            try:
                value = target.evaluate(expression_text, argument)
            except Exception as error:
                return {"ok": False, "error": f"Evaluation failed: {str(error).splitlines()[0]}"}
            # Playwright already deserialized the JS result into native Python; return it as-is and
            # in full, so the model gets a structure it can navigate — never a stringified, escaped,
            # or truncated blob. The model scopes its own JavaScript if it wants less back.
            return {"ok": True, "result": value, "url": _safe_url(page)}

        return self.guard(run)

    # Tabs and frames — the browser's own structure, named.

    def _primitive_tabs(self, **_: Any) -> dict:
        def run() -> dict:
            session = self.session()
            active = self.page(session)
            return {"ok": True, "tabs": [
                {
                    "id": session.tab_id(page), "title": _safe_title(page),
                    "url": _safe_url(page), "active": page is active,
                }
                for page in session.live_pages()
            ]}

        return self.guard(run)

    def _primitive_tab(self, tab: str, **_: Any) -> dict:
        def run() -> dict:
            session = self.session()
            session.live_pages()
            page = session.pages_by_id.get(tab)
            if page is None or page.is_closed():
                raise ToolFailure({"ok": False, "error": f"No open tab {tab!r}. Call tabs() for what is there."})
            session.page = page
            # Raises the window on the user's own screen. That is the point: this tool acts as the
            # user, and a tab being driven invisibly behind the one they are looking at would be
            # the surprising behaviour, not this.
            try:
                page.bring_to_front()
            except Exception:
                pass
            return {"ok": True, "did": f"Switched to {tab}", "url": _safe_url(page), "title": _safe_title(page)}

        return self.guard(run)

    def _primitive_new_tab(self, url: str = "", **_: Any) -> dict:
        def run() -> dict:
            session = self.session()
            page = session.context.new_page()
            # `context.on("page", adopt)` normally gets there first; adopting twice would double
            # every dialog, download and response handler on the page, so this only covers the case
            # where it did not.
            if page not in session.tab_ids:
                session.adopt(page)
            session.page = page
            if url:
                try:
                    page.goto(url, wait_until="domcontentloaded")
                except Exception:
                    pass  # a busy SPA may still be usable; the next read decides what is there
            try:
                page.bring_to_front()
            except Exception:
                pass
            identifier = session.tab_id(page)
            return {"ok": True, "did": f"Opened {identifier}", "id": identifier,
                    "url": _safe_url(page), "title": _safe_title(page)}

        return self.guard(run)

    def _primitive_close_tab(self, tab: str = "", **_: Any) -> dict:
        def run() -> dict:
            session = self.session()
            session.live_pages()
            page = session.pages_by_id.get(tab) if tab else self.page(session)
            if page is None or page.is_closed():
                raise ToolFailure({"ok": False, "error": f"No open tab {tab!r}. Call tabs() for what is there."})
            identifier = session.tab_ids.get(page, tab)
            was_active = page is session.page
            page.close()
            session.forget(page)
            if was_active:
                # Left for `page()` to heal on next use rather than picked here, so closing a tab
                # never has opening one as a side effect.
                session.page = None
            return {"ok": True, "did": f"Closed {identifier}"}

        return self.guard(run)

    def _primitive_frames(self, **_: Any) -> dict:
        def run() -> dict:
            session, page = self._live()
            _, session.frame_owners = _parse_snapshot(_snapshot(page))
            listing: list[dict] = []
            for identifier in sorted(session.frame_owners, key=lambda name: int(name[1:])):
                element_ref = session.frame_owners[identifier]
                record: dict[str, Any] = {"id": identifier, "element": element_ref,
                                          "parent": _frame_of(element_ref)}
                try:
                    frame = self._frame(session, page, identifier)
                except ToolFailure:
                    # One iframe that has gone must not cost the listing; a dead ref does not error,
                    # it waits out its timeout, which is why this one is short.
                    record["unavailable"] = True
                else:
                    record["url"] = frame.url
                    if frame.name:
                        record["name"] = frame.name
                listing.append(record)
            return {"ok": True, "frames": listing}

        return self.guard(run)

    def _primitive_navigate(self, url: str = "", *, history: str = "", browser: str = "chrome", **_: Any) -> dict:
        # Opening a tab is not a way of navigating, so it is `new_tab` that does it and says what it
        # made. This used to carry a `new_tab` flag that created a page and told the caller nothing.
        def run() -> dict:
            session = self.session(browser)
            page = self.page(session)
            try:
                if history == "back":
                    page.go_back(wait_until="domcontentloaded")
                elif history == "forward":
                    page.go_forward(wait_until="domcontentloaded")
                elif history == "reload":
                    page.reload(wait_until="domcontentloaded")
                elif url:
                    page.goto(url, wait_until="domcontentloaded")
                else:
                    return {"ok": False, "error": "navigate needs a url, or history: back, forward, or reload."}
            except Exception:
                pass  # a busy SPA may still be usable; the next search decides what is there
            _await_quiet(page)
            return {"ok": True, "did": f"Navigated to {url}" if url else f"Navigated {history}", "url": _safe_url(page), "title": _safe_title(page)}

        return self.guard(run)

    def _acted(self, session: _Session, page, did: str) -> dict:
        """The compact result of a control action: what it did, where it landed, and any dialog or
        download it triggered. What *changed* is found by the next ``find``, not diffed here."""
        result: dict[str, Any] = {"ok": True, "did": did, "url": _safe_url(page)}
        events = session.drain_events()
        if events:
            result["events"] = events
        return result


SURFACE = WebSurface()


def close() -> None:
    """Drop our connection to the browser. The user's Chrome is left running.

    Called by whoever opened the surface — the session's teardown — rather than from an
    `atexit` hook registered at import. Importing a module must not install process-wide
    cleanup on a host program that never asked for it, and a hook registered at import fires
    in every forked child as well as in the process that actually connected."""
    SURFACE.shutdown()
