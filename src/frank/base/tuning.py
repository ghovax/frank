"""The one place every tool's size, count, and timing limit is decided."""
from __future__ import annotations

import contextvars
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Optional, TypeVar

# The live model context window (in tokens) for the tool call currently executing.
current_context_window: contextvars.ContextVar[int] = contextvars.ContextVar(
    "current_context_window", default=0,
)


class WindowModel(NamedTuple):
    """The span of context windows the baselines are calibrated across, in tokens."""

    #: The standard window of the current flagship chat models, and so the harness's usual case.
    reference: int
    #: Assumed before the live window is known — the first call of a turn, when nothing has come back to say how large the model's context is.
    turn_zero: int
    #: A small local or older model. Below this the derived caps stop being useful.
    minimum: int
    #: The largest generally-available window (Gemini-class).
    maximum: int


WINDOW = WindowModel(reference=200_000, turn_zero=200_000, minimum=16_000, maximum=2_000_000)


class Family(NamedTuple):
    """How one scaling family turns a shipped default into a live value."""

    #: Where the knob lives on the policy, as an attribute path. Empty for a family with no knob.
    knob: str
    #: The knob value at which this family's multiplier is exactly 1.0.
    calibrated: float
    #: Whether the value also scales with the live context window.
    follows_window: bool
    #: The smallest resolved value that still means something — one item, one millisecond.
    floor: float


class Scaling(Enum):
    """How a tunable's shipped default becomes its live value."""

    # a token or character budget: window * context_share.text
    TEXT = Family(knob="context_share.text", calibrated=0.25, follows_window=True, floor=1.0)
    # how many entries come back: window * context_share.results
    RESULTS = Family(knob="context_share.results", calibrated=0.15, follows_window=True, floor=1.0)
    # a wait: timeout_multiplier only — time does not depend on the window
    TIME = Family(knob="timeout_multiplier", calibrated=1.0, follows_window=False, floor=0.001)
    # physical pacing, fixed shapes, pixel sizes: not scaled at all
    NONE = Family(knob="", calibrated=1.0, follows_window=False, floor=0.0)


logger = logging.getLogger(__name__)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


#: Where a tunable's long note lives, one markdown file per member, named for the member.
@dataclass(frozen=True)
class Default:
    """One tunable's shipped value, and how it scales."""

    value: float
    scaling: Scaling


class Tunable(Enum):
    """Every value a user may tune, with what it ships at and how that ships-at value scales."""

    # Text budgets, in TOKENS unless a character clip, scaled by the window and context_share.text.
    output_tokens = Default(16_000, Scaling.TEXT)
    fetch_tokens = Default(24_000, Scaling.TEXT)
    maximum_line_chars = Default(2_048, Scaling.TEXT)
    upstream_error_detail_tokens = Default(256, Scaling.TEXT)

    # Listing budgets, in item COUNTS, scaled by the window and context_share.results.
    read_lines = Default(2_000, Scaling.RESULTS)
    grep_results = Default(512, Scaling.RESULTS)
    grep_per_file = Default(512, Scaling.RESULTS)
    glob_results = Default(1_000, Scaling.RESULTS)
    web_search_maximum = Default(10, Scaling.RESULTS)
    remote_listing = Default(32_768, Scaling.RESULTS)
    # What a browser session keeps of the page's own traffic, so a `find` can surface the API behind a rendered view.
    web_exchanges = Default(250, Scaling.RESULTS)
    web_websockets = Default(32, Scaling.RESULTS)
    web_websocket_frames = Default(200, Scaling.RESULTS)

    # Timeouts.
    action_timeout_ms = Default(5_000, Scaling.TIME)
    navigation_timeout_ms = Default(20_000, Scaling.TIME)
    snapshot_timeout_ms = Default(10_000, Scaling.TIME)
    connect_timeout_ms = Default(10_000, Scaling.TIME)
    # A person's reaction time, not a network one: Chrome shows a consent box when a debugging client attaches, and this is how long we wait for somebody to find it and click Allow.
    browser_authorization_ms = Default(90_000, Scaling.TIME)
    drag_timeout_ms = Default(8_000, Scaling.TIME)
    screenshot_timeout_ms = Default(20_000, Scaling.TIME)
    read_text_timeout_ms = Default(10_000, Scaling.TIME)
    # Resolving a frame id to its live frame.
    frame_resolve_timeout_ms = Default(2_000, Scaling.TIME)
    # After SIGTERM, before SIGKILL — for a cancelled command and for a reaped session alike.
    sigterm_grace_seconds = Default(3.0, Scaling.TIME)
    ripgrep_seconds = Default(30.0, Scaling.TIME)
    # How long a backgroundable tool waits inline before it hands the work to the background runner (a non-killing wait window, the model-overridable `timeout` tool parameter's default — NOT a network deadline).
    bash_sync_window_seconds = Default(60.0, Scaling.TIME)
    slow_tool_sync_window_seconds = Default(10.0, Scaling.TIME)
    web_search_sync_window_seconds = Default(10.0, Scaling.TIME)
    accessibility_messaging_seconds = Default(2.0, Scaling.TIME)

    goal_continuation_turns = Default(12, Scaling.NONE)

    goal_blocked_turns = Default(3, Scaling.NONE)

    # The control plane and the processes it supervises.
    warm_workers = Default(2, Scaling.NONE)
    session_title_attempts = Default(3, Scaling.NONE)
    permission_reviewer_attempts = Default(3, Scaling.NONE)
    prototype_start_seconds = Default(120.0, Scaling.TIME)
    prototype_restart_seconds = Default(5.0, Scaling.TIME)
    session_idle_sleep_seconds = Default(18000.0, Scaling.TIME)
    session_start_seconds = Default(60.0, Scaling.TIME)
    daemon_startup_seconds = Default(45.0, Scaling.TIME)
    control_plane_call_seconds = Default(60.0, Scaling.TIME)
    model_catalogue_ttl_seconds = Default(60.0, Scaling.TIME)
    credential_refresh_leeway_seconds = Default(300.0, Scaling.TIME)
    daemon_probe_interval_seconds = Default(0.05, Scaling.TIME)
    daemon_probe_connect_seconds = Default(0.5, Scaling.TIME)
    oauth_poll_interval_seconds = Default(1.0, Scaling.TIME)
    oauth_poll_ceiling_seconds = Default(10.0, Scaling.TIME)
    oauth_poll_give_up_seconds = Default(300.0, Scaling.TIME)
    subscription_resume_ttl_seconds = Default(1_800.0, Scaling.TIME)
    model_silence_give_up_seconds = Default(180.0, Scaling.TIME)
    file_url_ttl_seconds = Default(600.0, Scaling.TIME)
    mcp_connect_seconds = Default(20.0, Scaling.TIME)
    card_resolve_seconds = Default(20.0, Scaling.TIME)

    # Commands on another machine, where patience is a property of the network.
    remote_command_seconds = Default(120.0, Scaling.TIME)
    remote_connect_seconds = Default(16.0, Scaling.TIME)
    remote_control_persist_seconds = Default(120.0, Scaling.TIME)

    # The control_screen timeout stack, which has to stay ordered rather than merely equal.
    control_script_seconds = Default(120.0, Scaling.TIME)
    surface_guard_margin_seconds = Default(30.0, Scaling.TIME)
    screencapture_seconds = Default(15.0, Scaling.TIME)
    open_url_seconds = Default(5.0, Scaling.TIME)

    # Fixed, deliberately NOT scaled — physical input-event pacing the OS needs for a synthesized click/keystroke/drag to register, fixed shapes, and pixel sizes.
    type_chunk_size = Default(20, Scaling.NONE)
    drag_steps = Default(12, Scaling.NONE)
    scroll_amount_pixels = Default(300, Scaling.NONE)
    settle_stable_reads = Default(2, Scaling.NONE)
    find_rephrasing_similarity = Default(0.45, Scaling.NONE)
    find_near_weight = Default(0.5, Scaling.NONE)
    find_anchor_margin = Default(0.02, Scaling.NONE)
    find_candidates = Default(5, Scaling.RESULTS)
    find_one_margin = Default(0.20, Scaling.NONE)
    find_many_ceiling = Default(50, Scaling.RESULTS)
    find_relevance_floor = Default(0.25, Scaling.NONE)
    click_interval_seconds = Default(0.01, Scaling.NONE)
    drag_step_interval_seconds = Default(0.01, Scaling.NONE)
    type_chunk_interval_seconds = Default(0.005, Scaling.NONE)
    focus_settle_seconds = Default(0.03, Scaling.NONE)
    # Pixels, not a share of anybody's context — this sat in the text family, where raising `context_share.text` silently enlarged every screenshot.
    stamped_image_side = Default(2_048, Scaling.NONE)
    accessibility_walk_budget_seconds = Default(3.0, Scaling.TIME)
    accessibility_ready_probe_seconds = Default(0.4, Scaling.TIME)
    accessibility_prewarm_interval_seconds = Default(0.4, Scaling.NONE)
    accessibility_ready_backoff_seconds = Default(0.2, Scaling.NONE)

    def __new__(cls, default: Default) -> "Tunable":
        """Give every member a value of its own."""
        member = object.__new__(cls)
        member._value_ = len(cls.__members__) + 1
        return member

    def __init__(self, default: Default) -> None:
        self.default = default.value
        self.scaling = default.scaling


# Tokenizer-backed text budgeting.
_ENCODING_NAME = "o200k_base"     # the current-generation general tokenizer; a good cross-model proxy

_encoding = None


def _bundled_vocabulary() -> None:
    """Point tiktoken at the vocabulary carried in a frozen build, if this is one."""
    import sys

    if not getattr(sys, "frozen", False) or "TIKTOKEN_CACHE_DIR" in os.environ:
        return
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "frank" / "tokenizer"
    if bundled.is_dir():
        os.environ["TIKTOKEN_CACHE_DIR"] = str(bundled)


def _get_encoding():
    """The encoding every budget in this harness is measured with."""
    global _encoding
    if _encoding is None:
        _bundled_vocabulary()
        import tiktoken

        _encoding = tiktoken.get_encoding(_ENCODING_NAME)
    return _encoding


def count_tokens(text: str) -> int:
    """How many tokens ``text`` is, by the same encoding :func:`clip_to_tokens` cuts on."""
    return len(_get_encoding().encode(text, disallowed_special=()))


def clip_to_tokens(text: str, budget: int) -> tuple[str, bool]:
    """Clip ``text`` to at most ``budget`` tokens, returning (clipped_text, was_truncated)."""
    budget = max(1, budget)
    encoding = _get_encoding()
    tokens = encoding.encode(text, disallowed_special=())
    if len(tokens) <= budget:
        return text, False
    return encoding.decode(tokens[:budget]), True


def tunable_names() -> tuple[str, ...]:
    """Every name that may appear under ``tuning.defaults``."""
    return tuple(member.name for member in Tunable)


def unknown_tunable_names(names: Iterable[str]) -> list[str]:
    known = set(tunable_names())
    return sorted(name for name in names if name not in known)


class _ContextShare(NamedTuple):
    """What proportion of the live context window one result may fill."""

    text: float = Scaling.TEXT.value.calibrated
    results: float = Scaling.RESULTS.value.calibrated


class TuningConfiguration:
    """The knob policy, structurally compatible with the Pydantic model in ``configuration.py`` (which is what is actually loaded)."""

    def __init__(
        self,
        text: float = Scaling.TEXT.value.calibrated,
        results: float = Scaling.RESULTS.value.calibrated,
        timeout_multiplier: float = 1.0,
        defaults: Optional[dict] = None,
        settle_poll_seconds: float = 0.05,
        settle_give_up_seconds: float = 1.5,
    ) -> None:
        self.context_share = _ContextShare(text, results)
        self.timeout_multiplier = timeout_multiplier
        self.defaults = dict(defaults or {})
        # Read only by the two screen surfaces.
        self.settle_poll_seconds = settle_poll_seconds
        self.settle_give_up_seconds = settle_give_up_seconds


@dataclass
class Tuning:
    """Resolves the policy against a live context window. :meth:`amount` and :meth:`duration` take a :class:`Tunable` and an optional ``window``; when the window is omitted they read :data:`current_context_window` (the calling agent's live window), falling back to the turn-zero seed while that is still unknown — so a call site simply asks ``active_tuning().amount(Tunable.output_tokens)`` and gets the right value for whoever is running."""

    policy: TuningConfiguration

    def _window(self, window: Optional[int]) -> int:
        effective = current_context_window.get() if window is None else window
        if not effective or effective <= 0:
            effective = WINDOW.turn_zero
        return int(_clamp(effective, WINDOW.minimum, WINDOW.maximum))

    def _window_scale(self, window: Optional[int]) -> float:
        return self._window(window) / WINDOW.reference

    def _default_for(self, tunable: Tunable) -> float:
        """What this tunable ships at, or what the configuration replaced it with."""
        override = getattr(self.policy, "defaults", None)
        if override:
            value = override.get(tunable.name)
            if value is not None:
                return float(value)
        return float(tunable.default)

    def _knob(self, path: str) -> float:
        """The live value of a family's knob, read by the path the family names."""
        value: object = self.policy
        for step in path.split("."):
            value = getattr(value, step)
        return float(value)  # type: ignore[arg-type]

    def _raw(self, tunable: Tunable, window: Optional[int]) -> float:
        """The live value, before it is rounded to an int or returned as a float."""
        family = tunable.scaling.value
        value = self._default_for(tunable)
        if family.follows_window:
            value *= self._window_scale(window)
        if family.knob:
            value *= self._knob(family.knob) / family.calibrated
        return max(family.floor, value)

    def amount(self, tunable: Tunable, window: Optional[int] = None) -> int:
        """A limit as an integer — a token budget, an item count, a millisecond timeout, a length."""
        return max(1, int(round(self._raw(tunable, window))))

    def duration(self, tunable: Tunable, window: Optional[int] = None) -> float:
        """A limit as a float of seconds — a timeout or a physical input-pacing interval."""
        return self._raw(tunable, window)

    def ratio(self, tunable: Tunable) -> float:
        """A limit as a bare fraction — a margin or a share, in neither seconds nor items."""
        return self._raw(tunable, None)

    def scale_timeout(self, seconds: float) -> float:
        """Apply the timeout knob to a caller-supplied or baseline IO timeout — the one place a command/connect/subprocess ceiling is adjusted for a slow (or fast) machine or link."""
        return max(0.1, seconds * self.policy.timeout_multiplier)

    # Settlement interval and ceiling come straight from the policy (they are the user's knobs, not scaled baselines); the stable-read count is an unscaled tunable.
    def settle_poll(self) -> float:
        return max(0.001, self.policy.settle_poll_seconds)

    def settle_give_up(self) -> float:
        return max(0.0, self.policy.settle_give_up_seconds)


# The active policy, bound per task rather than per process.
_active: contextvars.ContextVar[Tuning] = contextvars.ContextVar(
    "frank_active_tuning", default=Tuning(TuningConfiguration())
)


def set_tuning(tuning: Tuning) -> None:
    """Bind the tuning policy for this task and everything it spawns."""
    _active.set(tuning)


def tuning_from_policy(policy: object, screen_policy: object = None) -> Tuning:
    """Wrap loaded config sections into a :class:`Tuning`."""
    overrides = dict(getattr(policy, "defaults", None) or {})
    for name in unknown_tunable_names(overrides):
        overrides.pop(name, None)
    share = getattr(policy, "context_share", None)
    settle = getattr(screen_policy, "settle", None)
    return Tuning(TuningConfiguration(
        text=float(getattr(share, "text", Scaling.TEXT.value.calibrated)),
        results=float(getattr(share, "results", Scaling.RESULTS.value.calibrated)),
        timeout_multiplier=float(getattr(policy, "timeout_multiplier", 1.0)),
        defaults=overrides,
        settle_poll_seconds=float(getattr(settle, "poll_seconds", 0.05)),
        settle_give_up_seconds=float(getattr(settle, "give_up_seconds", 1.5)),
    ))


def active_tuning() -> Tuning:
    """The tuning policy bound for this task, or the calibrated baseline."""
    return _active.get()


_Reading = TypeVar("_Reading")


def settle(
    read: Callable[[], _Reading],
    *,
    interval: Optional[float] = None,
    ceiling: Optional[float] = None,
    stable_reads: Optional[int] = None,
) -> _Reading:
    """Poll a surface until it stops changing, instead of sleeping a fixed guess."""
    active = active_tuning()
    step = active.settle_poll() if interval is None else max(0.001, interval)
    limit = active.settle_give_up() if ceiling is None else max(0.0, ceiling)
    needed = active.amount(Tunable.settle_stable_reads) if stable_reads is None else stable_reads
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
