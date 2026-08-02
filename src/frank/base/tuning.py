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
  ``timeout_multiplier`` knob (time does not depend on the context window). Settlement is not a fixed
  sleep at all: :func:`settle` polls a surface until it stops changing, bounded by the configured
  interval and ceiling.

Every tunable value is defined **exactly once** as a member of :class:`Tunable`, carrying its
baseline and how it scales — no parallel wall of module constants and one-line accessor methods.
Two typed getters resolve any of them against the live window: :meth:`Tuning.amount` (an integer:
tokens, counts, milliseconds, characters) and :meth:`Tuning.duration` (a float of seconds).

Delivery mirrors the rest of the harness, and neither half is process-global. The static
*policy* is bound per task by :func:`set_tuning` at startup and on every config reload, so a
process hosting more than one session gives each its own. The
dynamic *budget* — the live context window — is threaded per call through
:data:`current_context_window`, a context variable the running agent sets around each tool
execution; it is copied into worker threads by ``asyncio.to_thread``, so both the async tools and
the thread-affine automation surfaces read the calling agent's real window without any of them
having to grow a parameter for it. Concurrent calls from different agents each see their own.
"""
from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Optional, TypeVar

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
# 1.0, so raising ``context_share.text`` above 0.25 enlarges every text budget proportionally, and so on.
_DEFAULT_TEXT_SHARE = 0.25
_DEFAULT_RESULTS_SHARE = 0.15


logger = logging.getLogger(__name__)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class Scaling(Enum):
    """How a tunable's shipped default becomes its live value.

    Each family answers to exactly one knob, named the same thing here and in the configuration,
    so a reader can tell what a setting moves without consulting a table."""

    TEXT = "text"        # a token or character budget: window * context_share.text
    RESULTS = "results"  # how many entries come back: window * context_share.results
    TIME = "time"        # a wait: timeout_multiplier only — time does not depend on the window
    NONE = "none"        # physical pacing, fixed shapes, pixel sizes: not scaled at all


#: Where a tunable's long note lives, one markdown file per member, named for the member.
#:
#: Eleven of the seventy-four notes carry more than half the prose in this module between them,
#: and one of them ran to 1,777 characters spliced together out of adjacent string literals — a
#: form that cannot hold a paragraph break, a list, or a number somebody wants to scan for. The
#: short ones stay inline, where a reader scanning the enum can see what a value is for without
#: opening anything; the long ones live next door as markdown and are read on demand.
#:
#: The same shape as ``runtime/tools/descriptions/*.md``, and read with a loader of this module's
#: own rather than that package's: ``tuning`` deliberately does not import the configuration
#: module, and the twelve lines below are cheaper than the dependency would be.
NOTES_DIRECTORY = Path(__file__).resolve().parent / "tuning_notes"


def _note(name: str) -> str:
    """The markdown note for one tunable, or "" when it has none."""
    path = NOTES_DIRECTORY / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class Default:
    """One tunable's shipped value, how it scales, and what it is for.

    ``about`` is carried rather than left in a comment because `frank configure` renders it and the
    configuration reference is generated from it — an explanation only a reader can see is one that
    drifts from the value beside it.

    Leave ``about`` empty and the text is read from ``tuning_notes/<member>.md`` instead — see
    :data:`NOTES_DIRECTORY`. An inline note may be written as an ordinary triple-quoted string
    laid out to fit the file; its whitespace is collapsed here, so how it is *wrapped* in the
    source is a question about reading the code and never about what a user is shown."""

    value: float
    scaling: Scaling
    about: str = ""

    def __post_init__(self) -> None:
        collapsed = " ".join(self.about.split())
        if collapsed != self.about:
            object.__setattr__(self, "about", collapsed)


class Tunable(Enum):
    """Every value a user may tune, with what it ships at and how that ships-at value scales.

    Deliberately lowercase. These are not constants — each is a *default* the configuration may
    replace under ``tuning.defaults``, and the casing is the first thing that says so. The member
    name is the configuration key verbatim, so there is one vocabulary rather than two.

    Resolve one with :meth:`Tuning.amount` (integers: tokens, counts, milliseconds, characters) or
    :meth:`Tuning.duration` (seconds), which apply the scaling for its family."""

    # Text budgets, in TOKENS unless a character clip, scaled by the window and context_share.text.
    # As a share of a 200K window: 16K ≈ 8% for one command's output, 24K ≈ 12% for a whole fetched
    # page (the rest overflows to a file). Enforced by clip_to_tokens.
    output_tokens = Default(
        16_000, Scaling.TEXT,
        """Tokens of inline output one tool may return before the rest overflows to a file.""",
    )
    fetch_tokens = Default(24_000, Scaling.TEXT, "Tokens of a fetched web page's text kept inline.")
    maximum_line_chars = Default(
        2_048, Scaling.TEXT,
        """Characters of a single over-long line kept before it is clipped, so one minified blob
        cannot fill a result on its own.""",
    )
    upstream_error_detail_tokens = Default(
        256, Scaling.TEXT,
        """Tokens of an upstream service's error body kept in the failure this harness raises —
        enough to carry the provider's own explanation, short of pasting a whole page into a
        message someone has to read.""",
    )

    # Listing budgets, in item COUNTS, scaled by the window and context_share.results.
    read_lines = Default(
        2_000, Scaling.RESULTS,
        """Lines a file read returns when no explicit limit is given.""",
    )
    grep_results = Default(512, Scaling.RESULTS, "Total matches one search returns.")
    grep_per_file = Default(
        512, Scaling.RESULTS,
        """Matches one search returns from any single file.""",
    )
    glob_results = Default(1_000, Scaling.RESULTS, "Paths one glob returns.")
    web_search_maximum = Default(
        10, Scaling.RESULTS,
        """Ceiling on the result count a web search may ask for, however many it requests.""",
    )
    remote_listing = Default(
        32_768, Scaling.RESULTS,
        """Paths listed on a remote machine before glob matching is applied locally.""",
    )
    # What a browser session keeps of the page's own traffic, so a `find` can surface the API
    # behind a rendered view. Budgets like any other listing: a bigger window affords more.
    web_exchanges = Default(
        250, Scaling.RESULTS,
        """Recent request/response pairs a browser session keeps, so a search can surface the API
        behind a rendered view.""",
    )
    web_websockets = Default(
        32, Scaling.RESULTS,
        """Live websockets a browser session tracks at once.""",
    )
    web_websocket_frames = Default(200, Scaling.RESULTS, "Frames retained per tracked websocket.")

    # Timeouts. Milliseconds (read with amount) for Playwright, seconds (read with duration) for the
    # subprocess/AX/settle IO; both scale only with timeout_multiplier.
    action_timeout_ms = Default(
        5_000, Scaling.TIME,
        """How long one browser action (click, type, hover) waits for its element.""",
    )
    navigation_timeout_ms = Default(
        20_000, Scaling.TIME,
        """How long a page load or navigation waits.""",
    )
    snapshot_timeout_ms = Default(
        10_000, Scaling.TIME,
        """How long an accessibility snapshot of a page waits.""",
    )
    connect_timeout_ms = Default(10_000, Scaling.TIME, "How long attaching to a browser waits.")
    # A person's reaction time, not a network one: Chrome shows a consent box when a debugging
    # client attaches, and this is how long we wait for somebody to find it and click Allow. It
    # was ten seconds, budgeted as if the browser were the slow party, and anyone slower than
    # that was told their endpoint had gone stale and advised to toggle the switch — dismissing
    # the prompt they were on their way to approving.
    browser_authorization_ms = Default(
        90_000, Scaling.TIME,
        """How long attaching waits for the user to approve Chrome's prompt.""",
    )
    drag_timeout_ms = Default(8_000, Scaling.TIME, "How long a drag between two elements waits.")
    screenshot_timeout_ms = Default(
        20_000, Scaling.TIME,
        """How long capturing a page screenshot waits.""",
    )
    read_text_timeout_ms = Default(10_000, Scaling.TIME, "How long reading a page's text waits.")
    # Resolving a frame id to its live frame. Deliberately far below the action timeout: a stale
    # aria-ref does not error, it waits, and `frames()` resolves every iframe it found — so one that
    # has gone would otherwise hold up the whole listing.
    frame_resolve_timeout_ms = Default(2_000, Scaling.TIME)
    # After SIGTERM, before SIGKILL — for a cancelled command and for a reaped session alike.
    # `daemon/lifecycle.py` used to carry its own `_TERMINATE_GRACE_SECONDS = 3.0` for the second
    # case, which was the same concept under a second name, at a different value, and outside the
    # timeout scale. The more generous of the two won: a session being wound down has more to
    # flush than a single command being cancelled.
    sigterm_grace_seconds = Default(
        3.0, Scaling.TIME,
        """How long a cancelled command or a reaped session has after SIGTERM before SIGKILL.""",
    )
    ripgrep_seconds = Default(30.0, Scaling.TIME, "How long one content search may run.")
    # How long a backgroundable tool waits inline before it hands the work to the background
    # runner (a non-killing wait window, the model-overridable `timeout` tool parameter's
    # default — NOT a network deadline). Central so the three tools' defaults live in one place
    # rather than as scattered private module constants.
    bash_sync_window_seconds = Default(
        60.0, Scaling.TIME,
        """How long a shell command runs inline before it moves to the background. It is not killed
        at this point, only handed off, and the model can override it per call.""",
    )
    slow_tool_sync_window_seconds = Default(
        10.0, Scaling.TIME,
        """The same inline window for fetching a URL or downloading a file.""",
    )
    web_search_sync_window_seconds = Default(
        10.0, Scaling.TIME,
        """The same inline window for a web search.""",
    )
    ax_messaging_seconds = Default(
        2.0, Scaling.TIME,
        """How long one accessibility message to an application waits, so a hung application costs
        a moment rather than the whole action.""",
    )

    # The control plane and the processes it supervises.
    warm_workers = Default(2, Scaling.NONE)
    session_title_attempts = Default(
        3, Scaling.NONE,
        """How many times a session asks the model to name itself before giving up. More than
        one because the answer is a single tool call the model can fail to make, and a session
        with no name is one nobody can find again in a sidebar.""",
    )
    prototype_start_seconds = Default(120.0, Scaling.TIME)
    prototype_restart_seconds = Default(
        5.0, Scaling.TIME,
        """How long the daemon waits before trying again after the prototype failed to restart.""",
    )
    session_idle_sleep_seconds = Default(18000.0, Scaling.TIME)
    session_start_seconds = Default(
        60.0, Scaling.TIME,
        """How long the daemon waits for a forked session to bind its socket and report ready.""",
    )
    daemon_startup_seconds = Default(
        45.0, Scaling.TIME,
        """How long a command waits for a daemon it just started to become reachable.""",
    )
    control_plane_call_seconds = Default(
        60.0, Scaling.TIME,
        """How long one call to the daemon waits.""",
    )
    model_catalogue_ttl_seconds = Default(
        60.0, Scaling.TIME,
        """How long the list of available models is cached.""",
    )
    credential_refresh_leeway_seconds = Default(
        300.0, Scaling.TIME,
        """How far ahead of its expiry an access token is refreshed.""",
    )
    daemon_probe_interval_seconds = Default(
        0.05, Scaling.TIME,
        """Pause between asks of whether another process's daemon socket answers yet, or whether a
        daemon being replaced has finally exited.""",
    )
    daemon_probe_connect_seconds = Default(
        0.5, Scaling.TIME,
        """How long one connect to a daemon socket waits before it counts as unanswered.""",
    )
    oauth_poll_interval_seconds = Default(
        1.0, Scaling.TIME,
        """First pause between asks of whether a browser sign-in has completed; it widens from
        here.""",
    )
    oauth_poll_ceiling_seconds = Default(
        10.0, Scaling.TIME,
        """Ceiling on the widening pause between sign-in polls, so a slow sign-in is not asked
        about every second for minutes.""",
    )
    oauth_poll_give_up_seconds = Default(
        300.0, Scaling.TIME,
        """How long a browser sign-in is waited for before it is abandoned — a person's whole trip
        through a consent screen, not a network round trip.""",
    )
    subscription_resume_ttl_seconds = Default(
        1_800.0, Scaling.TIME,
        """How long a subscription provider's server-side conversation state stays worth resuming
        from before the whole conversation is resent instead.""",
    )
    model_silence_give_up_seconds = Default(
        180.0, Scaling.TIME,
        """How long a model may hold a stream open saying nothing at all before the turn is ended.
        Long, because a model weighing a hard problem is silent and still working.""",
    )
    file_url_ttl_seconds = Default(600.0, Scaling.TIME, "How long a signed file URL stays valid.")
    mcp_connect_seconds = Default(
        20.0, Scaling.TIME,
        """How long connecting to one MCP server waits.""",
    )
    card_resolve_seconds = Default(
        20.0, Scaling.TIME,
        """How long fetching a remote agent's card waits.""",
    )

    # Commands on another machine, where patience is a property of the network.
    remote_command_seconds = Default(
        120.0, Scaling.TIME,
        """How long a command on another machine may run.""",
    )
    remote_connect_seconds = Default(
        16.0, Scaling.TIME,
        """How long opening an SSH connection waits.""",
    )
    remote_control_persist_seconds = Default(
        120.0, Scaling.TIME,
        """How long a shared SSH connection lingers after its last use, so the next command reuses
        it.""",
    )

    # The control_screen timeout stack, which has to stay ordered rather than merely equal. The
    # script's own ceiling is the one anybody would want to raise; the surface's guard and its
    # worker thread each sit a margin above it, so a long script can never outlive the machinery
    # waiting on it — which used to drop the connection and leave the surface half-dead.
    control_script_seconds = Default(
        120.0, Scaling.TIME,
        """How long one screen-control script may run.""",
    )
    surface_guard_margin_seconds = Default(
        30.0, Scaling.TIME,
        """How far above the script's own limit the machinery waiting on it sits, so raising that
        limit can never make the guard fire first and leave the surface half-dead.""",
    )
    screencapture_seconds = Default(15.0, Scaling.TIME, "How long capturing the screen waits.")
    open_url_seconds = Default(
        5.0, Scaling.TIME,
        """How long handing a URL to the system browser waits.""",
    )

    # Fixed, deliberately NOT scaled — physical input-event pacing the OS needs for a synthesized
    # click/keystroke/drag to register, fixed shapes, and pixel sizes.
    type_chunk_size = Default(20, Scaling.NONE, "Characters sent per synthesized keyboard event.")
    drag_steps = Default(
        12, Scaling.NONE,
        """Segments a drag is split into, so it looks like a hand moved it.""",
    )
    scroll_amount_pixels = Default(
        300, Scaling.NONE,
        """Pixels one scroll step moves a native window.""",
    )
    settle_stable_reads = Default(
        2, Scaling.NONE,
        """Identical consecutive reads that count a surface as having stopped changing.""",
    )
    find_rephrasing_similarity = Default(0.45, Scaling.NONE)
    find_near_weight = Default(0.5, Scaling.NONE)
    find_anchor_margin = Default(0.02, Scaling.NONE)
    find_candidates = Default(
        5, Scaling.RESULTS,
        """Elements find_one weighs against its best match, and offers back when it cannot choose
        between them.""",
    )
    find_one_margin = Default(0.20, Scaling.NONE)
    find_many_ceiling = Default(50, Scaling.RESULTS)
    find_relevance_floor = Default(0.25, Scaling.NONE)
    click_interval_seconds = Default(
        0.01, Scaling.NONE,
        """Pause between successive synthesized clicks.""",
    )
    drag_step_interval_seconds = Default(
        0.01, Scaling.NONE,
        """Pause between the interpolated steps of a drag.""",
    )
    type_chunk_interval_seconds = Default(0.005, Scaling.NONE, "Pause between typed chunks.")
    focus_settle_seconds = Default(
        0.03, Scaling.NONE,
        """Pause after focusing a field, before typing into it.""",
    )
    # Pixels, not a share of anybody's context — this sat in the text family, where raising
    # `context_share.text` silently enlarged every screenshot.
    stamped_image_side = Default(
        2_048, Scaling.NONE,
        """Longest side, in pixels, of a screenshot annotated with element labels.""",
    )
    ax_walk_budget_seconds = Default(3.0, Scaling.TIME)
    ax_ready_probe_seconds = Default(
        0.4, Scaling.TIME,
        """How long the readiness poll may spend deciding whether an app's tree has built yet.
        Short on purpose: it runs repeatedly while an app is still starting, and it only has to
        see past the window chrome.""",
    )
    ax_prewarm_interval_seconds = Default(
        0.4, Scaling.NONE,
        """Pause between pre-warming the frontmost application's accessibility tree.""",
    )
    ax_ready_backoff_seconds = Default(
        0.2, Scaling.NONE,
        """Ceiling on the widening pause between accessibility readiness probes.""",
    )

    def __new__(cls, default: Default) -> "Tunable":
        """Give every member a value of its own.

        Without this, `Enum` treats two members declared with the same `(baseline, scaling)` pair
        as aliases of one member — and this enum is full of them, because plenty of unrelated
        limits happen to share a number. Sixteen of the fifty-five names below collapsed that way:
        `Tunable.connect_timeout_ms is Tunable.snapshot_timeout_ms` was true, and asking either for
        its `.name` returned whichever was declared first.

        It was harmless while every reader only wanted the number, since aliases agree on that by
        definition. It stopped being harmless when the configuration began keying overrides on the
        name: an override for one would have silently moved its unrelated twins, and a name that
        lost the race would have been rejected as unknown."""
        member = object.__new__(cls)
        member._value_ = len(cls.__members__) + 1
        return member

    def __init__(self, default: Default) -> None:
        self.default = default.value
        self.scaling = default.scaling
        self._about = default.about

    @property
    def about(self) -> str:
        """What this tunable is for: the inline note, or the markdown file named after it.

        Read on demand rather than at import. Only `frank configure` and the generated
        configuration reference ask for these, and a process that never renders a settings page
        should not pay for seventy-four file reads to start."""
        return self._about or _note(self.name)


# Tokenizer-backed text budgeting. A real tokenizer maps a token budget to an accurate character
# cut for any content; a fixed characters-per-token ratio (which the old code assumed) is wrong for
# code, whitespace runs, and non-Latin scripts.
#
# The fallback below looks like dead code and is not, which is worth stating because "tiktoken is a
# hard dependency, so the tokenizer is always there" is the obvious reading and it is wrong.
# `tiktoken` ships no vocabulary: `get_encoding` fetches `o200k_base` from
# `openaipublic.blob.core.windows.net` on first use and caches it. Checked rather than assumed —
# with an empty cache directory and the network refused, `get_encoding` raises. So the reachable
# case is a first run with no network, exactly as the comment here has always claimed.
#
# What the fallback must not be is quiet. Four characters per token is a guess that is roughly
# right for English prose and wrong in both directions for the content this harness actually
# budgets — dense for minified code, generous for CJK — so a session running on it is one where
# every cap means something other than what it says. It says so once, loudly, and then gets on
# with it. Ending the session instead would trade a degraded budget for no harness at all.
#
# The way to remove it is to make the vocabulary present rather than to delete the branch: ship the
# encoding with the bundle, or warm the cache at install. Until one of those is true, deleting this
# turns an offline first run into a crash on the first tool result.
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
        except Exception as error:
            _encoding = None
            # Once per process, and at warning level, because every token budget for the rest of
            # this session is now an estimate. Silently degrading is what makes a caps bug take a
            # day to find: the numbers all still look like numbers.
            logger.warning(
                "token budgeting fell back to %d characters per token: the %s encoding could not "
                "be loaded (%s: %s). tiktoken downloads its vocabulary on first use, so this is "
                "usually a first run with no network. Every size cap this session is an estimate "
                "until the process restarts with the vocabulary cached.",
                _FALLBACK_CHARS_PER_TOKEN, _ENCODING_NAME, type(error).__name__, error,
            )
    return _encoding


def count_tokens(text: str) -> int:
    """How many tokens ``text`` is, by the same encoding :func:`clip_to_tokens` cuts on.

    Its counterpart: clipping answers "what fits", this answers "how much is there", and a caller
    fitting several pieces into one budget needs both. Falls back to the same coarse ratio, so the
    two agree about a text's size whether or not the tokenizer loaded."""
    encoding = _get_encoding()
    if encoding is None:
        return (len(text) + _FALLBACK_CHARS_PER_TOKEN - 1) // _FALLBACK_CHARS_PER_TOKEN
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


def tunable_names() -> tuple[str, ...]:
    """Every name that may appear under ``tuning.defaults``. The configuration validates against
    this rather than accepting anything, so a typo is an error at load rather than a setting that
    looks applied and is not."""
    return tuple(member.name for member in Tunable)


def unknown_tunable_names(names: Iterable[str]) -> list[str]:
    known = set(tunable_names())
    return sorted(name for name in names if name not in known)


class _ContextShare(NamedTuple):
    """What proportion of the live context window one result may fill."""

    text: float = _DEFAULT_TEXT_SHARE
    results: float = _DEFAULT_RESULTS_SHARE


class TuningConfiguration:
    """The knob policy, structurally compatible with the Pydantic model in ``configuration.py``
    (which is what is actually loaded). Kept as a plain attribute holder here so ``tuning`` has no
    import dependency on the config module; :func:`set_tuning` accepts either."""

    def __init__(
        self,
        text: float = _DEFAULT_TEXT_SHARE,
        results: float = _DEFAULT_RESULTS_SHARE,
        timeout_multiplier: float = 1.0,
        defaults: Optional[dict] = None,
        settle_poll_seconds: float = 0.05,
        settle_give_up_seconds: float = 1.5,
    ) -> None:
        self.context_share = _ContextShare(text, results)
        self.timeout_multiplier = timeout_multiplier
        self.defaults = dict(defaults or {})
        # Read only by the two screen surfaces. They live on the resolved policy rather than in
        # `tuning:` because settling is what a *surface* does after an action, not a budget.
        self.settle_poll_seconds = settle_poll_seconds
        self.settle_give_up_seconds = settle_give_up_seconds


@dataclass
class Tuning:
    """Resolves the policy against a live context window. :meth:`amount` and :meth:`duration` take
    a :class:`Tunable` and an optional ``window``; when the window is omitted they read
    :data:`current_context_window` (the calling agent's live window), falling back to the turn-zero
    seed while that is still unknown — so a call site simply asks
    ``active_tuning().amount(Tunable.output_tokens)`` and gets the right value for whoever is running."""

    policy: TuningConfiguration

    def _window(self, window: Optional[int]) -> int:
        effective = current_context_window.get() if window is None else window
        if not effective or effective <= 0:
            effective = _TURN_ZERO_WINDOW
        return int(_clamp(effective, _MINIMUM_WINDOW, _MAXIMUM_WINDOW))

    def _window_scale(self, window: Optional[int]) -> float:
        return self._window(window) / REFERENCE_WINDOW

    def _default_for(self, tunable: Tunable) -> float:
        """What this tunable ships at, or what the configuration replaced it with.

        An override replaces the *default*, not the resolved value, so family scaling still applies
        on top: ``action_timeout_ms: 10000`` under ``timeout_multiplier: 2.0`` resolves to twenty
        seconds, which is what somebody reaching for both would expect."""
        override = getattr(self.policy, "defaults", None)
        if override:
            value = override.get(tunable.name)
            if value is not None:
                return float(value)
        return float(tunable.default)

    def _raw(self, tunable: Tunable, window: Optional[int]) -> float:
        """The live value, before it is rounded to an int or returned as a float."""
        shipped = self._default_for(tunable)
        scaling = tunable.scaling
        if scaling is Scaling.TEXT:
            knob = self.policy.context_share.text / _DEFAULT_TEXT_SHARE
            return max(1.0, shipped * self._window_scale(window) * knob)
        if scaling is Scaling.RESULTS:
            knob = self.policy.context_share.results / _DEFAULT_RESULTS_SHARE
            return max(1.0, shipped * self._window_scale(window) * knob)
        if scaling is Scaling.TIME:
            return max(0.001, shipped * self.policy.timeout_multiplier)
        return shipped

    def amount(self, tunable: Tunable, window: Optional[int] = None) -> int:
        """A limit as an integer — a token budget, an item count, a millisecond timeout, a length."""
        return max(1, int(round(self._raw(tunable, window))))

    def duration(self, tunable: Tunable, window: Optional[int] = None) -> float:
        """A limit as a float of seconds — a timeout or a physical input-pacing interval."""
        return self._raw(tunable, window)

    def ratio(self, tunable: Tunable) -> float:
        """A limit as a bare fraction — a margin or a share, in neither seconds nor items.

        Separate from `duration`, which returns the same number: a threshold read through a method
        called "duration" is a unit error waiting to be copied, and this file exists so that the
        name of a value says what the value is.

        No window argument, unlike the others: a fraction does not grow with a context window, and
        offering the parameter would invite somebody to pass one."""
        return self._raw(tunable, None)

    def scale_timeout(self, seconds: float) -> float:
        """Apply the timeout knob to a caller-supplied or baseline IO timeout — the one place a
        command/connect/subprocess ceiling is adjusted for a slow (or fast) machine or link."""
        return max(0.1, seconds * self.policy.timeout_multiplier)

    # Settlement interval and ceiling come straight from the policy (they are the user's knobs, not
    # scaled baselines); the stable-read count is an unscaled tunable.
    def settle_poll(self) -> float:
        return max(0.001, self.policy.settle_poll_seconds)

    def settle_give_up(self) -> float:
        return max(0.0, self.policy.settle_give_up_seconds)


# The active policy, bound per task rather than per process. A process may host more than one
# session — a worker running a compaction alongside a user's turn today, an embedder running
# several tomorrow — and each is entitled to its own tuning. The default is the calibrated
# baseline, so a tool invoked before anything binds one still behaves.
_active: contextvars.ContextVar[Tuning] = contextvars.ContextVar(
    "frank_active_tuning", default=Tuning(TuningConfiguration())
)


def set_tuning(tuning: Tuning) -> None:
    """Bind the tuning policy for this task and everything it spawns.

    Returns nothing on purpose: callers install a policy for the life of a session rather than
    scoping it, and the context variable's default covers anything that runs before they do."""
    _active.set(tuning)


def tuning_from_policy(policy: object, screen_policy: object = None) -> Tuning:
    """Wrap loaded config sections into a :class:`Tuning`.

    ``policy`` is the ``tuning`` section; ``screen_policy`` is ``computer_control``, which owns the
    two settle knobs — settling is what a *surface* does after an action, not a budget, and having
    them under `tuning` made them look like one. Missing attributes fall back to what the code
    ships with, so a partial policy built by hand never breaks.

    Unknown names under ``defaults`` are dropped here rather than carried: the configuration layer
    rejects them at load, so anything reaching this far was built in code."""
    overrides = dict(getattr(policy, "defaults", None) or {})
    for name in unknown_tunable_names(overrides):
        overrides.pop(name, None)
    share = getattr(policy, "context_share", None)
    settle = getattr(screen_policy, "settle", None)
    return Tuning(TuningConfiguration(
        text=float(getattr(share, "text", _DEFAULT_TEXT_SHARE)),
        results=float(getattr(share, "results", _DEFAULT_RESULTS_SHARE)),
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
    """Poll a surface until it stops changing, instead of sleeping a fixed guess. Calls ``read``
    repeatedly, ``interval`` apart, and returns as soon as it yields the same value ``stable_reads``
    times in a row (the surface has settled) or ``ceiling`` seconds have elapsed (a page that never
    quiesces — a spinner, an animation — costs the ceiling, not forever). ``read`` should return a
    cheap, comparable signature of the surface (an element count, a snapshot, a scroll offset).
    Interval, ceiling, and stable-read count default to the active policy's settlement knobs."""
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
