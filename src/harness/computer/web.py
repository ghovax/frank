"""Web automation that connects to the user's *own* Chrome and drives it over the Chrome DevTools
Protocol (CDP).

Why the real browser, not a copy. The point of the browser tool is to act as the user — their
real logins, their real session. Copying a profile cannot do that anymore: Google's Device Bound
Session Credentials (DBSC, on by default) tie a login to a non-exportable key in this device's
Secure Enclave, so a copied profile's short-lived cookies can never be refreshed and the session
dies within minutes. The only way to hold a real login is to use the real profile in place.

How the connection is made. Modern Chrome (M136+) refuses ``--remote-debugging-port`` on the
default profile and no longer serves the ``/json`` discovery endpoints for the permission-gated
remote-debugging path — so daisy neither launches, reopens, quits, copies, nor deletes anything.
Instead the user turns on Chrome's own switch once — chrome://inspect/#remote-debugging → "Allow
remote debugging for this browser instance" — and approves the one-time permission prompt. That
starts a DevTools server on the user's *live* browser and writes a ``DevToolsActivePort`` file in
the profile directory holding the port and the browser-level WebSocket path
(``/devtools/browser/<uuid>``). daisy reads that file and connects to
``ws://127.0.0.1:<port>/devtools/browser/<uuid>`` — the user's real profile, real logins, real
session (DBSC intact, since nothing is copied). daisy only ever *connects*; the browser stays
entirely the user's.

Connection model. The DevTools connection is made to the *browser-level* endpoint (the
``/devtools/browser/<uuid>`` path from ``DevToolsActivePort``), which lives as long as Chrome does,
and page targets are attached by ``sessionId`` in flatten mode. This is the crucial robustness property: a page target's *own*
WebSocket endpoint is torn down by Chrome on any cross-document navigation, process swap, or
reload — exactly what a single-page app like Gmail does constantly — and a client bound to it
dies with a keepalive timeout and never recovers. Bound to the browser endpoint instead, the
connection rides straight through navigations, and if a page session is ever lost it is
transparently re-attached. A dedicated background reader thread drains protocol events (so a
chatty page never backs up the socket) and matches command replies to their callers. The agent
works in the user's current tab, activated so the user sees what it is doing.

Transport is deliberately dependency-light: the endpoint comes from a file the browser already
writes (``DevToolsActivePort``), and ``websockets`` (already bundled) speaks the protocol — no
Playwright/Puppeteer, which would drag a whole second Chromium (or a Node driver) into the app for
no gain, since we connect straight to the user's own Chrome.
"""
from __future__ import annotations

import atexit
import json
import threading
import time
from contextlib import suppress
from functools import lru_cache
from itertools import count
from pathlib import Path
from typing import Any, Callable, Optional

# In-page action bodies live as real .js files rather than string-concatenated Python, loaded
# once at runtime. They are bundled with the package (see the freeze spec's data collection).
_SCRIPTS_DIRECTORY = Path(__file__).parent / "scripts"


@lru_cache(maxsize=None)
def _script(name: str) -> str:
    return (_SCRIPTS_DIRECTORY / name).read_text()


from websockets.sync.client import connect as websocket_connect

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

# Shown when the user's Chrome is not reachable — i.e. they have not turned on Chrome's own
# remote-debugging switch. daisy never enables it for them (it grants full browser control, so it
# is the user's explicit, one-time choice).
_ENABLE_REMOTE_DEBUGGING_HINT = (
    "daisy can't reach your Chrome yet. Turn on its remote-debugging switch once: open "
    f"{REMOTE_DEBUGGING_URL} in Chrome, enable \"Allow remote debugging for this browser instance\", "
    "approve the permission prompt, then try again. daisy only connects to your browser — it never "
    "opens, closes, copies, or deletes it."
)


def _not_connected_payload() -> dict:
    """The structured result the tool returns when Chrome's remote-debugging switch is off, so the
    UI can render an alert with the address and a one-click button to open it."""
    return {
        "ok": False,
        "error": _ENABLE_REMOTE_DEBUGGING_HINT,
        "code": "browser_remote_debugging_off",
        "enable_url": REMOTE_DEBUGGING_URL,
    }

# Wrapper roles that carry no meaning of their own: walked through to reach real content
# without counting toward depth, so a page's deep <div> nesting (Gmail wraps everything a
# dozen generics deep) does not exhaust the depth budget before any content is reached.
_WEB_PASS_THROUGH_ROLES = frozenset({"generic", "none", "presentation", "GenericContainer", ""})

# Sub-text fragments and separators that are pure noise in an overview.
_WEB_SKIP_ROLES = frozenset({"InlineTextBox", "LineBreak"})

# Roles emitted as a single addressable element — their accessible name already says what they
# are — and not descended into. Covers controls, text, and repeated list/row items (a Gmail row
# carries the whole email summary as its name, which is exactly what a listing needs).
_WEB_LEAF_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch",
    "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "option", "heading", "StaticText",
    "img", "image", "slider", "spinbutton", "progressbar", "listitem", "row", "gridcell",
    "cell", "columnheader", "rowheader", "treeitem", "status", "disclosuretriangle",
})

# A container with more visible children than this is stood in for by a region rather than
# inlined, so a 400-row inbox becomes one addressable node instead of hundreds.
_REGION_CHILD_THRESHOLD = 20

# Roles that are interactive by definition. Any of these — or an element the page marks
# focusable — is flagged `clickable`, so the model targets real controls instead of a label
# (Gmail's category "tabs" are role=heading with cursor:default and are NOT focusable, so they
# come through without the flag). This reads from the accessibility data already fetched, so it
# costs nothing extra and works on any site regardless of framework.
_WEB_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch",
    "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "option", "slider", "spinbutton",
    "treeitem", "listbox", "menu", "menubar", "togglebutton", "scrollbar", "colorwell",
})

# Roles whose whole purpose is to flip state. The in-page click drives them reliably, so if the
# page fingerprint does not move we do NOT escalate to a second (pointer) click — that would just
# toggle them back.
_TOGGLE_ROLES = frozenset({"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio", "togglebutton"})

# The overview expands this many levels before turning deeper containers into region
# stand-ins, mirroring the native accessibility walk so both surfaces read the same way.
DEFAULT_OVERVIEW_DEPTH = 5


class _ConnectionLost(Exception):
    """The DevTools socket went away (Chrome quit, or the endpoint dropped). Distinct from a CDP
    command error so the connection layer knows to reopen rather than surface a protocol error."""


class _Pending:
    """One outstanding command awaiting its reply, resolved by the reader thread."""
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict = {}
        self.error: Optional[BaseException] = None


class CDPConnection:
    """A DevTools connection to the *browser* endpoint with a background reader thread.

    Page targets are attached by ``sessionId`` in flatten mode, so the connection survives page
    navigations (a page target's own socket does not — Chrome tears it down on every cross-document
    load). Commands are request/response JSON-RPC multiplexed by ``id``; the reader thread drains
    every frame, matches replies to their callers, drops the flood of protocol events, and watches
    for the attached page detaching or crashing so the next call transparently re-attaches. If the
    socket itself drops while Chrome is still alive, it is reopened once and retried."""

    def __init__(self, browser_websocket_url: str):
        self._browser_websocket_url = browser_websocket_url
        self._socket = None
        self._identifiers = count(1)
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._operation_lock = threading.RLock()
        self._page_session: Optional[str] = None
        self._page_target: Optional[str] = None
        self._alive = False
        # index -> {backend DOM node id, AX node id, role} of the most recent observe, so the model
        # acts on and drills into elements by index. Carried on the connection since the connection
        # now outlives individual tool calls (it is the user's own, persistent browser).
        self.registry: dict[int, dict] = {}
        # A monotonic count of real navigations on the active page — bumped by the reader thread
        # from CDP's own Page navigation events (a document load, or an in-document route change).
        # Callers wait on the condition rather than polling the page, so a click's effect is caught
        # natively and immediately with no round-trips.
        self._navigation_sequence = 0
        self._navigation_condition = threading.Condition()
        self._open_socket()

    def _open_socket(self) -> None:
        # ping_interval disabled: we do not rely on WebSocket keepalive (it is what killed the old
        # page-scoped connection with spurious 1011 timeouts). The reader thread's blocking recv
        # surfaces a genuinely dropped socket, and Chrome liveness is checked at the process level.
        self._socket = websocket_connect(
            self._browser_websocket_url, max_size=64 * 1024 * 1024, open_timeout=10, ping_interval=None,
        )
        self._alive = True
        self._page_session = None
        self._page_target = None
        reader = threading.Thread(target=self._read_loop, args=(self._socket,), daemon=True)
        reader.start()
        # Learn about targets appearing and going away, so a closed/crashed page is noticed.
        with suppress(Exception):
            self._raw_call("Target.setDiscoverTargets", {"discover": True}, None, 10.0)

    def _read_loop(self, sock) -> None:
        while True:
            try:
                raw = sock.recv()
            except Exception:
                # Socket dropped: fail every waiter so their calls can decide to reopen. Only the
                # current socket's reader touches state; a stale reader (after reopen) just exits.
                if sock is self._socket:
                    self._alive = False
                    self._fail_all(_ConnectionLost("the DevTools socket closed"))
                return
            with suppress(ValueError, TypeError):
                message = json.loads(raw)
                identifier = message.get("id")
                if identifier is not None:
                    self._resolve(identifier, message)
                else:
                    self._on_event(message)

    def _resolve(self, identifier: int, message: dict) -> None:
        with self._pending_lock:
            pending = self._pending.pop(identifier, None)
        if pending is None:
            return
        error = message.get("error")
        if error is not None:
            pending.error = RuntimeError(error.get("message", "CDP error"))
        else:
            pending.result = message.get("result", {})
        pending.event.set()

    def _fail_all(self, error: BaseException) -> None:
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for pending in waiters:
            pending.error = error
            pending.event.set()

    def _on_event(self, message: dict) -> None:
        method = message.get("method", "")
        params = message.get("params", {})
        session = message.get("sessionId")
        if method == "Target.detachedFromTarget" and params.get("sessionId") == self._page_session:
            self._page_session = None
        elif method in ("Target.targetDestroyed", "Target.targetCrashed"):
            target = params.get("targetId")
            if target and target == self._page_target:
                self._page_session = None
                self._page_target = None
        elif session is not None and session == self._page_session:
            # A navigation on the page we are driving. frameNavigated covers full document loads
            # (main frame only — ignore subframes); navigatedWithinDocument covers SPA route
            # changes via history.pushState/replaceState.
            navigated = method == "Page.navigatedWithinDocument" or (
                method == "Page.frameNavigated" and not params.get("frame", {}).get("parentId")
            )
            if navigated:
                with self._navigation_condition:
                    self._navigation_sequence += 1
                    self._navigation_condition.notify_all()

    def navigation_sequence(self) -> int:
        """The current navigation count, to capture before an action and compare after."""
        with self._navigation_condition:
            return self._navigation_sequence

    def await_navigation(self, since: int, timeout: float) -> bool:
        """Block until a navigation past ``since`` is seen, or the timeout lapses. Event-driven —
        the reader thread wakes this the instant CDP reports the navigation."""
        deadline = time.monotonic() + timeout
        with self._navigation_condition:
            while self._navigation_sequence == since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._navigation_condition.wait(remaining)
            return True

    def _raw_call(self, method: str, params: Optional[dict], session: Optional[str], timeout: float) -> dict:
        """Send one command and wait for its reply. Raises _ConnectionLost if the socket drops."""
        if self._socket is None or not self._alive:
            raise _ConnectionLost("no live DevTools socket")
        identifier = next(self._identifiers)
        pending = _Pending()
        with self._pending_lock:
            self._pending[identifier] = pending
        frame: dict[str, Any] = {"id": identifier, "method": method, "params": params or {}}
        if session:
            frame["sessionId"] = session
        try:
            self._socket.send(json.dumps(frame))
        except Exception as exception:
            with self._pending_lock:
                self._pending.pop(identifier, None)
            raise _ConnectionLost(str(exception))
        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(identifier, None)
            raise TimeoutError(f"CDP {method} timed out")
        if pending.error is not None:
            raise pending.error
        return pending.result

    def _reopen(self) -> None:
        """Reopen the browser socket (Chrome still alive) and restart the reader."""
        with suppress(Exception):
            if self._socket is not None:
                self._socket.close()
        self._fail_all(_ConnectionLost("reopening"))
        self._open_socket()

    @property
    def alive(self) -> bool:
        return self._alive and self._socket is not None

    def _attach_page(self, prefer_target: Optional[str] = None) -> str:
        """Attach to a drivable page target by sessionId, enabling the domains we read/act through,
        and bring it to the front so the user sees the agent working. Prefers a given targetId (a
        tab we just opened or switched to), else the user's current real web page."""
        targets = self._raw_call("Target.getTargets", None, None, 10.0).get("targetInfos", [])
        pages = [target for target in targets if _drivable_target(target)]
        if not pages:
            raise _ConnectionLost("no page target to attach to")
        chosen = None
        if prefer_target is not None:
            chosen = next((target for target in pages if target.get("targetId") == prefer_target), None)
        if chosen is None:
            # "The current tab": prefer a real http(s) page over a chrome:// / new-tab surface.
            chosen = next((target for target in pages if str(target.get("url", "")).startswith("http")), pages[-1])
        result = self._raw_call(
            "Target.attachToTarget", {"targetId": chosen["targetId"], "flatten": True}, None, 10.0,
        )
        session = result.get("sessionId", "")
        self._page_session = session
        self._page_target = chosen["targetId"]
        for domain in ("Page", "DOM", "Runtime", "Accessibility"):
            with suppress(Exception):
                self._raw_call(f"{domain}.enable", None, session, 10.0)
        # Keep the page reporting itself focused/visible even when its window is behind, so
        # single-page apps that pause rendering while "hidden" build their full tree regardless.
        with suppress(Exception):
            self._raw_call("Emulation.setFocusEmulationEnabled", {"enabled": True}, session, 10.0)
        # Raise the tab we are driving to the front, so the user watches the agent act in it.
        with suppress(Exception):
            self._raw_call("Target.activateTarget", {"targetId": chosen["targetId"]}, None, 10.0)
        return session

    def ensure_page(self) -> None:
        """Make sure a page is attached to act on — the user's current tab, or a fresh one if the
        window has no drivable tab open."""
        with self._operation_lock:
            if self._page_session is not None:
                return
            try:
                self._attach_page(self._page_target)
            except _ConnectionLost:
                created = self._raw_call("Target.createTarget", {"url": "about:blank"}, None, 10.0)
                self._attach_page(created.get("targetId"))

    def page_call(self, method: str, params: Optional[dict] = None, *, timeout: float = 20.0) -> dict:
        """A page-scoped command, self-healing: if the socket dropped it reopens; if the page
        session was lost (navigation/process swap) it re-attaches — once — then retries."""
        with self._operation_lock:
            for attempt in (0, 1):
                try:
                    if not self._alive:
                        self._reopen()
                    if self._page_session is None:
                        self._attach_page(self._page_target)
                    return self._raw_call(method, params, self._page_session, timeout)
                except _ConnectionLost:
                    self._page_session = None
                    if attempt == 1:
                        raise
                    if not self._alive:
                        self._reopen()

    def browser_call(self, method: str, params: Optional[dict] = None, *, timeout: float = 20.0) -> dict:
        """A browser-scoped command (Target.*), self-healing on a dropped socket."""
        with self._operation_lock:
            for attempt in (0, 1):
                try:
                    if not self._alive:
                        self._reopen()
                    return self._raw_call(method, params, None, timeout)
                except _ConnectionLost:
                    if attempt == 1:
                        raise
                    self._reopen()

    def use_target(self, target_id: str) -> None:
        """Make a specific page target the active one for subsequent page calls."""
        with self._operation_lock:
            self._page_session = None
            self._attach_page(target_id)

    def active_target(self) -> Optional[str]:
        return self._page_target

    def close(self) -> None:
        self._alive = False
        with suppress(Exception):
            if self._socket is not None:
                self._socket.close()


def _drivable_target(target: dict) -> bool:
    """A real, driveable web page — not a DevTools inspector page or an internal target."""
    if target.get("type") != "page":
        return False
    url = str(target.get("url", ""))
    return not url.startswith("devtools://")


_connection_lock = threading.Lock()
_connection: Optional[CDPConnection] = None


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


def _ensure_connected(browser: str = "chrome") -> tuple[Optional[CDPConnection], dict]:
    """The live connection to the user's own Chrome — reused when already up (across chat sessions
    and daisy restarts), otherwise established by reading ``DevToolsActivePort`` and connecting to
    the user's remote-debugging-enabled browser. Never launches, reopens, or quits anything. The
    second element is an empty dict on success, or the not-connected error payload."""
    global _connection
    with _connection_lock:
        if _connection is not None and _connection.alive:
            return _connection, {}
        if _connection is not None:
            _connection.close()
            _connection = None
        websocket_url = _devtools_websocket_url(browser)
        if websocket_url is None:
            return None, _not_connected_payload()
        try:
            connection = CDPConnection(websocket_url)
            connection.ensure_page()
        except Exception:
            # The file can be stale (switch turned back off, or Chrome relaunched); either way the
            # user needs the switch on for a live endpoint.
            return None, _not_connected_payload()
        _connection = connection
        return connection, {}


def close() -> None:
    """Drop our DevTools connection (e.g. on server stop). The user's Chrome is left running — it is
    their own browser now; daisy just reconnects to it next time."""
    global _connection
    with _connection_lock:
        if _connection is not None:
            _connection.close()
            _connection = None


# Close only our socket at exit; never the user's browser.
atexit.register(close)


# Length bounds on the free text one page node contributes, mirroring the native side.
_LABEL_LENGTH = 256
_VALUE_LENGTH = 512


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _ax_text(field: Any) -> str:
    """Unwrap a CDP accessibility field ({"value": ...}) to its plain string. Booleans arrive
    as JSON true/false → Python True/False; normalize them to lowercase so property comparisons
    (focusable, disabled, checked, …) match."""
    if isinstance(field, dict):
        value = field.get("value")
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
    return ""


def _node_property(node: dict, name: str) -> str:
    """A single AX property value by name (e.g. focusable), or "" when absent."""
    for entry in node.get("properties", []):
        if entry.get("name") == name:
            return _ax_text(entry.get("value"))
    return ""


def _is_clickable(role: str, node: dict) -> bool:
    """Whether the page treats this element as something to act on — an interactive role, or an
    element it marks focusable. A cheap, framework-agnostic signal drawn from the AX data."""
    if role in _WEB_INTERACTIVE_ROLES:
        return True
    return _node_property(node, "focusable") == "true"


def _ax_properties(node: dict) -> dict:
    """The handful of AX properties worth surfacing as affordance signals: disabled, checked,
    expanded, and whether the node is editable."""
    wanted = {"disabled": True, "checked": None, "expanded": None, "editable": None, "required": True}
    surfaced: dict[str, Any] = {}
    for entry in node.get("properties", []):
        name = entry.get("name")
        if name not in wanted:
            continue
        value = _ax_text(entry.get("value"))
        if value in ("", "false", "none"):
            continue
        surfaced[name] = True if value == "true" else value
    return surfaced


def _web_element(index: int, role: str, name: str, value: str, node: dict, *, children: Optional[int] = None) -> dict:
    """One page element as the model sees it: its semantic role (the web affordance), name,
    value, and any notable AX properties; a region carries its child count."""
    payload: dict[str, Any] = {"index": index, "role": role}
    truncated = False
    if name:
        payload["name"], clipped = _bounded(name, _LABEL_LENGTH)
        truncated = truncated or clipped
    if value:
        payload["value"], clipped = _bounded(value, _VALUE_LENGTH)
        truncated = truncated or clipped
    payload.update(_ax_properties(node))
    if _is_clickable(role, node):
        payload["clickable"] = True
    if children is not None:
        payload["children"] = children
    if truncated:
        payload["truncated"] = True
    return payload


def _flatten_ax(
    nodes: list[dict], maximum_depth: int, *, root_node_id: Optional[str] = None,
) -> tuple[list[dict], dict[int, dict]]:
    """Flatten the CDP accessibility tree into indexed elements, scan-then-drill: meaningless
    wrapper nodes are walked through without spending depth, real controls and text are emitted
    as leaves, and a container that is large or lies past the depth bound becomes an addressable
    region carrying its child count. ``root_node_id`` re-roots the flatten at a region to drill
    into it. Returns (elements, index -> {backend DOM node id, AX node id})."""
    by_id = {node["nodeId"]: node for node in nodes}
    if root_node_id is not None and root_node_id in by_id:
        root = by_id[root_node_id]
    else:
        root = next((node for node in nodes if _ax_text(node.get("role")) == "RootWebArea"),
                    nodes[0] if nodes else None)
    elements: list[dict] = []
    registry: dict[int, dict] = {}
    if root is None:
        return elements, registry

    def register(role: str, name: str, value: str, node: dict, children: Optional[int] = None) -> None:
        index = len(elements)
        elements.append(_web_element(index, role, name, value, node, children=children))
        registry[index] = {"backend": node.get("backendDOMNodeId"), "node": node.get("nodeId"), "role": role}

    # Seed with the root's children (the root itself — a RootWebArea or the drilled region — is
    # structure we descend past). Each frame is (AX node id, semantic depth).
    stack: list[tuple[Any, int]] = [(child_id, 0) for child_id in reversed(root.get("childIds", []))]
    while stack:
        node_id, depth = stack.pop()
        node = by_id.get(node_id)
        if node is None:
            continue
        role = _ax_text(node.get("role"))
        if role in _WEB_SKIP_ROLES:
            continue
        child_ids = node.get("childIds", [])
        if bool(node.get("ignored", False)) or role in _WEB_PASS_THROUGH_ROLES:
            # A wrapper: descend without emitting or spending depth.
            stack.extend((child_id, depth) for child_id in reversed(child_ids))
            continue
        name = _ax_text(node.get("name"))
        value = _ax_text(node.get("value"))
        if role in _WEB_LEAF_ROLES:
            if name or value:
                register(role, name, value, node)
            continue
        # A semantic container: stand it in for its subtree when it is large or deep, otherwise
        # descend one meaningful level.
        if child_ids and (len(child_ids) > _REGION_CHILD_THRESHOLD or depth >= maximum_depth):
            register(role, name, value, node, children=len(child_ids))
            continue
        stack.extend((child_id, depth + 1) for child_id in reversed(child_ids))
    return elements, registry


def _require_connection(browser: str = "chrome") -> tuple[Optional[CDPConnection], dict]:
    """The live connection to the user's Chrome, establishing it if needed. The second element is
    an empty dict on success, or a ready-to-return error payload."""
    return _ensure_connected(browser)


def _guarded(operation: Callable[[], dict]) -> dict:
    """Run a page operation, turning a hard connection loss (the user quit Chrome, or turned its
    remote-debugging switch off) into a clean, honest result and dropping the dead connection so the
    next call reconnects. Self-healing (socket reopen, page re-attach) happens *inside* the
    connection; only a genuine, unrecoverable loss reaches here."""
    try:
        return operation()
    except (_ConnectionLost, ConnectionError, OSError, TimeoutError) as exception:
        close()
        return {
            "ok": False,
            "error": f"The connection to your Chrome dropped ({exception}) — it may have been closed. Try again and daisy will reconnect.",
        }


def _resolve_object(connection: CDPConnection, backend_dom_node_id: Optional[int]) -> Optional[str]:
    """Resolve a backend DOM node to a live JS object handle to act on, or None if it is gone."""
    if backend_dom_node_id is None:
        return None
    with suppress(Exception):
        resolved = connection.page_call("DOM.resolveNode", {"backendNodeId": backend_dom_node_id})
        return resolved.get("object", {}).get("objectId")
    return None


def _await_load(connection: CDPConnection, timeout: float = 15.0) -> None:
    """Wait for the page to finish loading by polling document.readyState."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with suppress(Exception):
            state = connection.page_call(
                "Runtime.evaluate", {"expression": _script("page_ready_state.js"), "returnByValue": True},
            )
            if state.get("result", {}).get("value") == "complete":
                return
        time.sleep(0.15)


def _page_info(connection: CDPConnection) -> dict:
    """The page's current URL and title, read natively from the DevTools navigation history — no
    injected JS, so it works even under a strict Content-Security-Policy and cannot be fooled by a
    page that overrides location/title. Reflects live SPA route and title changes."""
    with suppress(Exception):
        history = connection.page_call("Page.getNavigationHistory")
        entries = history.get("entries", [])
        index = history.get("currentIndex", -1)
        if 0 <= index < len(entries):
            entry = entries[index]
            return {"url": entry.get("url", ""), "title": entry.get("title", "")}
    return {}


def _element_state(connection: CDPConnection, object_id: str) -> Optional[str]:
    """A localized signature of the element's own interactive state (expanded / pressed / selected
    / checked). Immune to page-wide churn, so an in-place toggle, disclosure, or tab selection is
    detected even when the URL and title do not move. None means the handle went stale."""
    with suppress(Exception):
        result = connection.page_call("Runtime.callFunctionOn", {
            "objectId": object_id,
            "returnByValue": True,
            "functionDeclaration": _script("element_state.js"),
        })
        return str(result.get("result", {}).get("value", ""))
    return None


def _element_center(connection: CDPConnection, backend_dom_node_id: Optional[int]) -> Optional[tuple[float, float]]:
    """The on-screen centre point of an element, from its box model (falling back to content quads).
    None when no box can be resolved (off-screen, display:none, or gone)."""
    if backend_dom_node_id is None:
        return None
    box = None
    with suppress(Exception):
        box = connection.page_call("DOM.getBoxModel", {"backendNodeId": backend_dom_node_id}).get("model", {}).get("content")
    if not box:
        with suppress(Exception):
            quads = connection.page_call("DOM.getContentQuads", {"backendNodeId": backend_dom_node_id}).get("quads", [])
            box = quads[0] if quads else None
    if not box or len(box) < 8:
        return None
    return (box[0] + box[2] + box[4] + box[6]) / 4, (box[1] + box[3] + box[5] + box[7]) / 4


def _dispatch_real_click(connection: CDPConnection, backend_dom_node_id: Optional[int]) -> bool:
    """A real, trusted mouse click at the element's on-screen centre — the full moved/pressed/
    released sequence a genuine user produces — for controls that ignore a synthetic .click().
    Returns False when no box can be resolved."""
    center = _element_center(connection, backend_dom_node_id)
    if center is None:
        return False
    center_x, center_y = center
    for event in (
        {"type": "mouseMoved", "x": center_x, "y": center_y},
        {"type": "mousePressed", "x": center_x, "y": center_y, "button": "left", "buttons": 1, "clickCount": 1},
        {"type": "mouseReleased", "x": center_x, "y": center_y, "button": "left", "buttons": 1, "clickCount": 1},
    ):
        with suppress(Exception):
            connection.page_call("Input.dispatchMouseEvent", event)
    return True


# Named keys a normal user presses, mapped to the fields Input.dispatchKeyEvent needs. Enter carries
# text so it submits forms/searches; the rest are pure key events (navigation, editing, dismissal).
_KEY_DEFINITIONS = {
    "enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
    "escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    "backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
    "delete": {"key": "Delete", "code": "Delete", "windowsVirtualKeyCode": 46},
    "arrowdown": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40},
    "arrowup": {"key": "ArrowUp", "code": "ArrowUp", "windowsVirtualKeyCode": 38},
    "arrowleft": {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37},
    "arrowright": {"key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39},
    "pagedown": {"key": "PageDown", "code": "PageDown", "windowsVirtualKeyCode": 34},
    "pageup": {"key": "PageUp", "code": "PageUp", "windowsVirtualKeyCode": 33},
    "home": {"key": "Home", "code": "Home", "windowsVirtualKeyCode": 36},
    "end": {"key": "End", "code": "End", "windowsVirtualKeyCode": 35},
}

# Fixed scroll expressions (no user input — safe to evaluate). down/up move by most of a viewport;
# top/bottom jump to the page ends. Scrolling a specific element uses DOM.scrollIntoViewIfNeeded.
_SCROLL_EXPRESSIONS = {
    "down": "window.scrollBy(0, Math.round(window.innerHeight * 0.9))",
    "up": "window.scrollBy(0, -Math.round(window.innerHeight * 0.9))",
    "top": "window.scrollTo(0, 0)",
    "bottom": "window.scrollTo(0, document.body.scrollHeight)",
}


def observe(depth: int = DEFAULT_OVERVIEW_DEPTH, element: Optional[int] = None) -> dict:
    """The current page's semantic tree, shallow-first, as indexed elements. With ``element``
    set to a region's index from the last observe, drill in and expand just that region."""
    connection, error = _require_connection()
    if error:
        return error

    def run() -> dict:
        root_node_id = None
        if element is not None:
            entry = connection.registry.get(element)
            if entry is None:
                return {"ok": False, "error": f"No element at index {element}. Observe the page first."}
            root_node_id = entry.get("node")
        tree = connection.page_call("Accessibility.getFullAXTree")
        elements, registry = _flatten_ax(tree.get("nodes", []), depth, root_node_id=root_node_id)
        connection.registry = registry
        location = _page_info(connection)
        return {
            "ok": True,
            "url": location.get("url", ""),
            "title": location.get("title", ""),
            "count": len(elements),
            "elements": elements,
        }

    return _guarded(run)


def navigate(url: str, browser: str = "chrome") -> dict:
    """Open a URL in the user's Chrome (connecting to it if needed) and return the page overview."""
    connection, error = _require_connection(browser)
    if error:
        return error

    def run() -> dict:
        connection.page_call("Page.navigate", {"url": url})
        _await_load(connection)
        return observe()

    return _guarded(run)


def click(index: int) -> dict:
    """Click an element, then confirm it actually did something. First the cheap in-page click;
    if the page does not change, escalate to a real trusted mouse click at the element's
    coordinates; if it still does not change, say so, so the model can adapt (a different target
    or a direct URL) instead of trusting a no-op."""
    connection, error = _require_connection()
    if error:
        return error

    def run() -> dict:
        entry = connection.registry.get(index)
        backend = entry.get("backend") if entry else None
        object_id = _resolve_object(connection, backend) if entry else None
        if object_id is None:
            return {"ok": False, "error": f"No element at index {index}, or it has left the page. Observe again."}

        before = connection.navigation_sequence()
        state_before = _element_state(connection, object_id)
        connection.page_call("Runtime.callFunctionOn", {"objectId": object_id, "functionDeclaration": _script("click_element.js")})
        if connection.await_navigation(before, 1.4):
            return {"ok": True, "did": f"Clicked element {index}", "changed": True}

        # No navigation. Did the element itself change in place (a disclosure expanded, a tab selected,
        # a box checked)? That is caught locally, immune to the page's ambient churn.
        state_after = _element_state(connection, object_id)
        if state_before is not None and state_after is not None and state_after != state_before:
            return {"ok": True, "did": f"Clicked element {index}", "changed": True}

        # Still nothing. A toggle (checkbox/switch/radio) is driven reliably by the in-page click, so a
        # second real-pointer click would only toggle it back — do not escalate those. Otherwise
        # escalate to a real, trusted pointer click for a control that ignores a synthetic .click().
        role = str(entry.get("role", ""))
        if role not in _TOGGLE_ROLES and _dispatch_real_click(connection, backend):
            if connection.await_navigation(before, 1.4):
                return {"ok": True, "did": f"Clicked element {index}", "changed": True, "via": "pointer"}

        return {
            "ok": True,
            "did": f"Clicked element {index}",
            "changed": False,
            "note": "The click produced no visible change — this element is likely not interactive (check its 'clickable' flag), or the change was in-place: observe again to confirm, and otherwise target a clickable element or navigate directly by URL.",
        }

    return _guarded(run)


def type_text(index: int, text: str) -> dict:
    connection, error = _require_connection()
    if error:
        return error

    def run() -> dict:
        entry = connection.registry.get(index)
        object_id = _resolve_object(connection, entry.get("backend")) if entry else None
        if object_id is None:
            return {"ok": False, "error": f"No element at index {index}, or it has left the page. Observe again."}
        # Focus the field and select what it holds, then paste the whole string in one shot via
        # Input.insertText — the browser's own text insertion, which fires the real input events a
        # page listens for (and works the same for inputs, textareas, and contenteditable).
        connection.page_call("Runtime.callFunctionOn", {"objectId": object_id, "functionDeclaration": _script("focus_field.js")})
        connection.page_call("Input.insertText", {"text": text})
        return {"ok": True, "did": f"Typed into element {index}"}

    return _guarded(run)


def read() -> dict:
    """The current page's visible text, bounded, for a quick read without the element tree."""
    connection, error = _require_connection()
    if error:
        return error

    def run() -> dict:
        evaluated = connection.page_call("Runtime.evaluate", {
            "expression": _script("page_text.js"), "returnByValue": True,
        })
        text, truncated = _bounded(str(evaluated.get("result", {}).get("value", "")), 20000)
        location = _page_info(connection)
        return {"ok": True, "url": location.get("url", ""), "title": location.get("title", ""), "text": text, "truncated": truncated}

    return _guarded(run)


def history_back() -> dict:
    connection, error = _require_connection()
    if error:
        return error

    def run() -> dict:
        connection.page_call("Runtime.evaluate", {"expression": _script("history_back.js")})
        _await_load(connection)
        return observe()

    return _guarded(run)


def history_forward() -> dict:
    connection, error = _require_connection()
    if error:
        return error

    def run() -> dict:
        connection.page_call("Runtime.evaluate", {"expression": _script("history_forward.js")})
        _await_load(connection)
        return observe()

    return _guarded(run)


def reload() -> dict:
    """Reload the current page and return its fresh overview."""
    connection, error = _require_connection()
    if error:
        return error

    def run() -> dict:
        connection.page_call("Page.reload")
        _await_load(connection)
        return observe()

    return _guarded(run)


def scroll(direction: str = "down", element: Optional[int] = None) -> dict:
    """Scroll the page — ``down``/``up`` by most of a viewport, ``top``/``bottom`` to the ends — or,
    with an ``element`` index from the last observe, bring that element into view. Returns the
    overview so the model sees what is now on screen."""
    connection, error = _require_connection()
    if error:
        return error

    def run() -> dict:
        if element is not None:
            entry = connection.registry.get(element)
            if entry is None:
                return {"ok": False, "error": f"No element at index {element}. Observe the page first."}
            connection.page_call("DOM.scrollIntoViewIfNeeded", {"backendNodeId": entry.get("backend")})
        else:
            expression = _SCROLL_EXPRESSIONS.get(direction.lower())
            if expression is None:
                return {"ok": False, "error": f"Unknown scroll direction {direction!r}. Use down, up, top, or bottom."}
            connection.page_call("Runtime.evaluate", {"expression": expression})
        time.sleep(0.2)
        return observe()

    return _guarded(run)


def press(key: str) -> dict:
    """Press a key on the focused element — Enter to submit a search or form, Escape to dismiss, Tab
    to move on, the arrows/Page keys to move within a list. Returns the resulting overview."""
    connection, error = _require_connection()
    if error:
        return error
    definition = _KEY_DEFINITIONS.get(key.strip().lower())
    if definition is None:
        return {"ok": False, "error": f"Unknown key {key!r}. Known: {', '.join(sorted(_KEY_DEFINITIONS))}."}

    def run() -> dict:
        before = connection.navigation_sequence()
        connection.page_call("Input.dispatchKeyEvent", {"type": "keyDown", **definition})
        connection.page_call("Input.dispatchKeyEvent", {"type": "keyUp", **definition})
        connection.await_navigation(before, 1.4)
        return observe()

    return _guarded(run)


def hover(index: int) -> dict:
    """Move the pointer over an element (revealing hover menus and tooltips) without clicking it."""
    connection, error = _require_connection()
    if error:
        return error

    def run() -> dict:
        entry = connection.registry.get(index)
        center = _element_center(connection, entry.get("backend")) if entry else None
        if center is None:
            return {"ok": False, "error": f"No element at index {index}, or it has no on-screen box. Observe again."}
        connection.page_call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": center[0], "y": center[1]})
        time.sleep(0.25)
        return observe()

    return _guarded(run)


def _tab_summaries(connection: CDPConnection) -> list[dict]:
    """The open tabs of the user's browser, each with the target id used to switch, and a marker
    for the one the agent is currently driving."""
    targets = connection.browser_call("Target.getTargets").get("targetInfos", [])
    active = connection.active_target()
    return [
        {
            "tab": target.get("targetId", ""),
            "title": target.get("title", ""),
            "url": target.get("url", ""),
            "active": target.get("targetId") == active,
        }
        for target in targets if _drivable_target(target)
    ]


def list_tabs() -> dict:
    """The open tabs of the user's browser, so the model can switch between them by id."""
    connection, error = _require_connection()
    if error:
        return error
    return _guarded(lambda: {"ok": True, "tabs": _tab_summaries(connection)})


def new_tab(url: str = "", browser: str = "chrome") -> dict:
    """Open a new tab (optionally at a URL), make it the active one, and return its overview."""
    connection, error = _require_connection(browser)
    if error:
        return error

    def run() -> dict:
        # Open the tab blank and attach, then drive the load through Page.navigate — the same path
        # navigate() uses. Creating the target with the URL directly races the observe against an
        # about:blank→load transition, so the first read can come back empty.
        created = connection.browser_call("Target.createTarget", {"url": "about:blank"})
        target_id = created.get("targetId", "")
        connection.use_target(target_id)
        if url:
            connection.page_call("Page.navigate", {"url": url})
            _await_load(connection)
        result = observe()
        result["tab"] = target_id
        return result

    return _guarded(run)


def switch_tab(tab: str) -> dict:
    """Make a given tab (by id from the tabs action) the active one and return its overview."""
    connection, error = _require_connection()
    if error:
        return error

    def run() -> dict:
        connection.use_target(tab)
        return observe()

    return _guarded(run)


def close_tab(tab: str) -> dict:
    """Close a tab by id. When it was the active one, fall back to whatever tab remains."""
    connection, error = _require_connection()
    if error:
        return error

    def run() -> dict:
        connection.browser_call("Target.closeTarget", {"targetId": tab})
        remaining = _tab_summaries(connection)
        if not remaining:
            return {"ok": True, "tabs": []}
        if connection.active_target() not in {summary["tab"] for summary in remaining}:
            connection.use_target(remaining[-1]["tab"])
        return {"ok": True, "tabs": _tab_summaries(connection)}

    return _guarded(run)
