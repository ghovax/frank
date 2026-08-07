"""The shared spine both automation surfaces are built on, so the model faces one vocabulary over two substrates."""

from __future__ import annotations

import concurrent.futures
import inspect
import logging
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from langmesh.base.configuration import PromptLoader
from langmesh.base.tuning import Tunable, active_tuning

logger = logging.getLogger(__name__)


def machinery_ceiling() -> float:
    """How long the machinery around a control script waits on it, a margin above the script's own ceiling."""
    tuning = active_tuning()
    return tuning.duration(Tunable.control_script) + tuning.duration(Tunable.surface_guard_margin)


def message_loader(folder: str) -> Callable[..., str]:
    """A message function bound to one surface's own folder, so files are named for what they say."""
    loader = PromptLoader(Path(__file__).parent / "messages" / folder)

    def message(name: str, **variables: str) -> str:
        return loader.load(name, variables).strip()

    return message


def find_occurrence(content: str, needle: str, occurrence: int = 1) -> int:
    """Where the given occurrence of `needle` starts in `content`, or -1, case-sensitively."""
    start = -1
    for _ in range(max(1, occurrence)):
        start = content.find(needle, start + 1)
        if start == -1:
            return -1
    return start


def _anchor_offset(content: str, anchor: Any, *, past: bool, occurrence: int) -> int:
    """Resolve one endpoint: an integer is a clamped offset, a string is an occurrence's start or its end."""
    if isinstance(anchor, int):
        return max(0, min(anchor, len(content)))
    index = find_occurrence(content, anchor, occurrence)
    if index < 0:
        raise ToolFailure(
            {
                "ok": False,
                "error": f"The text {anchor!r} is not in the field, so there is nothing to point at.",
            }
        )
    return index + (len(anchor) if past else 0)


def resolve_range(
    content: str,
    *,
    text: Optional[str] = None,
    anchor_from: Any = None,
    anchor_to: Any = None,
    select_all: bool = False,
    occurrence: int = 1,
) -> tuple[int, int]:
    """Turn a selection request into a start and length: by substring, by a from-to pair, or all of it."""
    if select_all:
        return 0, len(content)
    if text is not None:
        if not text:
            raise ToolFailure({"ok": False, "error": "select needs non-empty text to look for."})
        index = find_occurrence(content, text, occurrence)
        if index < 0:
            raise ToolFailure(
                {
                    "ok": False,
                    "error": f"The text {text!r} is not in the field, so there is nothing to select.",
                }
            )
        return index, len(text)
    if anchor_from is not None and anchor_to is not None:
        start = _anchor_offset(content, anchor_from, past=False, occurrence=occurrence)
        end = _anchor_offset(content, anchor_to, past=True, occurrence=occurrence)
        if end < start:
            start, end = end, start
        return start, end - start
    raise ToolFailure({"ok": False, "error": "select needs one of: text, a from/to pair, or all."})


@dataclass
class Glance:
    """One cheap look at a surface, taking the globals that can move and the ids present from a single read."""

    facts: dict[str, Any] = field(default_factory=dict)
    ids: frozenset[str] = frozenset()


def changes_between(before: dict, after: dict) -> dict:
    """What differs between two observations, reporting `{}` when an action changed nothing."""
    report: dict[str, Any] = {}
    for name in sorted(set(before) | set(after)):
        was, now = before.get(name), after.get(name)
        if was != now:
            report[name] = {"from": was, "to": now}
    return report


def appeared_between(before: Glance, after: Glance) -> frozenset[str]:
    """The ids present after an action and not before, as ids only, with hydration left to the caller."""
    return frozenset(after.ids - before.ids)


def resolve_caret(
    content: str,
    *,
    before: Optional[str] = None,
    after: Optional[str] = None,
    at_offset: Optional[int] = None,
    to_start: bool = False,
    to_end: bool = False,
    occurrence: int = 1,
) -> int:
    """Turn a caret request into one offset: the start, the end, an explicit position, or around an occurrence."""
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
    raise ToolFailure(
        {"ok": False, "error": "caret needs one of: before, after, at_offset, start, or end."}
    )


@dataclass
class Element:
    """One element in the single vocabulary a surface produces and retrieval ranks."""

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
    """A dedicated thread that owns a surface's live state, started lazily and restarted if it dies."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._queue: queue.Queue[Optional[tuple]] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # The worker's own identity, so `submit` can tell a nested call from an outside one.
        self._thread_id: Optional[int] = None

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()

    def _run(self) -> None:
        self._thread_id = threading.get_ident()
        while True:
            item = self._queue.get()
            if item is None:
                return
            operation, future = item
            try:
                future.set_result(operation())
            except BaseException as error:  # noqa: BLE001 (marshalled to the caller)
                future.set_exception(error)

    def submit(self, operation: Callable[[], Any], timeout: Optional[float] = None) -> Any:
        # Already on the worker, so run it here: queueing would block on a future only this thread could complete.
        if threading.get_ident() == self._thread_id:
            return operation()
        timeout = timeout if timeout is not None else machinery_ceiling()
        self._ensure_thread()
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((operation, future))
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self._queue.put(None)


class Surface:
    """Base for the two automation surfaces, owning the serial worker and the failure guard."""

    def __init__(self, worker_name: str, message: Callable[..., str]) -> None:
        self.worker = SerialWorker(worker_name)
        self.message = message

    def guard(self, operation: Callable[[], dict], *, timeout: Optional[float] = None) -> dict:
        """Submit one operation to the worker and shape every outcome into an honest payload."""

        timeout = timeout if timeout is not None else machinery_ceiling()

        def guarded() -> dict:
            try:
                return operation()
            except ToolFailure as failure:
                return failure.payload

        try:
            return self.worker.submit(guarded, timeout=timeout)
        except Exception as error:  # substrate errors, timeouts, a dead target
            # Logged with its traceback before anything narrows it, since one sentence cannot say where a failure came from.
            logger.exception("a %s operation failed", type(self).__name__)
            first_line = str(error).splitlines()[0] if str(error) else error.__class__.__name__
            try:
                self.worker.submit(self.on_recover, timeout=5.0)
            except Exception:
                logger.debug("recovery after a failed operation also failed", exc_info=True)
            return self.recover(first_line)

    def on_recover(self) -> dict:
        """Drop any state a failed operation may have left broken, on the worker thread."""
        return {}

    def recover(self, detail: str) -> dict:
        """The payload for an unexpected failure. Overridden with a surface-specific message."""
        return {"ok": False, "error": detail}

    # The primitives the dispatcher services rather than the surface, so their shapes are written here.
    PROVIDED_SIGNATURES = {
        "find_one": 'screen.find_one(query, clickable=None, near="", name="", context="")',
        "find_many": 'screen.find_many(query, limit=8, clickable=None, near="", name="", context="")',
        "wait_for": 'screen.wait_for(query, seconds=5, clickable=None, near="", name="", context="")',
    }
    RETRIEVAL_PRIMITIVES = tuple(PROVIDED_SIGNATURES)

    def signatures(self) -> dict[str, str]:
        """Every primitive this surface implements, with the shape it is called in, read off the code rather than restated."""
        found = {
            name[len("_primitive_") :]: self.spoken_signature(
                name[len("_primitive_") :], getattr(self, name)
            )
            for name in dir(self)
            if name.startswith("_primitive_")
        }
        return dict(sorted({**self.PROVIDED_SIGNATURES, **found}.items()))

    def primitives(self) -> tuple[str, ...]:
        """Every primitive this surface implements, discovered from the methods rather than declared in a list."""
        found = {name[len("_primitive_") :] for name in dir(self) if name.startswith("_primitive_")}
        return tuple(sorted(self.RETRIEVAL_PRIMITIVES + tuple(sorted(found))))

    def glance(self, target: str) -> Glance:
        """One cheap look at a target for diffing an action against: the globals that move, plus the ids present."""
        return Glance()

    def spoken_signature(self, name: str, handler: Callable) -> str:
        """A primitive's call shape in the vocabulary the script is written in, rather than the method's own signature."""
        rendered = []
        for index, parameter in enumerate(inspect.signature(handler).parameters.values()):
            if index == 0 or parameter.kind is inspect.Parameter.VAR_KEYWORD:
                continue  # the bound state, and the **_ catch-all
            if parameter.default is inspect.Parameter.empty:
                rendered.append(parameter.name)
            else:
                rendered.append(f"{parameter.name}={parameter.default!r}")
        # Spelled the way it is called, since the script binds `screen` and a bare name would not resolve.
        return f"screen.{name}({', '.join(rendered)})"

    def call_primitive(
        self, name: str, handler: Callable, bound: Any, arguments: list, keywords: dict
    ) -> dict:
        """Call one primitive, turning a wrong call into words rather than a Python exception."""
        try:
            inspect.signature(handler).bind(bound, *arguments, **keywords)
        except TypeError as mismatch:
            return {
                "ok": False,
                "error": self.message(
                    "wrong_arguments",
                    primitive=name,
                    detail=str(mismatch),
                    signature=self.spoken_signature(name, handler),
                ),
            }
        return handler(bound, *arguments, **keywords)

    def perform(self, target: str, operation: str, arguments: list, keywords: dict) -> dict:
        """Run one primitive against one target, with the target passed as a parameter rather than remembered."""
        raise NotImplementedError

    def preflight(self, operation: str) -> Optional[dict]:
        """An optional gate run before a read or an action, returning a failure payload to refuse."""
        return None

    def incomplete(self, message_name: str, **variables: str) -> dict:
        """A read that could not produce a usable view after waiting, as a message the model can act on."""
        return {"ok": False, "incomplete": True, "error": self.message(message_name, **variables)}
