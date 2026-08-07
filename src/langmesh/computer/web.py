"""Drives the user's own Chrome over the DevTools protocol: reads a page into searchable documents and acts on them with Playwright."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import tempfile
from collections import deque
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, Optional

from langmesh.computer.retrieval import Document, element_text, web_element_text
from langmesh.computer.surface import (
    Element,
    Glance,
    Surface,
    ToolFailure,
    message_loader,
    resolve_caret,
    resolve_range,
)
from langmesh.base.tuning import Tunable, active_tuning, settle

logger = logging.getLogger(__name__)


def _milliseconds(tunable: Tunable) -> int:
    """A tuned wait in the milliseconds Playwright takes. Every tunable is seconds; this is the one boundary that is not."""
    return max(1, int(active_tuning().amount(tunable) * 1000))


# A window is numbered by the window server and a tab by DevTools, so the prefix tells `_page_for` which one it has.
WINDOW_PREFIX = "win"
TAB_PREFIX = "tab"


@dataclass
class _Bound:
    """The session and page one primitive acts through, passed rather than remembered so two scripts cannot cross."""

    session: "_Session"
    page: Any


message = message_loader("browser")


def _page_script(name: str) -> str:
    """A page script read from `scripts/` at import, kept as real `.js` so editors and linters can see it."""
    return (Path(__file__).parent / "scripts" / f"{name}.js").read_text()


# The DOM selection Playwright has no native API for: an arbitrary substring, or the caret at an offset.
_APPLY_SELECTION_JS = _page_script("apply_selection")
# Tooltips keyed by the visible text that carries them, so a `title` can enrich an element's key.
_TITLES_BY_LABEL = _page_script("titles_by_label")
# What the page has focus on, in whatever words it publishes — one half of a glance's diff.
_FOCUSED_ELEMENT = _page_script("focused_element")


def _decode_body(text: str, content_type: str = "") -> Any:
    """A captured body as structured data when it is JSON, and as the plain string otherwise."""
    stripped = text.lstrip()
    if "json" in content_type or stripped[:1] in "{[":
        try:
            return json.loads(text)
        except Exception:
            pass
    return text


def _body_shape(value: Any) -> Any:
    """A captured body described by its shape: the same structure with every leaf replaced by the name of its type."""
    if isinstance(value, bool):
        return "bool"  # before int: a bool is one in Python, and not one to a reader
    if value is None:
        return "null"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return {key: _body_shape(entry) for key, entry in value.items()}
    if isinstance(value, list):
        # One shape unioned across all items, so a field only some rows carry still shows up.
        shapes = [_body_shape(item) for item in value]
        if not shapes:
            return []
        merged = shapes[0]
        for shape in shapes[1:]:
            merged = _merged_shape(merged, shape)
        return [merged]
    return type(value).__name__


def _merged_shape(left: Any, right: Any) -> Any:
    """Two shapes of the same array, combined into one that admits both."""
    if isinstance(left, dict) and isinstance(right, dict):
        return {
            key: _merged_shape(left[key], right[key])
            if key in left and key in right
            else left.get(key, right.get(key))
            for key in (*left, *(key for key in right if key not in left))
        }
    if isinstance(left, list) and isinstance(right, list):
        if not left:
            return right
        if not right:
            return left
        return [_merged_shape(left[0], right[0])]
    # An absent value says nothing about a type, so anything outranks `null`; otherwise the first shape stands.
    if left == "null":
        return right
    return left


# The Chromium browsers we can drive, mapped to the support directory holding each one's DevToolsActivePort file.
_SUPPORT_ROOT = Path.home() / "Library" / "Application Support"
BROWSERS = {
    "chrome": {"data": _SUPPORT_ROOT / "Google" / "Chrome"},
    "edge": {"data": _SUPPORT_ROOT / "Microsoft Edge"},
    "brave": {"data": _SUPPORT_ROOT / "BraveSoftware" / "Brave-Browser"},
}

# Where the user turns on Chrome's remote-debugging switch, surfaced so the interface can offer a one-click button.
REMOTE_DEBUGGING_URL = "chrome://inspect/#remote-debugging"


def _not_connected_payload() -> dict:
    """The result when remote debugging is off, structured so the interface can offer to open the switch."""
    return {
        "ok": False,
        "error": message("not_connected", enable_url=REMOTE_DEBUGGING_URL),
        "code": "browser_remote_debugging_off",
        "enable_url": REMOTE_DEBUGGING_URL,
    }


def _awaiting_authorization_payload(seconds: float) -> dict:
    """The result while Chrome asks the user to approve the connection, which is worth waiting out rather than retrying."""
    return {
        "ok": False,
        "awaiting": "browser_authorization",
        "error": message("awaiting_authorization", seconds=f"{seconds:g}"),
        "code": "browser_awaiting_authorization",
        "enable_url": REMOTE_DEBUGGING_URL,
    }


def _devtools_websocket_url(browser: str) -> Optional[str]:
    """The browser-level DevTools WebSocket URL from the `DevToolsActivePort` file, or `None` when the switch is off."""
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
        # Which pages have had their dialog, download and network handlers wired; ids come from the browser's own target list.
        self.tab_ids: dict[Any, bool] = {}
        self._tab_counter = count(1)
        # One CDP session against the browser rather than a page, so a listing never speaks to a renderer.
        self._device = None
        # The browser's own target id, mapped to the `tabN` the model sees and that tab's last known url.
        self._ids_by_target: dict[str, str] = {}
        self.url_by_tab: dict[str, str] = {}
        # Playwright `Page` mapped to its browser target id, asked once and kept, so an id survives navigation.
        self._targets_by_page: dict[Any, str] = {}
        self._targets_cache: Optional[list[dict]] = None
        self._targets_read: float = 0.0
        # What to do with the next JavaScript dialog; `None` acknowledges alerts and declines questions.
        self.pending_dialog: Optional[str] = None
        # Recent network exchanges as method, url, status and body shape, never contents, in a bounded rolling window.
        self.exchanges: deque[dict] = deque(maxlen=active_tuning().amount(Tunable.web_exchanges))
        self._exchange_counter = count(1)
        # Live WebSockets and their recent frames, keyed by a model-facing id and pruned oldest-first.
        self.websockets: dict[str, dict] = {}
        self._websocket_counter = count(1)
        # Dialogs auto-handled and downloads captured since the last result, drained into it.
        self.events: deque[dict] = deque(maxlen=8)
        # Frame id mapped to the aria-ref of the `iframe` that owns it, as the last snapshot stated it.
        self.frame_owners: dict[str, str] = {}

    def device(self):
        """The browser-level CDP session, made once and kept, since only there can a question be asked about the tabs."""
        if self._device is None:
            self._device = self.browser.new_browser_cdp_session()
        return self._device

    def describe_targets(self) -> list[dict]:
        """Every open tab as id, title, url and window, read from the browser in two commands rather than from each renderer."""
        # Memoised for a moment, because callers ask per page as well as per listing and N listings is N squared commands.
        now = time.monotonic()
        if self._targets_cache is not None and now - self._targets_read < 0.5:
            return self._targets_cache
        device = self.device()
        infos = device.send("Target.getTargets").get("targetInfos", [])
        # The window server's Chrome windows, read once and joined to Chrome's own by rectangle below.
        windows = _browser_windows_by_rectangle()
        described = []
        for info in infos:
            if info.get("type") != "page":
                continue
            target_id = info.get("targetId") or ""
            window, bounds = None, None
            try:
                placed = device.send("Browser.getWindowForTarget", {"targetId": target_id})
                # Chrome reports the window's rectangle for free, and the rectangle is the one thing it and the window server agree on.
                bounds = placed.get("bounds")
                window = _match_rectangle(bounds, windows)
            except Exception:  # noqa: BLE001 — a target that will not place itself is still a tab
                pass
            described.append(
                {
                    "target_id": target_id,
                    "title": info.get("title") or "",
                    "url": info.get("url") or "",
                    "window": window,
                    "bounds": bounds,
                }
            )
        self._targets_cache, self._targets_read = described, now
        return described

    def tab_id_for_target(self, target_id: str, url: str) -> str:
        """The model-facing id for a tab, keyed by the browser's target id so it survives the tab being discarded and restored."""
        identifier = self._ids_by_target.get(target_id)
        if identifier is None:
            identifier = f"tab{next(self._tab_counter)}"
            self._ids_by_target[target_id] = identifier
        self.url_by_tab[identifier] = url
        return identifier

    def target_of(self, page) -> str:
        """The browser's own target id for a page, asked once and kept, so a tab's identity survives its content changing."""
        remembered = self._targets_by_page.get(page)
        if remembered:
            return remembered
        try:
            session = self.context.new_cdp_session(page)
            target_id = str(
                (session.send("Target.getTargetInfo") or {})
                .get("targetInfo", {})
                .get("targetId", "")
            )
        except Exception:
            return ""
        if target_id:
            self._targets_by_page[page] = target_id
        return target_id

    def tab_id(self, page) -> str:
        """The id a page is known by, keyed on the browser's target with the url only as a fallback."""
        target_id = self.target_of(page)
        if target_id:
            known = self._ids_by_target.get(target_id)
            if known:
                return known
            return self.tab_id_for_target(target_id, _safe_url(page))
        url = _safe_url(page)
        for entry in self.describe_targets():
            if entry["url"] == url:
                return self.tab_id_for_target(entry["target_id"], entry["url"])
        return ""

    def live_pages(self) -> list:
        """The tabs still open, in the browser's own order."""
        return [page for page in self.context.pages if not page.is_closed()]

    def adopt(self, page) -> None:
        """Track a page and wire its dialog, download and network handling, answering dialogs at once so the page never freezes."""
        self.tab_ids[page] = True  # adopted; the model-facing id comes from the browser
        # Bind the page to the browser's handle now, one round trip, so the tab keeps its id through every navigation.
        self.target_of(page)

        def on_dialog(dialog) -> None:
            intent = self.pending_dialog
            self.pending_dialog = None
            if intent == "accept":
                accepted = True
            elif intent == "dismiss":
                accepted = False
            else:
                accepted = dialog.type == "alert"
            self.events.append(
                {"dialog": {"type": dialog.type, "message": dialog.message, "accepted": accepted}}
            )
            try:
                dialog.accept() if accepted else dialog.dismiss()
            except Exception:
                pass

        def on_download(download) -> None:
            try:
                destination = os.path.join(
                    tempfile.mkdtemp(prefix="langmesh-web-download-"), download.suggested_filename
                )
                download.save_as(destination)
                self.events.append({"download": {"path": destination, "url": download.url}})
            except Exception as error:
                self.events.append({"download": {"url": download.url, "error": str(error)}})

        def on_response(response) -> None:
            # Capture data-shaped exchanges as their shape only, so no response body is ever held or returned.
            try:
                request = response.request
                resource_type = request.resource_type
                entry: dict[str, Any] = {
                    "id": f"req{next(self._exchange_counter)}",
                    "method": request.method,
                    "url": request.url,
                    "status": response.status,
                    "type": resource_type,
                }
                request_headers: dict[str, str] = {}
                try:
                    request_headers = dict(request.headers)
                except Exception:
                    pass
                headers: dict[str, str] = {}
                try:
                    headers = dict(response.headers)
                except Exception:
                    pass
                # Header names are kept and values dropped, so a replay knows what the endpoint expects without carrying a bearer token.
                if request_headers:
                    entry["request_header_names"] = sorted(request_headers)
                if headers:
                    entry["response_header_names"] = sorted(headers)
                    if headers.get("content-type"):
                        entry["content_type"] = headers["content-type"]
                try:
                    post = request.post_data
                    if post:
                        entry["request_body"] = _body_shape(
                            _decode_body(post, request_headers.get("content-type", ""))
                        )
                except Exception:
                    pass
                content_type = headers.get("content-type", "")
                if resource_type in ("xhr", "fetch") and any(
                    marker in content_type
                    for marker in ("json", "javascript", "text", "xml", "graphql", "urlencoded")
                ):
                    try:
                        entry["response_body"] = _body_shape(
                            _decode_body(response.text(), content_type)
                        )
                    except Exception:
                        pass
                self.exchanges.append(entry)
            except Exception:
                pass

        def on_websocket(websocket) -> None:
            # Observe a WebSocket's frames, so the model can search them and act on the socket in-page with `evaluate`.
            identifier = f"ws{next(self._websocket_counter)}"
            if len(self.websockets) >= active_tuning().amount(Tunable.web_websockets):
                self.websockets.pop(next(iter(self.websockets)))
            record: dict[str, Any] = {
                "id": identifier,
                "url": websocket.url,
                "frames": deque(maxlen=active_tuning().amount(Tunable.web_websocket_frames)),
            }
            self.websockets[identifier] = record

            def note(direction: str):
                def handler(payload) -> None:
                    if isinstance(payload, (bytes, bytearray)):
                        record["frames"].append(
                            {"direction": direction, "binary_bytes": len(payload)}
                        )
                    else:
                        # Shaped like every other body, since a live feed never stops producing and verbatim frames do the most damage.
                        record["frames"].append(
                            {"direction": direction, "data": _body_shape(_decode_body(payload))}
                        )

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


# Playwright's ref-carrying accessibility snapshot, parsed into the shared indexed `Element` a find ranks.

_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "switch",
        "tab",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "slider",
        "spinbutton",
        "treeitem",
        "listbox",
        "menu",
        "menubar",
        "togglebutton",
        "scrollbar",
    }
)

_SURFACED_FLAGS = ("checked", "disabled", "expanded", "selected", "pressed", "active")

_SNAPSHOT_LINE = re.compile(r"^(\s*)-\s+(?P<head>[^\s\[\":]+)(?P<rest>.*)$")
_SNAPSHOT_NAME = re.compile(r'"((?:[^"\\]|\\.)*)"')
_SNAPSHOT_ATTRS = re.compile(r"\[([a-zA-Z-]+)(?:=([^\]]*))?\]")
# `- /url: "/wiki/Braille"` — the destination Playwright prints under a link, quoted or bare.
_SNAPSHOT_URL = re.compile(r'^\s*-\s*/url:\s*"?(?P<url>[^"]*?)"?\s*$')
# An aria-ref inside an iframe carries the frame it belongs to, and that prefix is reused as the frame's id.
_FRAME_PREFIX = re.compile(r"^(f\d+)e\d+$")

_LIVE_REGION_ROLES = frozenset({"alert", "status"})
_STRUCTURAL_ROLES = frozenset(
    {
        "region",
        "navigation",
        "main",
        "complementary",
        "banner",
        "contentinfo",
        "form",
        "search",
    }
)
# Roles whose accessible name labels the section around them, becoming the `context` of the plainer controls inside.
_LABEL_ROLES = _STRUCTURAL_ROLES | frozenset({"heading", "link"})


def _frame_of(reference: Optional[str]) -> str:
    """The frame an aria-ref belongs to (``f1e3`` gives ``f1``), or ``""`` for the main document."""
    match = _FRAME_PREFIX.match(reference or "")
    return match.group(1) if match else ""


def _parse_snapshot(snapshot: str) -> tuple[list[Element], dict[str, str]]:
    """Parse the aria snapshot into shared `Element` objects carrying their ref and nearest label, plus the frame ownership it states."""
    elements: list[Element] = []
    labels: dict[int, str] = {}
    frame_owners: dict[str, str] = {}
    open_iframes: list[tuple[int, str]] = []  # (depth, the iframe element's own ref)
    last_depth = -1  # depth of the most recently kept element
    for line in snapshot.splitlines():
        match = _SNAPSHOT_LINE.match(line)
        if match is None:
            continue
        depth = len(match.group(1))
        role = match.group("head")
        rest = match.group("rest")
        if role.startswith("/"):
            # A property line like `- /url: "..."` describes the element above it rather than being a node of its own.
            url_match = _SNAPSHOT_URL.match(line)
            if url_match and elements and depth > last_depth:
                elements[-1].flags["url"] = url_match.group("url")
            continue
        name_match = _SNAPSHOT_NAME.search(rest)
        name = name_match.group(1).replace('\\"', '"') if name_match else ""
        attributes = dict(_SNAPSHOT_ATTRS.findall(rest))
        tail = rest[name_match.end() :] if name_match else rest
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
        # Frame bookkeeping runs before the filter below, because an `iframe` node is exactly what that filter drops.
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
        element = Element(
            role=role,
            name=name,
            value=value or None,
            clickable=clickable,
            context=context,
            token=reference,
        )
        for flag in _SURFACED_FLAGS:
            if flag in attributes:
                element.flags[flag] = attributes[flag] if attributes[flag] else True
        elements.append(element)
        last_depth = depth
    return elements, frame_owners


def _snapshot(page) -> str:
    """The ref-carrying accessibility snapshot of the whole page (iframes inlined)."""
    return page.locator("body").aria_snapshot(
        mode="ai", timeout=_milliseconds(Tunable.snapshot_timeout)
    )


# Tooltips collected in one pass and keyed by the visible text they sit on, since the aria snapshot hides them.


def _folded_label(text: str) -> str:
    """The form a label is joined on, with whitespace collapsed and nothing else, used by both sides of the join."""
    return " ".join((text or "").split())


def _titles_by_label(page) -> dict[str, str]:
    """Every unambiguous tooltip on the page, or an empty mapping when the read fails."""
    try:
        found = page.evaluate(_TITLES_BY_LABEL)
    except Exception:  # noqa: BLE001 — an unreadable tooltip is not a reason to fail a find
        logger.debug("could not read tooltips from the page", exc_info=True)
        return {}
    if not isinstance(found, dict):
        return {}
    return {
        _folded_label(str(label)): str(title) for label, title in found.items() if label and title
    }


# Icon fonts render ligatures as Private Use Area characters that leak into a text read as garbage.
_PRIVATE_USE_CHARS = re.compile("[-\U000f0000-\U000ffffd\U00100000-\U0010fffd]")
_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def _clean_page_text(text: str) -> str:
    return _BLANK_LINES.sub("\n\n", _PRIVATE_USE_CHARS.sub("", text))


_KEY_ALIASES = {
    "enter": "Enter",
    "escape": "Escape",
    "tab": "Tab",
    "backspace": "Backspace",
    "delete": "Delete",
    "arrowdown": "ArrowDown",
    "arrowup": "ArrowUp",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "pagedown": "PageDown",
    "pageup": "PageUp",
    "home": "Home",
    "end": "End",
    "space": "Space",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "return": "Enter",
    "esc": "Escape",
    "forwarddelete": "Delete",
    # Playwright capitalises the function keys; a menu bar advertises them lowercase.
    **{f"f{number}": f"F{number}" for number in range(1, 13)},
}

# What a chord's modifiers are called here, in Playwright and on a window, so one spelling reaches all three.
_MODIFIER_NAMES = {
    "cmd": "Meta",
    "command": "Meta",
    "meta": "Meta",
    "super": "Meta",
    "win": "Meta",
    "⌘": "Meta",
    "ctrl": "Control",
    "control": "Control",
    "⌃": "Control",
    "opt": "Alt",
    "option": "Alt",
    "alt": "Alt",
    "⌥": "Alt",
    "shift": "Shift",
    "⇧": "Shift",
}


def _playwright_chord(key: str) -> str:
    """A chord written the way the harness writes chords, in the spelling Playwright wants."""
    parts = [part.strip() for part in key.strip().split("+") if part.strip()]
    if len(parts) <= 1:
        single = key.strip()
        return _KEY_ALIASES.get(single.lower(), single)
    *modifiers, final = parts
    named = [_MODIFIER_NAMES.get(modifier.lower(), modifier.capitalize()) for modifier in modifiers]
    resolved = _KEY_ALIASES.get(final.lower(), final if len(final) > 1 else final.lower())
    return "+".join([*named, resolved])


_SCROLL_DIRECTIONS = frozenset({"down", "up", "left", "right", "top", "bottom"})
_SCROLL_JUMP = 1_000_000


def _element_signature(page) -> int:
    """A cheap signature of what is on the page, fed to `settle` so a revealing action is waited out rather than slept through."""
    try:
        return len(_parse_snapshot(_snapshot(page))[0])
    except Exception:
        return -1


def _browser_windows_by_rectangle() -> list[tuple[tuple, int]]:
    """Every Chrome window the window server knows, as rectangle and id, for joining against Chrome's own rectangles."""
    from langmesh.computer import targets as target_registry

    found = []
    for window in target_registry.list_windows():
        placed = getattr(window, "bounds", None)
        identifier = str(window.id)
        if not placed or len(placed) != 4 or not identifier.startswith(f"{WINDOW_PREFIX}-"):
            continue
        if window.app != "Google Chrome":
            continue
        found.append((tuple(placed), int(identifier.split("-", 1)[1])))
    return found


def _match_rectangle(bounds: Optional[dict], windows: list[tuple[tuple, int]]) -> Optional[int]:
    """The window id whose rectangle is Chrome's `bounds`, within a point or two of frame-versus-content difference."""
    if not bounds:
        return None
    wanted = (bounds.get("left"), bounds.get("top"), bounds.get("width"), bounds.get("height"))
    if any(value is None for value in wanted):
        return None
    for rectangle, identifier in windows:
        if all(abs(a - b) <= 2 for a, b in zip(rectangle, wanted)):
            return identifier
    return None


def _safe_url(page) -> str:
    """A page's url, a cached attribute that costs nothing and cannot stall."""
    try:
        return page.url
    except Exception:
        return ""


def _await_quiet(page) -> None:
    """Let the DOM parse after an action without blocking on a stalled resource, bounded and swallowed."""
    ceiling_milliseconds = max(1, int(active_tuning().settle_give_up() * 1000))
    try:
        page.wait_for_load_state("domcontentloaded", timeout=ceiling_milliseconds)
    except Exception:
        pass


def _actionability_error(error: Exception) -> str:
    """The honest reason an action could not complete, keeping the diagnostic lines that name why."""
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    if not lines:
        return error.__class__.__name__
    headline = lines[0]
    diagnostics = [
        line
        for line in lines[1:]
        if (
            "intercepts pointer events" in line
            or line.startswith("waiting for")
            or "is not visible" in line
            or "is not enabled" in line
            or "is not stable" in line
        )
    ]
    seen: set[str] = set()
    unique = [line for line in diagnostics if not (line in seen or seen.add(line))]
    return " — ".join([headline, *unique[:3]]) if unique else headline


class WebSurface(Surface):
    """The Chrome and Playwright implementation of the shared `Surface`."""

    def __init__(self) -> None:
        super().__init__("langmesh-playwright", message)
        self._playwright = None
        self._session: Optional[_Session] = None

    # Failure and recovery.

    def on_recover(self) -> dict:
        self._session = None
        return {}

    def recover(self, detail: str) -> dict:
        return {"ok": False, "error": message("connection_dropped", detail=detail)}

    def preflight(self, operation: str) -> Optional[dict]:
        """Gate a read on the browser being reachable, so a switched-off Chrome surfaces as the not-connected payload up front."""
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
        # Budgeted as a human reaction time rather than a network timeout, since the user has to find and click Allow.
        budget = _milliseconds(Tunable.browser_authorization)
        try:
            connected = self._playwright.chromium.connect_over_cdp(websocket_url, timeout=budget)
        except PlaywrightTimeout:
            raise ToolFailure(_awaiting_authorization_payload(budget / 1000.0))
        except PlaywrightError as error:
            # The switch is demonstrably on, so this cannot be advice to turn it on: that would dismiss any prompt still waiting.
            raise ToolFailure(
                {
                    "ok": False,
                    "error": message("connection_refused", detail=str(error).splitlines()[0]),
                    "code": "browser_connection_refused",
                    "enable_url": REMOTE_DEBUGGING_URL,
                }
            )
        context = connected.contexts[0] if connected.contexts else connected.new_context()
        context.set_default_timeout(_milliseconds(Tunable.action_timeout))
        context.set_default_navigation_timeout(_milliseconds(Tunable.navigation_timeout))
        session = _Session(connected, context)
        # Pages are adopted when something first acts in them, since the listing reads from the browser and needs no handlers.
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

    def _title_of(self, session: _Session, page) -> str:
        """A page's title from the browser's listing, because a discarded renderer never answers `page.title()`."""
        url = _safe_url(page)
        if not url:
            return ""
        try:
            for entry in session.describe_targets():
                if entry["url"] == url:
                    return entry["title"]
        except Exception:  # noqa: BLE001 — a title is never worth failing an action over
            logger.debug("could not read a page's title from the browser listing", exc_info=True)
        return ""

    def _window_of(self, session: _Session, page) -> Optional[int]:
        """The window-server id of the window a page is displayed in, which only `Browser.getWindowForTarget` knows."""
        url = _safe_url(page)
        if not url:
            return None
        try:
            for entry in session.describe_targets():
                if entry["url"] == url:
                    return entry["window"]
        except Exception:  # noqa: BLE001 — a browser that will not say leaves the page unplaced
            logger.debug("could not resolve the window of a page", exc_info=True)
        return None

    def _page_for(self, session: _Session, target: str):
        """The page a target names: a tab by its DevTools id, or the front page of a window."""
        if target.startswith(f"{TAB_PREFIX}-") or not target.startswith(f"{WINDOW_PREFIX}-"):
            wanted = target.split("-", 1)[-1]
            names = {target, wanted, f"{TAB_PREFIX}{wanted}"}
            # By the browser's own target id first, since that is the one handle navigation does not change.
            for page in session.live_pages():
                bound_target = session._targets_by_page.get(page)
                if bound_target and session._ids_by_target.get(bound_target) in names:
                    return page
            for page in session.live_pages():
                if session.tab_id(page) in names:
                    return page
            # Only now the url the listing recorded, which describes where a tab has been rather than where it is.
            for candidate in names:
                url = session.url_by_tab.get(candidate)
                if url:
                    for page in session.context.pages:
                        if not page.is_closed() and _safe_url(page) == url:
                            return page
            raise ToolFailure({"ok": False, "error": f"Tab {target!r} is no longer open."})
        window_id = int(target.split("-", 1)[1])
        in_window = [
            page for page in session.live_pages() if self._window_of(session, page) == window_id
        ]
        if not in_window:
            # The window is real but Chrome reports no page in it: a window showing only its own interface, or one just closed.
            return self.page(session)
        return next((page for page in in_window if page is session.page), in_window[0])

    def _bind(self, target: str) -> _Bound:
        session = self.session()
        page = self._page_for(session, target)
        # The one place a page becomes one this tool acts through, and so the one place handlers are wired; idempotent.
        if page not in session.tab_ids:
            session.adopt(page)
        session.page = page
        return _Bound(session=session, page=page)

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
        """The Playwright locator for an element id from a recent search, using Playwright's own aria-ref."""
        if not ref:
            raise ToolFailure(
                {
                    "ok": False,
                    "error": "This action needs an element id from a find (find_one or find_many).",
                }
            )
        return page.locator(f"aria-ref={ref}")

    def _why_it_failed(self, page, element: str, error: Exception) -> str:
        """Why an action on `element` did not happen, and whether the element is on the page at all."""
        detail = _actionability_error(error)
        try:
            if page.locator(f"aria-ref={element}").count() == 0:
                return f"{detail} — {message('stale_element', identifier=element)}"
        except Exception:
            pass
        return detail

    def _field_text(self, locator) -> str:
        try:
            return locator.input_value()
        except Exception:
            return locator.text_content() or ""

    def _frame(self, session: _Session, page, identifier: str):
        """The live Playwright `Frame` a frame id names, resolved through the `iframe` the snapshot says owns it."""
        if not session.frame_owners:
            _, session.frame_owners = _parse_snapshot(_snapshot(page))
        element_ref = session.frame_owners.get(identifier)
        if element_ref is None:
            known = ", ".join(sorted(session.frame_owners)) or "none"
            raise ToolFailure(
                {
                    "ok": False,
                    "error": f"No frame {identifier!r} on this page (frames here: {known}). Call frames() for what is there.",
                }
            )
        if not element_ref:
            return page.main_frame
        frame = None
        try:
            handle = page.locator(f"aria-ref={element_ref}").element_handle(
                timeout=_milliseconds(Tunable.frame_resolve_timeout)
            )
            frame = handle.content_frame() if handle is not None else None
        except Exception:
            frame = None
        if frame is None:
            raise ToolFailure(
                {
                    "ok": False,
                    "error": f"Frame {identifier!r} is no longer on the page. Read the page again to get current frames.",
                }
            )
        return frame

    def _frame_or_page(self, session: _Session, page, identifier: str):
        return self._frame(session, page, identifier) if identifier else page

    # Perceiving — find.

    def documents(self, target: str = "") -> dict:
        """Read the page into retrieval documents: one per element and one per recent network exchange."""

        def run() -> dict:
            bound = self._bind(target)
            session, page = bound.session, bound.page
            documents: list[Document] = []
            elements, session.frame_owners = _parse_snapshot(_snapshot(page))
            tooltips = _titles_by_label(page)
            for element in elements:
                title = tooltips.get(_folded_label(element.name), "")
                # What the model reads keeps `context`; what the embedding ranks leaves it out, so siblings stay distinguishable.
                shown = element_text(
                    name=element.name, value=element.value, context=element.context
                )
                key = web_element_text(
                    name=element.name,
                    url=str(element.flags.get("url") or ""),
                    title=title,
                    value=element.value if isinstance(element.value, str) else "",
                )
                payload: dict[str, Any] = {"role": element.role}
                if title:
                    payload["title"] = title
                # Which frame the element sits in, so a model meeting `f1e3` need not spend a call asking what `f1` is.
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
                if shown:
                    payload["text"] = shown
                documents.append(Document(id=element.token or "", text=key, payload=payload))
            for exchange in list(session.exchanges):
                documents.append(
                    Document(
                        id=exchange["id"],
                        text=f"{exchange['method']} {exchange['url']}",
                        payload={"kind": "request", **exchange},
                    )
                )
            for record in list(session.websockets.values()):
                documents.append(
                    Document(
                        id=record["id"],
                        text=f"websocket {record['url']}",
                        payload={
                            "kind": "websocket",
                            "url": record["url"],
                            "frames": list(record["frames"]),
                        },
                    )
                )
            return {
                "ok": True,
                "url": _safe_url(page),
                "title": self._title_of(session, page),
                "documents": documents,
            }

        return self.guard(run)

    # Acting — control_screen. ``perform`` routes one primitive call to its handler.

    def perform(self, target: str, operation: str, arguments: list, keywords: dict) -> dict:
        handler = getattr(self, f"_primitive_{operation}", None)
        if handler is None:
            from langmesh.computer import targets as target_registry

            available = ", ".join(target_registry.vocabularies()[target_registry.PAGE_VOCABULARY])
            return {
                "ok": False,
                "error": f"A page has no {operation!r} action. It has: {available}.",
            }

        # Bound on the worker thread together with the call it binds for, because Playwright's sync API is thread-affine.
        def bound_call() -> dict:
            try:
                bound = self._bind(target)
            except ToolFailure as failure:
                return failure.payload
            return self.call_primitive(operation, handler, bound, arguments, keywords)

        return self.worker.submit(bound_call)

    def _primitive_click(
        self,
        bound: _Bound,
        element: str,
        *,
        button: str = "left",
        count: int = 1,
        dialog: str = "",
        **_: Any,
    ) -> dict:
        def run() -> dict:
            session, page = bound.session, bound.page
            session.pending_dialog = dialog or None
            try:
                self._locator(page, element).click(button=button, click_count=count)
            except Exception as error:
                raise ToolFailure(
                    {
                        "ok": False,
                        "error": f"Could not click {element}: {self._why_it_failed(page, element, error)}",
                    }
                )
            _await_quiet(page)
            return self._acted(session, page, f"Clicked {element}")

        return self.guard(run)

    def _primitive_type(
        self,
        bound: _Bound,
        element: str,
        text: str,
        *,
        submit: bool = False,
        mode: str = "replace",
        **_: Any,
    ) -> dict:
        def run() -> dict:
            session, page = bound.session, bound.page
            locator = self._locator(page, element)
            try:
                if mode == "insert":
                    locator.focus()
                    page.keyboard.insert_text(text)
                else:
                    locator.fill(text)
            except Exception as error:
                raise ToolFailure(
                    {
                        "ok": False,
                        "error": f"Could not type into {element}: {self._why_it_failed(page, element, error)}",
                    }
                )
            landed = self._field_text(locator)
            if not submit:
                result: dict[str, Any] = {
                    "ok": True,
                    "did": f"Typed into {element}",
                    "value": landed,
                }
                if mode == "replace" and landed != text:
                    result["note"] = message("type_clamped")
                return result
            session.pending_dialog = None
            locator.press("Enter")
            _await_quiet(page)
            result = self._acted(session, page, f"Typed into {element} and pressed Enter")
            result["value"] = landed
            return result

        return self.guard(run)

    def _primitive_press(self, bound: _Bound, key: str, **_: Any) -> dict:
        def run() -> dict:
            session, page = bound.session, bound.page
            resolved = _playwright_chord(key)
            try:
                page.keyboard.press(resolved)
            except Exception as error:
                return {
                    "ok": False,
                    "error": f"Could not press {key!r}: {_actionability_error(error)}",
                }
            _await_quiet(page)
            return self._acted(session, page, f"Pressed {resolved}")

        return self.guard(run)

    def _primitive_hover(self, bound: _Bound, element: str, **_: Any) -> dict:
        def run() -> dict:
            session, page = bound.session, bound.page
            try:
                self._locator(page, element).hover()
            except Exception as error:
                raise ToolFailure(
                    {
                        "ok": False,
                        "error": f"Could not hover {element}: {self._why_it_failed(page, element, error)}",
                    }
                )
            settle(lambda: _element_signature(page))
            return self._acted(session, page, f"Hovered {element}")

        return self.guard(run)

    def _primitive_scroll(
        self, bound: _Bound, element: Optional[str] = None, *, direction: str = "down", **_: Any
    ) -> dict:
        normalized_direction = direction.strip().lower()
        if normalized_direction not in _SCROLL_DIRECTIONS:
            return {
                "ok": False,
                "error": f"Unknown scroll direction {direction!r}. Use down, up, left, right, top, or bottom.",
            }

        def run() -> dict:
            session, page = bound.session, bound.page
            size = page.viewport_size or {"width": 1280, "height": 720}
            if element is not None:
                box = self._locator(page, element).bounding_box()
                if box is None:
                    raise ToolFailure(
                        {
                            "ok": False,
                            "error": f"Element {element!r} has no on-screen position to scroll at. Search again.",
                        }
                    )
                point_x = min(max(box["x"] + box["width"] / 2, 1), size["width"] - 1)
                point_y = min(max(box["y"] + box["height"] / 2, 1), size["height"] - 1)
                page.mouse.move(point_x, point_y)
            else:
                page.mouse.move(size["width"] / 2, size["height"] / 2)
            step_x, step_y = int(size["width"] * 0.875), int(size["height"] * 0.875)
            deltas = {
                "down": (0, step_y),
                "up": (0, -step_y),
                "right": (step_x, 0),
                "left": (-step_x, 0),
                "top": (0, -_SCROLL_JUMP),
                "bottom": (0, _SCROLL_JUMP),
            }
            delta_x, delta_y = deltas[normalized_direction]
            page.mouse.wheel(delta_x, delta_y)
            settle(lambda: _element_signature(page))
            return self._acted(session, page, f"Scrolled {normalized_direction}")

        return self.guard(run)

    def _primitive_choose(self, bound: _Bound, element: str, option: str, **_: Any) -> dict:
        def run() -> dict:
            session, page = bound.session, bound.page
            try:
                chosen = self._locator(page, element).select_option(option)
            except Exception as error:
                raise ToolFailure(
                    {
                        "ok": False,
                        "error": f"Could not choose {option!r} in {element}: {self._why_it_failed(page, element, error)}",
                    }
                )
            result = self._acted(session, page, f"Chose {option!r} in {element}")
            result["chosen"] = chosen
            return result

        return self.guard(run)

    def _primitive_upload(self, bound: _Bound, element: str, paths: Any, **_: Any) -> dict:
        def run() -> dict:
            resolved = [
                str(Path(path).expanduser())
                for path in ([paths] if isinstance(paths, str) else paths)
            ]
            missing = [path for path in resolved if not os.path.isfile(path)]
            if missing:
                return {"ok": False, "error": f"No such file: {', '.join(missing)}"}
            session, page = bound.session, bound.page
            locator = self._locator(page, element)
            try:
                locator.set_input_files(resolved)
            except Exception:
                try:
                    with page.expect_file_chooser() as chooser:
                        locator.click()
                    chooser.value.set_files(resolved)
                except Exception as error:
                    raise ToolFailure(
                        {
                            "ok": False,
                            "error": f"Could not upload to {element}: {self._why_it_failed(page, element, error)}",
                        }
                    )
            return self._acted(session, page, f"Attached {len(resolved)} file(s) to {element}")

        return self.guard(run)

    def _primitive_drag(
        self, bound: _Bound, element: str, onto: Optional[str] = None, **_: Any
    ) -> dict:
        def run() -> dict:
            if onto is None:
                return {"ok": False, "error": "drag needs onto — the element to drop onto."}
            session, page = bound.session, bound.page
            try:
                self._locator(page, element).drag_to(
                    self._locator(page, onto), timeout=_milliseconds(Tunable.drag_timeout)
                )
            except Exception as error:
                raise ToolFailure(
                    {
                        "ok": False,
                        "error": f"Could not drag {element} to {onto}: {self._why_it_failed(page, element, error)}",
                    }
                )
            return self._acted(session, page, f"Dragged {element} onto {onto}")

        return self.guard(run)

    def _primitive_select(
        self,
        bound: _Bound,
        element: str,
        *,
        text: Optional[str] = None,
        to_text: Optional[str] = None,
        select_all: bool = False,
        occurrence: int = 1,
        **_: Any,
    ) -> dict:
        def run() -> dict:
            page = bound.page
            locator = self._locator(page, element)
            content = self._field_text(locator)
            if select_all:
                start, length = resolve_range(content, select_all=True)
            elif to_text is not None:
                start, length = resolve_range(
                    content, anchor_from=text, anchor_to=to_text, occurrence=occurrence
                )
            else:
                start, length = resolve_range(content, text=text, occurrence=occurrence)
            if locator.evaluate(_APPLY_SELECTION_JS, [start, start + length]) is None:
                return {"ok": False, "error": message("select_unsupported")}
            return {"ok": True, "did": f"Selected {length} chars"}

        return self.guard(run)

    def _primitive_caret(
        self,
        bound: _Bound,
        element: str,
        *,
        before: Optional[str] = None,
        after: Optional[str] = None,
        at_offset: Optional[int] = None,
        edge: str = "",
        occurrence: int = 1,
        **_: Any,
    ) -> dict:
        def run() -> dict:
            page = bound.page
            locator = self._locator(page, element)
            content = self._field_text(locator)
            offset = resolve_caret(
                content,
                before=before,
                after=after,
                at_offset=at_offset,
                to_start=edge == "start",
                to_end=edge == "end",
                occurrence=occurrence,
            )
            if locator.evaluate(_APPLY_SELECTION_JS, [offset, offset]) is None:
                return {"ok": False, "error": message("select_unsupported")}
            return {"ok": True, "did": f"Caret at {offset}"}

        return self.guard(run)

    def _primitive_read(
        self, bound: _Bound, element: Optional[str] = None, *, frame: str = "", **_: Any
    ) -> dict:
        def run() -> dict:
            session, page = bound.session, bound.page
            timeout = _milliseconds(Tunable.read_text_timeout)
            if element is not None:
                # An element id already names its own frame, so `frame` adds nothing here.
                source = self._locator(page, element).inner_text(timeout=timeout)
            else:
                source = self._frame_or_page(session, page, frame).inner_text(
                    "body", timeout=timeout
                )
            # Lines, like a window's read: a script that wants the whole thing can join them, but cannot unjoin them.
            return {
                "ok": True,
                "lines": _clean_page_text(source).splitlines(),
                "url": _safe_url(page),
            }

        return self.guard(run)

    def _primitive_evaluate(
        self, bound: _Bound, expression: str, argument: Any = None, *, frame: str = "", **_: Any
    ) -> dict:
        def run() -> dict:
            session, page = bound.session, bound.page
            expression_text = expression.strip()
            if not expression_text:
                return {"ok": False, "error": "evaluate needs a JavaScript expression to run."}
            # A frame is its own origin with its own session, so running in one uses the credentials it actually holds.
            target = self._frame_or_page(session, page, frame)
            try:
                value = target.evaluate(expression_text, argument)
            except Exception as error:
                return {"ok": False, "error": f"Evaluation failed: {str(error).splitlines()[0]}"}
            # Return Playwright's deserialized result as-is and in full, so the model gets a structure it can navigate.
            return {"ok": True, "result": value, "url": _safe_url(page)}

        return self.guard(run)

    # Tabs and frames — the browser's own structure, named.

    def open_tabs(self) -> list[dict]:
        """Every tab of an already connected browser, or `[]`; never connects."""
        session = self._session
        if session is None or not session.browser.is_connected():
            return []
        return self.worker.submit(lambda: self._read_open_tabs(session))

    def _read_open_tabs(self, session: "_Session") -> list[dict]:
        """The body of `open_tabs`, on the thread that owns the connection, reading from the browser rather than the tabs."""
        try:
            active_url = _safe_url(session.page) if session.page is not None else ""
            return [
                {
                    "id": session.tab_id_for_target(entry["target_id"], entry["url"]),
                    "title": entry["title"],
                    "url": entry["url"],
                    "active": bool(active_url) and entry["url"] == active_url,
                    "app": "Chrome",
                    "window_number": entry["window"],
                }
                for entry in session.describe_targets()
            ]
        except Exception:  # noqa: BLE001 — a listing must never be the thing that fails
            logger.debug("could not list tabs of the connected browser", exc_info=True)
            return []

    def glance(self, target: str) -> Glance:
        """Title, url, focus and the elements present: what a page says cheaply, from one accessibility snapshot."""
        session = self._session
        if session is None or not session.browser.is_connected():
            return Glance()
        try:
            page = self._page_for(session, target)
        except Exception:  # noqa: BLE001 — an observation must never be the thing that fails
            return Glance()
        try:
            focused = page.evaluate(_FOCUSED_ELEMENT)
        except Exception:  # noqa: BLE001
            focused = None
        try:
            elements, _ = _parse_snapshot(_snapshot(page))
            ids = frozenset(str(element.token) for element in elements if element.token)
        except Exception:  # noqa: BLE001
            ids = frozenset()
        return Glance(
            # Kept whole, because these are compared for equality and a truncated focus makes two different elements identical.
            facts={
                "title": self._title_of(session, page),
                "url": _safe_url(page),
                "focus": (str(focused) if focused else None),
            },
            ids=ids,
        )

    def _primitive_focus(self, bound: _Bound, element: Optional[str] = None, **_: Any) -> dict:
        """Bring this tab to the front, or put the caret in one control, matching the two meanings a window gives the word."""

        def run() -> dict:
            session, page = bound.session, bound.page
            if element:
                self._locator(page, element).focus()
                return {"ok": True, "focused": element}
            page.bring_to_front()
            return {"ok": True, "focused": True, "tab": session.tab_id(page)}

        return self.guard(run)

    def _primitive_tabs(self, bound: _Bound, **_: Any) -> dict:
        def run() -> dict:
            session = self.session()
            active_url = _safe_url(self.page(session))
            # From the browser in two commands, never by asking each tab for its own title.
            return {
                "ok": True,
                "tabs": [
                    {
                        "id": session.tab_id_for_target(entry["target_id"], entry["url"]),
                        "title": entry["title"],
                        "url": entry["url"],
                        "active": bool(active_url) and entry["url"] == active_url,
                    }
                    for entry in session.describe_targets()
                ],
            }

        return self.guard(run)

    def _primitive_tab(self, bound: _Bound, tab: str, **_: Any) -> dict:
        def run() -> dict:
            session = self.session()
            # One resolver for every way a target can be named, so ids the listing minted resolve here too.
            page = self._page_for(session, tab)
            session.page = page
            # Raises the window on the user's own screen, since this tool acts as the user rather than behind them.
            try:
                page.bring_to_front()
            except Exception:
                pass
            return {
                "ok": True,
                "did": f"Switched to {tab}",
                "url": _safe_url(page),
                "title": self._title_of(session, page),
            }

        return self.guard(run)

    def _primitive_new_tab(self, bound: _Bound, url: str = "", **_: Any) -> dict:
        def run() -> dict:
            session = self.session()
            page = session.context.new_page()
            # Covers only the case where the `page` event did not get there first; adopting twice would double every handler.
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
            return {
                "ok": True,
                "did": f"Opened {identifier}",
                "id": identifier,
                "url": _safe_url(page),
                "title": self._title_of(session, page),
            }

        return self.guard(run)

    def _primitive_close_tab(self, bound: _Bound, tab: str = "", **_: Any) -> dict:
        def run() -> dict:
            session = self.session()
            page = self._page_for(session, tab) if tab else self.page(session)
            if page is None or page.is_closed():
                raise ToolFailure(
                    {"ok": False, "error": f"No open tab {tab!r}. Call tabs() for what is there."}
                )
            identifier = tab or session.tab_id(page)
            was_active = page is session.page
            page.close()
            session.tab_ids.pop(page, None)
            if was_active:
                # Left for `page()` to heal on next use, so closing a tab never has opening one as a side effect.
                session.page = None
            return {"ok": True, "did": f"Closed {identifier}"}

        return self.guard(run)

    def _primitive_frames(self, bound: _Bound, **_: Any) -> dict:
        def run() -> dict:
            session, page = bound.session, bound.page
            _, session.frame_owners = _parse_snapshot(_snapshot(page))
            listing: list[dict] = []
            for identifier in sorted(session.frame_owners, key=lambda name: int(name[1:])):
                element_ref = session.frame_owners[identifier]
                record: dict[str, Any] = {
                    "id": identifier,
                    "element": element_ref,
                    "parent": _frame_of(element_ref),
                }
                try:
                    frame = self._frame(session, page, identifier)
                except ToolFailure:
                    # One iframe that has gone must not cost the listing, so its timeout is short.
                    record["unavailable"] = True
                else:
                    record["url"] = frame.url
                    if frame.name:
                        record["name"] = frame.name
                listing.append(record)
            return {"ok": True, "frames": listing}

        return self.guard(run)

    def _primitive_navigate(
        self, bound: _Bound, url: str = "", *, history: str = "", **_: Any
    ) -> dict:
        # Opening a tab is not a way of navigating, so `new_tab` does it and says what it made.
        def run() -> dict:
            session, page = bound.session, bound.page
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
                    return {
                        "ok": False,
                        "error": "navigate needs a url, or history: back, forward, or reload.",
                    }
            except Exception:
                pass  # a busy SPA may still be usable; the next search decides what is there
            _await_quiet(page)
            return {
                "ok": True,
                "did": f"Navigated to {url}" if url else f"Navigated {history}",
                "url": _safe_url(page),
                "title": self._title_of(session, page),
            }

        return self.guard(run)

    def _acted(self, session: _Session, page, did: str) -> dict:
        """The compact result of a control action: what it did, where it landed, and any dialog or download it triggered."""
        result: dict[str, Any] = {"ok": True, "did": did, "url": _safe_url(page)}
        events = session.drain_events()
        if events:
            result["events"] = events
        return result


SURFACE = WebSurface()


def close() -> None:
    """Drop our connection to the browser, leaving the user's Chrome running; called by the session's teardown."""
    SURFACE.shutdown()
