from __future__ import annotations

from collections.abc import Iterable
import json
import logging
import os
from frank.base import environment_variables
from frank.base.tuning import Scaling
import re
from fnmatch import fnmatch
import shlex
import shutil
import sys
from pathlib import Path
from typing import ClassVar, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from frank.base.paths import configuration_file_path, database_file_path  # noqa: F401 — re-exported
from frank.base.permission_mode import PermissionMode


logger = logging.getLogger(__name__)


# Where state lives is the placement layer's business, not this module's: it follows the
# XDG convention and is resolved in `frank.base.paths`.

# The packaged configuration lives in a sibling YAML file so editing the template
# is a data change, not a code change. Used to seed the XDG configuration file
# on first run and as the base when persisting settings before any file exists.
PACKAGED_CONFIGURATION_PATH = Path(__file__).resolve().parent / "configuration.yaml"


def packaged_configuration_yaml() -> str:
    return PACKAGED_CONFIGURATION_PATH.read_text()


def _bundled_dotagents_root() -> Path:
    """The ``.agents`` directory shipped with the harness itself. The bundled
    agents under it are always available as a base layer, so every working
    directory sees at least the shipped profiles even when it has no ``.agents``
    of its own. Located by walking up from this file to the nearest ancestor
    that contains ``.agents/agents`` (an editable install points back at the
    source tree); falls back to the expected ``src/frank/core -> repo root``
    layout when the directory is absent, so a build that ships the agents
    elsewhere contributes nothing rather than erroring.

    In the frozen desktop app the package tree lives inside PyInstaller's
    ``_MEIPASS`` bundle root, where the walk from ``__file__`` never reaches the
    repo layout — so the bundle root (where the spec places ``.agents``) is
    checked first."""
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", sys.executable))
        if (bundle_root / ".agents" / "agents").is_dir():
            return bundle_root / ".agents"
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / ".agents" / "agents").is_dir():
            return candidate / ".agents"
    return here.parents[2] / ".agents"


BUNDLED_DOTAGENTS_ROOT = _bundled_dotagents_root()


def seed_home_agents() -> list[str]:
    """Non-destructively seed the home layer (``~/.agents``) with editable copies of the
    server-shipped agents and skills, filling in ONLY what is missing so a user's own
    edits are never overwritten.

    The bundled base under :data:`BUNDLED_DOTAGENTS_ROOT` is read-only (inside the frozen
    app), so a profile that exists only there cannot have its settings persisted — the
    settings UI would try to write into the read-only bundle. Seeding gives every shipped
    profile a writable home copy that overrides the base, which is what lets per-agent
    model choices (and any other edit) actually save. Returns the relative paths seeded,
    for logging. A no-op when the bundle ships no ``.agents``."""
    home_root = Path(Configuration.HOME_AGENTS_ROOT_DIRECTORY).expanduser()
    seeded: list[str] = []
    for kind in ("agents", "skills"):
        source_root = BUNDLED_DOTAGENTS_ROOT / kind
        if not source_root.is_dir():
            continue
        target_root = home_root / kind
        target_root.mkdir(parents=True, exist_ok=True)
        for entry in sorted(source_root.iterdir()):
            if entry.name.startswith("."):  # skip .DS_Store and other dotfiles
                continue
            target = target_root / entry.name
            if target.exists():
                continue  # a home copy already exists (possibly user-edited) — leave it
            try:
                if entry.is_dir():
                    shutil.copytree(entry, target)
                else:
                    shutil.copy2(entry, target)
                seeded.append(f"{kind}/{entry.name}")
            except OSError:
                # A single unseedable profile must never block startup or the others.
                continue
    return seeded


def save_api_keys(
    *,
    exa_api_key: str | None = None,
    composio_api_key: str | None = None,
    jina_api_key: str | None = None,
    firecrawl_api_key: str | None = None,
    web_fetch_proxy_url: str | None = None,
    permission_mode: str | None = None,
    sandbox: dict | None = None,
    worktree_strategy: str | None = None,
    compaction: dict | None = None,
    user_context_enabled: bool | None = None,
    computer_control_enabled: bool | None = None,
    dictation_enabled: bool | None = None,
    tuning: dict | None = None,
    daemon: dict | None = None,
    provider_keys: dict[str, str] | None = None,
    provider_base_urls: dict[str, str] | None = None,
) -> None:
    """Persist settings into the XDG configuration file, preserving the rest
    of the file. Only provided values are written. Creates the file from the
    default template if it does not exist yet."""
    path = configuration_file_path()
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    else:
        data = yaml.safe_load(packaged_configuration_yaml())
    if exa_api_key is not None:
        data.setdefault("exa", {})["api_key"] = exa_api_key
    if composio_api_key is not None:
        data.setdefault("composio", {})["api_key"] = composio_api_key
    if jina_api_key is not None:
        data.setdefault("jina", {})["api_key"] = jina_api_key
    if firecrawl_api_key is not None:
        data.setdefault("firecrawl", {})["api_key"] = firecrawl_api_key
    if web_fetch_proxy_url is not None:
        data.setdefault("web_fetch", {})["proxy_url"] = web_fetch_proxy_url
    if sandbox is not None:
        data.setdefault("sandbox", {}).update(sandbox)
    if worktree_strategy is not None:
        data.setdefault("workspace", {})["strategy"] = worktree_strategy
    if compaction is not None:
        data.setdefault("compaction", {}).update(compaction)
    if tuning is not None:
        data.setdefault("tuning", {}).update(tuning)
    if daemon is not None:
        data.setdefault("daemon", {}).update(daemon)
    if user_context_enabled is not None:
        data.setdefault("user_context", {})["enabled"] = user_context_enabled
    if computer_control_enabled is not None:
        data.setdefault("computer_control", {})["enabled"] = computer_control_enabled
    if dictation_enabled is not None:
        data.setdefault("dictation", {})["enabled"] = dictation_enabled
    if provider_keys is not None or provider_base_urls is not None:
        providers_section = data.setdefault("providers", {})
        all_provider_ids = {*(provider_keys or {}), *(provider_base_urls or {})}
        for provider_id in all_provider_ids:
            entry = dict(providers_section.get(provider_id) or {})
            if provider_keys is not None and provider_id in provider_keys:
                entry["api_key"] = provider_keys[provider_id]
            if provider_base_urls is not None and provider_id in provider_base_urls:
                entry["base_url"] = provider_base_urls[provider_id]
            providers_section[provider_id] = entry
    if permission_mode is not None:
        data.setdefault("agent", {})["permission_mode"] = permission_mode
    path.write_text(yaml.safe_dump(data, sort_keys=False))


class Section(BaseModel):
    """A part of the configuration file.

    Every section refuses a key it does not define. Without that, a typo — or a name that used
    to work and no longer does — is accepted, written back, listed by `frank configure`, and
    quietly does nothing, because a model ignores what it does not recognise. A setting that
    cannot take effect should say so at the moment it is written, not months later when
    somebody notices the behaviour never changed.

    The two exceptions are :class:`MCPServerConfiguration` and
    :class:`RemoteAgentServerConfiguration`, which parse files this project does not own."""

    model_config = {"extra": "forbid"}


class ExaConfiguration(Section):
    """Exa — the web-search tool's backend."""

    api_key: str = Field("", description="Exa API key. The EXA_API_KEY environment variable wins over this.")

    @property
    def effective_api_key(self) -> str:
        return os.environ.get(environment_variables.EXA_API_KEY) or self.api_key


class JinaConfiguration(Section):
    """Jina Reader — the web-fetch tool's default engine. It works without a key at a lower
    rate limit, so this is optional."""

    api_key: str = Field("", description="Jina API key, which raises the rate limit. JINA_API_KEY wins over this.")

    @property
    def effective_api_key(self) -> str:
        return os.environ.get(environment_variables.JINA_API_KEY) or self.api_key


class FirecrawlConfiguration(Section):
    """Firecrawl — the web-fetch tool's fallback engine, for pages Jina returns thin or
    blocked. Without a key the fallback is simply skipped."""

    api_key: str = Field("", description="Firecrawl API key. FIRECRAWL_API_KEY wins over this.")
    api_url: str = Field(
        "", description="A self-hosted Firecrawl instance to use instead of the hosted API."
    )

    @property
    def effective_api_key(self) -> str:
        return os.environ.get(environment_variables.FIRECRAWL_API_KEY) or self.api_key

    @property
    def effective_api_url(self) -> str:
        return os.environ.get(environment_variables.FIRECRAWL_API_URL) or self.api_url


class WebFetchConfiguration(Section):
    """Fetching a page directly, for sites that refuse the reader engines."""

    proxy_url: str = Field(
        "",
        description=(
            "Route direct fetches and file downloads through an HTTP or SOCKS proxy, for sites "
            "that block by address. Empty fetches directly. Credentials may be embedded as "
            "http://user:pass@host:port."
        ),
    )

    @property
    def effective_proxy_url(self) -> str:
        return os.environ.get(environment_variables.FRANK_FETCH_PROXY) or self.proxy_url


class FilesystemConfiguration(Section):
    """Which paths a tool's child process may read and write.

    The system is readable and is not listed here — `/usr` and `/etc` are not secrets, and denying
    them breaks every command while protecting nothing. What these lists govern is the user's own
    home, which is closed by default: ``readable`` is the allowlist that keeps toolchains working,
    and ``deny`` wins over it. The defaults are chosen to protect what no toolchain touches and to
    leave alone what every toolchain needs, so credential directories stay readable — breaking
    `git push` to protect a key is a trade made by someone who does not have to use the result."""

    readable: list[str] = Field(
        default=[
            # Where a person's own agents, skills and workflows live. Not a convenience: a
            # `control_screen` script imports its workflows and its skills' script packages from
            # here, and without the read those imports fail inside the sandbox with
            # `PermissionError: Operation not permitted` — which names no file and reads as a
            # broken tool rather than a missing permission. Everything under it is the person's
            # own instructions to the harness, which the harness is the intended reader of.
            "~/.agents",
            "~/.config", "~/.local", "~/.ssh", "~/.gitconfig", "~/.gitignore_global",
            "~/.cargo", "~/.rustup", "~/.npmrc", "~/.nvm", "~/.pyenv", "~/.docker", "~/.netrc",
        ],
        description="Paths under your home a tool child may read. The system is readable and is not listed.",
    )
    writable: list[str] = Field(
        default=["$WORKSPACE", "$TMPDIR", "/tmp", "$XDG_CACHE_HOME", "~/.cache"],
        description=(
            "Paths a tool child may write. Deliberately narrower than readable, because a wrong "
            "write is the failure people actually meet. $WORKSPACE is the session's own directory."
        ),
    )
    # `/tmp` is listed beside `$TMPDIR` because on macOS they are not the same place. `$TMPDIR`
    # expands to a per-user directory under `/var/folders`, so a session whose writable set named
    # only `$TMPDIR` was refused at `/tmp` — the one scratch path every convention points at, and
    # the first one anything reaches for. What came back was `Operation not permitted`, naming no
    # path, which reads as a broken tool rather than a boundary. Nothing of the user's lives in
    # `/tmp`; it is world-writable already, and cleared by the system.
    grantable: list[str] = Field(
        default=[],
        description=(
            "Paths an agent may be granted at runtime without asking a person. Empty means every "
            "request is asked about. Paths under deny are never grantable, whatever is listed here."
        ),
    )
    deny: list[str] = Field(
        default=[
            "~/Documents", "~/Desktop", "~/Downloads", "~/Pictures", "~/Movies", "~/Music",
            "~/Library/Mail", "~/Library/Messages", "~/Library/Safari",
        ],
        description="Paths refused outright. Wins over readable and writable.",
    )


class SandboxConfiguration(Section):
    """A session's confinement, enforced by the operating system rather than inferred from the
    text of a command. Every field but the two path/network ones is POSIX under its own name:
    ``limits`` are ``setrlimit(2)`` constants taking the integers that call takes, ``umask`` is
    ``umask(2)``, ``nice`` is ``nice(2)``.

    ``enforce`` decides what happens where no backend can enforce this. ``required`` refuses to
    create the session and says what is missing, which is the direct answer to how this setting
    came to exist: a key that claimed to confine and silently did not."""

    enforce: Literal["required", "preferred", "off"] = Field(
        "required",
        description=(
            "What to do where no backend can enforce this. required refuses to create the "
            "session and says what is missing; preferred runs with resource limits only and says "
            "so; off does not confine at all."
        ),
    )
    filesystem: FilesystemConfiguration = Field(
        default_factory=FilesystemConfiguration,
        description="Which paths a tool child may read and write.",
    )
    network: bool = Field(True, description="Whether a tool child may reach the network at all.")
    limits: dict[str, int] = Field(
        default={
            "RLIMIT_CORE": 0,
            "RLIMIT_FSIZE": 8 * 1024 * 1024 * 1024,
            "RLIMIT_NPROC": 2048,
        },
        description="POSIX resource limits, by their setrlimit(2) names and in that call's own units.",
    )
    umask: Optional[str] = Field(
        None,
        description='Octal umask for tool children, as a string such as "0077". Null leaves it alone.',
    )
    nice: int = Field(0, description="Scheduling priority for tool children, as nice(2) takes it.")

    def to_profile(self):
        """This configuration as the :class:`frank.base.confinement.Profile` the spawn path
        applies. Imported inside the method because confinement reads this module's types back."""
        from frank.base import confinement

        return confinement.Profile(
            filesystem=confinement.Filesystem(
                readable=tuple(self.filesystem.readable),
                writable=tuple(self.filesystem.writable),
                deny=tuple(self.filesystem.deny),
                grantable=tuple(self.filesystem.grantable),
            ),
            network=self.network,
            limits={name: int(value) for name, value in self.limits.items()},
            umask=int(self.umask, 8) if self.umask else None,
            nice=self.nice,
            enforce=self.enforce,
        )


class WorkspaceConfiguration(Section):
    """Where a session's tools actually run."""

    strategy: Literal["none", "branch", "worktree"] = Field(
        "none",
        description=(
            "none runs in the project directory itself; branch gives each session its own "
            "branch; worktree gives each session its own git worktree, so parallel sessions "
            "never tread on each other."
        ),
    )


#: Compaction settings that were renamed, and the name that now carries their value. A section
#: refuses keys it does not define, on purpose — so without this a rename does not merely ignore
#: an old setting, it stops the whole configuration file from loading, and the daemon with it.
_COMPACTION_RENAMED = {
    "auto": "automatic",
    "observer_context_fraction": "reclaim_at_fraction",
    "reflector_observation_fraction": "condense_log_at_fraction",
}

#: Compaction settings that are gone with nothing standing in for them. `keep_recent_turns`
#: counted user turns, which is the measure this section stopped using — an unattended run is one
#: turn and hundreds of tool results — and the pruning pass beside it was removed once it was
#: shown to be unreachable. Dropped rather than translated, and said out loud rather than
#: silently, because a setting that can no longer take effect should be reported, not obeyed.
_COMPACTION_REMOVED = (
    "keep_recent_turns", "preserve_recent_tokens", "prune", "prune_tool_results",
    "pruned_result_tokens",
)


class CompactionConfiguration(Section):
    """Conversation memory management, modelled on Mastra's Observational Memory: as raw
    history grows, an Observer folds older turns into a dense, timestamped observation
    log kept at the front of the context; a Reflector condenses that log when it grows
    large. Fractions are of the model's context window.

    ``automatic`` is on by default: a self-managing turn runs unbounded, so its context must be
    kept within the window on its own rather than relying on a tool-call ceiling to stop it
    first. Manual (button-triggered) compaction always works regardless of it.

    Every fraction here is of the window a conversation may actually occupy — the model's window
    less the room reserved for the answer — rather than of the raw number, so "85% full" is 85%
    of what is really available to fill."""

    @model_validator(mode="before")
    @classmethod
    def _carry_old_names(cls, values):
        if not isinstance(values, dict):
            return values
        carried = dict(values)
        for old, new in _COMPACTION_RENAMED.items():
            if old in carried:
                # An explicit new name wins: somebody who wrote both meant the current one.
                carried.setdefault(new, carried[old])
                carried.pop(old)
        for gone in _COMPACTION_REMOVED:
            if carried.pop(gone, None) is not None:
                logger.warning(
                    "ignoring compaction.%s: the setting no longer exists. Remove it from your "
                    "configuration file.", gone,
                )
        return carried

    automatic: bool = Field(
        True, description="Reclaim context on its own as it fills. Manual compaction works either way."
    )
    reclaim_at_fraction: float = Field(
        0.85,
        description=(
            "Fold once the conversation passes this share of the usable window. Late on purpose: "
            "a fold is the one thing that rewrites the prefix a provider had cached, so the "
            "fewer of them the better."
        ),
    )
    condense_log_at_fraction: float = Field(
        0.3, description="Condense the observation log once the log itself passes this share."
    )
    output_reserve_fraction: float = Field(
        0.1,
        description=(
            "Share of the window held back for the answer, so folding still has room to run. "
            "The rest is the usable window every other fraction here is measured against."
        ),
    )
    recent_working_set_fraction: float = Field(
        0.25,
        description=(
            "Share of the usable window kept verbatim rather than folded into the log. Sized "
            "in tokens rather than turns because an unattended run is one turn and hundreds "
            "of tool results."
        ),
    )
    verbatim_user_fraction: float = Field(
        0.1,
        description=(
            "Share of the usable window that carries the user's own messages through a fold, "
            "word for word. Their instructions are the specification, and a paraphrase of a "
            "specification is a different specification."
        ),
    )


class ContextShareConfiguration(Section):
    """What proportion of the live context window one result may fill.

    The defaults are read from the scaling families rather than written again here. They are the
    values every baseline in `tuning` is calibrated against — at exactly these fractions each
    family's multiplier is 1.0 — so a copy typed out in this file is a copy that can disagree with
    the arithmetic that assumes it, silently and in a direction nobody would think to check."""

    text: float = Field(
        Scaling.TEXT.value.calibrated,
        description="Share one result's text may fill — output, fetched pages.",
    )
    results: float = Field(
        Scaling.RESULTS.value.calibrated,
        description="Share a set of results may fill — matches, lines, records.",
    )


class TuningConfiguration(Section):
    """How large, how many, and how patient the tools are — the single home for what used to be
    dozens of scattered constants. Budgets are derived from the *live* model context window, so a
    small model gets tight caps and a large one gets room; `context_share` says how much of that
    window one result may take, and `timeout_multiplier` stretches every wait."""

    context_share: ContextShareConfiguration = Field(
        default_factory=ContextShareConfiguration,
        description="What proportion of the live context window one result may fill.",
    )
    timeout_multiplier: float = Field(
        1.0, description="Multiplier on every wait. 2.0 doubles them for a slow machine; 1.0 is neutral."
    )
    defaults: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Override one tunable by its own name, in its own unit — see `frank configure --all`. "
            "An override replaces the shipped default, so context_share and timeout_multiplier "
            "still apply on top."
        ),
    )

    @field_validator("defaults")
    @classmethod
    def _known_defaults(cls, value: dict[str, float]) -> dict[str, float]:
        from frank.base.tuning import unknown_tunable_names

        unknown = unknown_tunable_names(value)
        if unknown:
            raise ValueError(
                f"unknown tuning default(s): {', '.join(unknown)}. "
                "The names that exist are the members of `frank.base.tuning.Tunable`; "
                "`frank configure --all` lists them with their defaults."
            )
        return value


class UserContextConfiguration(Section):
    """Opt-in enrichment of the system prompt with a snapshot of *how the user works on
    this machine* — their most-visited directories, the shape of their home folder, files
    they have touched recently, the applications they have installed and are running, and
    the websites they visit most. It gives the agent strong, immediate pointers to the
    user's world so it aligns with their habits from the first turn.

    Off by default and deliberately so: this is more personal than the neutral system
    probe. Everything it reads is local metadata (filesystem timestamps, app bundles,
    browser-history visit counts) and it never leaves the machine except into the model
    prompt the user is already sending. Full URLs are reduced to hostnames and only
    counts are kept, so browsing content is never included. Enable it only when the
    behavioral boost is worth surfacing this data to the model."""

    enabled: bool = Field(
        False,
        description=(
            "Put a snapshot of how you work — frequent folders, applications, sites — in the "
            "system prompt. Off by default: it puts personally identifying information in front "
            "of your model provider."
        ),
    )


class SettleConfiguration(Section):
    """After an action, how long to wait for a surface to stop changing before reading it. Polling
    rather than a fixed sleep, so a fast page costs one interval and a slow one costs the ceiling."""

    poll_seconds: float = Field(0.05, description="How often to re-check whether the surface has settled.")
    give_up_seconds: float = Field(1.5, description="The longest to wait before reading it anyway.")


class RetrievalConfiguration(Section):
    """How a screen is ranked when a script asks for an element by name.

    Two static embedding models score every element against the query, and a character similarity
    scores it a third time; the three are standardised and added. The defaults are fitted, not
    guessed — 108,710 labelled queries over 50 recorded windows and pages — but they are settings
    rather than facts, because the right ones depend on what the queries look like, and that is a
    property of the work rather than of the harness.

    The models are named for what they read. A multilingual one handles a desktop whose labels are
    not in English; an English one is markedly better at a query that describes a purpose instead
    of quoting a label (27.4% against 20.7% top-1 on queries written that way). Neither replaces
    the other — used alone the English model is 4.3 points worse on native windows — so both run,
    and either can be turned off by clearing its name."""

    multilingual_rank_model: str = Field(
        "minishlab/M2V_multilingual_output",
        description=(
            "The static embedding that ranks by meaning across languages. Also the model whose "
            "plain cosine backs the relevance floor, so clearing it disables that floor. Empty "
            "turns it off."
        ),
    )
    english_rank_model: str = Field(
        "minishlab/potion-base-32M",
        description=(
            "A second static embedding, ranked alongside the first rather than instead of it. "
            "Stronger on queries that describe what an element is for; weaker on exact labels. "
            "Empty turns it off."
        ),
    )
    lexical_gate_short_words: int = Field(
        3, ge=0,
        description=(
            "Queries of this many words or fewer are treated as a label quoted off the screen, "
            "and the character similarity counts in full. This is what makes a retyped number, a "
            "changed case or a truncated label still find its element."
        ),
    )
    lexical_gate_long_words: int = Field(
        7, ge=1,
        description=(
            "Queries of this many words or more are treated as a description of a purpose, and "
            "the character similarity is ignored — its spelling agrees with nothing, so it ranks "
            "by coincidence. Between the two bounds it fades out linearly. Raising this makes "
            "long queries behave more like short ones."
        ),
    )


class ComputerControlConfiguration(Section):
    """Opt-in ability for the agent to control macOS apps through the `computer` tool —
    reading the accessibility tree and clicking/typing/navigating. Off by default because
    it lets the agent drive the whole machine; enabling it also requires the user to grant
    Accessibility access in System Settings."""

    enabled: bool = Field(False, description="Let the agent drive native macOS apps and your Chrome.")
    settle: SettleConfiguration = Field(
        default_factory=SettleConfiguration,
        description="How long a screen surface is given to stop changing after an action.",
    )
    retrieval: RetrievalConfiguration = Field(
        default_factory=RetrievalConfiguration,
        description="Which models rank a screen, and when a query's spelling stops being evidence.",
    )


class DictationTimingConfiguration(Section):
    """How long dictation waits, and how hard it tries, before it gives up and says so.

    Separated from the rest of the section because these are the numbers a slow machine needs to
    move, and nothing else about dictation is.

    There is deliberately no limit on *loading*. It takes as long as it takes — the first one
    fetches about a gigabyte over a connection nobody can predict — so any number would
    eventually declare a working download a failure. Loading is reported as a state instead, and
    the microphone button shows it arriving. The sample rate is not here either: 16 kHz mono is
    what the model takes, so it is a fact rather than a preference, and a setting for it would
    only be a way to make transcription quietly wrong."""

    minimum_transcription_timeout_seconds: float = Field(
        30.0,
        description="The floor on how long one transcription may take before it is treated as wedged.",
    )
    transcription_timeout_realtime_multiplier: float = Field(
        0.5,
        description=(
            "Added to the floor, per second of audio. Transcription runs comfortably faster "
            "than real time on Apple Silicon, so reaching the limit means stuck, not busy."
        ),
    )
    maximum_attempts: int = Field(
        2,
        ge=1,
        description=(
            "How many workers one recording may be given. A hung or crashed worker is replaced "
            "and the same audio submitted again, because asking somebody to say it twice is the "
            "worst answer available; beyond this it is a real fault and is reported."
        ),
    )
    worker_shutdown_seconds: float = Field(
        2.0, description="How long a worker is given to exit on its own before it is killed."
    )


class DictationConfiguration(Section):
    """Opt-in speech-to-text for the composer, transcribed on this machine.

    Off by default, and the default is the interesting part: nothing here talks to a network
    service, but the first use downloads about a gigabyte of model weights, and a feature that
    quietly spends a person's bandwidth the first time they touch a microphone button is a
    feature that decided something on their behalf. Enabling it is that decision, made once.

    The weights land in the standard Hugging Face cache (``~/.cache/huggingface``), shared with
    every other tool on the machine that uses it — so turning this on twice, or reinstalling,
    does not download them twice."""

    enabled: bool = Field(False, description="Let the composer take dictation, transcribed on this Mac.")
    model: str = Field(
        "mlx-community/parakeet-tdt-0.6b-v3",
        description="The speech-recognition model to transcribe with, from Hugging Face.",
    )
    timing: DictationTimingConfiguration = Field(
        default_factory=DictationTimingConfiguration,
        description="How long dictation waits before it gives up.",
    )


class ComposioConfiguration(Section):
    """Composio integration via its hosted MCP endpoint. When enabled, the harness
    points at Composio's "connect" MCP URL and exposes it as a normal
    streamable_http MCP server, so Composio's tools flow through the same
    list_mcp_tools/call_mcp_tool path as any other MCP server — no new agent, no
    SDK provisioning. Which toolkits (gmail, notion, …) are available is decided
    by the MCP server you configure in the Composio dashboard; the agent then
    discovers tools dynamically through COMPOSIO_SEARCH_TOOLS / COMPOSIO_GET_TOOL_SCHEMAS
    and runs them with COMPOSIO_MULTI_EXECUTE_TOOL, authorizing accounts via
    COMPOSIO_MANAGE_CONNECTIONS on first use."""

    enabled: bool = Field(False, description="Expose Composio's hosted gateway as one MCP server.")
    url: str = Field(
        "https://connect.composio.dev/mcp",
        description='The hosted MCP URL from the Composio dashboard\'s "connect" page.',
    )
    api_key: str = Field(
        "",
        description="The API key shown beside that URL. COMPOSIO_API_KEY wins over this.",
    )
    server_name: str = Field("composio", description="The MCP server name its tools appear under.")
    timeout_seconds: float = Field(60, description="How long one call to that gateway waits.")

    @property
    def effective_api_key(self) -> str:
        return os.environ.get(environment_variables.COMPOSIO_API_KEY) or self.api_key


class MCPServerConfiguration(BaseModel):
    """One MCP server, from an ``mcp.json`` entry.

    Deliberately permissive about keys it does not model: ``mcp.json`` is a format this project
    reads rather than owns, and a server declared for several tools carries whatever fields any
    of them wanted. Refusing those would make a working file unloadable."""

    enabled: bool = True
    transport: Literal["stdio", "streamable_http"] = "stdio"
    stateful: bool = True
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = {}
    timeout_seconds: float = 30


class MCPConfiguration(Section):
    """The MCP servers available to a session, loaded from ``mcp.json`` in the ``.agents`` roots
    rather than from the configuration file — a list that changes on its own schedule and is
    watched for live reload."""

    servers: dict[str, MCPServerConfiguration] = Field(
        default={}, description="Declared MCP servers, by name. Read from mcp.json, not from this file."
    )

    def enabled_servers(self) -> dict[str, MCPServerConfiguration]:
        return {
            name: server
            for name, server in self.servers.items()
            if server.enabled
        }

    @classmethod
    def from_dotagents_roots(cls, roots: Iterable[Path]) -> MCPConfiguration:
        servers: dict[str, MCPServerConfiguration] = {}
        for root in roots:
            path = root / "mcp.json"
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            raw_servers = data.get("mcpServers", data.get("servers", {}))
            for name, raw_configuration in raw_servers.items():
                configuration = dict(raw_configuration)
                if "type" in configuration and "transport" not in configuration:
                    configuration["transport"] = configuration.pop("type")
                servers[name] = MCPServerConfiguration(**configuration)
        return cls(servers=servers)


class RemoteAgentAuthConfiguration(BaseModel):
    """How to authenticate to one external A2A agent. ``${VAR}`` in any secret field is
    expanded from the environment at load time, so tokens never live in the tracked file."""

    type: Literal["none", "bearer", "api_key", "oauth2"] = "none"
    token: str = ""
    header: str = "Authorization"
    scheme_prefix: str = "Bearer"
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = []


class RemoteAgentServerConfiguration(BaseModel):
    """One registered external A2A agent (a ``remote-agents.json`` entry). Permissive about
    unmodelled keys for the same reason :class:`MCPServerConfiguration` is."""

    enabled: bool = True
    card_url: str = ""
    auth: RemoteAgentAuthConfiguration = RemoteAgentAuthConfiguration()
    card_ttl_seconds: int = 3600
    # Hostnames allowed beyond the card_url origin (the origin is always allowed).
    allowed_hosts: list[str] = []
    # Permit private/loopback targets (e.g. a Frank-to-Frank loopback).
    allow_private: bool = False
    # Which local agent profiles may delegate to this remote agent. Empty = all profiles.
    allowed_profiles: list[str] = []


class RemoteAgentsConfiguration(Section):
    """The set of external A2A agents the harness may delegate to, loaded from
    ``remote-agents.json`` in the ``.agents`` roots rather than from the configuration file."""

    agents: dict[str, RemoteAgentServerConfiguration] = Field(
        default={},
        description="Registered remote agents, by name. Read from remote-agents.json, not from this file.",
    )

    def enabled_agents(self) -> dict[str, RemoteAgentServerConfiguration]:
        return {
            name: agent
            for name, agent in self.agents.items()
            if agent.enabled and agent.card_url
        }

    @classmethod
    def from_dotagents_roots(cls, roots: Iterable[Path]) -> RemoteAgentsConfiguration:
        agents: dict[str, RemoteAgentServerConfiguration] = {}
        for root in roots:
            path = root / "remote-agents.json"
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            raw_agents = data.get("agents", {})
            for name, raw_configuration in raw_agents.items():
                configuration = dict(raw_configuration)
                raw_auth = dict(configuration.get("auth") or {})
                for secret_field in ("token", "client_secret", "client_id"):
                    if isinstance(raw_auth.get(secret_field), str):
                        raw_auth[secret_field] = os.path.expandvars(raw_auth[secret_field])
                if raw_auth:
                    configuration["auth"] = raw_auth
                agents[name] = RemoteAgentServerConfiguration(**configuration)
        return cls(agents=agents)


class TelemetryExporterConfiguration(Section):
    """Where traces are sent."""

    endpoint: str = Field("", description="The OTLP collector to export to. Empty exports nothing.")
    protocol: str = Field("http/protobuf", description="The OTLP protocol that collector speaks.")
    headers: dict[str, str] = Field(
        default={},
        description="Headers sent with each export, for a collector that authenticates. ${VARIABLE} is expanded.",
    )


class TelemetryConfiguration(Section):
    """OpenTelemetry export of agent traces to a user-chosen OTLP backend. Off until an
    endpoint is set, so nothing leaves the machine by default. Only span structure and
    usage/metadata are exported (no prompt or completion bodies)."""

    enabled: bool = Field(False, description="Export traces at all.")
    exporter: TelemetryExporterConfiguration = Field(
        default_factory=TelemetryExporterConfiguration, description="Where traces are sent."
    )
    sample_ratio: float = Field(1.0, description="Share of traces exported. 1.0 exports every one.")

    def resolved_headers(self) -> dict[str, str]:
        return {key: os.path.expandvars(value) for key, value in self.exporter.headers.items()}


class ProviderCredential(Section):
    """Credentials for one model provider."""

    api_key: str = Field(
        "", description="This provider's API key. Its conventional environment variable wins over this."
    )
    base_url: str = Field(
        "",
        description=(
            "Where to reach it, for an OpenAI-compatible provider. The first-party clouds leave "
            "this blank, since their endpoints are already known."
        ),
    )


class AgentDefaults(Section):
    """What a session gets when its creator did not say."""

    permission_mode: Literal["default", "permissive", "classify", "read_only"] = Field(
        "default",
        description=(
            "default asks before anything its rules do not name; permissive runs what the "
            "model called low-risk and asks about the rest; classify decides every ambiguous "
            "call itself, allowing or denying without ever asking, for work nobody is "
            "watching; read_only allows reads and denies every write and side effect. A "
            "default, not a ceiling: `frank create --mode` overrides it, and a child is "
            "clamped against its parent either way."
        ),
    )


class Configuration(Section):
    HOME_AGENTS_ROOT_DIRECTORY: ClassVar[str] = "~/.agents"
    AGENTS_ROOT_DIRECTORY: ClassVar[str] = ".agents"
    AGENTS_DIRECTORY: ClassVar[str] = ".agents/agents"
    SKILLS_DIRECTORY: ClassVar[str] = ".agents/skills"

    providers: dict[str, ProviderCredential] = Field(
        default={},
        description=(
            "Credentials per model provider, keyed by provider name — anthropic, openai, google, "
            "openrouter, xai and the rest; frank.base.providers holds the full list with each "
            "one's environment variable. Which model a session runs is chosen by its agent "
            "profile, so nothing here nominates one."
        ),
    )
    exa: ExaConfiguration = Field(default_factory=ExaConfiguration, description="The web-search backend.")
    jina: JinaConfiguration = Field(
        default_factory=JinaConfiguration, description="The default page-fetching engine."
    )
    firecrawl: FirecrawlConfiguration = Field(
        default_factory=FirecrawlConfiguration, description="The fallback page-fetching engine."
    )
    web_fetch: WebFetchConfiguration = Field(
        default_factory=WebFetchConfiguration, description="Fetching a page directly."
    )
    sandbox: SandboxConfiguration = Field(
        default_factory=SandboxConfiguration,
        description="What a session's tool children may do, enforced by the operating system.",
    )
    workspace: WorkspaceConfiguration = Field(
        default_factory=WorkspaceConfiguration, description="Where a session's tools run."
    )
    compaction: CompactionConfiguration = Field(
        default_factory=CompactionConfiguration, description="How conversation history is folded as it grows."
    )
    user_context: UserContextConfiguration = Field(
        default_factory=UserContextConfiguration,
        description="Whether the system prompt describes how you work on this machine.",
    )
    computer_control: ComputerControlConfiguration = Field(
        default_factory=ComputerControlConfiguration, description="Driving the screen."
    )
    dictation: DictationConfiguration = Field(
        default_factory=DictationConfiguration, description="Speaking to the composer instead of typing."
    )
    tuning: TuningConfiguration = Field(
        default_factory=TuningConfiguration,
        description="How large, how many, and how patient the tools are.",
    )
    composio: ComposioConfiguration = Field(
        default_factory=ComposioConfiguration, description="Composio's hosted MCP gateway."
    )
    mcp: MCPConfiguration = Field(
        default_factory=MCPConfiguration, description="MCP servers, read from mcp.json."
    )
    remote_agents: RemoteAgentsConfiguration = Field(
        default_factory=RemoteAgentsConfiguration,
        description="Agents on other hosts, read from remote-agents.json.",
    )
    telemetry: TelemetryConfiguration = Field(
        default_factory=TelemetryConfiguration, description="OpenTelemetry export."
    )
    agent: AgentDefaults = Field(
        default_factory=AgentDefaults,
        description=(
            "What a session runs under when its creator does not say. An agent profile that "
            "declares its own stricter mode still wins: this is a floor for sessions created "
            "without one, not a way to loosen a profile written to be careful."
        ),
    )

    @classmethod
    def load(cls, *, seed: bool = True) -> Configuration:
        """Load the configuration from the XDG configuration file.

        Seeds the file from the packaged template on first run, because a person who has just
        installed Frank needs something to edit and something for `frank configure` to teach
        from. `seed=False` reads without creating: a library embedded in someone else's program
        should not leave a configuration file in their home directory as a side effect of being
        imported, and a program that supplies its own configuration never wanted ours anyway.
        """
        path = configuration_file_path()
        if not path.exists():
            if not seed:
                return cls()
            path.write_text(packaged_configuration_yaml())
        return cls.from_yaml(path)

    @classmethod
    def from_yaml(cls, path: str | Path) -> Configuration:
        with open(path) as file_handle:
            data = yaml.safe_load(file_handle)
        configuration = cls(**(data or {}))
        configuration.mcp = MCPConfiguration.from_dotagents_roots(configuration.agents_root_directories())
        configuration.remote_agents = RemoteAgentsConfiguration.from_dotagents_roots(configuration.agents_root_directories())
        return configuration

    def configured_provider_keys(self) -> dict[str, str]:
        """Configured (non-empty) API keys per provider, for credential resolution
        and for filtering the model picker to usable models."""
        return {
            identifier: credential.api_key
            for identifier, credential in self.providers.items()
            if credential.api_key
        }

    def configured_provider_bases(self) -> dict[str, str]:
        """Configured non-empty base URLs per provider."""
        return {
            identifier: credential.base_url
            for identifier, credential in self.providers.items()
            if credential.base_url
        }

    def agents_root_directories(self) -> list[Path]:
        return _dedupe_paths([
            Path(self.HOME_AGENTS_ROOT_DIRECTORY).expanduser(),
            Path(self.AGENTS_ROOT_DIRECTORY),
        ])

    def agent_directories(self) -> list[Path]:
        return _dedupe_paths([
            # Bundled (server-shipped) agents are the always-present base layer;
            # home and project agents override a bundled profile with the same id.
            BUNDLED_DOTAGENTS_ROOT / "agents",
            Path(self.HOME_AGENTS_ROOT_DIRECTORY).expanduser() / "agents",
            Path(self.AGENTS_ROOT_DIRECTORY) / "agents",
            Path(self.AGENTS_DIRECTORY),
        ])

    def skill_directories(self) -> list[Path]:
        return _dedupe_paths([
            # Bundled (server-shipped) skills are the always-present base layer, exactly
            # like agents — home and project skills override a bundled skill of the same id.
            BUNDLED_DOTAGENTS_ROOT / "skills",
            Path(self.HOME_AGENTS_ROOT_DIRECTORY).expanduser() / "skills",
            Path(self.AGENTS_ROOT_DIRECTORY) / "skills",
            Path(self.SKILLS_DIRECTORY),
        ])

    def memory_directories(self) -> list[Path]:
        return _dedupe_paths([
            Path(self.HOME_AGENTS_ROOT_DIRECTORY).expanduser() / "memories",
            Path(self.AGENTS_ROOT_DIRECTORY) / "memories",
        ])

    # Working-directory-scoped resolution.
    #
    # The home root (``~/.agents``) is always global. Project-relative roots
    # (``.agents`` and friends) are resolved against the *session's working
    # directory* rather than the harness's launch CWD, so a session working
    # outside the directory the harness happens to have been started in is never
    # advertised that directory's agents/skills/memories/MCP servers — and the
    # paths it is handed are valid for where it actually runs. Each ``*_for``
    # method mirrors its CWD-relative counterpart above; prefer these everywhere
    # a working directory is known (every session and every UI folder selection).

    def _local_base(self, working_directory: str) -> Path:
        """The directory project-relative ``.agents`` roots resolve against — the
        working directory, or the harness's CWD as a last resort when none is given."""
        return Path(working_directory).expanduser() if working_directory else Path.cwd()

    def _resolve_local(self, working_directory: str, directory: str) -> Path:
        path = Path(directory).expanduser()
        return path if path.is_absolute() else self._local_base(working_directory) / path

    def home_agents_root(self) -> Path:
        """The global ``~/.agents`` root — the scope shared by every folder."""
        return Path(self.HOME_AGENTS_ROOT_DIRECTORY).expanduser()

    def project_agents_root_for(self, working_directory: str) -> Path:
        """The working directory's own ``.agents`` root — the project-local scope.
        Equals :meth:`home_agents_root` when the working directory is the home
        directory (in which case nothing is project-specific)."""
        return self._resolve_local(working_directory, self.AGENTS_ROOT_DIRECTORY)

    def agents_root_directories_for(self, working_directory: str) -> list[Path]:
        return _dedupe_paths([
            self.home_agents_root(),
            self.project_agents_root_for(working_directory),
        ])

    def agent_directories_for(self, working_directory: str) -> list[Path]:
        return _dedupe_paths([
            # Bundled (server-shipped) agents are the always-present base layer;
            # home and project agents override a bundled profile with the same id.
            BUNDLED_DOTAGENTS_ROOT / "agents",
            Path(self.HOME_AGENTS_ROOT_DIRECTORY).expanduser() / "agents",
            self._resolve_local(working_directory, self.AGENTS_ROOT_DIRECTORY) / "agents",
            self._resolve_local(working_directory, self.AGENTS_DIRECTORY),
        ])

    def skill_directories_for(self, working_directory: str) -> list[Path]:
        return _dedupe_paths([
            # Bundled (server-shipped) skills are the always-present base layer, exactly
            # like agents — home and project skills override a bundled skill of the same id.
            BUNDLED_DOTAGENTS_ROOT / "skills",
            Path(self.HOME_AGENTS_ROOT_DIRECTORY).expanduser() / "skills",
            self._resolve_local(working_directory, self.AGENTS_ROOT_DIRECTORY) / "skills",
            self._resolve_local(working_directory, self.SKILLS_DIRECTORY),
        ])

    def memory_directories_for(self, working_directory: str) -> list[Path]:
        return _dedupe_paths([
            Path(self.HOME_AGENTS_ROOT_DIRECTORY).expanduser() / "memories",
            self._resolve_local(working_directory, self.AGENTS_ROOT_DIRECTORY) / "memories",
        ])

    def mcp_configuration_for(self, working_directory: str) -> MCPConfiguration:
        """The MCP servers declared for a working directory: those in the home
        ``mcp.json`` plus the working directory's own, deduped by name (the
        working directory overriding home). Used to filter what the UI lists and
        to grow the shared server pool, never to scope the launch directory in."""
        return MCPConfiguration.from_dotagents_roots(self.agents_root_directories_for(working_directory))

    def remote_agents_configuration_for(self, working_directory: str) -> RemoteAgentsConfiguration:
        """The external A2A agents declared for a working directory: those in the home
        ``remote-agents.json`` plus the working directory's own."""
        return RemoteAgentsConfiguration.from_dotagents_roots(self.agents_root_directories_for(working_directory))


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser()
        key = resolved.resolve() if resolved.exists() else resolved.absolute()
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return result


class NamedToolPermissions(BaseModel):
    """Per-call permission rules for a tool whose calls have a *name*.

    `bash` matches patterns against the segments of a command line, which is a shape only a
    shell has. An MCP call and a screen script each have a simpler identity — `server.tool` and
    the primitive a script reaches for — and both were left with no rules at all: whatever a
    person had configured, these two paths reported "ask" to the barrier and to the classifier,
    every time. So a `deny` written for one of them was not merely unmatched, it was unread.

    Longest matching pattern wins, as with commands, so a specific rule beats a broad one
    however they are ordered in the file.
    """

    permissions: dict[str, str] = {}

    def decide(self, subject: str, unmatched: str = "ask") -> str:
        """The configured decision for ``subject``, or ``unmatched`` when no pattern names it."""
        best_length, best = 0, unmatched
        for pattern, decision in self.permissions.items():
            if not pattern or not fnmatch(subject, pattern):
                continue
            if len(pattern) > best_length:
                best_length, best = len(pattern), str(decision).lower()
        return best


class BashToolConfiguration(BaseModel):
    enabled: bool = True
    background_allowed: bool = True
    permissions: dict[str, str] = {}

    _SHELL_SPLIT = re.compile(r"\s*(?:&&|\|\||[;|])\s*")
    _SUBSHELL = re.compile(r"\$\((.+?)\)|`(.+?)`")

    # File-writing output redirection: `> f`, `>> f`, `&> f`, `>| f`, `1> f` —
    # but not fd duplications (`2>&1`, `>&2`) or throwaway devices.
    _REDIRECT = re.compile(
        r"(?<![0-9>&<])(?:&>{1,2}|>\||[0-9]?>{1,2})\s*(?!&)(?!/dev/(?:null|stderr|stdout|fd/))\S"
    )

    # Heads that only read state. Dual-use tools (sed/find/curl/wget/tar/git/
    # xargs) and runtimes (python/node/...) are handled separately, not here.
    _READ_ONLY_HEADS = frozenset({
        "cat", "bat", "tac", "nl", "less", "more", "head", "tail", "ls", "dir",
        "vdir", "tree", "wc", "sort", "uniq", "cut", "paste", "comm", "join",
        "column", "fold", "rev", "tr", "expand", "unexpand", "grep", "egrep",
        "fgrep", "rg", "ag", "ack", "fd", "fdfind", "locate", "mlocate", "stat",
        "file", "du", "df", "pwd", "echo", "printf", "date", "cal", "which",
        "type", "whereis", "printenv", "ps", "pgrep", "uname", "whoami", "id",
        "groups", "hostname", "uptime", "free", "vmstat", "lsof", "netstat",
        "ss", "dig", "nslookup", "host", "ping", "traceroute", "mtr", "md5sum",
        "sha1sum", "sha256sum", "sha512sum", "b2sum", "cksum", "diff", "cmp",
        "colordiff", "basename", "dirname", "realpath", "readlink", "test",
        "true", "false", "sleep", "seq", "jq", "yq", "xxd", "od", "strings",
        "hexdump", "ldd", "nm", "objdump", "readelf", "size", "man", "tldr",
        "info", "history", "help", "base64",
    })
    # Heads that always modify state (writes, installs, privileged operations).
    _MUTATING_HEADS = frozenset({
        "rm", "rmdir", "mv", "cp", "link", "ln", "mkdir", "touch", "install",
        "truncate", "dd", "chmod", "chown", "chgrp", "chattr", "shred",
        "mkfifo", "mknod", "rsync", "scp", "sftp", "ftp", "tee", "ed", "ex",
        "vi", "vim", "nano", "emacs", "zip", "unzip", "gzip", "gunzip", "bzip2",
        "bunzip2", "xz", "unxz", "7z", "kill", "killall", "pkill", "mount",
        "umount", "mkfs", "fdisk", "parted", "crontab", "at", "systemctl",
        "service", "launchctl", "reboot", "shutdown", "halt", "poweroff",
        "pip", "pip3", "pipx", "npm", "pnpm", "yarn", "cargo", "gem",
        "composer", "brew", "apt", "apt-get", "dpkg", "yum", "dnf", "zypper",
        "pacman", "apk", "nix-env", "make", "cmake", "ninja", "gradle", "mvn",
        "psql", "mysql", "mongo", "redis-cli", "sqlite3", "useradd", "userdel",
        "groupadd", "passwd", "uv",
    })
    # git subcommands that only read.
    _GIT_READ_SUBCOMMANDS = frozenset({
        "log", "show", "diff", "status", "branch", "tag", "blame", "rev-parse",
        "ls-files", "ls-tree", "cat-file", "describe", "remote", "config",
        "grep", "shortlog", "reflog", "whatchanged", "name-rev", "for-each-ref",
        "symbolic-ref", "rev-list", "count-objects", "var", "show-ref",
        "merge-base", "help", "version",
    })
    # Benign wrappers that prefix the real command and can be skipped over.
    _COMMAND_WRAPPERS = frozenset({
        "env", "nohup", "nice", "time", "stdbuf", "command", "builtin", "exec",
        "setsid", "ionice", "timeout", "watch", "xargs",
    })

    def read_only_assessment(self, command: str) -> tuple[str, str]:
        """Classify a command for read-only enforcement.

        Returns ``(classification, detail)`` where classification is one of:
        ``"read_only"`` — provably non-mutating; ``"mutating"`` — a write,
        install, or privileged action was detected (``detail`` names it); or
        ``"unknown"`` — could not be classified statically (e.g. a script or
        runtime), in which case the caller defers to the model's own
        ``read_only`` declaration. Stronger than a flat prefix denylist: it
        tokenises each segment (including subshells), understands dual-use tools
        (sed/find/curl/wget/tar/git/xargs) and file redirections, and treats
        unrecognised commands as unknown rather than silently allowing them.
        """
        seen_unknown = False
        for segment in self._extract_segments(command):
            classification, detail = self._assess_segment(segment)
            if classification == "mutating":
                return ("mutating", detail)
            if classification == "unknown":
                seen_unknown = True
        return ("unknown", "") if seen_unknown else ("read_only", "")

    def _assess_segment(self, segment: str) -> tuple[str, str]:
        if self._REDIRECT.search(segment):
            return ("mutating", "output redirection to a file")
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):  # FOO=bar env prefix
                index += 1
                continue
            if token in self._COMMAND_WRAPPERS and token != "xargs":
                index += 1
                continue
            break
        if index >= len(tokens):
            return ("read_only", "")
        head = tokens[index].split("/")[-1]
        arguments = tokens[index + 1:]
        return self._classify_command(head, arguments)

    def _classify_command(self, head: str, arguments: list[str]) -> tuple[str, str]:
        if head in ("sudo", "su", "doas"):
            return ("mutating", head)
        if head in self._MUTATING_HEADS:
            return ("mutating", head)
        if head in ("sed", "perl"):
            if any(re.match(r"^-[A-Za-z]*i", argument) or argument.startswith("--in-place") for argument in arguments):
                return ("mutating", f"{head} in-place edit")
            return ("read_only", "")
        if head == "gawk":
            return ("mutating", "gawk in-place") if any("inplace" in argument for argument in arguments) else ("read_only", "")
        if head == "awk":
            return ("read_only", "")
        if head == "find":
            mutating_actions = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
            return ("mutating", "find with -delete/-exec") if any(argument in mutating_actions for argument in arguments) else ("read_only", "")
        if head == "curl":
            writers = {"-o", "-O", "--output", "--remote-name", "-J", "--remote-header-name", "--create-dirs", "--dump-header"}
            return ("mutating", "curl writing to a file") if any(argument in writers for argument in arguments) else ("read_only", "")
        if head == "wget":
            for position, argument in enumerate(arguments):
                if argument in ("-O-", "-qO-", "--output-document=-"):
                    return ("read_only", "")
                if argument in ("-O", "--output-document") and position + 1 < len(arguments) and arguments[position + 1] == "-":
                    return ("read_only", "")
            return ("mutating", "wget writing to a file")
        if head == "tar":
            if any((argument.startswith("-") and "t" in argument and "x" not in argument and "c" not in argument) or argument == "--list" for argument in arguments):
                return ("read_only", "")
            return ("mutating", "tar create/extract")
        if head == "git":
            subcommand = next((argument for argument in arguments if not argument.startswith("-")), "")
            return ("read_only", "") if subcommand in self._GIT_READ_SUBCOMMANDS else ("mutating", f"git {subcommand}".strip())
        if head == "xargs":
            inner = next((argument for argument in arguments if not argument.startswith("-")), "").split("/")[-1]
            if inner and (inner in self._MUTATING_HEADS or inner in ("sed", "tee", "sudo", "rm")):
                return ("mutating", f"xargs {inner}")
            if inner in self._READ_ONLY_HEADS:
                return ("read_only", "")
            return ("unknown", "")
        if head in self._READ_ONLY_HEADS:
            return ("read_only", "")
        return ("unknown", "")

    def evaluate_permission(self, command: str, unmatched: str = "allow") -> str:
        """The configured decision for ``command``. ``unmatched`` is returned when no
        pattern matches — "allow" for a normal turn, but "ask" for a delegated agent under the
        interactive policy, so an unlisted command escalates rather than running."""
        segments = self._extract_segments(command)
        best_match_length = 0
        best_decision = unmatched
        for segment in segments:
            for pattern, decision in self.permissions.items():
                if self._segment_matches(segment, pattern):
                    if not best_match_length or len(pattern) > best_match_length:
                        best_match_length = len(pattern)
                        best_decision = decision.lower()
        return best_decision

    def command_matches(self, command: str, patterns: Iterable[str]) -> bool:
        """Whether any segment of ``command`` matches any of ``patterns`` — the same
        matching used for configured rules, reused for session-scoped allowlists."""
        segments = self._extract_segments(command)
        return any(
            self._segment_matches(segment, pattern)
            for segment in segments
            for pattern in patterns
            if pattern
        )

    def _extract_segments(self, command: str) -> list[str]:
        """Split a command string into individual segments to check.

        Splits on shell operators (&&, ||, ;, |) and extracts the contents
        of subshells ($(...) and backticks).
        """
        segments = [segment.strip() for segment in self._SHELL_SPLIT.split(command) if segment.strip()]
        for match in self._SUBSHELL.finditer(command):
            inner = (match.group(1) or match.group(2)).strip()
            if inner:
                segments.extend(self._extract_segments(inner))
        return segments

    @staticmethod
    def _segment_matches(segment: str, pattern: str) -> bool:
        if pattern.endswith("*"):
            keyword = pattern[:-1].rstrip()
            if segment.startswith(keyword):
                return True
            return bool(re.search(r"(?:^|\s)" + re.escape(keyword) + r"(?:\s|$)", segment))
        return segment == pattern


class ToolsConfiguration(BaseModel):
    """Which of the harness's tools an agent has, and how the ones with settings behave.

    Three tools have settings of their own, and they are the three whose calls can be told
    apart from one another: `bash` by its command, `mcp` by `server.tool`, and `screen` by the
    primitive a script reaches for. Each carries a permission table. Every other tool is on or
    off, which `disabled` says uniformly rather than by inventing a section per tool that would
    carry one field.

    Two ways to narrow the roster, and they are complements rather than duplicates.
    `tools_enabled` on the agent is an *allow-list*: naming one tool means naming all of them,
    which is right for an agent defined by a small capability set. `disabled` is a *deny-list*:
    it takes the full roster and removes from it, which is right when an agent should have
    everything except shell access. Using both means a tool must survive each.
    """

    bash: BashToolConfiguration = BashToolConfiguration()
    # The other two tools whose calls can be named, and so can be ruled on. Empty means no rule,
    # which leaves the barrier's own default in force exactly as before.
    mcp: NamedToolPermissions = NamedToolPermissions()
    screen: NamedToolPermissions = NamedToolPermissions()
    disabled: list[str] = Field(
        default_factory=list,
        description=(
            "Tools this agent may not use, by name. The complement of the profile's "
            "tools_enabled allow-list, for an agent that should have everything except a few."
        ),
    )

    def is_enabled(self, tool_name: str) -> bool:
        """Whether this agent may use `tool_name` at all.

        `bash.enabled` is honoured here rather than being a second mechanism: it predates
        `disabled`, it is what the settings interface writes, and a switch that a person can
        see and set must actually do something."""
        if tool_name in self.disabled:
            return False
        if tool_name == "bash" and not self.bash.enabled:
            return False
        return True


class AgentConfiguration(BaseModel):
    name: str = ""
    title: str = ""
    aliases: list[str] = []
    color: str = ""
    description: str = ""
    role: str = ""
    enabled: bool = True
    # Names of the skills (files in the skills directory) this agent may use.
    # Empty means every available skill is offered to the agent by default.
    skills: list[str] = []
    # The model and its provider are separate fields, mirroring the global config:
    # a human editing an AGENT.md sees both explicitly. ``model_identifier``
    # recombines them into the ``provider/model`` form the factory expects.
    model: Optional[str] = None
    provider: Optional[str] = None
    reasoning_effort: str = "high"
    # default: per-command permission rules, and anything unmatched is asked about.
    # permissive: the same rules, but an unmatched command runs and only the risk the model
    # declared escalates — medium or high asks, low does not. No classifier, no model call.
    # classify: the default rules plus a classifier that settles every ambiguous call itself,
    # allowing or denying and never asking. read_only: hard-block all writes. No bypass mode.
    # `None` means the card has no opinion, and that is the default — the same convention as
    # `sandbox` below, and for the same reason.
    #
    # This used to default to `"default"`, which is a *value*, and the control plane reads it as
    # the loosest mode the profile allows. So every card that simply did not mention permissions
    # — which is all of them — imposed a ceiling of `default`, and `permissive` and `classify`
    # were unreachable: a person picked one, the daemon clamped it straight back,
    # and the chip returned to "Ask for approval" with no explanation. A card that wants to bound
    # its agent still says so and is still obeyed; silence is no longer a bound.
    permission_mode: Optional[Literal["default", "permissive", "classify", "read_only"]] = None

    # An agent's own confinement, narrowing the global one. An investigator and a build agent
    # genuinely want different filesystems, and the harness already accepts that agents differ in
    # what they may do. Unset means "whatever the machine's configuration says"; set, it is still
    # clamped against the session that created this one, so it can only ever narrow.
    sandbox: Optional[SandboxConfiguration] = None
    tools: ToolsConfiguration = ToolsConfiguration()
    tools_enabled: list[str] = []
    system_prompt: str = ""

    @property
    def identifier(self) -> str:
        return self.name

    @property
    def display_name(self) -> str:
        return self.title or self.name

    @property
    def model_identifier(self) -> Optional[str]:
        """The agent's model as the ``provider/model`` form the factory expects."""
        if not self.model or not self.provider:
            return None
        return f"{self.provider}/{self.model}"

    @property
    def permission_policy(self) -> Optional[PermissionMode]:
        """The card's configured permission mode, or ``None`` where it declares none.

        ``None`` is not a mode and must not be coerced into one: this is a *ceiling*, and every
        caller passes it to `more_restrictive`, which ignores absent inputs. Coercing it to
        `DEFAULT` — which is what this did — turned every silent card into a ceiling nobody
        wrote."""
        return PermissionMode.parse(self.permission_mode) if self.permission_mode else None

    @classmethod
    def from_markdown(cls, path: str | Path) -> AgentConfiguration:
        path = Path(path)
        with open(path) as file_handle:
            content = file_handle.read()

        frontmatter_match = re.match(
            r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL
        )
        if not frontmatter_match:
            raise ValueError(f"No YAML frontmatter found in {path}")

        frontmatter = yaml.safe_load(frontmatter_match.group(1)) or {}
        markdown_body = frontmatter_match.group(2).strip()
        default_identifier = path.parent.name if path.name.upper() == "AGENT.MD" else path.stem
        frontmatter.setdefault("name", default_identifier)
        frontmatter.setdefault("title", frontmatter["name"])

        # Everything the agent is, in one file. There used to be a `configuration.json` beside
        # this one holding the model preset, the permission ceiling and the tool toggles — in
        # camelCase, while the front matter above is snake_case — and loading meant merging the
        # two, which is why the same fact had two spellings and two places to be wrong. A skill
        # is one `SKILL.md`; an agent is one `AGENT.md`.
        tools_data = frontmatter.pop("tools", {})
        tools_configuration = (
            ToolsConfiguration(**{name: value for name, value in tools_data.items()})
            if tools_data
            else ToolsConfiguration()
        )

        return cls(
            **frontmatter,
            tools=tools_configuration,
            system_prompt=markdown_body,
        )


def write_agent_markdown(path: str | Path, configuration: AgentConfiguration) -> None:
    """Write a profile back to its `AGENT.md`, front matter and body together.

    The body is the agent's system prompt and is written verbatim from
    :attr:`AgentConfiguration.system_prompt`, so a round trip through the settings interface
    cannot quietly reword an agent's instructions.

    Only what differs from the defaults is written. A profile that says nothing about tools
    should not grow a `tools:` block full of the values it would have had anyway — the file is
    something a person reads and edits by hand, and every line in it should mean a decision
    somebody made.
    """
    path = Path(path)
    body = configuration.system_prompt.strip()
    front = configuration.model_dump(
        mode="json", exclude_defaults=True, exclude_none=True, exclude={"system_prompt"},
    )
    # `name` is the identity the rest of the harness addresses this agent by; it is worth
    # stating even when it matches the directory it lives in.
    front.setdefault("name", configuration.identifier)
    rendered = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{rendered}\n---\n\n{body}\n" if body else f"---\n{rendered}\n---\n")


class PermissionEvaluator:
    def __init__(self, agent_configuration: AgentConfiguration):
        self._configuration = agent_configuration

    def check_tool_enabled(self, tool_name: str) -> None:
        """Refuse a tool the agent's profile does not list.

        Applies to whatever `tools_enabled` names, which is what the field always meant — an
        agent that may not run shell commands must not be handed `bash` because the gate only
        looked at one privileged tool."""
        if (
            self._configuration.tools_enabled
            and tool_name not in self._configuration.tools_enabled
        ):
            raise PermissionDenied(
                f"Tool '{tool_name}' is not enabled for agent '{self._configuration.identifier}'"
            )

    def check_tool_not_disabled(self, tool_name: str) -> None:
        """Refuse a tool the agent's profile has switched off.

        Checked at call time as well as when the roster is built, for the same reason
        `check_tool_enabled` is: the roster decides what the model is *offered*, and a model
        may call a tool it was never offered. A gate that only filtered the roster would be a
        suggestion."""
        if not self._configuration.tools.is_enabled(tool_name):
            raise PermissionDenied(
                f"Tool '{tool_name}' is disabled for agent '{self._configuration.identifier}'"
            )

    def evaluate_bash_permission(self, command: str, unmatched: str = "allow") -> str:
        return self._configuration.tools.bash.evaluate_permission(command, unmatched=unmatched)

    def check_bash_background(self) -> None:
        if not self._configuration.tools.bash.background_allowed:
            raise PermissionDenied("Background bash execution is not allowed")

    def check_tool(self, tool_name: str, /, **arguments) -> None:
        # tool_name is positional-only so that a tool whose own arguments include a
        # key named "tool_name" (e.g. call_mcp_tool) does not collide with it.
        self.check_tool_enabled(tool_name)
        self.check_tool_not_disabled(tool_name)


class PermissionDenied(RuntimeError):
    """A tool call refused by policy — not by the operating system.

    Named apart from the builtin `PermissionError` deliberately, and this is not cosmetic. It
    used to *be* called `PermissionError`, shadowing the builtin inside this module while
    subclassing `RuntimeError` rather than it, so the two handlers written to catch a policy
    denial — `except PermissionError` in the tool dispatcher, which resolved to the builtin —
    caught nothing at all. A `tools_enabled` violation escaped as an unhandled error instead of
    becoming the tool denial the model was supposed to see.

    The builtin means EACCES: the kernel said no. This means the harness said no. Two different
    facts deserve two different names.
    """


class PromptLoader:
    def __init__(self, prompts_directory: str | Path, extension: str = "md"):
        self._directory = Path(prompts_directory)
        self._extension = extension

    def load(self, template_name: str, variables: dict[str, str]) -> str:
        path = self._directory / f"{template_name}.{self._extension}"
        if not path.exists():
            return ""
        content = path.read_text()
        return self._replace_variables(content, variables, template_name)

    @classmethod
    def render(cls, template: str, variables: dict[str, str], template_name: str = "") -> str:
        """Render a template that is already in hand rather than one on disk.

        For a catalogue that carries its prompts in memory. The substitution and its strictness
        are identical — it is the *source* of the text that differs, which is the whole point of
        the catalogue seam."""
        return cls._replace_variables(template, variables, template_name)

    @staticmethod
    def _replace_variables(template: str, variables: dict[str, str], template_name: str = "") -> str:
        """Substitute ``{{ name }}`` placeholders (spaced or not) from ``variables``.

        Rendering is strict: a placeholder with no matching variable, or any ``{{ … }}`` in the
        template that is not a well-formed placeholder, raises rather than silently shipping raw
        braces into a prompt the model would see. The strictness applies to the *template* only —
        braces that appear inside a substituted value (a tool result echoing Handlebars, a file
        of Jinja, AppleScript record syntax) are data, never placeholders, and pass through.

        A placeholder that sits alone on its line and has nothing to put there takes the line
        with it, along with the blank line that separated it from what follows. Most sections of
        a prompt are optional — no agent context in a library run, no screen guidance unless the
        tool is on, no instructions on a bare machine — and a template cannot say "and the gap
        too", so every absent one used to leave the blank lines that were meant to surround it.
        Five of them stacked up in the system prompt, one run reaching five blank lines. Doing it
        here keeps the templates written the way they read, and touches only the template's own
        whitespace: a value is substituted after this and is never rewritten."""
        where = f" in prompt '{template_name}'" if template_name else ""
        placeholder = re.compile(r"\{\{\s*(\w+)\s*\}\}")

        def drop_if_empty(match: re.Match[str]) -> str:
            name = match.group(1)
            supplied = variables.get(name)
            return "" if supplied is not None and not str(supplied).strip() else match.group(0)

        # The placeholder's own line, plus one blank line after it if there is one.
        own_line = r"^[ \t]*\{\{\s*(\w+)\s*\}\}[ \t]*"
        template = re.sub(own_line + r"\n(?:[ \t]*\n)?", drop_if_empty, template, flags=re.M)

        # What is left on a line of its own contributes its content and nothing else: the
        # template's own newline already ends the line, so a value that ends in one — which every
        # value read from a file does — would spend it on a blank line nobody asked for. The
        # template decides the spacing; the value decides the words.
        sections = set(re.findall(own_line + r"$", template, flags=re.M))
        variables = {
            name: str(value).strip() if name in sections else value
            for name, value in variables.items()
        }

        # A {{ … }} in the template that is not a well-formed {{ name }} (a dotted path, a space
        # in the name, an unclosed brace) is a template bug — catch it before substitution so it
        # can't be confused with stray output. Checked on the template, so injected values are safe.
        malformed = re.search(r"\{\{.*?\}\}", placeholder.sub("", template), re.DOTALL)
        if malformed is not None:
            raise ValueError(f"Malformed placeholder {malformed.group(0)!r}{where}.")

        def replacer(match: re.Match[str]) -> str:
            variable_name = match.group(1)
            if variable_name not in variables:
                raise ValueError(
                    f"Unresolved placeholder '{{{{ {variable_name} }}}}'{where}: no value was provided (given: {sorted(variables)})."
                )
            # Rendered rather than required to already be a string. A caller that passed a window's
            # width as the `int` it is raised `TypeError: sequence item 2: expected str instance,
            # int found` out of `re.sub` — which the surface guard then swallowed, so the model was
            # handed "The action failed (sequence item 2: expected str instance, int found)" in
            # place of the message explaining that the application withholds its interface. A
            # message that cannot render is worse than one that says the wrong thing, because the
            # failure looks like it came from the application rather than from the template.
            return str(variables[variable_name])

        # Accept both the spaced ({{ name }}) and unspaced ({{name}}) forms.
        return placeholder.sub(replacer, template)


def _as_directories(directories: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(directories, (str, Path)):
        return [Path(directories).expanduser()]
    return [Path(directory).expanduser() for directory in directories]


def _agent_paths(agents_directories: str | Path | Iterable[str | Path], include_aliases: bool = False) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for directory in _as_directories(agents_directories):
        if not directory.is_dir():
            continue
        # `AGENT.md`, in that spelling, exactly as a skill is `SKILL.md`. Accepting a lowercase
        # variant as well meant two files could name the same agent and which one won depended
        # on glob order, and it meant the convention had to be explained twice.
        candidates = [
            *sorted(directory.glob("*.md")),
            *sorted(directory.glob("*/AGENT.md")),
        ]
        for path in candidates:
            try:
                configuration = AgentConfiguration.from_markdown(path)
                if not configuration.enabled:
                    continue
                paths[configuration.identifier] = path
                if include_aliases:
                    for alias in configuration.aliases:
                        paths[alias] = path
            except Exception:
                fallback = path.parent.name if path.name.upper() == "AGENT.MD" else path.stem
                paths[fallback] = path
    return paths


def load_agent_configuration(
    name: str, agents_directory: str | Path | Iterable[str | Path]
) -> AgentConfiguration:
    paths = _agent_paths(agents_directory, include_aliases=True)
    path = paths.get(name)
    if path is None:
        searched = ", ".join(str(directory) for directory in _as_directories(agents_directory))
        raise FileNotFoundError(f"Agent configuration not found: {name} (searched: {searched})")
    return AgentConfiguration.from_markdown(path)


def agent_configuration_path(
    name: str, agents_directory: str | Path | Iterable[str | Path]
) -> Path:
    paths = _agent_paths(agents_directory, include_aliases=True)
    path = paths.get(name)
    if path is None:
        searched = ", ".join(str(directory) for directory in _as_directories(agents_directory))
        raise FileNotFoundError(f"Agent configuration not found: {name} (searched: {searched})")
    return path


def list_agent_route_names(agents_directory: str | Path | Iterable[str | Path]) -> list[str]:
    return sorted(_agent_paths(agents_directory, include_aliases=True))


def list_agents(agents_directory: str | Path | Iterable[str | Path]) -> list[dict[str, str]]:
    agents = []
    for name, path in sorted(_agent_paths(agents_directory).items()):
        try:
            config = AgentConfiguration.from_markdown(path)
            agents.append({
                "id": config.identifier,
                "name": config.identifier,
                "title": config.display_name,
                # What the agent is for — surfaced as the subtitle in the UI's agent picker.
                "description": config.description,
                # The resolved ``provider/model`` identifier; empty means the
                # agent has not configured a runnable model.
                "model": config.model_identifier or "",
            })
        except Exception:
            agents.append({"id": name, "name": name, "title": name, "description": "", "model": ""})
    return agents


