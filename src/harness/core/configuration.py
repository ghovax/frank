from collections.abc import Iterable
import json
import os
import re
import shlex
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel


# The harness keeps its mutable state — the configuration file and the chat
# history database — under a dot-directory in the user's home, not inside the
# repository. The home directory is the single source of truth.
HARNESS_HOME_DIRECTORY = Path("~/.harness").expanduser()
CONFIGURATION_FILENAME = "configuration.yaml"
EXAMPLE_CONFIGURATION_FILENAME = "configuration.yaml.example"
DATABASE_FILENAME = "history.db"

# Used only as a last resort when seeding ~/.harness/configuration.yaml and no
# in-repo configuration.yaml or configuration.yaml.example is available. Keep in
# sync with configuration.yaml.example, which is the human-facing reference.
DEFAULT_CONFIGURATION_YAML = """\
api:
  endpoint: "https://opencode.ai/zen/go/v1"
  model: "deepseek-v4-flash"
  api_key: ""

exa:
  api_key: ""

sandbox:
  enabled: true

default_agent: assistant
agents_root_directory: .agents
home_agents_root_directory: ~/.agents
agents_directory: .agents/agents
skills_directory: .agents/skills
"""


def harness_home_directory() -> Path:
    """The ~/.harness directory, created on first use."""
    HARNESS_HOME_DIRECTORY.mkdir(parents=True, exist_ok=True)
    return HARNESS_HOME_DIRECTORY


def configuration_file_path() -> Path:
    return harness_home_directory() / CONFIGURATION_FILENAME


def database_file_path() -> Path:
    return harness_home_directory() / DATABASE_FILENAME


def save_api_keys(
    *,
    api_key: str | None = None,
    exa_api_key: str | None = None,
    composio_consumer_api_key: str | None = None,
    sandbox_enabled: bool | None = None,
) -> None:
    """Persist settings into ~/.harness/configuration.yaml, preserving the rest
    of the file. Only provided values are written. Creates the file from the
    default template if it does not exist yet."""
    path = configuration_file_path()
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    else:
        data = yaml.safe_load(DEFAULT_CONFIGURATION_YAML)
    if api_key is not None:
        data.setdefault("api", {})["api_key"] = api_key
    if exa_api_key is not None:
        data.setdefault("exa", {})["api_key"] = exa_api_key
    if composio_consumer_api_key is not None:
        data.setdefault("composio", {})["consumer_api_key"] = composio_consumer_api_key
    if sandbox_enabled is not None:
        data.setdefault("sandbox", {})["enabled"] = sandbox_enabled
    path.write_text(yaml.safe_dump(data, sort_keys=False))


class ApiConfiguration(BaseModel):
    endpoint: str
    model: str
    api_key: str = ""

    @property
    def effective_api_key(self) -> str:
        return os.environ.get("OPENCODE_API_KEY") or self.api_key


class ExaConfiguration(BaseModel):
    api_key: str = ""

    @property
    def effective_api_key(self) -> str:
        return os.environ.get("EXA_API_KEY") or self.api_key


class SandboxConfiguration(BaseModel):
    enabled: bool = True


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
    # The consumer key shown next to the URL (sent as the x-consumer-api-key
    # header). May also be supplied via COMPOSIO_CONSUMER_API_KEY (env wins).
    consumer_api_key: str = ""
    # The MCP server name the tools appear under (call_mcp_tool's `server`).
    server_name: str = "composio"
    timeout_seconds: float = 60

    @property
    def effective_consumer_api_key(self) -> str:
        return os.environ.get("COMPOSIO_CONSUMER_API_KEY") or self.consumer_api_key


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


class GlobalConfiguration(BaseModel):
    api: ApiConfiguration
    exa: ExaConfiguration = ExaConfiguration()
    sandbox: SandboxConfiguration = SandboxConfiguration()
    composio: ComposioConfiguration = ComposioConfiguration()
    mcp: MCPConfiguration = MCPConfiguration()
    default_agent: str = "assistant"
    agents_root_directory: str = ".agents"
    home_agents_root_directory: str = "~/.agents"
    agents_directory: str = ".agents/agents"
    skills_directory: str = ".agents/skills"
    # How deep a chain of agents delegating to other agents may go, to bound
    # runaway delegation (agent A spawns B spawns C ...).
    maximum_delegation_depth: int = 8
    maximum_history_age_days: int = 30

    @classmethod
    def load(cls) -> "GlobalConfiguration":
        """Load the configuration from ~/.harness/configuration.yaml, creating the
        home directory and the file on first run. The seed is taken from, in order:
        a legacy configuration.yaml in the working directory (migrated), the
        in-repo configuration.yaml.example, then a built-in default."""
        path = configuration_file_path()
        if not path.exists():
            legacy_path = Path(CONFIGURATION_FILENAME)
            example_path = Path(EXAMPLE_CONFIGURATION_FILENAME)
            if legacy_path.exists():
                path.write_text(legacy_path.read_text())
            elif example_path.exists():
                path.write_text(example_path.read_text())
            else:
                path.write_text(DEFAULT_CONFIGURATION_YAML)
        return cls.from_yaml(path)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "GlobalConfiguration":
        with open(path) as file_handle:
            data = yaml.safe_load(file_handle)
        configuration = cls(**data)
        configuration.mcp = MCPConfiguration.from_dotagents_roots(configuration.agents_root_directories())
        return configuration

    def agents_root_directories(self) -> list[Path]:
        return _dedupe_paths([
            Path(self.home_agents_root_directory).expanduser(),
            Path(self.agents_root_directory),
        ])

    def agent_directories(self) -> list[Path]:
        return _dedupe_paths([
            Path(self.home_agents_root_directory).expanduser() / "agents",
            Path(self.agents_root_directory) / "agents",
            Path(self.agents_directory),
        ])

    def skill_directories(self) -> list[Path]:
        return _dedupe_paths([
            Path(self.home_agents_root_directory).expanduser() / "skills",
            Path(self.agents_root_directory) / "skills",
            Path(self.skills_directory),
        ])

    def memory_directories(self) -> list[Path]:
        return _dedupe_paths([
            Path(self.home_agents_root_directory).expanduser() / "memories",
            Path(self.agents_root_directory) / "memories",
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
        return Path(self.home_agents_root_directory).expanduser()

    def project_agents_root_for(self, working_directory: str) -> Path:
        """The working directory's own ``.agents`` root — the project-local scope.
        Equals :meth:`home_agents_root` when the working directory is the home
        directory (in which case nothing is project-specific)."""
        return self._resolve_local(working_directory, self.agents_root_directory)

    def agents_root_directories_for(self, working_directory: str) -> list[Path]:
        return _dedupe_paths([
            self.home_agents_root(),
            self.project_agents_root_for(working_directory),
        ])

    def agent_directories_for(self, working_directory: str) -> list[Path]:
        return _dedupe_paths([
            Path(self.home_agents_root_directory).expanduser() / "agents",
            self._resolve_local(working_directory, self.agents_root_directory) / "agents",
            self._resolve_local(working_directory, self.agents_directory),
        ])

    def skill_directories_for(self, working_directory: str) -> list[Path]:
        return _dedupe_paths([
            Path(self.home_agents_root_directory).expanduser() / "skills",
            self._resolve_local(working_directory, self.agents_root_directory) / "skills",
            self._resolve_local(working_directory, self.skills_directory),
        ])

    def memory_directories_for(self, working_directory: str) -> list[Path]:
        return _dedupe_paths([
            Path(self.home_agents_root_directory).expanduser() / "memories",
            self._resolve_local(working_directory, self.agents_root_directory) / "memories",
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
    model: Optional[str] = None
    reasoning_effort: str = "high"
    # Safety bound on the per-turn tool-calling loop. A runtime detail, defaulted
    # here rather than restated in every agent file.
    maximum_iterations: int = 25
    # default: per-command permission rules. read_only: hard-block all writes
    # (investigation agents). bypass: allow everything.
    permission_mode: Literal["default", "read_only", "bypass"] = "default"
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
        default_identifier = path.parent.name if path.name == "agent.md" else path.stem
        frontmatter.setdefault("name", default_identifier)
        frontmatter.setdefault("title", frontmatter["name"])
        if "connection-type" in frontmatter:
            frontmatter["connection_type"] = frontmatter.pop("connection-type")

        configuration_path = path.with_name("config.json")
        if configuration_path.exists():
            frontmatter = _merge_agent_config(frontmatter, json.loads(configuration_path.read_text()))

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
        return self._replace_variables(content, variables)

    @staticmethod
    def _replace_variables(template: str, variables: dict[str, str]) -> str:
        def replacer(match: re.Match) -> str:
            variable_name = match.group(1)
            return variables.get(variable_name, match.group(0))

        return re.sub(r"\{\{\s*(\w+)\s*\}\}", replacer, template)


def _as_directories(directories: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(directories, (str, Path)):
        return [Path(directories).expanduser()]
    return [Path(directory).expanduser() for directory in directories]


def _agent_paths(agents_directories: str | Path | Iterable[str | Path], include_aliases: bool = False) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for directory in _as_directories(agents_directories):
        if not directory.is_dir():
            continue
        candidates = [*sorted(directory.glob("*.md")), *sorted(directory.glob("*/agent.md"))]
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
                fallback = path.parent.name if path.name == "agent.md" else path.stem
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


def list_available_agents(agents_directory: str | Path | Iterable[str | Path]) -> list[str]:
    return sorted(_agent_paths(agents_directory))


def list_agent_route_names(agents_directory: str | Path | Iterable[str | Path]) -> list[str]:
    return sorted(_agent_paths(agents_directory, include_aliases=True))


def list_agents(agents_directory: str | Path | Iterable[str | Path]) -> list[dict[str, str]]:
    agents = []
    for name, path in sorted(_agent_paths(agents_directory).items()):
        try:
            config = AgentConfiguration.from_markdown(path)
            agents.append({"id": config.identifier, "name": config.identifier, "title": config.display_name})
        except Exception:
            agents.append({"id": name, "name": name, "title": name})
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


def _merge_agent_config(frontmatter: dict, configuration: dict) -> dict:
    merged = dict(frontmatter)
    model_configuration = configuration.get("modelConfig", {})
    if "model" in model_configuration:
        merged["model"] = model_configuration["model"]
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
    tool_configuration = configuration.get("toolConfig", {})
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
