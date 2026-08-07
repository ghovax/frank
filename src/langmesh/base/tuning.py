"""The one place every tool's size, count and timing limit is decided, and how each scales with the live context window."""

from __future__ import annotations

import contextvars
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Optional, TypeVar

# The live context window for the tool call currently executing, or 0 when it is not yet known.
current_context_window: contextvars.ContextVar[int] = contextvars.ContextVar(
    "current_context_window",
    default=0,
)


class WindowModel(NamedTuple):
    """The span of context windows the baselines are calibrated across, as one object because the four numbers only mean anything together."""

    #: The standard window of current flagship models, where every baseline equals its calibrated production value.
    reference: int
    #: Assumed before the live window is known, on the first call of a turn.
    turn_zero: int
    #: A small local or older model. Below this the derived caps stop being useful.
    minimum: int
    #: The largest generally-available window, clamped here rather than by a floor and ceiling on every derived value.
    maximum: int


WINDOW = WindowModel(reference=200_000, turn_zero=200_000, minimum=16_000, maximum=2_000_000)


class Family(NamedTuple):
    """How one scaling family turns a shipped default into a live value, stated here once."""

    #: Where the knob lives on the policy, as an attribute path. Empty for a family with no knob.
    knob: str
    #: The knob value at which this family's multiplier is exactly 1.0.
    calibrated: float
    #: Whether the value also scales with the live context window.
    follows_window: bool
    #: The smallest resolved value that still means something — one item, one millisecond.
    floor: float


class Scaling(Enum):
    """How a tunable's shipped default becomes its live value, each family answering to exactly one named knob."""

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


#: Where a tunable's long note lives, one markdown file per member and named for it.
@dataclass(frozen=True)
class Default:
    """One tunable's shipped value and how it scales; what it is for lives with the interface's words instead."""

    value: float
    scaling: Scaling


class Tunable(Enum):
    """Every value a user may tune, lowercase because each is a default the configuration may replace."""

    # Text budgets in tokens unless a character clip, scaled by the window and the text share, enforced by `clip_to_tokens`.
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
    # What a browser session keeps of the page's own traffic, budgeted like any other listing.
    web_exchanges = Default(250, Scaling.RESULTS)
    web_websockets = Default(32, Scaling.RESULTS)
    web_websocket_frames = Default(200, Scaling.RESULTS)

    # Timeouts, in milliseconds for Playwright and seconds for the rest, both scaling only with the timeout multiplier.
    action_timeout = Default(5.0, Scaling.TIME)
    navigation_timeout = Default(20.0, Scaling.TIME)
    snapshot_timeout = Default(10.0, Scaling.TIME)
    connect_timeout = Default(10.0, Scaling.TIME)
    # A person's reaction time rather than a network one: how long to wait for somebody to find Chrome's consent box.
    browser_authorization = Default(90.0, Scaling.TIME)
    drag_timeout = Default(8.0, Scaling.TIME)
    screenshot_timeout = Default(20.0, Scaling.TIME)
    read_text_timeout = Default(10.0, Scaling.TIME)
    # Resolving a frame id, kept far below the action timeout because a stale ref waits rather than erroring.
    frame_resolve_timeout = Default(2.0, Scaling.TIME)
    # After SIGTERM and before SIGKILL, for a cancelled command and a reaped session alike.
    sigterm_grace = Default(3.0, Scaling.TIME)
    ripgrep = Default(30.0, Scaling.TIME)
    # How long a backgroundable tool waits inline before handing the work to the background runner.
    bash_sync_window = Default(60.0, Scaling.TIME)
    slow_tool_sync_window = Default(10.0, Scaling.TIME)
    web_search_sync_window = Default(10.0, Scaling.TIME)
    accessibility_messaging = Default(2.0, Scaling.TIME)

    goal_continuation_turns = Default(12, Scaling.NONE)

    goal_blocked_turns = Default(3, Scaling.NONE)

    # The control plane and the processes it supervises.
    attach_snapshot_rows = Default(400, Scaling.NONE)
    session_title_attempts = Default(3, Scaling.NONE)
    permission_reviewer_attempts = Default(3, Scaling.NONE)
    compaction_attempts = Default(3, Scaling.NONE)
    session_idle_sleep = Default(18000.0, Scaling.TIME)
    daemon_startup = Default(45.0, Scaling.TIME)
    control_plane_call = Default(60.0, Scaling.TIME)
    model_catalogue_ttl = Default(60.0, Scaling.TIME)
    credential_refresh_leeway = Default(300.0, Scaling.TIME)
    daemon_probe_interval = Default(0.05, Scaling.TIME)
    daemon_probe_connect = Default(0.5, Scaling.TIME)
    oauth_poll_interval = Default(1.0, Scaling.TIME)
    oauth_poll_ceiling = Default(10.0, Scaling.TIME)
    oauth_poll_give_up = Default(300.0, Scaling.TIME)
    subscription_resume_ttl = Default(1_800.0, Scaling.TIME)
    model_silence_give_up = Default(180.0, Scaling.TIME)
    file_url_ttl = Default(600.0, Scaling.TIME)
    mcp_connect = Default(20.0, Scaling.TIME)
    card_resolve = Default(20.0, Scaling.TIME)

    # Commands on another machine, where patience is a property of the network.
    remote_command = Default(120.0, Scaling.TIME)
    remote_connect = Default(16.0, Scaling.TIME)
    remote_control_persist = Default(120.0, Scaling.TIME)

    # The `control_screen` timeout stack, ordered so a long script can never outlive the machinery watching it.
    control_script = Default(120.0, Scaling.TIME)
    surface_guard_margin = Default(30.0, Scaling.TIME)
    screencapture = Default(15.0, Scaling.TIME)
    open_url = Default(5.0, Scaling.TIME)

    # Fixed and deliberately unscaled: input-event pacing the OS needs, fixed shapes, and pixel sizes.
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
    click_interval = Default(0.01, Scaling.NONE)
    drag_step_interval = Default(0.01, Scaling.NONE)
    type_chunk_interval = Default(0.005, Scaling.NONE)
    focus_settle = Default(0.03, Scaling.NONE)
    # Pixels rather than a share of anybody's context, so raising the text share cannot enlarge a screenshot.
    stamped_image_side = Default(2_048, Scaling.NONE)
    accessibility_walk_budget = Default(3.0, Scaling.TIME)
    accessibility_ready_probe = Default(0.4, Scaling.TIME)
    accessibility_prewarm_interval = Default(0.4, Scaling.NONE)
    accessibility_ready_backoff = Default(0.2, Scaling.NONE)

    def __new__(cls, default: Default) -> "Tunable":
        """Give every member a value of its own, since `Enum` would otherwise alias members that share a baseline."""
        member = object.__new__(cls)
        member._value_ = len(cls.__members__) + 1
        return member

    def __init__(self, default: Default) -> None:
        self.default = default.value
        self.scaling = default.scaling


# Tokenizer-backed text budgeting, because a fixed characters-per-token ratio is wrong in both directions.
_ENCODING_NAME = "o200k_base"  # the current-generation general tokenizer; a good cross-model proxy

_encoding = None


def _bundled_vocabulary() -> None:
    """Point tiktoken at a frozen build's bundled vocabulary before it is imported, since the cache directory is read at fetch time."""
    import sys

    if not getattr(sys, "frozen", False) or "TIKTOKEN_CACHE_DIR" in os.environ:
        return
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "langmesh" / "tokenizer"
    if bundled.is_dir():
        os.environ["TIKTOKEN_CACHE_DIR"] = str(bundled)


def _get_encoding():
    """The encoding every budget here is measured with, raising rather than guessing when it cannot be loaded."""
    global _encoding
    if _encoding is None:
        _bundled_vocabulary()
        import tiktoken

        _encoding = tiktoken.get_encoding(_ENCODING_NAME)
    return _encoding


def count_tokens(text: str) -> int:
    """How many tokens `text` is, by the same encoding `clip_to_tokens` cuts on."""
    return len(_get_encoding().encode(text, disallowed_special=()))


def clip_to_tokens(text: str, budget: int) -> tuple[str, bool]:
    """Clip `text` to at most `budget` tokens on a real token boundary, reporting whether it was truncated."""
    budget = max(1, budget)
    encoding = _get_encoding()
    tokens = encoding.encode(text, disallowed_special=())
    if len(tokens) <= budget:
        return text, False
    return encoding.decode(tokens[:budget]), True


def tunable_names() -> tuple[str, ...]:
    """Every name that may appear under `tuning.defaults`, so a typo is an error at load rather than a silent no-op."""
    return tuple(member.name for member in Tunable)


def unknown_tunable_names(names: Iterable[str]) -> list[str]:
    known = set(tunable_names())
    return sorted(name for name in names if name not in known)


class _ContextShare(NamedTuple):
    """What proportion of the live context window one result may fill."""

    text: float = Scaling.TEXT.value.calibrated
    results: float = Scaling.RESULTS.value.calibrated


class TuningConfiguration:
    """The knob policy as a plain attribute holder, so `tuning` needs no import of the configuration module."""

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
        # Read only by the two screen surfaces, because settling is what a surface does after an action rather than a budget.
        self.settle_poll_seconds = settle_poll_seconds
        self.settle_give_up_seconds = settle_give_up_seconds


@dataclass
class Tuning:
    """Resolves the policy against a live context window, falling back to the turn-zero window when none is bound."""

    policy: TuningConfiguration

    def _window(self, window: Optional[int]) -> int:
        effective = current_context_window.get() if window is None else window
        if not effective or effective <= 0:
            effective = WINDOW.turn_zero
        return int(_clamp(effective, WINDOW.minimum, WINDOW.maximum))

    def _window_scale(self, window: Optional[int]) -> float:
        return self._window(window) / WINDOW.reference

    def _default_for(self, tunable: Tunable) -> float:
        """What this tunable ships at, or what the configuration replaced it with, still subject to family scaling."""
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
        """The live value before rounding, as one expression per family so a knob and its calibration cannot disagree."""
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
        """A limit as a bare fraction, kept apart from `duration` so a threshold is never read under a unit that is not its own."""
        return self._raw(tunable, None)

    def scale_timeout(self, seconds: float) -> float:
        """Apply the timeout knob to an IO ceiling, the one place a slow or fast machine is accounted for."""
        return max(0.1, seconds * self.policy.timeout_multiplier)

    # The settle interval and ceiling are the user's own knobs rather than scaled baselines.
    def settle_poll(self) -> float:
        return max(0.001, self.policy.settle_poll_seconds)

    def settle_give_up(self) -> float:
        return max(0.0, self.policy.settle_give_up_seconds)


# The active policy, bound per task, because one process may host several sessions each entitled to its own tuning.
_active: contextvars.ContextVar[Tuning] = contextvars.ContextVar(
    "langmesh_active_tuning", default=Tuning(TuningConfiguration())
)


def set_tuning(tuning: Tuning) -> None:
    """Bind the tuning policy for this task and everything it spawns."""
    _active.set(tuning)


def tuning_from_policy(policy: object, screen_policy: object = None) -> Tuning:
    """Wrap the loaded configuration sections into a `Tuning`, taking the settle knobs from the screen policy."""
    overrides = dict(getattr(policy, "defaults", None) or {})
    for name in unknown_tunable_names(overrides):
        overrides.pop(name, None)
    share = getattr(policy, "context_share", None)
    settle = getattr(screen_policy, "settle", None)
    return Tuning(
        TuningConfiguration(
            text=float(getattr(share, "text", Scaling.TEXT.value.calibrated)),
            results=float(getattr(share, "results", Scaling.RESULTS.value.calibrated)),
            timeout_multiplier=float(getattr(policy, "timeout_multiplier", 1.0)),
            defaults=overrides,
            settle_poll_seconds=float(getattr(settle, "poll_seconds", 0.05)),
            settle_give_up_seconds=float(getattr(settle, "give_up_seconds", 1.5)),
        )
    )


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
    """Poll a surface until it stops changing, returning once it reads the same value repeatedly or the ceiling elapses."""
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
