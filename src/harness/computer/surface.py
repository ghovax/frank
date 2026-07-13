"""The shared spine both automation surfaces are built on: the web surface (Chrome over the
DevTools protocol) and the native macOS surface (the accessibility tree). Each drives a very
different substrate, but the model faces one vocabulary and one contract, and that contract
lives here.

What is shared, and why it belongs in one place:

* One **observe → act-by-index** model. Every observe returns a flat list of indexed elements
  and rebuilds a registry mapping each index to an opaque target token (a web aria-ref, a
  native AX handle); the model then acts by index. The element schema is identical across
  surfaces (``Element`` below), so a button reads the same whether it is in a web page or a
  native window.
* One **serial worker**. Each surface owns exactly one thread that touches its live state, and
  public operations submit closures onto it. For the web surface this is a hard requirement
  (Playwright's sync API is thread-affine); for the native surface it is what serializes the
  index registry instead of a lock over module globals.
* One **failure protocol**. ``ToolFailure`` carries a structured payload as control flow;
  ``Surface.guard`` wraps every operation so an unexpected exception becomes an honest payload
  and the surface gets a chance to recover (drop a dead connection, forget a stale registry).
* One **result shape**. ``digest`` (after an action) and ``overview`` (after an observe or a
  navigation) both end in ``finish``, which stamps the surface's location fields, a ``changed``
  flag, and any out-of-band events. The location fields are the one thing that differs — a URL
  and title for the web, an app and window for the native surface — so each surface supplies
  them and the rest is common.

User- and model-facing prose is never inlined here; it is loaded from ``messages/*.md`` like
every other prompt in the harness.
"""
from __future__ import annotations

import concurrent.futures
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from harness.core.configuration import PromptLoader


def message_loader(folder: str) -> Callable[..., str]:
    """A message function bound to ``messages/<folder>/`` — each surface keeps its own folder so
    the files are named for what they say (``not_connected``) rather than which tool owns them."""
    loader = PromptLoader(Path(__file__).parent / "messages" / folder)

    def message(name: str, **variables: str) -> str:
        return loader.load(name, variables).strip()

    return message


# Length bounds on the free text a single element contributes, so one verbose node (a web app
# that stuffs a whole email body into a label, a native field holding a paragraph) cannot flood
# an observation. A control's own contents get more room than its label; both are powers of two.
LABEL_LENGTH = 256   # an element's name
VALUE_LENGTH = 512   # an element's own value/contents


def bounded(text: str, limit: int) -> tuple[str, bool]:
    """Clip text to a length bound, reporting whether anything was cut."""
    if len(text) > limit:
        return text[:limit], True
    return text, False


def find_occurrence(content: str, needle: str, occurrence: int = 1) -> int:
    """The character index where the given (1-based) ``occurrence`` of ``needle`` starts in
    ``content``, or -1 when there are fewer than that many. Case-sensitive, since editing targets
    exact text."""
    start = -1
    for _ in range(max(1, occurrence)):
        start = content.find(needle, start + 1)
        if start == -1:
            return -1
    return start


def _anchor_offset(content: str, anchor: Any, *, past: bool, occurrence: int) -> int:
    """Resolve one endpoint. An int anchor is a character offset (clamped); a string anchor is the
    given ``occurrence`` of that text, at its start or, with ``past``, just after it."""
    if isinstance(anchor, int):
        return max(0, min(anchor, len(content)))
    index = find_occurrence(content, anchor, occurrence)
    if index < 0:
        raise ToolFailure({"ok": False, "error": f"The text {anchor!r} is not in the field, so there is nothing to point at."})
    return index + (len(anchor) if past else 0)


def resolve_range(
    content: str, *, text: Optional[str] = None, anchor_from: Any = None,
    anchor_to: Any = None, select_all: bool = False, occurrence: int = 1,
) -> tuple[int, int]:
    """Turn a selection request into a (start, length) range within ``content``. Addressed by a
    substring (``text``, its ``occurrence``), by a ``from``/``to`` pair (each a substring or an
    offset), or by ``all``. Raises ``ToolFailure`` when the request names text that is not present
    or specifies nothing selectable."""
    if select_all:
        return 0, len(content)
    if text is not None:
        if not text:
            raise ToolFailure({"ok": False, "error": "select needs non-empty text to look for."})
        index = find_occurrence(content, text, occurrence)
        if index < 0:
            raise ToolFailure({"ok": False, "error": f"The text {text!r} is not in the field, so there is nothing to select."})
        return index, len(text)
    if anchor_from is not None and anchor_to is not None:
        start = _anchor_offset(content, anchor_from, past=False, occurrence=occurrence)
        end = _anchor_offset(content, anchor_to, past=True, occurrence=occurrence)
        if end < start:
            start, end = end, start
        return start, end - start
    raise ToolFailure({"ok": False, "error": "select needs one of: text, a from/to pair, or all."})


def resolve_caret(
    content: str, *, before: Optional[str] = None, after: Optional[str] = None,
    at_offset: Optional[int] = None, to_start: bool = False, to_end: bool = False, occurrence: int = 1,
) -> int:
    """Turn a caret request into a single character offset within ``content``: the ``start`` or
    ``end`` of the field, an explicit ``at_offset``, or just ``before``/``after`` an occurrence of a
    substring. Raises ``ToolFailure`` when a named substring is absent."""
    if to_start:
        return 0
    if to_end:
        return len(content)
    if at_offset is not None:
        return max(0, min(int(at_offset), len(content)))
    if before is not None:
        return _anchor_offset(content, before, past=False, occurrence=occurrence)
    if after is not None:
        return _anchor_offset(content, after, past=True, occurrence=occurrence)
    raise ToolFailure({"ok": False, "error": "caret needs one of: before, after, at_offset, start, or end."})


@dataclass
class Element:
    """One indexed element, in the single vocabulary the model acts on across both surfaces.

    ``name`` is the element's accessible name (the web accessible name; the native title,
    description, or help, whichever it has). ``value`` is its own contents. ``clickable`` marks
    a control the model can target (a web interactive role or pointer cursor; a native node that
    exposes a press-like AX action). ``flags`` holds only the notable states (disabled, selected,
    checked, expanded, …); unremarkable defaults are omitted as noise. ``children`` is set only
    on a region — a container an observe chose not to expand — and counts what waits inside so
    the model can drill. ``actions`` is the native AX action list when present. ``token`` is the
    opaque handle used to act on the element (an aria-ref, an AX registry entry); it never
    reaches the model."""
    index: int
    role: str
    name: str = ""
    value: Any = None
    clickable: bool = False
    flags: dict[str, Any] = field(default_factory=dict)
    children: Optional[int] = None
    actions: list[str] = field(default_factory=list)
    truncated: bool = False
    token: Any = None

    def payload(self) -> dict[str, Any]:
        """The model-facing dict: index and role always, everything else only when populated."""
        data: dict[str, Any] = {"index": self.index, "role": self.role}
        if self.name:
            data["name"], name_clipped = bounded(self.name, LABEL_LENGTH)
        else:
            name_clipped = False
        value_clipped = False
        if isinstance(self.value, str):
            if self.value:
                data["value"], value_clipped = bounded(self.value, VALUE_LENGTH)
        elif self.value is not None:
            data["value"] = self.value
        for flag, state in self.flags.items():
            data[flag] = state
        if self.clickable:
            data["clickable"] = True
        if self.children is not None:
            data["children"] = self.children
        if self.actions:
            data["actions"] = self.actions
        if self.truncated or name_clipped or value_clipped:
            data["truncated"] = True
        return data


class ToolFailure(Exception):
    """A structured tool result raised as control flow inside a worker; carries the payload."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("error", ""))
        self.payload = payload


class SerialWorker:
    """A dedicated thread that owns a surface's live state. Public operations submit closures
    and block on the result; they never touch the owned state themselves. The thread is started
    lazily and restarted if it ever dies, so a surface can be used, dropped, and used again."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            operation, future = item
            try:
                future.set_result(operation())
            except BaseException as error:  # noqa: BLE001 (marshalled to the caller)
                future.set_exception(error)

    def submit(self, operation: Callable[[], Any], timeout: float = 120.0) -> Any:
        self._ensure_thread()
        future: "concurrent.futures.Future" = concurrent.futures.Future()
        self._queue.put((operation, future))
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self._queue.put(None)


class Surface:
    """Base for the two automation surfaces. Owns the serial worker, the failure/recovery guard,
    and the shared result shapers. Subclasses supply the substrate: how to resolve a target, how
    to snapshot it into elements, how to act, and the location fields for a result."""

    #: Roles whose text is a live announcement (a validation error, a status line) and so is
    #: always kept in a digest even though it is not clickable. Web surfaces set this; the native
    #: surface leaves it empty.
    live_region_roles: frozenset[str] = frozenset()

    def __init__(self, worker_name: str, message: Callable[..., str]) -> None:
        self.worker = SerialWorker(worker_name)
        self.message = message

    # Failure handling.

    def guard(self, operation: Callable[[], dict], *, timeout: float = 120.0) -> dict:
        """Submit one operation to the worker and shape every outcome into an honest payload. A
        ``ToolFailure`` becomes its payload; anything unexpected becomes ``recover``'s payload
        after the surface is given a chance to drop dead state."""

        def guarded() -> dict:
            try:
                return operation()
            except ToolFailure as failure:
                return failure.payload

        try:
            return self.worker.submit(guarded, timeout=timeout)
        except Exception as error:  # substrate errors, timeouts, a dead target
            first_line = str(error).splitlines()[0] if str(error) else error.__class__.__name__
            try:
                self.worker.submit(self.on_recover, timeout=5.0)
            except Exception:
                pass
            return self.recover(first_line)

    def on_recover(self) -> dict:
        """Drop any state a failed operation may have left broken (a lost connection, a stale
        registry), on the worker thread. Overridden by surfaces that hold a connection."""
        return {}

    def recover(self, detail: str) -> dict:
        """The payload for an unexpected failure. Overridden with a surface-specific message."""
        return {"ok": False, "error": detail}

    # Result shaping.

    def location_fields(self, context: Any, /) -> dict:
        """The fields identifying where a result was taken — ``{"url", "title"}`` for the web,
        ``{"app", "window"}`` for the native surface. Supplied by the subclass. The context is
        positional so each surface can name it for what it is (a page, a snapshot)."""
        raise NotImplementedError

    def finish(
        self,
        result: dict,
        *,
        context: Any,
        elements: list[dict],
        changed: Optional[bool] = None,
        events: Optional[list[dict]] = None,
        empty_hint: str = "",
    ) -> dict:
        """The fields every reading result shares: the surface's location, an optional ``changed``
        flag, an empty-surface hint, and any out-of-band events captured since the last result."""
        result.update(self.location_fields(context))
        if changed is not None:
            result["changed"] = bool(changed)
        if not elements and empty_hint:
            result.setdefault("hint", empty_hint)
        for event in events or []:
            result.update(event)
        return result

    def overview(
        self,
        *,
        context: Any,
        elements: list[dict],
        changed: Optional[bool] = None,
        events: Optional[list[dict]] = None,
        notes: Optional[list[str]] = None,
        truncated: bool = False,
        empty_hint: str = "",
    ) -> dict:
        """The full surface as indexed elements — what observe and the navigation actions return."""
        result: dict[str, Any] = {"ok": True, "count": len(elements), "elements": elements}
        if truncated:
            result["truncated"] = True
        collected = [note for note in (notes or []) if note]
        if collected:
            result["note"] = " ".join(collected)
        return self.finish(
            result, context=context, elements=elements, changed=changed,
            events=events, empty_hint=empty_hint,
        )

    def digest(
        self,
        *,
        context: Any,
        elements: list[dict],
        changed: Optional[bool] = None,
        events: Optional[list[dict]] = None,
        truncated: bool = False,
        truncated_note: str = "",
        prose_note_name: str = "",
        empty_hint: str = "",
    ) -> dict:
        """The result an acting call returns: the surface's complete actionable surface — every
        clickable element plus any live-region announcement — with bulk prose deferred to
        observe/read. Nothing the model could act on is hidden; what is deferred is reading
        material, and the note says how much. ``prose_note_name`` names the message rendered with
        the count this method computes."""
        listed = [
            element for element in elements
            if element.get("clickable") or element.get("role") in self.live_region_roles
        ]
        prose_deferred = len(elements) - len(listed)
        result: dict[str, Any] = {"ok": True, "count": len(elements), "elements": listed}
        if truncated and truncated_note:
            result["truncated"] = True
            result["note"] = truncated_note
        elif prose_deferred and prose_note_name:
            result["note"] = self.message(prose_note_name, count=str(prose_deferred))
        return self.finish(
            result, context=context, elements=listed, changed=changed,
            events=events, empty_hint=empty_hint,
        )
