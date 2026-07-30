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
import inspect
import logging
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from frank.base.configuration import PromptLoader
from frank.base.tuning import Tunable, active_tuning

logger = logging.getLogger(__name__)


def machinery_ceiling() -> float:
    """How long the machinery around a control script waits on it.

    A margin above the script's own ceiling, deliberately, so the two stay ordered however they
    are configured. When the guard fires first it drops the connection and calls `on_recover`,
    which leaves the surface half-dead while the script it was waiting on is still running —
    the failure that made three coincidentally-equal constants a bug rather than a tidiness
    problem."""
    tuning = active_tuning()
    return tuning.duration(Tunable.control_script_seconds) + tuning.duration(Tunable.surface_guard_margin_seconds)


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


@dataclass
class Glance:
    """One cheap look at a surface: the globals that can move, and which elements are present.

    Both from a single read, deliberately. Bracketing an action used to cost four full tree walks
    — an ``observe`` and a full ``documents`` on each side — and the ``documents`` half was worse
    than slow: it rebuilds the surface's id→element map, so every id the model was already holding
    silently re-pointed at whatever now sat at that tree path. One walk, ids only, no registry
    touched."""

    facts: dict[str, Any] = field(default_factory=dict)
    ids: frozenset[str] = frozenset()


def changes_between(before: dict, after: dict) -> dict:
    """What differs between two observations, as `{field: {"from": …, "to": …}}`.

    Only fields that actually moved. An action that changed nothing reports `{}` — the signal a
    model needs to stop guessing: an action used to return what it *touched* and never what it
    *changed*, so a script that clicks a tab and then cannot find the field inside it had no way
    to tell a failed click from a slow pane from a differently-named field. Four attempts at a
    two-step task came out of that gap."""
    report: dict[str, Any] = {}
    for name in sorted(set(before) | set(after)):
        was, now = before.get(name), after.get(name)
        if was != now:
            report[name] = {"from": was, "to": now}
    return report


def appeared_between(before: Glance, after: Glance) -> frozenset[str]:
    """The ids present after an action and not before. Ids only; hydration is the caller's call.

    What became newly available is the most useful thing a model can learn, because it is what it
    can act on next — but "everything new, uncapped, with full payloads" is a page of elements
    when the action happened to navigate, and that is exactly what one Enter key produced. The
    count is always truthful; how much of it is spelled out is decided where the budget is
    known."""
    return frozenset(after.ids - before.ids)


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

    def submit(self, operation: Callable[[], Any], timeout: Optional[float] = None) -> Any:
        timeout = timeout if timeout is not None else machinery_ceiling()
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

    def guard(self, operation: Callable[[], dict], *, timeout: Optional[float] = None) -> dict:
        """Submit one operation to the worker and shape every outcome into an honest payload. A
        ``ToolFailure`` becomes its payload; anything unexpected becomes ``recover``'s payload after
        the surface is given a chance to drop dead state."""

        timeout = timeout if timeout is not None else machinery_ceiling()

        def guarded() -> dict:
            try:
                return operation()
            except ToolFailure as failure:
                return failure.payload

        try:
            return self.worker.submit(guarded, timeout=timeout)
        except Exception as error:  # substrate errors, timeouts, a dead target
            # Logged with its traceback before anything narrows it. What reaches the model is one
            # sentence, and one sentence cannot say where a failure came from: an
            # `AttributeError: 'AXUIElementCreateApplication'` — a Python symbol that failed to
            # resolve — arrived as "The action failed … Observe the app again and retry", and the
            # model spent a turn theorising about the application's readiness. The traceback that
            # would have named it was discarded here, so nobody could do better afterwards.
            logger.exception("A %s operation failed", type(self).__name__)
            first_line = str(error).splitlines()[0] if str(error) else error.__class__.__name__
            try:
                self.worker.submit(self.on_recover, timeout=5.0)
            except Exception:
                logger.debug("Recovery after a failed operation also failed", exc_info=True)
            return self.recover(first_line)

    def on_recover(self) -> dict:
        """Drop any state a failed operation may have left broken (a lost connection, stale
        handles), on the worker thread. Overridden by surfaces that hold a connection."""
        return {}

    def recover(self, detail: str) -> dict:
        """The payload for an unexpected failure. Overridden with a surface-specific message."""
        return {"ok": False, "error": detail}

    # The primitives the dispatcher services rather than the surface, so their shapes are written
    # here instead of discovered below. These four are the only hand-written signatures left.
    #
    # `wait_for` and `sleep` are here because a script could not previously wait for anything at
    # all: `import time` is outside the safe set, so any script that polled classified `unknown`
    # and was refused. That made "click, wait for the pane, then find" — the commonest shape in
    # this whole domain — unwritable, and left splitting the work across tool calls as the only
    # option. Scripts were short because the environment made them short.
    PROVIDED_SIGNATURES = {
        "find_one": 'screen.find_one(query, clickable=None, name="", context="")',
        "find_many": 'screen.find_many(query, limit=8, all=False, clickable=None, name="", context="")',
        "wait_for": 'screen.wait_for(query, seconds=5, clickable=None, name="", context="")',
        "sleep": "screen.sleep(seconds)",
    }
    RETRIEVAL_PRIMITIVES = tuple(PROVIDED_SIGNATURES)

    def signatures(self) -> dict[str, str]:
        """Every primitive this surface implements, with the shape it is actually called in.

        This is what the model is handed, and it is read off the code rather than restated beside
        it. A hand-written list drifts the moment a parameter is renamed, and it did: the
        description promised ``evaluate(javascript, …)``, ``tab(id)`` and ``close_tab(id="")``
        while the implementations took ``expression`` and ``tab`` — so a script written from the
        documentation failed the signature check that exists to catch exactly that mistake. The
        list cannot disagree with the code if there is only one of them."""
        found = {
            name[len("_primitive_"):]: self.spoken_signature(name[len("_primitive_"):], getattr(self, name))
            for name in dir(self) if name.startswith("_primitive_")
        }
        return dict(sorted({**self.PROVIDED_SIGNATURES, **found}.items()))

    def primitives(self) -> tuple[str, ...]:
        """Every primitive this surface implements, discovered from the surface itself.

        Read off the methods rather than declared in a list, because a list is a second place to
        state the same fact and the two drift: the script namespace was a fixed tuple of twenty
        names while a native window implemented eight, and nothing connected the two."""
        found = {name[len("_primitive_"):] for name in dir(self) if name.startswith("_primitive_")}
        return tuple(sorted(self.RETRIEVAL_PRIMITIVES + tuple(sorted(found))))

    def glance(self, target: str) -> Glance:
        """One cheap look at a target, for diffing an action against.

        Deliberately small and deliberately global: title, focus, selection, and — where they
        exist — url; plus the set of element ids present. A full re-read after every action is
        truthful and unaffordable, and the acted-on subtree alone misses the consequences that
        matter most, which are exactly the ones that happen elsewhere: a pane switching, a page
        navigating, focus moving.

        A surface that cannot answer cheaply returns what it can. An empty glance makes the diff
        empty, which reads as "nothing observable changed" — true, and better than a guess."""
        return Glance()

    def spoken_signature(self, name: str, handler: Callable) -> str:
        """A primitive's call shape, in the vocabulary the script is written in.

        Not ``NativeSurface._primitive_type(self, state, ref, text, *, submit=False)``. The model
        never wrote that name, cannot see that class, and did not pass those first two arguments;
        showing it a Python signature it never called is the same failure as showing it a
        traceback and calling it an explanation."""
        rendered = []
        for index, parameter in enumerate(inspect.signature(handler).parameters.values()):
            if index == 0 or parameter.kind is inspect.Parameter.VAR_KEYWORD:
                continue                       # the bound state, and the **_ catch-all
            if parameter.default is inspect.Parameter.empty:
                rendered.append(parameter.name)
            else:
                rendered.append(f"{parameter.name}={parameter.default!r}")
        # Spelled the way it is called. The signatures went into the model's context as bare
        # names — `find_many(query, …)` — while the runner bound only `screen`, so a script
        # written from exactly what it had been handed hit an unbound name and the whole helper
        # appeared to die before doing anything. One statement of the calling convention.
        return f"screen.{name}({', '.join(rendered)})"

    def call_primitive(self, name: str, handler: Callable, bound: Any, arguments: list, keywords: dict) -> dict:
        """Call one primitive, turning a wrong call into words instead of a Python exception.

        The arguments are checked against the signature *before* the call, so a mismatch is
        answered and anything the body itself raises still propagates to the guard. A model that
        wrote ``type("DateTimeClasses", submit=True)`` — forgetting that a field comes first — was
        told ``_primitive_type() missing 1 required positional argument: 'text'``, and had to
        reverse-engineer a private method's signature to find out what it should have written."""
        try:
            inspect.signature(handler).bind(bound, *arguments, **keywords)
        except TypeError as mismatch:
            return {"ok": False, "error": self.message(
                "wrong_arguments", primitive=name, detail=str(mismatch),
                signature=self.spoken_signature(name, handler),
            )}
        return handler(bound, *arguments, **keywords)

    def perform(self, target: str, operation: str, arguments: list, keywords: dict) -> dict:
        """Run one primitive against one target. Every surface answers this shape.

        ``target`` is a parameter rather than remembered state. It used to be neither: the tool
        boundary took a target and the surface underneath kept a single ``_last_pid``, set by
        whichever read happened last anywhere in the process — so ``press`` and ``focus`` and
        ``read`` all acted on "the last one", two concurrent scripts shared it, and ``read()``
        failed with "nothing to read yet" for reasons no script could see."""
        raise NotImplementedError

    def preflight(self, operation: str) -> Optional[dict]:
        """An optional gate run before a read or an action (a permission the surface needs).
        Returns a failure payload to refuse, or ``None`` to proceed."""
        return None

    def incomplete(self, message_name: str, **variables: str) -> dict:
        """A read that could not produce a usable view after waiting — a clear message the model can
        act on (wait and retry, or surface the blocker to the user)."""
        return {"ok": False, "incomplete": True, "error": self.message(message_name, **variables)}
