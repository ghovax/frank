"""The one place every tool's size, count, and timing limit is decided.

Historically the tool backends carried some sixty scattered magic numbers — an element cap here,
a read window there, a dozen different Playwright ``timeout=`` literals — each a fixed guess that
neither scaled with the model nor could be tuned. This module replaces all of them with a single
policy that answers two kinds of question:

* **How much may one tool result be?** Size and count caps (a page's element listing, a read
  window, a truncation ceiling, a grep result set) are *token budgets*, so they scale with the
  live model context window: a small local model gets tight caps that keep it from drowning, a
  million-token model gets room to work. Text budgets are counted in **tokens** and enforced with
  a real tokenizer (:func:`clip_to_tokens`), never a fixed characters-per-token guess.

* **How long may one action wait, and how does a surface settle?** Timeouts scale only with the
  ``timeout_scale`` knob (time does not depend on the context window). Settlement is not a fixed
  sleep at all: :func:`settle` polls a surface until it stops changing, bounded by the configured
  interval and ceiling.

Every tunable value is defined **exactly once** as a member of :class:`Limit`, carrying its
baseline and how it scales — no parallel wall of module constants and one-line accessor methods.
Two typed getters resolve any of them against the live window: :meth:`Tuning.amount` (an integer:
tokens, counts, milliseconds, characters) and :meth:`Tuning.duration` (a float of seconds).

Delivery mirrors the rest of the daisy. The static *policy* is process-global, pushed in by
:func:`set_tuning` at startup and on every config reload (exactly like ``set_exa_client``). The
dynamic *budget* — the live context window — is threaded per call through
:data:`current_context_window`, a context variable the running agent sets around each tool
execution; it is copied into worker threads by ``asyncio.to_thread``, so both the async tools and
the thread-affine automation surfaces read the calling agent's real window without any of them
having to grow a parameter for it. Concurrent calls from different agents each see their own.
"""
from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, TypeVar

# The live model context window (in tokens) for the tool call currently executing. The agent
# runtime sets this around each tool dispatch and resets it after; ``asyncio.to_thread`` copies
# the context into the worker thread, so the automation surfaces see it too. 0 means "not known
# yet" (before the first model call reports usage), which the resolver treats as the turn-zero seed.
current_context_window: contextvars.ContextVar[int] = contextvars.ContextVar(
    "current_context_window", default=0,
)


# Calibration, grounded in the real spread of model context windows (tokens):
#   * 200K is the standard window of the current flagship chat models (the harness's usual case),
#     so it is the reference at which every baseline below equals its calibrated production value —
#     defaults reproduce today's behaviour and only *change* it for genuinely smaller/larger models.
#   * A window is clamped into [16K, 2M] before scaling: 16K is a small local/older model, 2M is the
#     largest generally-available window (Gemini-class). Outside that range the caps would be
#     degenerate, so the clamp, not a per-cap floor/ceiling, keeps every derived value sane.
REFERENCE_WINDOW = 200_000
_TURN_ZERO_WINDOW = 200_000
_MINIMUM_WINDOW = 16_000
_MAXIMUM_WINDOW = 2_000_000

# The knob values the baselines are calibrated against: at these fractions the family multiplier is
# 1.0, so raising ``output_fraction`` above 0.25 enlarges every text budget proportionally, and so on.
_DEFAULT_OUTPUT_FRACTION = 0.25
_DEFAULT_LISTING_FRACTION = 0.15


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class _Scale(Enum):
    """How a :class:`Limit`'s baseline is scaled to its live value."""
    OUTPUT = "output"    # a token/char budget: window * output_fraction
    LISTING = "listing"  # an item count: window * listing_fraction
    TIMEOUT = "timeout"  # a ceiling: timeout_scale only (time does not depend on the window)
    FIXED = "fixed"      # a physical/clip constant: not scaled at all


class Limit(Enum):
    """Every tunable value, defined once as ``(baseline, scaling)``. Resolve one with
    :meth:`Tuning.amount` (integer values: tokens, counts, milliseconds, characters) or
    :meth:`Tuning.duration` (seconds), which apply the scaling for its family."""

    # Text budgets, in TOKENS unless a character clip, scaled by the window and output_fraction.
    # As a share of a 200K window: 16K ≈ 8% for one command's output, 4K ≈ 2% for a read window,
    # 24K ≈ 12% for a whole fetched page (the rest overflows to a file). Enforced by clip_to_tokens.
    OUTPUT_TOKENS = (16_000, _Scale.OUTPUT)        # one tool's inline output (bash, model result)
    READ_WINDOW_TOKENS = (4_000, _Scale.OUTPUT)    # one page/file read window, paged past
    EVALUATE_TOKENS = (4_000, _Scale.OUTPUT)       # a browser evaluate() JSON result
    FETCH_TOKENS = (24_000, _Scale.OUTPUT)         # a fetched web page's inline text
    MAXIMUM_LINE_CHARS = (2_048, _Scale.OUTPUT)    # a single over-long line clipped (minified blob)

    # Listing budgets, in item COUNTS, scaled by the window and listing_fraction.
    ELEMENT_CAP = (300, _Scale.LISTING)            # elements in a full surface overview
    PROSE_BUDGET = (80, _Scale.LISTING)            # non-interactive text rows kept in an overview
    FIND_LIMIT = (25, _Scale.LISTING)              # matches a find returns
    READ_LINES = (2_000, _Scale.LISTING)           # lines a read returns on a non-positive limit
    GREP_RESULTS = (512, _Scale.LISTING)           # total grep matches
    GREP_PER_FILE = (512, _Scale.LISTING)          # grep matches per file
    GLOB_RESULTS = (1_000, _Scale.LISTING)         # files a glob returns
    NETWORK_LIMIT = (50, _Scale.LISTING)           # recent network requests the browser reader gives
    WEB_SEARCH_MAXIMUM = (10, _Scale.LISTING)      # ceiling on requested web-search results
    REMOTE_LISTING = (32_768, _Scale.LISTING)      # remote paths listed before glob matching

    # Timeouts. Milliseconds (read with amount) for Playwright, seconds (read with duration) for the
    # subprocess/AX/settle IO; both scale only with timeout_scale.
    ACTION_TIMEOUT_MS = (5_000, _Scale.TIMEOUT)
    NAVIGATION_TIMEOUT_MS = (20_000, _Scale.TIMEOUT)
    SNAPSHOT_TIMEOUT_MS = (10_000, _Scale.TIMEOUT)
    CONNECT_TIMEOUT_MS = (10_000, _Scale.TIMEOUT)
    DRAG_TIMEOUT_MS = (8_000, _Scale.TIMEOUT)
    SCREENSHOT_TIMEOUT_MS = (20_000, _Scale.TIMEOUT)
    READ_TEXT_TIMEOUT_MS = (10_000, _Scale.TIMEOUT)
    EXPECTATION_TIMEOUT_MS = (8_000, _Scale.TIMEOUT)  # waiting for a model-stated outcome to appear
    SIGTERM_GRACE_SECONDS = (2.0, _Scale.TIMEOUT)            # after SIGTERM, before SIGKILL, on cancel
    RIPGREP_SECONDS = (30.0, _Scale.TIMEOUT)
    # How long a backgroundable tool waits inline before it hands the work to the background
    # runner (a non-killing wait window, the model-overridable `timeout` tool parameter's
    # default — NOT a network deadline). Central so the three tools' defaults live in one place
    # rather than as scattered private module constants.
    BASH_SYNC_WINDOW_SECONDS = (60.0, _Scale.TIMEOUT)        # bash: sync by default, long window
    SLOW_TOOL_SYNC_WINDOW_SECONDS = (10.0, _Scale.TIMEOUT)   # fetch_url / download_file: short window
    WEB_SEARCH_SYNC_WINDOW_SECONDS = (10.0, _Scale.TIMEOUT)  # web_search: short window
    AX_MESSAGING_SECONDS = (2.0, _Scale.TIMEOUT)             # per-AX-message ceiling against a hung app
    SCREENCAPTURE_SECONDS = (15.0, _Scale.TIMEOUT)
    OPEN_URL_SECONDS = (5.0, _Scale.TIMEOUT)

    # Fixed, deliberately NOT scaled — per-field clip lengths, the element ref length, and the
    # physical input-event pacing the OS needs for a synthesized click/keystroke/drag to register.
    LABEL_LENGTH = (256, _Scale.FIXED)                       # an element's accessible name in a payload
    VALUE_LENGTH = (512, _Scale.FIXED)                       # an element's own value/contents
    REF_LENGTH = (6, _Scale.FIXED)                           # the opaque element ref (base-62)
    TYPE_CHUNK_SIZE = (20, _Scale.FIXED)                     # characters per synthesized keyboard event
    DRAG_STEPS = (12, _Scale.FIXED)                          # interpolation segments a drag is split into
    SCROLL_AMOUNT_PIXELS = (300, _Scale.FIXED)               # one wheel step for the native surface
    SETTLE_STABLE_READS = (2, _Scale.FIXED)                  # identical reads that count a surface settled
    NO_EFFECT_LIMIT = (3, _Scale.FIXED)                      # consecutive no-effect actions before a surface stops accepting blind ones
    CLICK_INTERVAL_SECONDS = (0.01, _Scale.FIXED)            # between successive synthesized clicks
    DRAG_STEP_INTERVAL_SECONDS = (0.01, _Scale.FIXED)        # between interpolated drag-move events
    TYPE_CHUNK_INTERVAL_SECONDS = (0.005, _Scale.FIXED)      # between typed chunks
    FOCUS_SETTLE_SECONDS = (0.03, _Scale.FIXED)              # after focusing a field, before typing

    def __init__(self, baseline: float, scaling: _Scale) -> None:
        self.baseline = baseline
        self.scaling = scaling


# Tokenizer-backed text budgeting. A real tokenizer maps a token budget to an accurate character
# cut for any content; a fixed characters-per-token ratio (which the old code assumed) is wrong for
# code, whitespace runs, and non-Latin scripts. The encoding is loaded lazily and cached, with a
# coarse character estimate as the fallback if it cannot load at all (e.g. an offline first run of
# the frozen app) so budgeting degrades rather than crashing.
_ENCODING_NAME = "o200k_base"     # the current-generation general tokenizer; a good cross-model proxy
_FALLBACK_CHARS_PER_TOKEN = 4     # only used when the tokenizer is unavailable
# A single token decodes to at most this many characters, so budget * this characters is a safe
# superset of the first ``budget`` tokens — tokenizing only that prefix bounds the work on a huge
# output instead of encoding the whole thing.
_MAXIMUM_CHARS_PER_TOKEN = 32

_encoding = None
_encoding_loaded = False


def _get_encoding():
    global _encoding, _encoding_loaded
    if not _encoding_loaded:
        _encoding_loaded = True
        try:
            import tiktoken

            _encoding = tiktoken.get_encoding(_ENCODING_NAME)
        except Exception:
            _encoding = None
    return _encoding


def count_tokens(text: str) -> int:
    """The token count of ``text`` under the reference tokenizer (a coarse character estimate when
    the tokenizer is unavailable)."""
    encoding = _get_encoding()
    if encoding is None:
        return -(-len(text) // _FALLBACK_CHARS_PER_TOKEN)  # ceil division
    return len(encoding.encode(text, disallowed_special=()))


def clip_to_tokens(text: str, budget: int) -> tuple[str, bool]:
    """Clip ``text`` to at most ``budget`` tokens, returning (clipped_text, was_truncated). The cut
    is placed on a real token boundary, so the budget means what it says regardless of the content's
    density — unlike a fixed characters-per-token slice. Only the head that can possibly hold the
    first ``budget`` tokens is tokenized, so a multi-megabyte output is not encoded in full."""
    budget = max(1, budget)
    encoding = _get_encoding()
    if encoding is None:
        cap = budget * _FALLBACK_CHARS_PER_TOKEN
        return (text, False) if len(text) <= cap else (text[:cap], True)
    head = text[: budget * _MAXIMUM_CHARS_PER_TOKEN]
    tokens = encoding.encode(head, disallowed_special=())
    if len(head) == len(text) and len(tokens) <= budget:
        return text, False
    return encoding.decode(tokens[:budget]), True


class TuningConfiguration:
    """The 5-knob policy, structurally compatible with the Pydantic model in ``configuration.py``
    (which is what is actually loaded). Kept as a plain attribute holder here so ``tuning`` has no
    import dependency on the config module; :func:`set_tuning` accepts either."""

    def __init__(
        self,
        output_fraction: float = _DEFAULT_OUTPUT_FRACTION,
        listing_fraction: float = _DEFAULT_LISTING_FRACTION,
        settle_interval_seconds: float = 0.05,
        settle_ceiling_seconds: float = 1.5,
        timeout_scale: float = 1.0,
    ) -> None:
        self.output_fraction = output_fraction
        self.listing_fraction = listing_fraction
        self.settle_interval_seconds = settle_interval_seconds
        self.settle_ceiling_seconds = settle_ceiling_seconds
        self.timeout_scale = timeout_scale


@dataclass
class Tuning:
    """Resolves the policy against a live context window. :meth:`amount` and :meth:`duration` take
    a :class:`Limit` and an optional ``window``; when the window is omitted they read
    :data:`current_context_window` (the calling agent's live window), falling back to the turn-zero
    seed while that is still unknown — so a call site simply asks
    ``active_tuning().amount(Limit.ELEMENT_CAP)`` and gets the right value for whoever is running."""

    policy: TuningConfiguration

    def _window(self, window: Optional[int]) -> int:
        effective = current_context_window.get() if window is None else window
        if not effective or effective <= 0:
            effective = _TURN_ZERO_WINDOW
        return int(_clamp(effective, _MINIMUM_WINDOW, _MAXIMUM_WINDOW))

    def _window_scale(self, window: Optional[int]) -> float:
        return self._window(window) / REFERENCE_WINDOW

    def _raw(self, limit: Limit, window: Optional[int]) -> float:
        """The scaled value of a limit, before it is rounded to an int or returned as a float."""
        scaling = limit.scaling
        if scaling is _Scale.OUTPUT:
            knob = self.policy.output_fraction / _DEFAULT_OUTPUT_FRACTION
            return max(1.0, limit.baseline * self._window_scale(window) * knob)
        if scaling is _Scale.LISTING:
            knob = self.policy.listing_fraction / _DEFAULT_LISTING_FRACTION
            return max(1.0, limit.baseline * self._window_scale(window) * knob)
        if scaling is _Scale.TIMEOUT:
            return max(0.001, limit.baseline * self.policy.timeout_scale)
        return float(limit.baseline)

    def amount(self, limit: Limit, window: Optional[int] = None) -> int:
        """A limit as an integer — a token budget, an item count, a millisecond timeout, a length."""
        return max(1, int(round(self._raw(limit, window))))

    def duration(self, limit: Limit, window: Optional[int] = None) -> float:
        """A limit as a float of seconds — a timeout or a physical input-pacing interval."""
        return self._raw(limit, window)

    def scale_timeout(self, seconds: float) -> float:
        """Apply the timeout knob to a caller-supplied or baseline IO timeout — the one place a
        command/connect/subprocess ceiling is adjusted for a slow (or fast) machine or link."""
        return max(0.1, seconds * self.policy.timeout_scale)

    # Settlement interval and ceiling come straight from the policy (they are the user's knobs, not
    # scaled baselines); the stable-read count is a fixed Limit.
    def settle_interval(self) -> float:
        return max(0.001, self.policy.settle_interval_seconds)

    def settle_ceiling(self) -> float:
        return max(0.0, self.policy.settle_ceiling_seconds)


# Process-global active policy, pushed in at startup and on every config reload. Defaults to the
# calibrated baselines so imports before the server wires it up still behave.
_active: Tuning = Tuning(TuningConfiguration())


def set_tuning(tuning: Tuning) -> None:
    """Install the process-global tuning policy (called at startup and on each config reload)."""
    global _active
    _active = tuning


def tuning_from_policy(policy: object) -> Tuning:
    """Wrap a loaded config section (the Pydantic ``TuningConfiguration``, or any object exposing
    the same five attributes) into a :class:`Tuning`. Missing attributes fall back to the defaults,
    so a partial or older config never breaks."""
    return Tuning(TuningConfiguration(
        output_fraction=float(getattr(policy, "output_fraction", _DEFAULT_OUTPUT_FRACTION)),
        listing_fraction=float(getattr(policy, "listing_fraction", _DEFAULT_LISTING_FRACTION)),
        settle_interval_seconds=float(getattr(policy, "settle_interval_seconds", 0.05)),
        settle_ceiling_seconds=float(getattr(policy, "settle_ceiling_seconds", 1.5)),
        timeout_scale=float(getattr(policy, "timeout_scale", 1.0)),
    ))


def active_tuning() -> Tuning:
    """The process-global tuning policy."""
    return _active


_Reading = TypeVar("_Reading")


def settle(
    read: Callable[[], _Reading],
    *,
    interval: Optional[float] = None,
    ceiling: Optional[float] = None,
    stable_reads: Optional[int] = None,
) -> _Reading:
    """Poll a surface until it stops changing, instead of sleeping a fixed guess. Calls ``read``
    repeatedly, ``interval`` apart, and returns as soon as it yields the same value ``stable_reads``
    times in a row (the surface has settled) or ``ceiling`` seconds have elapsed (a page that never
    quiesces — a spinner, an animation — costs the ceiling, not forever). ``read`` should return a
    cheap, comparable signature of the surface (an element count, a snapshot, a scroll offset).
    Interval, ceiling, and stable-read count default to the active policy's settlement knobs."""
    active = active_tuning()
    step = active.settle_interval() if interval is None else max(0.001, interval)
    limit = active.settle_ceiling() if ceiling is None else max(0.0, ceiling)
    needed = active.amount(Limit.SETTLE_STABLE_READS) if stable_reads is None else stable_reads
    deadline = time.monotonic() + limit
    latest = read()
    repeats = 1
    while time.monotonic() < deadline:
        time.sleep(step)
        current = read()
        repeats = repeats + 1 if current == latest else 1
        latest = current
        if repeats >= needed:
            break
    return latest
