from __future__ import annotations

from collections.abc import Iterable
import json
import logging
import os
from langmesh.base import environment_variables
from langmesh.base.tuning import Scaling
import re
from fnmatch import fnmatch
import shutil
import sys
from pathlib import Path
from typing import ClassVar, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from langmesh.base.paths import configuration_file_path, database_file_path  # noqa: F401 — re-exported
from langmesh.base.permission_mode import PermissionMode


logger = logging.getLogger(__name__)


# Where state lives is the placement layer's business, resolved in `langmesh.base.paths`.

# The packaged configuration is a sibling YAML file, so editing the template is a data change.
PACKAGED_CONFIGURATION_PATH = Path(__file__).resolve().parent / "configuration.yaml"


def packaged_configuration_yaml() -> str:
    return PACKAGED_CONFIGURATION_PATH.read_text()


def _bundled_dotagents_root() -> Path:
    """The ``.agents`` directory shipped with the harness, so every folder sees the base profiles."""
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
    """Seed ``~/.agents`` with editable copies, filling only what is missing so a person's edits survive."""
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
    toolbox_enabled: bool | None = None,
    dictation_enabled: bool | None = None,
    tuning: dict | None = None,
    daemon: dict | None = None,
    provider_keys: dict[str, str] | None = None,
    provider_base_urls: dict[str, str] | None = None,
) -> None:
    """Persist settings into the configuration file, preserving the rest and writing only what was given."""
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
    if toolbox_enabled is not None:
        data.setdefault("toolbox", {})["enabled"] = toolbox_enabled
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
    """A part of the configuration file, refusing every key it does not define so a typo is an error."""

    model_config = {"extra": "forbid"}


class ExaConfiguration(Section):
    """Exa — the web-search tool's backend."""

    api_key: str = Field("", json_schema_extra={"secret": True})

    @property
    def effective_api_key(self) -> str:
        return os.environ.get(environment_variables.EXA_API_KEY) or self.api_key


class JinaConfiguration(Section):
    """Jina Reader, the web-fetch tool's default engine. The key is optional: it works keyless, slower."""

    api_key: str = Field("", json_schema_extra={"secret": True})

    @property
    def effective_api_key(self) -> str:
        return os.environ.get(environment_variables.JINA_API_KEY) or self.api_key


class FirecrawlConfiguration(Section):
    """Firecrawl, the fallback for pages Jina returns thin. Without a key the fallback is skipped."""

    api_key: str = Field("", json_schema_extra={"secret": True})
    api_url: str = Field("")

    @property
    def effective_api_key(self) -> str:
        return os.environ.get(environment_variables.FIRECRAWL_API_KEY) or self.api_key

    @property
    def effective_api_url(self) -> str:
        return os.environ.get(environment_variables.FIRECRAWL_API_URL) or self.api_url


class WebFetchConfiguration(Section):
    """Fetching a page directly, for sites that refuse the reader engines."""

    proxy_url: str = Field("")

    @property
    def effective_proxy_url(self) -> str:
        return os.environ.get(environment_variables.FETCH_PROXY) or self.proxy_url


class FilesystemConfiguration(Section):
    """Which paths a tool's child may read and write. The system stays readable; the home is closed."""

    readable: list[str] = Field(
        default=[
            # Where a person's own agents, skills and workflows live, which a screen script imports from.
            "~/.agents",
            "~/.config", "~/.local", "~/.ssh", "~/.gitconfig", "~/.gitignore_global",
            "~/.cargo", "~/.rustup", "~/.npmrc", "~/.nvm", "~/.pyenv", "~/.docker", "~/.netrc",
        ],
    )
    writable: list[str] = Field(default=["$WORKSPACE", "$TMPDIR", "/tmp", "$XDG_CACHE_HOME", "~/.cache"])
    # `/tmp` beside `$TMPDIR` because on macOS they are not the same place, and nothing personal lives there.
    grantable: list[str] = Field(default=[])
    deny: list[str] = Field(default=[
            "~/Documents", "~/Desktop", "~/Downloads", "~/Pictures", "~/Movies", "~/Music",
            "~/Library/Mail", "~/Library/Messages", "~/Library/Safari",
        ])


class SandboxConfiguration(Section):
    """A session's confinement, enforced by the operating system. Every other field is POSIX under its own name."""

    enforce: Literal["required", "preferred", "off"] = Field("required")
    filesystem: FilesystemConfiguration = Field(default_factory=FilesystemConfiguration)
    network: bool = Field(False)
    limits: dict[str, int] = Field(default={
            "RLIMIT_CORE": 0,
            "RLIMIT_FSIZE": 8 * 1024 * 1024 * 1024,
            "RLIMIT_NPROC": 2048,
        })
    umask: Optional[str] = Field(None)
    nice: int = Field(0)

    def to_profile(self):
        """This configuration as the :class:`~langmesh.base.confinement.Profile` the spawn path applies."""
        from langmesh.base import confinement

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

    strategy: Literal["none", "branch", "worktree"] = Field("none")


#: Renamed compaction settings, since a section that refuses unknown keys would refuse the whole file.
_COMPACTION_RENAMED = {
    "auto": "automatic",
    "observer_context_fraction": "reclaim_at_fraction",
}

#: Compaction settings that are gone with nothing standing in for them.
_COMPACTION_REMOVED = (
    "keep_recent_turns", "preserve_recent_tokens", "prune", "prune_tool_results",
    "pruned_result_tokens", "condense_log_at_fraction", "reflector_observation_fraction",
)


class CompactionConfiguration(Section):
    """Conversation memory: each exchange is recorded as it closes, and the turns behind the window are dropped."""

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

    automatic: bool = Field(True)
    reclaim_at_fraction: float = Field(0.85)
    output_reserve_fraction: float = Field(0.1)
    recent_working_set_fraction: float = Field(0.25)
    verbatim_user_fraction: float = Field(0.1)


class ContextShareConfiguration(Section):
    """What proportion of the live window one result may fill, read from the scaling families."""

    text: float = Field(Scaling.TEXT.value.calibrated)
    results: float = Field(Scaling.RESULTS.value.calibrated)


class TuningConfiguration(Section):
    """How large, how many and how patient the tools are, derived from the live context window."""

    context_share: ContextShareConfiguration = Field(default_factory=ContextShareConfiguration)
    timeout_multiplier: float = Field(1.0)
    defaults: dict[str, float] = Field(default_factory=dict)

    @field_validator("defaults")
    @classmethod
    def _known_defaults(cls, value: dict[str, float]) -> dict[str, float]:
        from langmesh.base.tuning import unknown_tunable_names

        unknown = unknown_tunable_names(value)
        if unknown:
            raise ValueError(
                f"unknown tuning default(s): {', '.join(unknown)}. "
                "The names that exist are the members of `langmesh.base.tuning.Tunable`; "
                "`langmesh configure --all` lists them with their defaults."
            )
        return value


class UserContextConfiguration(Section):
    """Opt-in snapshot of how the user works on this machine, folded into the system prompt."""

    enabled: bool = Field(False)
    refresh_hours: float = Field(6.0, gt=0)


class SettleConfiguration(Section):
    """How long to wait for a surface to stop changing, polled so a fast page costs one interval."""

    poll_seconds: float = Field(0.05)
    give_up_seconds: float = Field(1.5)


class RetrievalConfiguration(Section):
    """How a screen is ranked when a script asks for an element by name. The defaults are fitted, not chosen."""

    multilingual_rank_model: str = Field("minishlab/M2V_multilingual_output")
    english_rank_model: str = Field("minishlab/potion-base-32M")
    lexical_gate_short_words: int = Field(3, ge=0)
    lexical_gate_long_words: int = Field(7, ge=1)


class ComputerControlConfiguration(Section):
    """Opt-in control of macOS apps through the screen tools. Off by default, and needs an Accessibility grant."""

    enabled: bool = Field(False)
    settle: SettleConfiguration = Field(default_factory=SettleConfiguration)
    retrieval: RetrievalConfiguration = Field(default_factory=RetrievalConfiguration)


class PermissionReviewerConfiguration(Section):
    """The model call that answers a gate where nobody is there, on the session's model at a lower effort."""

    reasoning_effort: Literal["minimal", "low", "medium", "high"] = Field("low")


class ToolboxConfiguration(Section):
    """Whether a session may install tools for itself, into a directory deleted when it ends."""

    enabled: bool = Field(True)


class DictationTimingConfiguration(Section):
    """How long dictation waits before giving up, separated because these are what a slow machine must move."""

    minimum_transcription_timeout_seconds: float = Field(30.0)
    transcription_timeout_realtime_multiplier: float = Field(0.5)
    maximum_attempts: int = Field(2, ge=1)
    worker_shutdown_seconds: float = Field(2.0)


class DictationConfiguration(Section):
    """Opt-in speech-to-text, transcribed locally. Off by default: the first use downloads about a gigabyte."""

    enabled: bool = Field(False)
    model: str = Field("mlx-community/parakeet-tdt-0.6b-v3")
    timing: DictationTimingConfiguration = Field(default_factory=DictationTimingConfiguration)


class ComposioConfiguration(Section):
    """Composio's hosted MCP endpoint, exposed as an ordinary streamable_http server."""

    enabled: bool = Field(False)
    url: str = Field("https://connect.composio.dev/mcp")
    api_key: str = Field("", json_schema_extra={"secret": True})
    server_name: str = Field("composio")
    timeout_seconds: float = Field(60)

    @property
    def effective_api_key(self) -> str:
        return os.environ.get(environment_variables.COMPOSIO_API_KEY) or self.api_key


class MCPServerConfiguration(BaseModel):
    """One MCP server from an ``mcp.json`` entry, permissive about keys it does not model."""

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
    """The MCP servers available to a session, from ``mcp.json`` rather than the configuration file."""

    servers: dict[str, MCPServerConfiguration] = Field(default={})

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
    """How to authenticate to one external agent. ``${VAR}`` expands at load, so tokens stay out of the file."""

    type: Literal["none", "bearer", "api_key", "oauth2"] = "none"
    token: str = ""
    header: str = "Authorization"
    scheme_prefix: str = "Bearer"
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = []


class RemoteAgentServerConfiguration(BaseModel):
    """One registered external A2A agent, permissive about unmodelled keys for the same reason."""

    enabled: bool = True
    card_url: str = ""
    auth: RemoteAgentAuthConfiguration = RemoteAgentAuthConfiguration()
    card_ttl_seconds: int = 3600
    # Hostnames allowed beyond the card_url origin (the origin is always allowed).
    allowed_hosts: list[str] = []
    # Permit private/loopback targets (e.g. a LangMesh-to-LangMesh loopback).
    allow_private: bool = False
    # Which local agent profiles may delegate to this remote agent. Empty = all profiles.
    allowed_profiles: list[str] = []


class RemoteAgentsConfiguration(Section):
    """The external agents the harness may delegate to, from ``remote-agents.json``."""

    agents: dict[str, RemoteAgentServerConfiguration] = Field(default={})

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

    endpoint: str = Field("")
    protocol: Literal["http/protobuf", "grpc"] = Field("http/protobuf")
    headers: dict[str, str] = Field(default={})


class TelemetryConfiguration(Section):
    """OTLP export of traces, off until an endpoint is set, and carrying no prompt or completion bodies."""

    enabled: bool = Field(False)
    exporter: TelemetryExporterConfiguration = Field(default_factory=TelemetryExporterConfiguration)
    sample_ratio: float = Field(1.0)

    def resolved_headers(self) -> dict[str, str]:
        return {key: os.path.expandvars(value) for key, value in self.exporter.headers.items()}


class ProviderCredential(Section):
    """Credentials for one model provider."""

    api_key: str = Field("", json_schema_extra={"secret": True})
    base_url: str = Field("")


class AgentDefaults(Section):
    """What a session gets when its creator did not say."""

    permission_mode: Literal["ask", "automatic"] = Field("ask")


class Configuration(Section):
    HOME_AGENTS_ROOT_DIRECTORY: ClassVar[str] = "~/.agents"
    AGENTS_ROOT_DIRECTORY: ClassVar[str] = ".agents"
    AGENTS_DIRECTORY: ClassVar[str] = ".agents/agents"
    SKILLS_DIRECTORY: ClassVar[str] = ".agents/skills"

    providers: dict[str, ProviderCredential] = Field(default={})
    exa: ExaConfiguration = Field(default_factory=ExaConfiguration)
    jina: JinaConfiguration = Field(default_factory=JinaConfiguration)
    firecrawl: FirecrawlConfiguration = Field(default_factory=FirecrawlConfiguration)
    web_fetch: WebFetchConfiguration = Field(default_factory=WebFetchConfiguration)
    sandbox: SandboxConfiguration = Field(default_factory=SandboxConfiguration)
    workspace: WorkspaceConfiguration = Field(default_factory=WorkspaceConfiguration)
    compaction: CompactionConfiguration = Field(default_factory=CompactionConfiguration)
    user_context: UserContextConfiguration = Field(default_factory=UserContextConfiguration)
    computer_control: ComputerControlConfiguration = Field(default_factory=ComputerControlConfiguration)
    toolbox: ToolboxConfiguration = Field(default_factory=ToolboxConfiguration)
    permission_reviewer: PermissionReviewerConfiguration = Field(default_factory=PermissionReviewerConfiguration)
    dictation: DictationConfiguration = Field(default_factory=DictationConfiguration)
    tuning: TuningConfiguration = Field(default_factory=TuningConfiguration)
    composio: ComposioConfiguration = Field(default_factory=ComposioConfiguration)
    mcp: MCPConfiguration = Field(default_factory=MCPConfiguration)
    remote_agents: RemoteAgentsConfiguration = Field(default_factory=RemoteAgentsConfiguration)
    telemetry: TelemetryConfiguration = Field(default_factory=TelemetryConfiguration)
    agent: AgentDefaults = Field(default_factory=AgentDefaults)

    @classmethod
    def load(cls, *, seed: bool = True) -> Configuration:
        """Load the configuration, seeding the file from the packaged template on first run."""
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
        """The non-empty API keys per provider, for credential resolution and for filtering the model picker."""
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
            # Bundled agents are the base layer; home and project profiles override one of the same id.
            BUNDLED_DOTAGENTS_ROOT / "agents",
            Path(self.HOME_AGENTS_ROOT_DIRECTORY).expanduser() / "agents",
            Path(self.AGENTS_ROOT_DIRECTORY) / "agents",
            Path(self.AGENTS_DIRECTORY),
        ])

    def skill_directories(self) -> list[Path]:
        return _dedupe_paths([
            # Bundled skills are the base layer, exactly like agents.
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

    # Project-relative roots resolve against the session's working directory, not the harness's CWD.

    def _local_base(self, working_directory: str) -> Path:
        """What project-relative ``.agents`` roots resolve against, falling back to the harness's CWD."""
        return Path(working_directory).expanduser() if working_directory else Path.cwd()

    def _resolve_local(self, working_directory: str, directory: str) -> Path:
        path = Path(directory).expanduser()
        return path if path.is_absolute() else self._local_base(working_directory) / path

    def home_agents_root(self) -> Path:
        """The global ``~/.agents`` root — the scope shared by every folder."""
        return Path(self.HOME_AGENTS_ROOT_DIRECTORY).expanduser()

    def project_agents_root_for(self, working_directory: str) -> Path:
        """The working directory's own ``.agents`` root, which equals the home root when they are the same place."""
        return self._resolve_local(working_directory, self.AGENTS_ROOT_DIRECTORY)

    def agents_root_directories_for(self, working_directory: str) -> list[Path]:
        return _dedupe_paths([
            self.home_agents_root(),
            self.project_agents_root_for(working_directory),
        ])

    def agent_directories_for(self, working_directory: str) -> list[Path]:
        return _dedupe_paths([
            # Bundled agents are the base layer; home and project profiles override one of the same id.
            BUNDLED_DOTAGENTS_ROOT / "agents",
            Path(self.HOME_AGENTS_ROOT_DIRECTORY).expanduser() / "agents",
            self._resolve_local(working_directory, self.AGENTS_ROOT_DIRECTORY) / "agents",
            self._resolve_local(working_directory, self.AGENTS_DIRECTORY),
        ])

    def skill_directories_for(self, working_directory: str) -> list[Path]:
        return _dedupe_paths([
            # Bundled skills are the base layer, exactly like agents.
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
        """The MCP servers declared for a working directory: home plus its own, deduped, the folder winning."""
        return MCPConfiguration.from_dotagents_roots(self.agents_root_directories_for(working_directory))

    def remote_agents_configuration_for(self, working_directory: str) -> RemoteAgentsConfiguration:
        """The external agents declared for a working directory: home plus its own."""
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
    """Per-call permission rules for a tool whose calls have a name."""

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

    #: Commands whose damage is not bounded by the confinement, and what to do about each.
    DESTRUCTIVE_DEFAULTS: ClassVar[dict[str, str]] = {
        "rm -rf *": "ask",
        "rm -fr *": "ask",
        "rm -r *": "ask",
        "git reset --hard*": "ask",
        "git clean -*": "ask",
        "git push --force*": "ask",
        "git push -f*": "ask",
        "sudo *": "ask",
        "chmod -R *": "ask",
        "chown -R *": "ask",
        "dd *": "ask",
        "mkfs*": "deny",
        "shutdown*": "deny",
        "reboot*": "deny",
    }

    @property
    def effective_permissions(self) -> dict[str, str]:
        """The rules actually in force: the destructive seeds, with the person's own over them."""
        return {**self.DESTRUCTIVE_DEFAULTS, **self.permissions}

    def evaluate_permission(self, command: str, unmatched: str = "allow") -> str:
        """The configured decision for `command`, with `unmatched` returned when no pattern matches."""
        segments = self._extract_segments(command)
        rules = self.effective_permissions
        best_match_length = 0
        best_decision = unmatched
        for segment in segments:
            for pattern, decision in rules.items():
                if self._segment_matches(segment, pattern):
                    if not best_match_length or len(pattern) > best_match_length:
                        best_match_length = len(pattern)
                        best_decision = decision.lower()
        return best_decision

    def command_matches(self, command: str, patterns: Iterable[str]) -> bool:
        """Whether any segment of `command` matches any of `patterns`."""
        segments = self._extract_segments(command)
        return any(
            self._segment_matches(segment, pattern)
            for segment in segments
            for pattern in patterns
            if pattern
        )

    def _extract_segments(self, command: str) -> list[str]:
        """Split a command into segments, on shell operators and through subshells."""
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
    """Which of the harness's tools an agent has, and how the ones with settings behave."""

    bash: BashToolConfiguration = BashToolConfiguration()
    # The other two tools whose calls can be named and ruled on; empty leaves the default in force.
    mcp: NamedToolPermissions = NamedToolPermissions()
    screen: NamedToolPermissions = NamedToolPermissions()
    disabled: list[str] = Field(default_factory=list)

    def is_enabled(self, tool_name: str) -> bool:
        """Whether this agent may use `tool_name` at all."""
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
    # The skills this agent may use; empty offers every available one.
    skills: list[str] = []
    # The model and its provider are separate fields, recombined into an identifier where one is wanted.
    model: Optional[str] = None
    provider: Optional[str] = None
    reasoning_effort: str = "high"
    # A ceiling: the loosest mode a session on this profile may have. `None` is the default, and bounds nothing.
    permission_mode: Optional[Literal["ask", "automatic"]] = None

    # An agent's own confinement, narrowing the global one. Unset means whatever the machine says.
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
        """The card's mode, or ``None`` where it declares none. A ceiling, so absent must not be coerced to a value."""
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

        # Everything the agent is, in one file: front matter for the settings, body for the prompt.
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
    """Write a profile back to its `AGENT.md`, the body verbatim so a round trip cannot reword the prompt."""
    path = Path(path)
    body = configuration.system_prompt.strip()
    front = configuration.model_dump(
        mode="json", exclude_defaults=True, exclude_none=True, exclude={"system_prompt"},
    )
    # `name` is the identity the harness addresses this agent by, stated even when it matches the directory.
    front.setdefault("name", configuration.identifier)
    rendered = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{rendered}\n---\n\n{body}\n" if body else f"---\n{rendered}\n---\n")


class PermissionEvaluator:
    def __init__(self, agent_configuration: AgentConfiguration):
        self._configuration = agent_configuration

    def check_tool_enabled(self, tool_name: str) -> None:
        """Refuse a tool the profile does not list, whatever `tools_enabled` names."""
        if (
            self._configuration.tools_enabled
            and tool_name not in self._configuration.tools_enabled
        ):
            raise PermissionDenied(
                f"Tool '{tool_name}' is not enabled for agent '{self._configuration.identifier}'"
            )

    def check_tool_not_disabled(self, tool_name: str) -> None:
        """Refuse a tool the profile switched off, at call time too, since a model may call one it was never offered."""
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
        # Positional-only, so a tool whose own arguments include `tool_name` does not collide with it.
        self.check_tool_enabled(tool_name)
        self.check_tool_not_disabled(tool_name)


class PermissionDenied(RuntimeError):
    """A tool call refused by policy rather than by the operating system, and named apart from the builtin."""


class PromptLoader:
    def __init__(self, prompts_directory: str | Path, extension: str = "md"):
        self._directory = Path(prompts_directory)
        self._extension = extension

    def load(self, template_name: str, variables: dict[str, str]) -> str:
        """A template rendered with these variables, its text read from disk only when the file has changed."""
        from langmesh.base.file_cache import parsed_file

        path = self._directory / f"{template_name}.{self._extension}"
        content = parsed_file(path, lambda each: each.read_text())
        if content is None:
            return ""
        return self._replace_variables(content, variables, template_name)

    @classmethod
    def render(cls, template: str, variables: dict[str, str], template_name: str = "") -> str:
        """Render a template already in hand, for a catalogue that carries its prompts in memory."""
        return cls._replace_variables(template, variables, template_name)

    @staticmethod
    def _replace_variables(template: str, variables: dict[str, str], template_name: str = "") -> str:
        """Substitute ``{{ name }}`` placeholders strictly: a missing variable or a malformed brace raises."""
        where = f" in prompt '{template_name}'" if template_name else ""
        placeholder = re.compile(r"\{\{\s*(\w+)\s*\}\}")

        def drop_if_empty(match: re.Match[str]) -> str:
            name = match.group(1)
            supplied = variables.get(name)
            return "" if supplied is not None and not str(supplied).strip() else match.group(0)

        # The placeholder's own line, plus one blank line after it if there is one.
        own_line = r"^[ \t]*\{\{\s*(\w+)\s*\}\}[ \t]*"
        template = re.sub(own_line + r"\n(?:[ \t]*\n)?", drop_if_empty, template, flags=re.M)

        # A placeholder alone on a line contributes its content only, since the template's newline already ends it.
        sections = set(re.findall(own_line + r"$", template, flags=re.M))
        variables = {
            name: str(value).strip() if name in sections else value
            for name, value in variables.items()
        }

        # A malformed brace is a template bug, caught before substitution so it is not read as stray output.
        malformed = re.search(r"\{\{.*?\}\}", placeholder.sub("", template), re.DOTALL)
        if malformed is not None:
            raise ValueError(f"Malformed placeholder {malformed.group(0)!r}{where}.")

        def replacer(match: re.Match[str]) -> str:
            variable_name = match.group(1)
            if variable_name not in variables:
                raise ValueError(
                    f"Unresolved placeholder '{{{{ {variable_name} }}}}'{where}: no value was provided (given: {sorted(variables)})."
                )
            # Rendered rather than required to be a string, so an `int` does not raise out of `re.sub`.
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
        # `AGENT.md`, in that spelling, exactly as a skill is `SKILL.md`.
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
                # The resolved `provider/model`; empty means no runnable model is configured.
                "model": config.model_identifier or "",
            })
        except Exception:
            agents.append({"id": name, "name": name, "title": name, "description": "", "model": ""})
    return agents


