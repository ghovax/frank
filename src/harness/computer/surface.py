"""The shared spine both automation surfaces are built on: the web surface (Chrome over the
DevTools protocol) and the native macOS surface (the accessibility tree). Each drives a very
different substrate, but the model faces one vocabulary and one contract, and that contract
lives here.

What is shared, and why it belongs in one place:

* One **observe → act-by-ref** model. Every observe returns a flat list of elements, each
  carrying a stable ``ref`` (a short opaque id like ``k7Bn2p``); the ``ElementRegistry`` mints that ref
  once per element *identity* (role, name, and position in the tree) and rebinds it to the
  element's live target token on every observe (a web aria-ref, a native AX handle — both are
  snapshot-scoped, so they must be refreshed while the ref the model holds stays put). A ref
  therefore survives across calls: acting on one that has since vanished fails cleanly instead
  of bricking the loop, and a bad ``find`` never wipes what the model was paging through. The
  element schema is identical across surfaces (``Element`` below), so a button reads the same
  whether it is in a web page or a native window.
* One **serial worker**. Each surface owns exactly one thread that touches its live state, and
  public operations submit closures onto it. For the web surface this is a hard requirement
  (Playwright's sync API is thread-affine); for the native surface it is what serializes the
  registry instead of a lock over module globals.
* One **failure protocol**. ``ToolFailure`` carries a structured payload as control flow;
  ``Surface.guard`` wraps every operation so an unexpected exception becomes an honest payload
  and the surface gets a chance to recover (drop a dead connection, forget a stale registry).
* One **closed-loop result shape**. ``overview`` (after an observe or a navigation) returns the
  full addressable surface; ``digest`` (after an action) is **diff-first** — it returns just
  what changed (elements that appeared, disappeared, or updated) plus any live-region
  announcement, and only falls back to the whole surface when the change was wholesale (a
  navigation, a near-total rerender). Both end in ``finish``, which stamps the surface's
  location fields, the change flags, and any out-of-band events. The location fields are the one
  thing that differs — a URL and title for the web, an app and window for the native surface —
  so each surface supplies them and the rest is common.

User- and model-facing prose is never inlined here; it is loaded from ``messages/*.md`` like
every other prompt in the harness.
"""
from __future__ import annotations

import concurrent.futures
import queue
import random
import string
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from harness.core.configuration import PromptLoader
# Every tunable value — the window-scaled caps, the timeouts, and the fixed per-field clip and ref
# lengths — is reached through the one central policy object, so nothing here holds a bare
# constant of its own.
from harness.core.tuning import Limit, active_tuning


def message_loader(folder: str) -> Callable[..., str]:
    """A message function bound to ``messages/<folder>/`` — each surface keeps its own folder so
    the files are named for what they say (``not_connected``) rather than which tool owns them."""
    loader = PromptLoader(Path(__file__).parent / "messages" / folder)

    def message(name: str, **variables: str) -> str:
        return loader.load(name, variables).strip()

    return message


# The free text a single element contributes is length-bounded so one verbose node (a web app that
# stuffs a whole email body into a label, a native field holding a paragraph) cannot flood an
# observation. A control's own contents get more room than its label; both bounds are fixed
# ``Limit`` members read through ``active_tuning()`` at payload time.


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
    """One element, in the single vocabulary the model acts on across both surfaces.

    ``ref`` is the stable opaque handle the model targets (``k7Bn2p``); the ``ElementRegistry`` assigns it
    from the element's identity and rebinds it to the live ``token`` on every observe. ``name`` is
    the element's accessible name (the web accessible name; the native title, description, or
    help, whichever it has). ``value`` is its own contents. ``context`` is a short trail of the
    element's named ancestors (the nearest heading/landmark/region), so twenty identical "Add to
    Cart" buttons are told apart by the product they sit under. ``clickable`` marks a control the
    model can target (a web interactive role or pointer cursor; a native node that exposes a
    press-like AX action). ``flags`` holds only the notable states (disabled, selected, checked,
    expanded, …); unremarkable defaults are omitted as noise. ``children`` is set only on a
    region — a container an observe chose not to expand — and counts what waits inside so the
    model can drill. ``actions`` is the native AX action list when present. ``token`` is the
    opaque live handle used to act on the element (an aria-ref, an AX element); it never reaches
    the model. ``ref`` is filled in by the registry after construction."""
    role: str
    name: str = ""
    value: Any = None
    clickable: bool = False
    context: str = ""
    flags: dict[str, Any] = field(default_factory=dict)
    children: Optional[int] = None
    actions: list[str] = field(default_factory=list)
    truncated: bool = False
    ref: str = ""
    token: Any = None

    def identity(self, occurrence: int) -> tuple:
        """The stable key the registry mints a ref from: what the element is (role, name, a short
        prefix of its value) and where it sits (its ancestor trail), plus an ``occurrence`` index
        that separates otherwise-identical siblings. Stable across re-observations of the same
        surface, so the same element keeps the same ref."""
        value_key = self.value[:32] if isinstance(self.value, str) else self.value
        return (self.context, self.role, self.name, value_key, occurrence)

    def payload(self) -> dict[str, Any]:
        """The model-facing dict: ref and role always, everything else only when populated."""
        tuning = active_tuning()
        data: dict[str, Any] = {"ref": self.ref, "role": self.role}
        if self.name:
            data["name"], name_clipped = bounded(self.name, tuning.amount(Limit.LABEL_LENGTH))
        else:
            name_clipped = False
        value_clipped = False
        if isinstance(self.value, str):
            if self.value:
                data["value"], value_clipped = bounded(self.value, tuning.amount(Limit.VALUE_LENGTH))
        elif self.value is not None:
            data["value"] = self.value
        if self.context:
            data["context"] = self.context
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


class ElementRegistry:
    """The durable ref↔token map for one surface. A ref is minted once per element *identity* and
    kept for the surface's life; the live token behind it is rebound on every observe (aria-refs
    and AX handles are snapshot-scoped, so they go stale, but the ref the model holds does not).

    Two properties fall out of this and matter to the model: a ref stays valid across calls, so an
    observe/find between seeing an element and acting on it does not invalidate what was seen; and
    the map is only ever added to, never wiped, so a ``find`` that matches nothing cannot brick a
    paging loop the way replacing a positional index list did. Acting on a ref whose element has
    since vanished resolves to a stale token and fails cleanly — the surface reports it and the
    model re-observes, rather than silently acting on the wrong thing."""

    #: The nanoID alphabet and length. Deliberately *not* a running counter: an opaque random id
    #: (``k7Bn2p``) reads as a handle, not an ordinal, so it does not invite positional reasoning
    #: the way ``1``/``2`` would; the mixed case also means it can never be mistaken for an integer
    #: index. And because the id space is sparse, a stale or fabricated ref almost never collides
    #: with a live element — it resolves to nothing and fails cleanly, rather than silently acting
    #: on whatever happens to sit at that number. Six base-62 chars is ~57 billion ids, far more
    #: than the few hundred elements a surface ever holds. The length itself is the central
    #: policy's ``ref_length`` (fixed, but read through the one tuning surface like everything else).
    _ALPHABET = string.ascii_letters + string.digits

    def __init__(self) -> None:
        self._refs: dict[tuple, str] = {}     # identity → ref, grow-only within a surface's life
        self._tokens: dict[str, Any] = {}     # ref → live token, rebound each observe
        self._random = random.Random()

    def _mint(self) -> str:
        """A fresh opaque ref, regenerated on the vanishingly rare collision with a live one."""
        length = active_tuning().amount(Limit.REF_LENGTH)
        while True:
            ref = "".join(self._random.choices(self._ALPHABET, k=length))
            if ref not in self._tokens:
                return ref

    def bind(self, elements: list["Element"]) -> None:
        """Assign each element its stable ref (minting a new one only for a never-seen identity)
        and rebind that ref to the element's current live token. Identities are disambiguated by
        an occurrence counter so identical siblings get distinct refs."""
        seen: dict[tuple, int] = {}
        for element in elements:
            base = element.identity(0)
            occurrence = seen.get(base, 0)
            seen[base] = occurrence + 1
            identity = element.identity(occurrence)
            ref = self._refs.get(identity)
            if ref is None:
                ref = self._mint()
                self._refs[identity] = ref
            element.ref = ref
            self._tokens[ref] = element.token

    def token(self, ref: str) -> Any:
        """The live token bound to a ref, or ``None`` when the ref was never seen."""
        return self._tokens.get(ref)

    def __contains__(self, ref: str) -> bool:
        return ref in self._tokens

    def reset(self) -> None:
        """Forget everything (a dropped connection, a fresh app)."""
        self._refs.clear()
        self._tokens.clear()


# Payload keys the diff ignores when deciding whether an element "updated": the ones that never
# change for a given element (its identity and structure), plus the volatile focus marker. A click
# lands focus on its target, flipping ``active`` — treating that as a change would make almost every
# click report ``changed`` and drown out the real signal, so focus movement is not a content change.
_STABLE_KEYS = frozenset({
    "ref", "role", "name", "context", "clickable", "children", "actions", "active",
})


def diff_elements(previous: list[dict], current: list[dict]) -> Optional[dict]:
    """The structured delta between two observations of one surface, keyed by the stable ref so a
    reordering is never mistaken for a change. Returns the elements that ``appeared``,
    ``disappeared``, or ``updated`` (same ref, changed value/state), or ``None`` when nothing
    moved. This is what an acting result leads with instead of re-listing the whole surface."""
    previous_by_ref = {element["ref"]: element for element in previous if element.get("ref")}
    current_by_ref = {element["ref"]: element for element in current if element.get("ref")}
    appeared = [element for ref, element in current_by_ref.items() if ref not in previous_by_ref]
    disappeared = [element for ref, element in previous_by_ref.items() if ref not in current_by_ref]

    def state(element: dict) -> dict:
        return {key: value for key, value in element.items() if key not in _STABLE_KEYS}

    updated = [
        element for ref, element in current_by_ref.items()
        if ref in previous_by_ref and state(element) != state(previous_by_ref[ref])
    ]
    delta = {
        name: value
        for name, value in (("appeared", appeared), ("disappeared", disappeared), ("updated", updated))
        if value
    }
    return delta or None


# The fraction of newly-appeared elements above which a change is shown as the full fresh surface
# rather than a delta. A heuristic ratio, not a token budget, so it stays fixed.
_WHOLESALE_APPEARED_FRACTION = 0.6


def _is_wholesale(delta: Optional[dict], current: list[dict]) -> bool:
    """Whether a change is better shown as the full fresh surface than as a delta: a navigation or
    near-total rerender, where most of what is on screen is new. A local change (a menu opening, a
    field validating) is not wholesale and comes back as just the delta."""
    if not delta:
        return False
    appeared = len(delta.get("appeared", []))
    return appeared >= _WHOLESALE_APPEARED_FRACTION * max(1, len(current))


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
        # How many acting calls in a row have changed nothing. A run of these means the model
        # is repeating something that isn't working; past ``NO_EFFECT_LIMIT`` the surface stops
        # accepting blind actions and hands back the situation so the model tries a different
        # approach. Any read (observe) or any action that does change something resets it.
        self._no_effect = 0
        # Whether the last read produced a usable view. A screenshot (pixels) is only offered
        # once this is False — i.e. the semantic surface could not be read — so pixels stay the
        # last resort, never a routine choice.
        self._readable = True

    # Progress tracking (shared by both surfaces).

    def _reset_progress(self) -> None:
        self._no_effect = 0

    def _note_effect(self, changed: bool) -> None:
        self._no_effect = 0 if changed else self._no_effect + 1

    def stuck(self) -> bool:
        return self._no_effect >= active_tuning().amount(Limit.NO_EFFECT_LIMIT)

    def pixels_allowed(self) -> bool:
        """A screenshot is only permitted when the last read could not be read semantically."""
        return not self._readable

    def incomplete(self, message_name: str, **variables: str) -> dict:
        """A read that could not produce a usable view after waiting. Marks the surface
        unreadable (so a screenshot becomes available as the fallback) and returns a clear
        message the model can act on (wait/retry, or fall back)."""
        self._readable = False
        return {"ok": False, "incomplete": True, "error": self.message(message_name, **variables)}

    # Failure handling.

    def guard(self, operation: Callable[[], dict], *, acting: bool = False, timeout: float = 120.0) -> dict:
        """Submit one operation to the worker and shape every outcome into an honest payload. A
        ``ToolFailure`` becomes its payload; anything unexpected becomes ``recover``'s payload
        after the surface is given a chance to drop dead state.

        When ``acting`` (a call that tries to change something) and the model is ``stuck`` — a run
        of actions that changed nothing — the surface refuses to keep acting blind and returns a
        message telling the model to look again and try a different approach. Cleared by the next
        observe."""

        def guarded() -> dict:
            if acting and self.stuck():
                return {"ok": False, "stuck": True, "error": self.message("stuck")}
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
        # A fresh look clears the "stuck" streak and records whether the surface could be read at
        # all — an empty read is what makes a screenshot available as the fallback.
        self._reset_progress()
        self._readable = bool(elements)
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
        current: list[dict],
        previous: Optional[list[dict]] = None,
        url_changed: Optional[bool] = None,
        events: Optional[list[dict]] = None,
        truncated: bool = False,
        truncated_note: str = "",
        prose_note_name: str = "",
        empty_hint: str = "",
        no_change_note: str = "",
    ) -> dict:
        """The **diff-first** result an acting call returns. Instead of re-listing the whole
        actionable surface on every step (thousands of tokens of unchanged buttons), it leads with
        the delta against the surface as it stood before the action — what ``appeared``,
        ``disappeared``, or ``updated`` — plus any live-region announcement (a validation error, a
        status line). Only a *wholesale* change (a navigation, a near-total rerender) falls back to
        the full fresh surface, since there a delta would be the whole surface anyway and the model
        needs the new lay of the land. ``changed`` stays as an honest boolean either way; a local
        change carries ``changes`` (the structured delta), a wholesale one carries ``elements``."""
        delta = diff_elements(previous, current) if previous is not None else None
        content_changed = delta is not None
        result: dict[str, Any] = {"ok": True}
        if url_changed is not None:
            result["url_changed"] = bool(url_changed)
        result["changed"] = bool(content_changed or url_changed)

        if url_changed or previous is None or _is_wholesale(delta, current):
            # A fresh surface: the full actionable set (every clickable plus any announcement),
            # with bulk prose deferred to observe/read, exactly as an overview defers it.
            listed = [
                element for element in current
                if element.get("clickable") or element.get("role") in self.live_region_roles
            ]
            prose_deferred = len(current) - len(listed)
            result["count"] = len(current)
            result["elements"] = listed
            if truncated and truncated_note:
                result["truncated"] = True
                result["note"] = truncated_note
            elif prose_deferred and prose_note_name:
                result["note"] = self.message(prose_note_name, count=str(prose_deferred))
        else:
            # A local change: just the delta, plus any announcement the page made in response.
            announcements = [element for element in current if element.get("role") in self.live_region_roles]
            if delta:
                result["changes"] = delta
            if announcements:
                result["announcements"] = announcements
            if not delta and not announcements and no_change_note:
                result["note"] = no_change_note

        # An action that changed nothing feeds the "stuck" streak; one that did resets it. Once
        # the streak is over the limit, say so plainly and prominently so the model stops
        # repeating and looks for another way in.
        self._note_effect(bool(result.get("changed")))
        if self.stuck():
            result["stuck"] = True
            result["note"] = self.message("stuck")

        return self.finish(
            result, context=context, elements=current, changed=None,
            events=events, empty_hint=empty_hint,
        )
