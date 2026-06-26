import os
import re
import shlex
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel


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


class GlobalConfiguration(BaseModel):
    api: ApiConfiguration
    exa: ExaConfiguration = ExaConfiguration()
    default_agent: str = "research-synthesist"
    agents_directory: str = "agents"
    skills_directory: str = "skills"
    # How deep a chain of agents delegating to other agents may go, to bound
    # runaway delegation (agent A spawns B spawns C ...).
    maximum_delegation_depth: int = 8
    maximum_history_age_days: int = 30

    @classmethod
    def from_yaml(cls, path: str | Path) -> "GlobalConfiguration":
        with open(path) as file_handle:
            data = yaml.safe_load(file_handle)
        return cls(**data)


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
    name: str
    label: str = ""
    color: str = ""
    description: str = ""
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

    @classmethod
    def from_markdown(cls, path: str | Path) -> "AgentConfiguration":
        with open(path) as file_handle:
            content = file_handle.read()

        frontmatter_match = re.match(
            r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL
        )
        if not frontmatter_match:
            raise ValueError(f"No YAML frontmatter found in {path}")

        frontmatter = yaml.safe_load(frontmatter_match.group(1))
        markdown_body = frontmatter_match.group(2).strip()

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
                f"Tool '{tool_name}' is not enabled for agent '{self._configuration.name}'"
            )

    def evaluate_bash_permission(self, command: str) -> str:
        return self._configuration.tools.bash.evaluate_permission(command)

    def check_bash_background(self) -> None:
        if not self._configuration.tools.bash.background_allowed:
            raise PermissionError("Background bash execution is not allowed")

    def check_tool(self, tool_name: str, **arguments) -> None:
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


def load_agent_configuration(
    name: str, agents_directory: str | Path
) -> AgentConfiguration:
    path = Path(agents_directory) / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent configuration not found: {path}")
    return AgentConfiguration.from_markdown(path)


def list_available_agents(agents_directory: str | Path) -> list[str]:
    return sorted(path.stem for path in Path(agents_directory).glob("*.md"))


def list_agents_with_labels(agents_directory: str | Path) -> list[dict[str, str]]:
    agents = []
    for path in sorted(Path(agents_directory).glob("*.md")):
        try:
            config = load_agent_configuration(path.stem, agents_directory)
            agents.append({"name": config.name, "label": config.label or config.name})
        except Exception:
            agents.append({"name": path.stem, "label": path.stem})
    return agents
