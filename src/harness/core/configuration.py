from collections.abc import Iterable
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import ClassVar, Literal, Optional

import yaml
from pydantic import BaseModel


# The harness keeps its mutable state — the configuration file and the chat
# history database — under a dot-directory in the user's home, not inside the
# repository. The home directory is the single source of truth.
DAISY_HOME_DIRECTORY = Path("~/.daisy").expanduser()
CONFIGURATION_FILENAME = "configuration.yaml"
DATABASE_FILENAME = "history.db"

# The packaged configuration lives in a sibling YAML file so editing the template
# is a data change, not a code change. Used to seed ~/.daisy/configuration.yaml
# on first run and as the base when persisting settings before any file exists.
PACKAGED_CONFIGURATION_PATH = Path(__file__).resolve().parent / "configuration.yaml"


def packaged_configuration_yaml() -> str:
    return PACKAGED_CONFIGURATION_PATH.read_text()


def _bundled_dotagents_root() -> Path:
    """The ``.agents`` directory shipped with the server itself. The bundled
    agents under it are always available as a base layer, so every working
    directory sees at least the shipped profiles even when it has no ``.agents``
    of its own. Located by walking up from this file to the nearest ancestor
    that contains ``.agents/agents`` (an editable install points back at the
    source tree); falls back to the expected ``src/harness/core -> repo root``
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
    home_root = Path(GlobalConfiguration.HOME_AGENTS_ROOT_DIRECTORY).expanduser()
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


def harness_home_directory() -> Path:
    """The ~/.daisy directory, created on first use."""
    DAISY_HOME_DIRECTORY.mkdir(parents=True, exist_ok=True)
    return DAISY_HOME_DIRECTORY


def configuration_file_path() -> Path:
    return harness_home_directory() / CONFIGURATION_FILENAME


def database_file_path() -> Path:
    return harness_home_directory() / DATABASE_FILENAME


def save_api_keys(
    *,
    exa_api_key: str | None = None,
    composio_api_key: str | None = None,
    jina_api_key: str | None = None,
    firecrawl_api_key: str | None = None,
    web_fetch_proxy_url: str | None = None,
    permission_mode: str | None = None,
    sandbox_enabled: bool | None = None,
    workspace_strategy: str | None = None,
    compaction: dict | None = None,
    user_context_enabled: bool | None = None,
    computer_control_enabled: bool | None = None,
    tuning: dict | None = None,
    provider_keys: dict[str, str] | None = None,
    provider_base_urls: dict[str, str] | None = None,
) -> None:
    """Persist settings into ~/.daisy/configuration.yaml, preserving the rest
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
    if sandbox_enabled is not None:
        data.setdefault("sandbox", {})["enabled"] = sandbox_enabled
    if workspace_strategy is not None:
        data.setdefault("workspace", {})["strategy"] = workspace_strategy
    if compaction is not None:
        data.setdefault("compaction", {}).update(compaction)
    if tuning is not None:
        data.setdefault("tuning", {}).update(tuning)
    if user_context_enabled is not None:
        data.setdefault("user_context", {})["enabled"] = user_context_enabled
    if computer_control_enabled is not None:
        data.setdefault("computer_control", {})["enabled"] = computer_control_enabled
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


class ExaConfiguration(BaseModel):
    api_key: str = ""

    @property
    def effective_api_key(self) -> str:
        return os.environ.get("EXA_API_KEY") or self.api_key


class JinaConfiguration(BaseModel):
    """Jina Reader (r.jina.ai) — the web-fetch tool's default engine. Optional: Jina
    works keyless (lower rate limits); a key raises the quota."""
    api_key: str = ""

    @property
    def effective_api_key(self) -> str:
        return os.environ.get("JINA_API_KEY") or self.api_key


class FirecrawlConfiguration(BaseModel):
    """Firecrawl — the web-fetch tool's fallback engine for pages Jina returns
    thin/blocked. Optional: the fallback is skipped without a key. ``api_url`` points
    at a self-hosted instance instead of the hosted API (edited in the file, not the
    settings dialog)."""
    api_key: str = ""
    api_url: str = ""

    @property
    def effective_api_key(self) -> str:
        return os.environ.get("FIRECRAWL_API_KEY") or self.api_key

    @property
    def effective_api_url(self) -> str:
        return os.environ.get("FIRECRAWL_API_URL") or self.api_url


class WebFetchConfiguration(BaseModel):
    """Optional proxy for the web-fetch tool's direct (browser-impersonating) tier
    and the file-download tool — routes those requests through an HTTP/SOCKS proxy
    for sites that block by IP reputation. Empty means direct. Bring your own (e.g. a
    residential proxy); credentials may be embedded as ``http://user:pass@host:port``."""
    proxy_url: str = ""

    @property
    def effective_proxy_url(self) -> str:
        return os.environ.get("DAISY_FETCH_PROXY") or self.proxy_url


class SandboxConfiguration(BaseModel):
    enabled: bool = True


class WorkspaceConfiguration(BaseModel):
    strategy: Literal["none", "branch", "worktree"] = "none"
    # Artifact versioning captures only the specific files the agent writes, so a write
    # larger than this cap is recorded as a placeholder version rather than stored.
    artifact_maximum_bytes: int = 128 * 1024 * 1024


class CompactionConfiguration(BaseModel):
    """Conversation memory management, modelled on Mastra's Observational Memory: as raw
    history grows, an Observer folds older turns into a dense, timestamped observation
    log kept at the front of the context; a Reflector condenses that log when it grows
    large. Fractions are of the model's context window.

    ``auto`` is off by default — some users want one long session with only manual
    (button-triggered) compaction; others opt into automatic. Manual compaction always
    works regardless of ``auto``."""

    auto: bool = False
    # Run the Observer when live context exceeds this fraction of the window.
    observer_context_fraction: float = 0.6
    # Run the Reflector when the observation log itself exceeds this fraction.
    reflector_observation_fraction: float = 0.3
    # Recent user turns always kept verbatim (never folded into observations).
    keep_recent_turns: int = 6


class TuningConfiguration(BaseModel):
    """How large, how many, and how patient the tools are — the single home for what used to be
    dozens of scattered magic numbers. Size and count caps are token budgets: the concrete limit a
    tool applies is derived from the *live* model context window (see ``harness.core.tuning``), so a
    small model gets tight caps and a large one gets room. These two fractions scale the whole
    output/listing family from calibrated defaults; timeouts scale only with ``timeout_scale``;
    settlement is polling, not a fixed sleep, bounded by the interval and ceiling."""

    # Share of the context window one tool's textual output may fill (read windows, command
    # output, fetched pages, evaluate results). Higher enlarges every text cap proportionally.
    output_fraction: float = 0.25
    # Share of the window a structured listing may fill (page elements, find matches, grep/glob
    # results, file lines). Higher lengthens every listing proportionally.
    listing_fraction: float = 0.15
    # How often to re-check whether a surface has settled after an action, in seconds.
    settle_interval_seconds: float = 0.05
    # The longest to wait for a surface to settle before proceeding anyway, in seconds.
    settle_ceiling_seconds: float = 1.5
    # Multiplier on every action, navigation, and IO timeout.
    timeout_scale: float = 1.0


class UserContextConfiguration(BaseModel):
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
    enabled: bool = False


class ComputerControlConfiguration(BaseModel):
    """Opt-in ability for the agent to control macOS apps through the `computer` tool —
    reading the accessibility tree and clicking/typing/navigating. Off by default because
    it lets the agent drive the whole machine; enabling it also requires the user to grant
    Accessibility access in System Settings."""
    enabled: bool = False


class ComposioConfiguration(BaseModel):
    """Composio integration via its hosted MCP endpoint. When enabled, the harness
    points at Composio's "connect" MCP URL and exposes it as a normal
    streamable_http MCP server, so Composio's tools flow through the same
    list_mcp_tools/call_mcp_tool path as any other MCP server — no new agent, no
    SDK provisioning. Which toolkits (gmail, notion, …) are available is decided
    by the MCP server you configure in the Composio dashboard; the agent then
    discovers tools dynamically through COMPOSIO_SEARCH_TOOLS / COMPOSIO_GET_TOOL_SCHEMAS
    and runs them with COMPOSIO_MULTI_EXECUTE_TOOL, authorizing accounts via
    COMPOSIO_MANAGE_CONNECTIONS on first use."""
    enabled: bool = False
    # The hosted MCP URL from the Composio dashboard (MCP / "connect" page).
    url: str = "https://connect.composio.dev/mcp"
    # The API key shown next to the URL (sent as the x-consumer-api-key header).
    # May also be supplied via COMPOSIO_API_KEY (env wins).
    api_key: str = ""
    # The MCP server name the tools appear under (call_mcp_tool's `server`).
    server_name: str = "composio"
    timeout_seconds: float = 60

    @property
    def effective_api_key(self) -> str:
        return os.environ.get("COMPOSIO_API_KEY") or self.api_key


class MCPServerConfiguration(BaseModel):
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


class MCPConfiguration(BaseModel):
    servers: dict[str, MCPServerConfiguration] = {}

    def enabled_servers(self) -> dict[str, MCPServerConfiguration]:
        return {
            name: server
            for name, server in self.servers.items()
            if server.enabled
        }

    @classmethod
    def from_dotagents_roots(cls, roots: Iterable[Path]) -> "MCPConfiguration":
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


class ProviderCredential(BaseModel):
    """Credentials for one LLM provider. ``base_url`` is only meaningful for the
    OpenAI-compatible providers (opencode and custom); first-party clouds leave it
    blank because LiteLLM knows their endpoints."""

    api_key: str = ""
    base_url: str = ""


class GlobalConfiguration(BaseModel):
    HOME_AGENTS_ROOT_DIRECTORY: ClassVar[str] = "~/.agents"
    AGENTS_ROOT_DIRECTORY: ClassVar[str] = ".agents"
    AGENTS_DIRECTORY: ClassVar[str] = ".agents/agents"
    SKILLS_DIRECTORY: ClassVar[str] = ".agents/skills"

    providers: dict[str, ProviderCredential] = {}
    exa: ExaConfiguration = ExaConfiguration()
    jina: JinaConfiguration = JinaConfiguration()
    firecrawl: FirecrawlConfiguration = FirecrawlConfiguration()
    web_fetch: WebFetchConfiguration = WebFetchConfiguration()
    sandbox: SandboxConfiguration = SandboxConfiguration()
    workspace: WorkspaceConfiguration = WorkspaceConfiguration()
    compaction: CompactionConfiguration = CompactionConfiguration()
    user_context: UserContextConfiguration = UserContextConfiguration()
    computer_control: ComputerControlConfiguration = ComputerControlConfiguration()
    tuning: TuningConfiguration = TuningConfiguration()
    composio: ComposioConfiguration = ComposioConfiguration()
    mcp: MCPConfiguration = MCPConfiguration()
    default_agent: str = "general-assistant"
    # How deep a chain of agents delegating to other agents may go, to bound
    # runaway delegation (agent A spawns B spawns C ...).
    maximum_delegation_depth: int = 8
    maximum_history_age_days: int = 30

    @classmethod
    def load(cls) -> "GlobalConfiguration":
        """Load the configuration from ~/.daisy/configuration.yaml, creating the
        home directory and the file on first run from the packaged configuration."""
        path = configuration_file_path()
        if not path.exists():
            path.write_text(packaged_configuration_yaml())
        return cls.from_yaml(path)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "GlobalConfiguration":
        with open(path) as file_handle:
            data = yaml.safe_load(file_handle)
        configuration = cls(**data)
        configuration.mcp = MCPConfiguration.from_dotagents_roots(configuration.agents_root_directories())
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
        """Configured (non-empty) base URLs per provider (opencode/custom only)."""
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
    # directory* rather than the server's launch CWD, so a session working
    # outside the directory the server happens to have been started in is never
    # advertised that directory's agents/skills/memories/MCP servers — and the
    # paths it is handed are valid for where it actually runs. Each ``*_for``
    # method mirrors its CWD-relative counterpart above; prefer these everywhere
    # a working directory is known (every session and every UI folder selection).

    def _local_base(self, working_directory: str) -> Path:
        """The directory project-relative ``.agents`` roots resolve against — the
        working directory, or the server's CWD as a last resort when none is given."""
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

    def mcp_configuration_for(self, working_directory: str) -> "MCPConfiguration":
        """The MCP servers declared for a working directory: those in the home
        ``mcp.json`` plus the working directory's own, deduped by name (the
        working directory overriding home). Used to filter what the UI lists and
        to grow the shared server pool, never to scope the launch directory in."""
        return MCPConfiguration.from_dotagents_roots(self.agents_root_directories_for(working_directory))


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

    def evaluate_permission(self, command: str) -> str:
        segments = self._extract_segments(command)
        best_match_length = 0
        best_decision = "allow"
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


class SpawnAgentToolConfiguration(BaseModel):
    enabled: bool = True


class ToolsConfiguration(BaseModel):
    bash: BashToolConfiguration = BashToolConfiguration()
    spawn_agent: SpawnAgentToolConfiguration = SpawnAgentToolConfiguration()


class AgentConfiguration(BaseModel):
    name: str = ""
    title: str = ""
    aliases: list[str] = []
    color: str = ""
    description: str = ""
    role: str = ""
    enabled: bool = True
    connection_type: str = "internal"
    # Names of the skills (files in the skills directory) this agent may use.
    # Empty means every available skill is offered to the agent by default.
    skills: list[str] = []
    # The model and its provider are separate fields, mirroring the global config:
    # a human editing an AGENT.md sees both explicitly. ``model_identifier``
    # recombines them into the ``provider/model`` form the factory expects.
    model: Optional[str] = None
    provider: Optional[str] = None
    reasoning_effort: str = "high"
    # Safety bound on the per-turn tool-calling loop. A runtime detail, defaulted
    # here rather than restated in every agent file.
    maximum_iterations: int = 256
    # default: per-command permission rules. auto: use the default rules plus an
    # LLM classifier to auto-approve safe bash calls and escalate the rest.
    # read_only: hard-block all writes (investigation agents). bypass: allow everything.
    permission_mode: Literal["default", "auto", "read_only", "bypass"] = "default"
    tools: ToolsConfiguration = ToolsConfiguration()
    tools_enabled: list[str] = []
    system_prompt: str = ""
    stream_agent_progress: bool = True

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

    @classmethod
    def from_markdown(cls, path: str | Path) -> "AgentConfiguration":
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
        if "connection-type" in frontmatter:
            frontmatter["connection_type"] = frontmatter.pop("connection-type")

        # The per-agent JSON sidecar lives next to the markdown profile.
        configuration_path = path.with_name("configuration.json")
        if configuration_path.exists():
            frontmatter = _merge_agent_configuration(frontmatter, json.loads(configuration_path.read_text()))

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


class PermissionEvaluator:
    def __init__(self, agent_configuration: AgentConfiguration):
        self._configuration = agent_configuration

    def check_tool_enabled(self, tool_name: str) -> None:
        if tool_name != "spawn_agent":
            return
        if (
            self._configuration.tools_enabled
            and tool_name not in self._configuration.tools_enabled
        ):
            raise PermissionError(
                f"Tool '{tool_name}' is not enabled for agent '{self._configuration.identifier}'"
            )

    def evaluate_bash_permission(self, command: str) -> str:
        return self._configuration.tools.bash.evaluate_permission(command)

    def check_bash_background(self) -> None:
        if not self._configuration.tools.bash.background_allowed:
            raise PermissionError("Background bash execution is not allowed")

    def check_tool(self, tool_name: str, /, **arguments) -> None:
        # tool_name is positional-only so that a tool whose own arguments include a
        # key named "tool_name" (e.g. call_mcp_tool) does not collide with it.
        self.check_tool_enabled(tool_name)


class PermissionError(RuntimeError):
    pass


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

    @staticmethod
    def _replace_variables(template: str, variables: dict[str, str], template_name: str = "") -> str:
        """Substitute ``{{ name }}`` placeholders (spaced or not) from ``variables``.

        Rendering is strict: a placeholder with no matching variable, or any ``{{ … }}``
        left standing after substitution (a malformed or unknown placeholder), raises
        rather than silently shipping the raw braces into a prompt the model would see."""
        where = f" in prompt '{template_name}'" if template_name else ""

        def replacer(match: re.Match[str]) -> str:
            variable_name = match.group(1)
            if variable_name not in variables:
                raise ValueError(
                    f"Unresolved placeholder '{{{{ {variable_name} }}}}'{where}: no value was provided (given: {sorted(variables)})."
                )
            return variables[variable_name]

        # Accept both the spaced ({{ name }}) and unspaced ({{name}}) forms.
        rendered = re.sub(r"\{\{\s*(\w+)\s*\}\}", replacer, template)
        # Anything of the {{ … }} shape still present could not be substituted — a bad name
        # or unsupported syntax. Fail loudly instead of leaking it into the prompt.
        leftover = re.search(r"\{\{.*?\}\}", rendered, re.DOTALL)
        if leftover is not None:
            raise ValueError(f"Unsubstituted placeholder {leftover.group(0)!r}{where} after rendering.")
        return rendered


def _as_directories(directories: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(directories, (str, Path)):
        return [Path(directories).expanduser()]
    return [Path(directory).expanduser() for directory in directories]


def _agent_paths(agents_directories: str | Path | Iterable[str | Path], include_aliases: bool = False) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for directory in _as_directories(agents_directories):
        if not directory.is_dir():
            continue
        candidates = [*sorted(directory.glob("*.md")), *sorted(directory.glob("*/AGENT.md"))]
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


def list_available_agents(agents_directory: str | Path | Iterable[str | Path]) -> list[str]:
    return sorted(_agent_paths(agents_directory))


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


def describe_available_agents(
    agents_directory: str | Path | Iterable[str | Path]
) -> list[dict[str, str]]:
    """Available agents with the metadata a delegating model needs to choose one.

    `list_available_agents` returns bare ids, which tells the model *that* it can
    delegate but not *to whom* or *for what* — so it can't match a task to the
    right specialist and tends to do everything itself. This carries each agent's
    human title, its `description` (what it is for), and its declared `role`
    (e.g. `delegation-target`) so the model can pick deliberately.
    """
    described = []
    for name, path in sorted(_agent_paths(agents_directory).items()):
        try:
            config = AgentConfiguration.from_markdown(path)
            described.append({
                "id": config.identifier,
                "title": config.display_name,
                "description": config.description,
                "role": config.role,
            })
        except Exception:
            described.append({"id": name, "title": name, "description": "", "role": ""})
    return described


def _merge_agent_configuration(frontmatter: dict, configuration: dict) -> dict:
    merged = dict(frontmatter)
    # ``preset`` carries the model, provider, and reasoning effort; ``tools``
    # carries the bash/spawn-agent/tool toggles.
    model_configuration = configuration.get("preset", {})
    if "model" in model_configuration:
        merged["model"] = model_configuration["model"]
    if "provider" in model_configuration:
        merged["provider"] = model_configuration["provider"]
    if "reasoningEffort" in model_configuration:
        merged["reasoning_effort"] = model_configuration["reasoningEffort"]
    if "reasoning_effort" in model_configuration:
        merged["reasoning_effort"] = model_configuration["reasoning_effort"]
    if "permissionMode" in configuration:
        merged["permission_mode"] = configuration["permissionMode"]
    if "permission_mode" in configuration:
        merged["permission_mode"] = configuration["permission_mode"]
    if "streamAgentProgress" in configuration:
        merged["stream_agent_progress"] = configuration["streamAgentProgress"]
    tool_configuration = configuration.get("tools", {})
    if tool_configuration:
        tools = dict(merged.get("tools", {}))
        if "enabledBuiltinTools" in tool_configuration:
            merged["tools_enabled"] = tool_configuration["enabledBuiltinTools"]
        if "bash" in tool_configuration:
            bash = dict(tool_configuration["bash"])
            if "backgroundAllowed" in bash:
                bash["background_allowed"] = bash.pop("backgroundAllowed")
            tools["bash"] = bash
        if "spawnAgent" in tool_configuration:
            tools["spawn_agent"] = tool_configuration["spawnAgent"]
        if "spawn_agent" in tool_configuration:
            tools["spawn_agent"] = tool_configuration["spawn_agent"]
        merged["tools"] = tools
    return merged
