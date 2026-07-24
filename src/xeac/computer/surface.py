"""The shared spine both automation surfaces are built on: the web surface (Chrome over the
DevTools protocol) and the native macOS surface (the accessibility tree). Each drives a very
different substrate, but the model faces one contract — the ``control_screen`` script reads the
surface into elements with ``find_one``/``find_many`` and acts on them with the same primitives —
and the pieces common to both live here.

What is shared:

* One **element vocabulary** (``Element``): a role, an accessible name, a value, the context of the
  nearest labelling ancestor, and the live handle used to act on it. A surface turns its substrate
  into these; retrieval ranks them; the control primitives act on them by handle.
* One **serial worker**. Each surface owns exactly one thread that touches its live state, and
  public operations submit closures onto it (Playwright's sync API is thread-affine; the native
  surface uses it to serialize its handle map).
* One **failure protocol**. ``ToolFailure`` carries a structured payload as control flow;
  ``Surface.guard`` wraps every operation so an unexpected exception becomes an honest payload and
  the surface gets a chance to recover (drop a dead connection, forget stale handles).

User- and model-facing prose is never inlined here; it is loaded from ``messages/*.md`` like every
other prompt in the harness.
"""
from __future__ import annotations

import concurrent.futures
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from xeac.base.configuration import PromptLoader


def message_loader(folder: str) -> Callable[..., str]:
    """A message function bound to ``messages/<folder>/`` — each surface keeps its own folder so the
    files are named for what they say (``not_connected``) rather than which tool owns them."""
    loader = PromptLoader(Path(__file__).parent / "messages" / folder)

    def message(name: str, **variables: str) -> str:
        return loader.load(name, variables).strip()

    return message


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
    substring (``text``, its ``occurrence``), by a ``from``/``to`` pair, or by ``all``."""
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
    ``end`` of the field, an explicit ``at_offset``, or ``before``/``after`` an occurrence of text."""
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
    """One element in the single vocabulary a surface produces and retrieval ranks. ``name`` is the
    accessible name; ``value`` its own contents; ``context`` a short trail of its named ancestors
    (the nearest heading/landmark), so twenty identical "Add to Cart" buttons are told apart by the
    product they sit under; ``clickable`` marks a control the model can act on; ``flags`` holds the
    notable states (disabled, selected, …); ``children``/``actions`` are native extras; ``token`` is
    the live handle used to act on the element (a web aria-ref, a native registry entry)."""
    role: str
    name: str = ""
    value: Any = None
    clickable: bool = False
    context: str = ""
    flags: dict[str, Any] = field(default_factory=dict)
    children: Optional[int] = None
    actions: list[str] = field(default_factory=list)
    token: Any = None


class ToolFailure(Exception):
    """A structured tool result raised as control flow inside a worker; carries the payload."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("error", ""))
        self.payload = payload


class SerialWorker:
    """A dedicated thread that owns a surface's live state. Public operations submit closures and
    block on the result; they never touch the owned state themselves. The thread is started lazily
    and restarted if it ever dies, so a surface can be used, dropped, and used again."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._queue: queue.Queue[Optional[tuple]] = queue.Queue()
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
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((operation, future))
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self._queue.put(None)


class Surface:
    """Base for the two automation surfaces. Owns the serial worker and the failure/recovery guard.
    Subclasses supply the substrate: how to read it into documents, and how to perform each action."""

    def __init__(self, worker_name: str, message: Callable[..., str]) -> None:
        self.worker = SerialWorker(worker_name)
        self.message = message

    def guard(self, operation: Callable[[], dict], *, timeout: float = 120.0) -> dict:
        """Submit one operation to the worker and shape every outcome into an honest payload. A
        ``ToolFailure`` becomes its payload; anything unexpected becomes ``recover``'s payload after
        the surface is given a chance to drop dead state."""

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
        """Drop any state a failed operation may have left broken (a lost connection, stale
        handles), on the worker thread. Overridden by surfaces that hold a connection."""
        return {}

    def recover(self, detail: str) -> dict:
        """The payload for an unexpected failure. Overridden with a surface-specific message."""
        return {"ok": False, "error": detail}

    def preflight(self, operation: str) -> Optional[dict]:
        """An optional gate run before a read or an action (a permission the surface needs).
        Returns a failure payload to refuse, or ``None`` to proceed."""
        return None

    def incomplete(self, message_name: str, **variables: str) -> dict:
        """A read that could not produce a usable view after waiting — a clear message the model can
        act on (wait and retry, or surface the blocker to the user)."""
        return {"ok": False, "incomplete": True, "error": self.message(message_name, **variables)}
