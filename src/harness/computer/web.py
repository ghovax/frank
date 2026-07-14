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
import json
import os
import re
import tempfile
from collections import deque
from itertools import count
from pathlib import Path
from typing import Any, Callable, Optional

from harness.computer.surface import (
    Element, ElementRegistry, Surface, ToolFailure, message_loader, resolve_caret, resolve_range,
)
# Every size, count, and timing limit comes from the central tuning policy: the caps scale with the
# live model context window, the timeouts with the timeout knob, and settlement is a poll (settle)
# rather than a fixed sleep.
from harness.core.tuning import Limit, active_tuning, clip_to_tokens, settle
# The verbatim-first matcher and compact diff window from the file-edit tool: the `edit` action
# changes a field's text exactly the way edit_file changes a file, reusing that tested logic.
from harness.tools.file_tools import _context_diff_window as context_diff_window, _match_find_text as match_find_text

message = message_loader("browser")

# The DOM selection helper Playwright has no native API for (selecting an arbitrary substring or
# placing the caret at an offset). Kept as a real .js file and loaded at runtime, like every other
# prompt/asset in the harness, rather than inlined in Python. Bundled by the freeze spec.
_APPLY_SELECTION_JS = (Path(__file__).parent / "scripts" / "apply_selection.js").read_text()

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
        # The durable ref↔token map: mints a stable ref per element identity and rebinds it to the
        # live aria-ref on every observe, so a ref the model holds survives across calls and a
        # no-match find never wipes what a paging loop was working through.
        self.registry = ElementRegistry()
        # The element payloads from the last full-page observe, so an acting result can diff
        # against them and report only what changed instead of re-listing the whole surface.
        self.last_elements: list[dict] = []
        # What to do with the next JavaScript dialog this action triggers, when the model asked
        # ("accept"/"dismiss"); None means the default (acknowledge alerts, decline questions).
        self.pending_dialog: Optional[str] = None
        # A rolling log of network requests the page made, for the `network` reader.
        self.requests: deque[dict] = deque(maxlen=100)
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
        """Track a page and wire its dialog/download/network handling. Dialogs are answered
        immediately because an unanswered dialog freezes the page. By default an alert is
        acknowledged and a question (confirm/prompt) is declined; when the action that triggered
        it asked to ``accept`` or ``dismiss``, that intent wins — so a delete-with-confirm flow is
        possible without giving up the safe default. Every dialog is reported in the next result."""
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

        def on_response(response) -> None:
            # A compact request/response log the `network` action reads: the page's own XHR/fetch
            # and document traffic, which structured extraction (an XHR JSON endpoint) often wants
            # rather than the rendered DOM. Best-effort; a detached response read simply skips.
            try:
                request = response.request
                self.requests.append({
                    "method": request.method, "url": request.url,
                    "status": response.status, "type": request.resource_type,
                })
            except Exception:
                pass

        page.on("dialog", on_dialog)
        page.on("download", on_download)
        page.on("response", on_response)


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

# The overview element cap and the non-interactive prose budget are token budgets: they scale with
# the live model context window and the ``listing_fraction`` knob (see ``harness.core.tuning``), so
# they are resolved per call from ``active_tuning()`` rather than fixed here. A page reporting
# itself loaded before its framework has painted anything is handled by settlement polling (the
# snapshot is re-read until its element count stops changing), not a fixed "still rendering" floor.

# Live-region roles: what a page announces after an action (a validation error, a status
# line). Always included in acting results, so the announcement is visible without a read.
_LIVE_REGION_ROLES = frozenset({"alert", "status"})

# Landmark container roles: the page's skeleton and the natural drill targets. There are only a
# handful per page, and omitting one (budgeting it out as if it were prose) would hide a whole
# branch the model might want to expand — so they are always kept in an overview, never budgeted.
_STRUCTURAL_ROLES = frozenset({
    "region", "navigation", "main", "complementary", "banner", "contentinfo", "form", "search",
})

# Roles whose accessible name labels the section or card around them, and so becomes the `context`
# of the plainer controls that follow inside it. Landmarks title their whole branch; a heading
# titles its section; a card's leading link is usually the product/article title that its "Add to
# Cart"/"Save" button belongs to. This is what tells twenty identical buttons apart.
_LABEL_ROLES = _STRUCTURAL_ROLES | frozenset({"heading", "link"})

def _relevance(element: Element, words: list[str]) -> tuple:
    """How well an element answers the model's stated ``goal``: how many of the goal's words appear
    in its role/name/context/value, with a clickable element winning ties. Used to rank which
    elements survive the overview cap, so a goal-relevant control is never starved out by document
    order (the nav links and filters that happen to come first in the DOM)."""
    haystack = " ".join([
        element.role, element.name, element.context,
        element.value if isinstance(element.value, str) else "",
    ]).lower()
    matches = sum(1 for word in words if word in haystack)
    return (matches, 1 if element.clickable else 0)


def _parse_snapshot(
    snapshot: str, limit: Optional[int] = None, prose_budget: Optional[int] = None,
    goal: str = "",
) -> tuple[list[Element], bool, int]:
    """Parse the ai-mode aria snapshot (YAML-shaped, one node per line, ``[ref=...]`` markers,
    iframe contents inlined with frame-scoped refs) into shared ``Element`` objects, each carrying
    its aria-ref as ``token`` and the ``context`` of its nearest labelling ancestor/sibling.
    Interactive elements always make the list; non-interactive text is kept up to ``prose_budget``
    and counted past it. When the collected set exceeds ``limit``, a stated ``goal`` ranks which
    elements survive (relevance-first, re-sorted back into document order) so the cap never starves
    the relevant controls in favour of whatever came first in the DOM; without a goal the cap keeps
    document order. Returns (elements, truncated, omitted_text). ``limit`` and ``prose_budget``
    default to the window-scaled caps from the active tuning policy; find passes explicit large
    sentinels so a match can never hide behind them."""
    if limit is None:
        limit = active_tuning().amount(Limit.ELEMENT_CAP)
    if prose_budget is None:
        prose_budget = active_tuning().amount(Limit.PROSE_BUDGET)
    elements: list[Element] = []
    prose_kept = 0
    omitted_text = 0
    # depth (indentation width) → the accessible name of the last labelling node seen there, so a
    # control inherits the heading/card title it sits under as its `context`.
    labels: dict[int, str] = {}
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
        # Trailing `: text` after the last bracket/name is the node's text content.
        tail = rest
        if name_match:
            tail = rest[name_match.end():]
        tail = _SNAPSHOT_ATTRS.sub("", tail)
        tail = tail.lstrip()
        value = tail[1:].strip() if tail.startswith(":") else ""
        if role == "text" and not name:
            name, value = value, ""

        # Leaving a subtree: labels recorded deeper than this line belonged to siblings we have
        # passed, so drop them before reading or setting context at this depth.
        for stale in [key for key in labels if key > depth]:
            del labels[stale]
        context = next((labels[key] for key in sorted(labels, reverse=True) if key <= depth), "")
        if name and role in _LABEL_ROLES:
            labels[depth] = name

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
        element = Element(role=role, name=name, value=value or None, clickable=clickable, context=context, token=reference)
        for flag in _SURFACED_FLAGS:
            if flag in attributes:
                element.flags[flag] = attributes[flag] if attributes[flag] else True
        elements.append(element)

    truncated = len(elements) > limit
    if truncated:
        words = [word for word in re.split(r"\W+", goal.lower()) if word] if goal.strip() else []
        if words:
            ranked = sorted(range(len(elements)), key=lambda position: _relevance(elements[position], words), reverse=True)
            kept = set(ranked[:limit])
            elements = [element for position, element in enumerate(elements) if position in kept]
        else:
            elements = elements[:limit]
    return elements, truncated, omitted_text


def _snapshot(page, root_locator=None) -> str:
    """The ref-carrying accessibility snapshot: the whole page, or one element's subtree when a
    ``root_locator`` is given (refs are page-scoped either way, so subtree elements act
    normally). This is the tree-shaped progressive discovery the DOM affords: skim the page,
    then expand just the branch that matters."""
    target = root_locator if root_locator is not None else page.locator("body")
    return target.aria_snapshot(mode="ai", timeout=active_tuning().amount(Limit.SNAPSHOT_TIMEOUT_MS))


# Icon fonts (Material Symbols, Font Awesome, and the like) render their ligatures as characters
# in Unicode's Private Use Areas, which leak into a text extraction as meaningless glyphs. Strip
# those, then collapse the blank lines they leave behind. The three ranges are the BMP PUA and
# the two supplementary PUA planes.
_PRIVATE_USE_CHARS = re.compile("[-\U000f0000-\U000ffffd\U00100000-\U0010fffd]")
_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def _clean_page_text(text: str) -> str:
    text = _PRIVATE_USE_CHARS.sub("", text)
    return _BLANK_LINES.sub("\n\n", text)


# The read window, the evaluate-result clip, and the network-log length are all token budgets,
# resolved per call from ``active_tuning()`` (read_window_tokens / evaluate_tokens / network_limit)
# so they scale with the live model context window. The two text clips are enforced with the real
# tokenizer via clip_to_tokens; longer pages are read progressively via `offset`.

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


def _element_signature(page) -> int:
    """A cheap, comparable signature of what is currently on the page — the number of readable
    elements in a fresh snapshot. Fed to ``settle`` so an action that reveals content (a hover
    menu, a scrolled-in row) is waited out until the count stops changing, instead of a blind
    fixed sleep. Returns -1 on a transient snapshot failure so a hiccup reads as 'still moving'."""
    try:
        elements, _, _ = _parse_snapshot(_snapshot(page))
        return len(elements)
    except Exception:
        return -1


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


def _as_int(value: Any) -> Optional[int]:
    """A tool argument coerced to int, or None when it is absent or blank."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_ref(value: Any) -> Optional[str]:
    """An element argument coerced to a ref string, or None when absent/blank. A bare integer (a
    model that slipped back into positional thinking) is stringified so it still routes to a
    lookup that fails cleanly with the current-refs hint, rather than a type error."""
    if value is None or value == "":
        return None
    return str(value).strip()


def _await_quiet(page) -> None:
    """Let the page's DOM parse after an action without ever blocking on one stalled resource.
    ``domcontentloaded`` is the hard, cheap signal that the new document exists; it is bounded by
    the settlement ceiling and its timeout is swallowed. Playwright's own ``networkidle`` wait is
    deliberately not used here — Playwright marks it discouraged, and a single hung ad tracker
    would cost the whole ceiling for no signal. What is actually on the page is decided by the
    settlement poll in the read that follows (the snapshot is re-read until it stops changing)."""
    ceiling_ms = max(1, int(active_tuning().settle_ceiling() * 1000))
    try:
        page.wait_for_load_state("domcontentloaded", timeout=ceiling_ms)
    except Exception:
        pass


def _await_expectation(page, expect: str, timeout_ms: Optional[int] = None) -> Optional[bool]:
    """When the model states what an action should produce, wait for it instead of a fixed pause:
    ``expect`` is text that should appear on the page. Returns whether it showed up in time, or
    ``None`` when no expectation was given. This is the explicit override for the cases the smart
    default cannot know about (a toast, an async result, a slow post-submit redirect)."""
    wanted = expect.strip()
    if not wanted:
        return None
    if timeout_ms is None:
        timeout_ms = active_tuning().amount(Limit.EXPECTATION_TIMEOUT_MS)
    try:
        page.get_by_text(wanted, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _actionability_error(error: Exception) -> str:
    """The honest reason an action could not complete, kept from Playwright's message instead of
    truncated to its first line. Playwright names *why* — most valuably the element that
    ``intercepts pointer events`` (a cookie banner over the button), and what it was ``waiting
    for`` — a few lines into the message; the headline plus those diagnostic lines is what lets the
    model dismiss the overlay rather than blindly retry. The retry-spam call-log lines are dropped."""
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    if not lines:
        return error.__class__.__name__
    headline = lines[0]
    diagnostics = [
        line for line in lines[1:]
        if ("intercepts pointer events" in line or line.startswith("waiting for")
            or "is not visible" in line or "is not enabled" in line or "is not stable" in line)
    ]
    # De-duplicate the "waiting for" spam Playwright prints once per retry, keep first of each kind.
    seen: set[str] = set()
    unique = [line for line in diagnostics if not (line in seen or seen.add(line))]
    return " — ".join([headline, *unique[:3]]) if unique else headline


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
            connected = self._playwright.chromium.connect_over_cdp(websocket_url, timeout=active_tuning().amount(Limit.CONNECT_TIMEOUT_MS))
        except PlaywrightTimeout:
            # The port answers but the endpoint does not: the DevToolsActivePort file outlived
            # the debugging session (the infobar's Stop, or a toggle-off). The remedy differs
            # from "never enabled": the switch must be cycled to mint a fresh endpoint.
            raise ToolFailure(_stale_endpoint_payload())
        except PlaywrightError:
            raise ToolFailure(_not_connected_payload())
        context = connected.contexts[0] if connected.contexts else connected.new_context()
        # Set the action and navigation ceilings once on the context (Playwright's intended
        # mechanism) instead of repeating a timeout on every call. Individual operations that
        # genuinely warrant a different ceiling (a snapshot, a drag, a screenshot, a text read)
        # still pass their own; everything else inherits these.
        context.set_default_timeout(active_tuning().amount(Limit.ACTION_TIMEOUT_MS))
        context.set_default_navigation_timeout(active_tuning().amount(Limit.NAVIGATION_TIMEOUT_MS))
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

    def _read_page(self, session: _Session, page, *, settle_page: bool, root_locator=None, goal: str = "") -> tuple[list[dict], bool, int]:
        """Snapshot the page (or one subtree), bind every element a stable ref in the session's
        durable registry, and return (payloads, truncated, omitted_text). When ``settle_page`` is
        set, the snapshot is polled until its element count stops changing (a JS-heavy app reports
        itself loaded long before its framework has painted anything the accessibility tree can
        see, so a single read catches an empty shell); ``goal`` ranks which elements survive the
        cap. A page that never quiesces costs the settlement ceiling, not forever."""
        latest: dict = {}

        def probe() -> int:
            snapshot = _snapshot(page, root_locator)
            elements, truncated, omitted_text = _parse_snapshot(snapshot, goal=goal)
            latest["value"] = (elements, truncated, omitted_text)
            return len(elements)

        if settle_page:
            settle(probe)
        else:
            probe()
        elements, truncated, omitted_text = latest["value"]
        session.registry.bind(elements)
        payloads = [element.payload() for element in elements]
        if root_locator is None:
            # A drilled subtree never stands in for the whole page as the diff baseline.
            session.last_elements = payloads
        return payloads, truncated, omitted_text

    @staticmethod
    def _drain(session: _Session) -> list[dict]:
        events: list[dict] = []
        while session.events:
            events.append(session.events.popleft())
        return events

    def _overview(self, session: _Session, page, *, before_url: Optional[str] = None, settle_page: bool = True, root_locator=None, goal: str = "") -> dict:
        """The full page (or one drilled subtree) as ref-addressed elements — what observe and the
        navigation actions return."""
        elements, truncated, omitted_text = self._read_page(session, page, settle_page=settle_page, root_locator=root_locator, goal=goal)
        notes = []
        if truncated:
            notes.append(message("overview_truncated", limit=str(active_tuning().amount(Limit.ELEMENT_CAP))))
        if omitted_text:
            notes.append(message("text_omitted", count=str(omitted_text)))
        result = self.overview(
            context=page, elements=elements, events=self._drain(session), notes=notes,
            truncated=truncated, empty_hint=message("empty_page_hint"),
        )
        if before_url is not None:
            result["url_changed"] = _safe_url(page) != before_url
        return result

    def _digest(self, session: _Session, page, *, previous: list[dict], before_url: Optional[str]) -> dict:
        """The diff-first result an acting call returns: what changed on the page against ``previous``
        (the surface as it stood before the action), the full surface only on a wholesale change."""
        current, truncated, _ = self._read_page(session, page, settle_page=True)
        url_changed = None if before_url is None else _safe_url(page) != before_url
        return self.digest(
            context=page, current=current, previous=previous, url_changed=url_changed,
            events=self._drain(session), truncated=truncated,
            truncated_note=message("overview_truncated", limit=str(active_tuning().amount(Limit.ELEMENT_CAP))),
            prose_note_name="digest_prose", empty_hint=message("empty_page_hint"),
            no_change_note=message("click_no_change"),
        )

    def _locator(self, session: _Session, page, ref: str):
        """The Playwright locator for an element ref from an earlier observe/find. Raises a clean
        failure when the ref was never seen or points at plain text; a ref whose element has since
        left the page resolves to a stale aria-ref and surfaces as an ordinary action failure."""
        if ref not in session.registry:
            raise ToolFailure({"ok": False, "error": f"No element {ref!r}. Observe or find the page first to get current refs."})
        reference = session.registry.token(ref)
        if not reference:
            raise ToolFailure({
                "ok": False,
                "error": f"Element {ref!r} is plain text with no actionable node. Target a clickable element instead.",
            })
        return page.locator(f"aria-ref={reference}")

    def _viewport(self, session: _Session, page) -> dict:
        """The page's CSS-pixel visual viewport, read over CDP. ``page.viewport_size`` is ``None``
        on a browser we merely connected to (we never set it), which used to force scroll geometry
        onto a hardcoded 1280×720 guess; ``Page.getLayoutMetrics`` reports the real size in CSS
        pixels — the same coordinate space clicks and wheel gestures use — on any display, Retina
        included."""
        try:
            cdp = session.context.new_cdp_session(page)
            metrics = cdp.send("Page.getLayoutMetrics")
            cdp.detach()
            viewport = metrics.get("cssVisualViewport") or metrics.get("cssLayoutViewport") or {}
            width = viewport.get("clientWidth")
            height = viewport.get("clientHeight")
            if width and height:
                return {"width": float(width), "height": float(height)}
        except Exception:
            pass
        fallback = page.viewport_size or {"width": 1280, "height": 720}
        return {"width": float(fallback["width"]), "height": float(fallback["height"])}

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

    def navigate(self, url: str, browser: str = "chrome", *, goal: str = "", expect: str = "") -> dict:
        """Open a URL in the user's Chrome (connecting to it if needed) and return the overview. Waits
        for the DOM, not the ``load`` event, so a stalled ad resource never holds the navigation."""

        def run() -> dict:
            session = self.session(browser)
            page = self.page(session)
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception:
                # A page that never even parses the DOM in time (busy SPA, hanging resource) can
                # still be usable; the settle-aware overview decides what is actually there.
                pass
            _await_quiet(page)
            met = _await_expectation(page, expect)
            result = self._overview(session, page, goal=goal)
            if met is not None:
                result["expected_found"] = met
            return result

        return self.guard(run)

    def observe(self, element: Optional[str] = None, *, goal: str = "") -> dict:
        """The current page's semantic tree as ref-addressed elements (iframes included). With
        ``element``, just that element's subtree in full detail — the tree-shaped way to expand one
        region of a large page without paying for the rest. ``goal`` ranks a capped listing."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            root_locator = self._locator(session, page, element) if element is not None else None
            result = self._overview(session, page, settle_page=element is None, root_locator=root_locator, goal=goal)
            if element is not None:
                result["did"] = f"Expanded element {element}"
            return result

        return self.guard(run)

    def find(self, query: str) -> dict:
        """Search the whole page (iframes included) for elements whose name or value contains
        ``query``, case-insensitively. Clickable matches first, each with its disambiguating
        ``context``; matches are bound into the durable registry (merged, never replacing it), so a
        no-match find cannot brick a scroll/observe paging loop the way wiping the registry did."""

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
            matches = [
                element for element in elements
                if needle in element.name.lower()
                or (isinstance(element.value, str) and needle in element.value.lower())
                or needle in element.context.lower()
            ]
            matches.sort(key=lambda element: 0 if element.clickable else 1)
            find_limit = active_tuning().amount(Limit.FIND_LIMIT)
            truncated = len(matches) > find_limit
            matches = matches[:find_limit]
            # Bind the matches into the same durable registry an observe uses: an element seen by
            # both keeps one stable ref, and the map is only added to, never wiped.
            session.registry.bind(matches)
            listed = [element.payload() for element in matches]
            result: dict[str, Any] = {
                "ok": True, "url": page.url, "title": _safe_title(page),
                "query": query, "count": len(listed), "elements": listed,
            }
            if truncated:
                result["truncated"] = True
                result["note"] = message("find_truncated", limit=str(find_limit))
            if not listed:
                result["note"] = message("find_no_match")
            return result

        return self.guard(run)

    def click(self, ref: Optional[str] = None, *, x: Optional[int] = None, y: Optional[int] = None,
              clicks: int = 1, button: str = "left", dialog: str = "", expect: str = "") -> dict:
        """Click an element (Playwright's full actionability pipeline) or an x/y viewport point (the
        canvas/no-structure fallback), with ``clicks`` for double/triple and ``button`` for a
        context click. ``dialog`` sets what to do with a confirm/prompt the click triggers; ``expect``
        waits for the click's expected result. Returns the diff of what changed."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            before_elements = session.last_elements
            session.pending_dialog = dialog or None
            try:
                if ref is not None:
                    self._locator(session, page, ref).click(button=button, click_count=clicks)
                    did = f"Clicked {ref}"
                elif x is not None and y is not None:
                    page.mouse.click(x, y, button=button, click_count=clicks)
                    did = f"Clicked ({x}, {y})"
                else:
                    return {"ok": False, "error": "The click action needs an element ref or an x/y point."}
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not click: {_actionability_error(error)}"})
            _await_quiet(page)
            met = _await_expectation(page, expect)
            result = self._digest(session, self.page(session), previous=before_elements, before_url=before_url)
            result["did"] = did
            if met is not None:
                result["expected_found"] = met
            return result

        return self.guard(run)

    def type_text(self, ref: str, text: str, submit: bool = False, mode: str = "replace", *, expect: str = "") -> dict:
        """Enter text into a field. ``replace`` (default) fills it in one shot; ``insert`` inserts
        at the caret, replacing any selection. Reads the field back and returns its resulting
        ``value`` so a silently-rejected entry (a ``maxlength`` clamp, an input mask) is visible
        rather than assumed. With ``submit``, press Enter and return the resulting page's diff."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            before_elements = session.last_elements
            locator = self._locator(session, page, ref)
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
            met = _await_expectation(page, expect)
            result = self._digest(session, self.page(session), previous=before_elements, before_url=before_url)
            result["did"] = f"Typed into {ref} and pressed Enter"
            result["value"] = landed
            if met is not None:
                result["expected_found"] = met
            return result

        return self.guard(run)

    def _field_text(self, locator) -> str:
        """The editable text of a field, read with native accessors: an input/textarea's value, or
        the element's text content for anything else. Text content (not inner text) is what the
        selection script indexes against, so the offsets computed here line up with the DOM."""
        try:
            return locator.input_value()
        except Exception:
            return locator.text_content() or ""

    def select(self, ref: str, *, text: Optional[str] = None, to_text: Optional[str] = None,
               select_all: bool = False, occurrence: int = 1) -> dict:
        """Select text in a field, addressed by content: a substring (``text``), a range from
        ``text`` through ``to_text``, or the whole field (``select_all``). The range is computed
        from the field's text, then placed with the loaded selection script."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            locator = self._locator(session, page, ref)
            content = self._field_text(locator)
            if select_all:
                start, length = resolve_range(content, select_all=True)
            elif to_text is not None:
                start, length = resolve_range(content, anchor_from=text, anchor_to=to_text, occurrence=occurrence)
            else:
                start, length = resolve_range(content, text=text, occurrence=occurrence)
            placed = locator.evaluate(_APPLY_SELECTION_JS, [start, start + length])
            if placed is None:
                return {"ok": False, "error": message("select_unsupported")}
            return {"ok": True, "did": f"Selected {length} chars", "via": "dom"}

        return self.guard(run)

    def caret(self, ref: str, *, before: Optional[str] = None, after: Optional[str] = None,
              at_offset: Optional[int] = None, edge: str = "", occurrence: int = 1) -> dict:
        """Place the insertion point in a field: ``before``/``after`` a substring, ``at_offset`` a
        character offset, or at the ``start``/``end`` edge."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            locator = self._locator(session, page, ref)
            content = self._field_text(locator)
            offset = resolve_caret(
                content, before=before, after=after, at_offset=at_offset,
                to_start=edge == "start", to_end=edge == "end", occurrence=occurrence,
            )
            placed = locator.evaluate(_APPLY_SELECTION_JS, [offset, offset])
            if placed is None:
                return {"ok": False, "error": message("select_unsupported")}
            return {"ok": True, "did": f"Caret at {offset}", "via": "dom"}

        return self.guard(run)

    def edit_text(self, ref: str, find: str, replace: str, *, replace_all: bool = False) -> dict:
        """Change a field's text by replacing ``find`` with ``replace`` — the same verbatim-first,
        must-be-unique matching as the file-edit tool — then write the result back with Playwright's
        native ``fill`` (which works on inputs, textareas, and contenteditable). Returns a compact
        before/after diff."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            locator = self._locator(session, page, ref)
            content = self._field_text(locator)
            if find == replace:
                return {"ok": False, "error": "find and replace are identical; nothing to change."}
            matched = match_find_text(content, find, replace)
            if matched is None:
                return {"ok": False, "error": "The text to change is not in the field. Copy it exactly as it appears."}
            effective, found, replacement, occurrences = matched
            if occurrences > 1 and not replace_all:
                return {"ok": False, "error": f"{occurrences} matches for that text; make it unique or set replace_all."}
            after = effective.replace(found, replacement, -1 if replace_all else 1)
            locator.fill(after)
            before_window, after_window = context_diff_window(effective, after)
            count = occurrences if replace_all else 1
            return {"ok": True, "did": f"Changed {count} occurrence(s)", "before": before_window, "after": after_window}

        return self.guard(run)

    def clipboard_action(self, action: str, ref: Optional[str] = None) -> dict:
        """Copy, cut, or paste via the real OS clipboard, using the browser's own trusted keyboard
        shortcut (so it interoperates with the user's clipboard and the native surface)."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            if ref is not None:
                self._locator(session, page, ref).focus()
            combo = {"copy": "c", "cut": "x", "paste": "v"}[action]
            page.keyboard.press(f"ControlOrMeta+{combo}")
            _await_quiet(page)
            return {"ok": True, "did": action.capitalize()}

        return self.guard(run)

    def press(self, key: str, *, expect: str = "") -> dict:
        """Press a key (or chord like Control+A) on the focused element and return the diff."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            before_elements = session.last_elements
            resolved = _KEY_ALIASES.get(key.strip().lower(), key.strip())
            try:
                page.keyboard.press(resolved)
            except Exception as error:
                return {"ok": False, "error": f"Could not press {key!r}: {_actionability_error(error)}"}
            _await_quiet(page)
            met = _await_expectation(page, expect)
            result = self._digest(session, self.page(session), previous=before_elements, before_url=before_url)
            if met is not None:
                result["expected_found"] = met
            return result

        return self.guard(run)

    def hover(self, ref: Optional[str] = None, *, x: Optional[int] = None, y: Optional[int] = None) -> dict:
        """Move the pointer over an element or an x/y point (revealing hover menus and tooltips)
        without clicking."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_elements = session.last_elements
            try:
                if ref is not None:
                    self._locator(session, page, ref).hover()
                elif x is not None and y is not None:
                    page.mouse.move(x, y)
                else:
                    return {"ok": False, "error": "The hover action needs an element ref or an x/y point."}
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not hover: {_actionability_error(error)}"})
            # Let a hover menu or tooltip appear, waiting only until the page stops changing.
            settle(lambda: _element_signature(page))
            return self._digest(session, page, previous=before_elements, before_url=None)

        return self.guard(run)

    def scroll(self, direction: str = "down", ref: Optional[str] = None) -> dict:
        """Scroll exactly the way a person does: point the mouse at the pane (over the ``ref``
        element, or the viewport centre for the page) and turn the wheel, trusted input that the
        browser's own scroll chaining routes to the right scroller. down/up/left/right move by most
        of a viewport; top/bottom fling to the ends. The diff reports what new content appeared."""
        wanted = direction.strip().lower()
        if wanted not in _SCROLL_DIRECTIONS:
            return {"ok": False, "error": f"Unknown scroll direction {direction!r}. Use down, up, left, right, top, or bottom."}

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            before_elements = session.last_elements
            size = self._viewport(session, page)
            if ref is not None:
                # Aim the wheel at the element's pane. Take the element's current box and clamp the
                # point into the viewport rather than hovering it to centre, so repeated paging keeps
                # the wheel over the same pane even once the anchor has scrolled out of sight. If the
                # element is not laid out yet, hover to bring it into view, then re-measure.
                locator = self._locator(session, page, ref)
                box = locator.bounding_box()
                if box is None:
                    try:
                        locator.hover()
                        box = locator.bounding_box()
                    except Exception:
                        box = None
                if box is None:
                    raise ToolFailure({"ok": False, "error": f"Element {ref!r} has no on-screen position to scroll at. Observe again."})
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
            # Let the scroll land and lazily-rendered content (virtualized lists) paint, waiting
            # only until the page stops changing rather than a blind fixed pause.
            settle(lambda: _element_signature(page))
            result = self._digest(session, page, previous=before_elements, before_url=before_url)
            result["did"] = f"Scrolled {wanted}"
            return result

        return self.guard(run)

    def choose(self, ref: str, option: str) -> dict:
        """Pick an option in a native <select> dropdown. Playwright matches the given string
        against the option's value or its visible label."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            before_elements = session.last_elements
            locator = self._locator(session, page, ref)
            try:
                chosen = locator.select_option(option)
            except Exception as error:
                raise ToolFailure({"ok": False, "error": f"Could not choose {option!r} in {ref}: {_actionability_error(error)}"})
            result = self._digest(session, page, previous=before_elements, before_url=before_url)
            result["did"] = f"Chose {option!r} in {ref}"
            result["chosen"] = chosen
            return result

        return self.guard(run)

    def upload(self, ref: str, paths: list[str]) -> dict:
        """Attach local files to a file input (or a control that opens a file chooser)."""

        def run() -> dict:
            resolved = [str(Path(path).expanduser()) for path in paths]
            missing = [path for path in resolved if not os.path.isfile(path)]
            if missing:
                return {"ok": False, "error": f"No such file: {', '.join(missing)}"}
            session = self.session()
            page = self.page(session)
            before_url = page.url
            before_elements = session.last_elements
            locator = self._locator(session, page, ref)
            try:
                locator.set_input_files(resolved)
            except Exception:
                # Not a file input itself; it may be a button that opens the file chooser.
                try:
                    with page.expect_file_chooser() as chooser_info:
                        locator.click()
                    chooser_info.value.set_files(resolved)
                except Exception as error:
                    raise ToolFailure({"ok": False, "error": f"Could not upload to {ref}: {_actionability_error(error)}"})
            result = self._digest(session, page, previous=before_elements, before_url=before_url)
            result["did"] = f"Attached {len(resolved)} file(s) to {ref}"
            return result

        return self.guard(run)

    def drag(self, ref: Optional[str] = None, to_element: Optional[str] = None, *,
             x: Optional[int] = None, y: Optional[int] = None, to_x: Optional[int] = None,
             to_y: Optional[int] = None) -> dict:
        """Drag from a source element/point to a target element/point — drag and drop, or a
        drag-to-select. Element to element uses Playwright's hit-checked sequence; a point drag
        presses, moves, and releases the mouse."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            before_url = page.url
            before_elements = session.last_elements
            if ref is not None and to_element is not None:
                source = self._locator(session, page, ref)
                target = self._locator(session, page, to_element)
                try:
                    source.drag_to(target, timeout=active_tuning().amount(Limit.DRAG_TIMEOUT_MS))
                except Exception as error:
                    raise ToolFailure({"ok": False, "error": f"Could not drag {ref} to {to_element}: {_actionability_error(error)}"})
                did = f"Dragged {ref} onto {to_element}"
            else:
                start = self._point_of(session, page, ref, x, y)
                end = self._point_of(session, page, to_element, to_x, to_y)
                if start is None or end is None:
                    return {"ok": False, "error": "The drag action needs a source and target, each an element ref or an x/y point."}
                page.mouse.move(start[0], start[1])
                page.mouse.down()
                page.mouse.move(end[0], end[1], steps=12)
                page.mouse.up()
                did = f"Dragged ({round(start[0])}, {round(start[1])}) to ({round(end[0])}, {round(end[1])})"
            result = self._digest(session, page, previous=before_elements, before_url=before_url)
            result["did"] = did
            return result

        return self.guard(run)

    def _point_of(self, session: _Session, page, ref: Optional[str], x: Optional[int], y: Optional[int]):
        """A viewport point for a drag endpoint: an element's centre, or an explicit x/y, or None."""
        if ref is not None:
            box = self._locator(session, page, ref).bounding_box()
            if box is None:
                return None
            return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        if x is not None and y is not None:
            return float(x), float(y)
        return None

    def read(self, offset: int = 0, ref: Optional[str] = None) -> dict:
        """The page's visible text, one bounded window at a time. With ``ref``, only that element's
        subtree. ``offset`` continues a truncated read; the result says exactly which offset reaches
        the next window."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            if ref is not None:
                source = self._locator(session, page, ref).inner_text(timeout=active_tuning().amount(Limit.READ_TEXT_TIMEOUT_MS))
            else:
                source = page.inner_text("body", timeout=active_tuning().amount(Limit.READ_TEXT_TIMEOUT_MS))
            text = _clean_page_text(source)
            start = max(0, int(offset))
            # Size each window by a token budget (measured with the real tokenizer) rather than a
            # fixed character count, so a page of dense markup is paged as fairly as plain prose.
            window, truncated = clip_to_tokens(text[start:], active_tuning().amount(Limit.READ_WINDOW_TOKENS))
            next_offset = start + len(window)
            result: dict[str, Any] = {
                "ok": True, "url": page.url, "title": _safe_title(page),
                "text": window, "truncated": truncated, "total_chars": len(text),
            }
            if start:
                result["offset"] = start
            if truncated:
                result["note"] = message("read_truncated", next_offset=str(next_offset))
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
            # scale="css" captures at CSS-pixel resolution, not the display's device pixels. On a
            # Retina Mac (2× device pixels) the default doubles every coordinate, so a model reading
            # a click point off the image would miss by 2×; CSS scale keeps the image in the same
            # coordinate space clicks and the viewport use.
            page.screenshot(path=path, type="png", scale="css", timeout=active_tuning().amount(Limit.SCREENSHOT_TIMEOUT_MS))
            return {
                "ok": True, "image_path": path, "url": page.url, "title": _safe_title(page),
                "did": "Captured the visible viewport",
            }

        return self.guard(run)

    def evaluate(self, expression: str) -> dict:
        """Run a JavaScript expression in the page and return its result. The most direct path to
        structured extraction — reading an XHR endpoint's JSON, pulling a table into an array,
        computing an aggregate — versus paging 16 KB text windows. Runs in the user's real,
        signed-in page, so it is as privileged as anything on that origin; it rides the same
        explicit opt-in as the rest of the tool, and every call carries the model's justification.
        The result is JSON-serialized and length-bounded."""

        def run() -> dict:
            expression_text = expression.strip()
            if not expression_text:
                return {"ok": False, "error": "The evaluate action needs a JavaScript expression to run."}
            session = self.session()
            page = self.page(session)
            try:
                value = page.evaluate(expression_text)
            except Exception as error:
                return {"ok": False, "error": f"Evaluation failed: {str(error).splitlines()[0]}"}
            try:
                rendered = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                rendered = str(value)
            clipped, truncated = clip_to_tokens(rendered, active_tuning().amount(Limit.EVALUATE_TOKENS))
            result: dict[str, Any] = {
                "ok": True, "did": "Evaluated JavaScript", "url": page.url,
                "result": clipped,
            }
            if truncated:
                result["truncated"] = True
                result["note"] = message("evaluate_truncated")
            return result

        return self.guard(run)

    def network(self, query: str = "") -> dict:
        """The recent network requests the page made (method, url, status, resource type), newest
        last, optionally filtered by a substring of the url. Reads the page's own XHR/fetch traffic
        — the data endpoints behind a rendered view — which is often the fastest, most accurate way
        to extract what a page shows without walking its DOM."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            needle = query.strip().lower()
            entries = [
                entry for entry in session.requests
                if not needle or needle in entry["url"].lower()
            ]
            entries = entries[-active_tuning().amount(Limit.NETWORK_LIMIT):]
            return {
                "ok": True, "url": page.url, "count": len(entries), "requests": entries,
            }

        return self.guard(run)

    def history_back(self) -> dict:
        def run() -> dict:
            session = self.session()
            page = self.page(session)
            page.go_back(wait_until="domcontentloaded")
            return self._overview(session, page)

        return self.guard(run)

    def history_forward(self) -> dict:
        def run() -> dict:
            session = self.session()
            page = self.page(session)
            page.go_forward(wait_until="domcontentloaded")
            return self._overview(session, page)

        return self.guard(run)

    def reload(self) -> dict:
        """Reload the current page and return its fresh overview."""

        def run() -> dict:
            session = self.session()
            page = self.page(session)
            page.reload(wait_until="domcontentloaded")
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
                    page.goto(url, wait_until="domcontentloaded")
                except Exception:
                    pass
            # A deliberately blank tab has nothing to settle for; don't make the caller wait.
            result = self._overview(session, page, settle_page=bool(url))
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
        text = str(arguments.get("text", ""))
        key = str(arguments.get("key", ""))
        direction = str(arguments.get("direction") or "down")
        tab = str(arguments.get("tab", ""))
        browser_name = str(arguments.get("browser_name") or "chrome")
        ref = _as_ref(arguments.get("element"))
        to_ref = _as_ref(arguments.get("to_element"))
        point_x = _as_int(arguments.get("x"))
        point_y = _as_int(arguments.get("y"))
        clicks = int(arguments.get("clicks", 1) or 1)
        button = str(arguments.get("button") or "left")
        occurrence = int(arguments.get("occurrence", 1) or 1)
        goal = str(arguments.get("goal", ""))
        expect = str(arguments.get("expect", ""))
        dialog = str(arguments.get("dialog", ""))

        def needs_element(name: str) -> dict:
            return {"ok": False, "error": f"The {name} action needs an element ref from the last observe/find."}

        if action == "navigate":
            if not url:
                return {"ok": False, "error": "The navigate action needs a url."}
            return self.navigate(url, browser=browser_name, goal=goal, expect=expect)
        if action == "observe":
            return self.observe(element=ref, goal=goal)
        if action == "find":
            query = str(arguments.get("query", ""))
            if not query.strip():
                return {"ok": False, "error": "The find action needs a query — the text to look for on the page."}
            return self.find(query)
        if action == "evaluate":
            return self.evaluate(str(arguments.get("expression", "") or text))
        if action == "network":
            return self.network(str(arguments.get("query", "")))
        if action == "screenshot":
            return self.screenshot()
        if action == "click":
            return self.click(ref, x=point_x, y=point_y, clicks=clicks, button=button, dialog=dialog, expect=expect)
        if action == "hover":
            return self.hover(ref, x=point_x, y=point_y)
        if action == "type":
            if ref is None:
                return needs_element("type")
            return self.type_text(ref, text, submit=bool(arguments.get("submit", False)), mode=str(arguments.get("mode", "replace") or "replace"), expect=expect)
        if action == "edit":
            if ref is None:
                return needs_element("edit")
            find = str(arguments.get("find", ""))
            if not find:
                return {"ok": False, "error": "The edit action needs find (the exact text to change) and replace."}
            return self.edit_text(ref, find, str(arguments.get("replace", "")), replace_all=bool(arguments.get("replace_all", False)))
        if action == "select":
            if ref is None:
                return needs_element("select")
            return self.select(
                ref, text=text or None, to_text=str(arguments.get("to_text", "")) or None,
                select_all=bool(arguments.get("select_all", False)), occurrence=occurrence,
            )
        if action == "caret":
            if ref is None:
                return needs_element("caret")
            return self.caret(
                ref, before=str(arguments.get("before", "")) or None,
                after=str(arguments.get("after", "")) or None, at_offset=_as_int(arguments.get("at_offset")),
                edge=str(arguments.get("edge", "")), occurrence=occurrence,
            )
        if action in ("copy", "cut", "paste"):
            return self.clipboard_action(action, ref)
        if action == "choose":
            if ref is None:
                return needs_element("choose")
            option = str(arguments.get("option", ""))
            if not option:
                return {"ok": False, "error": "The choose action needs an option — the visible label (or value) to pick."}
            return self.choose(ref, option)
        if action == "upload":
            if ref is None:
                return needs_element("upload")
            paths = arguments.get("paths") or []
            if isinstance(paths, str):
                paths = [paths]
            if not paths:
                return {"ok": False, "error": "The upload action needs paths — the local file(s) to attach."}
            return self.upload(ref, [str(path) for path in paths])
        if action == "drag":
            return self.drag(
                ref, to_ref, x=point_x, y=point_y,
                to_x=_as_int(arguments.get("to_x")), to_y=_as_int(arguments.get("to_y")),
            )
        if action == "press":
            if not key:
                return {"ok": False, "error": "The press action needs a key (e.g. Enter)."}
            return self.press(key, expect=expect)
        if action == "scroll":
            return self.scroll(direction, ref=ref)
        if action == "read":
            return self.read(offset=int(arguments.get("offset", 0) or 0), ref=ref)
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
