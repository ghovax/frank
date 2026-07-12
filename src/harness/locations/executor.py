"""The location execution primitive: run shell commands and move files against a
location, whether it is the home server's own filesystem or a remote reached over SSH.

The remote executor shells out to the system ``ssh`` with a per-host **ControlMaster**
socket, so the first connection is reused (multiplexed) by every later command and file
transfer — matching the "multiplexed OpenSSH, nothing installed on the remote" model.
Commands run through a login shell (``bash -lc``) in the location's base directory, so
the remote's own environment (PATH, tool shims) is in effect — the same reasoning as the
local terminal login-env work.

Beyond raw command execution, the executor is the *filesystem abstraction* the file
tools (``read_file``, ``edit_file``, ``write_file``, ``find_files``, ``search_content``)
are written against: path resolution, text IO, glob matching (mtime-sorted), and regex
search all go through it, so local and remote tool calls share one result-building code
path in ``file_tools`` and differ only in which executor carries the primitives.

These are synchronous primitives; the async runtime calls them off-loop (``to_thread``),
consistent with the rest of the server's blocking-work discipline.
"""

from __future__ import annotations

import abc
import os
import posixpath
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 120.0
DEFAULT_CONNECT_TIMEOUT = 16.0
# Keep the multiplexed master alive briefly after the last use so bursts of tool calls
# reuse one connection without holding it open forever.
CONTROL_PERSIST_SECONDS = 120

# How many matches a grep may return and how many matches a single file may
# contribute — identical for local and remote so the model sees one contract.
MAXIMUM_GREP_RESULTS = 512
MAXIMUM_GREP_MATCHES_PER_FILE = 512
# Upper bound on how many remote paths are listed before glob matching; keeps a
# pathological remote tree (node_modules, build output) from flooding the wire.
MAXIMUM_REMOTE_LISTING = 32_768


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _login_script(command: str, cwd: str, env: dict[str, str] | None) -> str:
    """A single shell script: cd into the base dir, export any extra env, run the
    command. Shell-quoted so it is safe to hand to ``bash -lc`` on either side."""
    prefix = ""
    if env:
        prefix = "".join(f"export {name}={shlex.quote(str(value))}; " for name, value in env.items())
    return f"cd {shlex.quote(cwd)} && {prefix}{command}"


def glob_to_regex(pattern: str) -> str:
    """Translate a ``Path.glob``-style pattern (``**``, ``*``, ``?``) to a regex over
    ``/``-separated relative paths, so remote glob matching mirrors the local
    ``Path.glob`` semantics: ``**`` crosses directory boundaries, ``*``/``?`` do not."""
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 3] == "**/":
                parts.append("(?:[^/]+/)*")
                index += 3
                continue
            if pattern[index : index + 2] == "**":
                parts.append(".*")
                index += 2
                continue
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
        index += 1
    return "".join(parts)


def _include_glob_to_regex(pattern: str) -> str:
    """Translate a simple *filename* glob (``*`` and ``?`` only) to a regex — the
    ``include`` filter matches file names, never paths."""
    translated = []
    for char in pattern:
        if char == "*":
            translated.append("[^/]*")
        elif char == "?":
            translated.append("[^/]")
        else:
            translated.append(re.escape(char))
    return "".join(translated)


class LocationExecutor(abc.ABC):
    """Run commands and read/write/search files against one location."""

    #: Whether this executor operates on the home server's own filesystem. The file
    #: tools use it for local-only guards (home-directory search refusal).
    is_local: bool = False

    @abc.abstractmethod
    def run(self, command: str, cwd: str, *, timeout: float = DEFAULT_TIMEOUT, env: dict[str, str] | None = None) -> CommandResult: ...

    @abc.abstractmethod
    def read_bytes(self, path: str) -> bytes: ...

    @abc.abstractmethod
    def run_bytes(self, command: str, cwd: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        """Run a command and return its raw (undecoded) stdout bytes, raising ``OSError``
        on a non-zero exit. Needed for binary output like ``git cat-file blob`` that the
        text-decoding ``run`` would corrupt."""

    @abc.abstractmethod
    def write_bytes(self, path: str, data: bytes) -> None: ...

    @abc.abstractmethod
    def exists(self, path: str) -> bool: ...

    @abc.abstractmethod
    def is_directory(self, path: str) -> bool: ...

    @abc.abstractmethod
    def home_directory(self) -> str: ...

    @abc.abstractmethod
    def resolve(self, base_directory: str, file_path: str) -> str:
        """Resolve a tool's possibly-relative ``file_path`` against the location's
        base directory, expanding ``~``, and return the absolute path string."""

    @abc.abstractmethod
    def glob_files(self, base_directory: str, pattern: str, limit: int) -> list[str]:
        """Absolute paths of files under ``base_directory`` matching the glob
        ``pattern``, newest (by mtime) first, capped at ``limit``."""

    @abc.abstractmethod
    def grep(self, pattern: str, target: str, include: str | None, max_results: int) -> list[str]:
        """``path:line:content`` matches of regex ``pattern`` under the absolute
        ``target`` path, optionally filtered by an ``include`` filename glob."""

    def read_text(self, path: str) -> str:
        return self.read_bytes(path).decode("utf-8", errors="replace")

    def write_text(self, path: str, content: str) -> None:
        self.write_bytes(path, content.encode("utf-8"))


class LocalExecutor(LocationExecutor):
    """Executes on the home server's own filesystem."""

    is_local = True

    def run(self, command: str, cwd: str, *, timeout: float = DEFAULT_TIMEOUT, env: dict[str, str] | None = None) -> CommandResult:
        completed = subprocess.run(
            ["bash", "-lc", _login_script(command, cwd, None)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})} if env else None,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def read_bytes(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def run_bytes(self, command: str, cwd: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        completed = subprocess.run(
            ["bash", "-lc", _login_script(command, cwd, None)],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr.decode("utf-8", errors="replace").strip() or f"command failed: {command}")
        return completed.stdout

    def write_bytes(self, path: str, data: bytes) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def is_directory(self, path: str) -> bool:
        return Path(path).is_dir()

    def home_directory(self) -> str:
        return str(Path.home())

    def resolve(self, base_directory: str, file_path: str) -> str:
        candidate = Path(file_path).expanduser()
        if not candidate.is_absolute():
            base = Path(base_directory) if base_directory else Path.cwd()
            candidate = base / candidate
        return str(candidate.resolve(strict=False))

    def glob_files(self, base_directory: str, pattern: str, limit: int) -> list[str]:
        base = Path(base_directory) if base_directory else Path.cwd()
        if not base.exists():
            raise FileNotFoundError(f"Directory does not exist: {base}")
        matches = [match for match in base.glob(pattern) if not match.is_dir()]
        matches.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
        return [str(match) for match in matches[:limit]]

    def grep(self, pattern: str, target: str, include: str | None, max_results: int) -> list[str]:
        if shutil.which("rg"):
            try:
                return self._grep_with_ripgrep(pattern, target, include, max_results)
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
        return self._grep_python(pattern, target, include, max_results)

    def _grep_with_ripgrep(self, pattern: str, target: str, include: str | None, max_results: int) -> list[str]:
        command = [
            "rg", "--line-number", "--no-heading", "--color=never",
            "--max-count", str(MAXIMUM_GREP_MATCHES_PER_FILE),
        ]
        if include:
            command += ["--glob", include]
        command += ["-e", pattern, "--", target]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        # rg exits 1 on "no matches", 2 on a real error (bad pattern, IO failure).
        if result.returncode not in (0, 1):
            raise ValueError((result.stderr or "").strip() or "search failed")
        return (result.stdout or "").splitlines()[:max_results]

    def _grep_python(self, pattern: str, target: str, include: str | None, max_results: int) -> list[str]:
        """Fallback grep using a pure-Python walk (used when ripgrep is unavailable)."""
        try:
            regex = re.compile(pattern)
        except re.error as exception:
            raise ValueError(f"Invalid regular expression: {exception}") from exception
        include_re = re.compile(_include_glob_to_regex(include)) if include else None
        root = Path(target)
        files = [root] if root.is_file() else root.rglob("*")
        results: list[str] = []
        for file in files:
            if file.is_dir():
                continue
            if include_re is not None and not include_re.fullmatch(file.name):
                continue
            try:
                text = file.read_text(errors="ignore")
            except OSError:
                continue
            matches_in_file = 0
            for line_no, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    results.append(f"{file}:{line_no}:{line}")
                    matches_in_file += 1
                    if len(results) >= max_results:
                        return results
                    if matches_in_file >= MAXIMUM_GREP_MATCHES_PER_FILE:
                        break
        return results


class SshExecutor(LocationExecutor):
    """Executes on a remote host over a multiplexed SSH connection. ``alias`` is the
    ``~/.ssh/config`` Host alias (so the connection inherits that host's full config)."""

    is_local = False

    def __init__(self, alias: str, control_directory: Path | None = None):
        self.alias = alias
        self._control_directory = (control_directory or Path("~/.daisy/ssh-control").expanduser())
        self._control_directory.mkdir(parents=True, exist_ok=True)
        self._home_directory: str | None = None
        self._ripgrep_available: bool | None = None

    def _mux_options(self) -> list[str]:
        # `%C` is ssh's short hash of (localhost, remotehost, port, user) — a stable,
        # length-safe socket name that keeps the ControlPath under the Unix socket limit.
        control_path = str(self._control_directory / "%C")
        return [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={control_path}",
            "-o", f"ControlPersist={CONTROL_PERSIST_SECONDS}",
        ]

    def _ssh(self, remote_command: str, *, timeout: float, extra_options: list[str] | None = None, stdin: bytes | None = None) -> subprocess.CompletedProcess:
        argv = ["ssh", *self._mux_options(), *(extra_options or []), self.alias, remote_command]
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def ssh_argv(self, command: str, cwd: str, env: dict[str, str] | None = None) -> list[str]:
        """The exact ssh argv a ``run`` would execute — the runtime wraps a remote
        ``bash`` tool call in this so the normal local bash machinery (sync ceiling,
        backgrounding, output capping) drives the remote command."""
        remote = f"bash -lc {shlex.quote(_login_script(command, cwd, env))}"
        return ["ssh", *self._mux_options(), self.alias, remote]

    def run(self, command: str, cwd: str, *, timeout: float = DEFAULT_TIMEOUT, env: dict[str, str] | None = None) -> CommandResult:
        remote = f"bash -lc {shlex.quote(_login_script(command, cwd, env))}"
        completed = self._ssh(remote, timeout=timeout)
        return CommandResult(
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
        )

    def read_bytes(self, path: str) -> bytes:
        completed = self._ssh(f"cat -- {shlex.quote(path)}", timeout=DEFAULT_TIMEOUT)
        if completed.returncode != 0:
            raise OSError(completed.stderr.decode("utf-8", errors="replace").strip() or f"failed to read {path}")
        return completed.stdout

    def run_bytes(self, command: str, cwd: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        remote = f"bash -lc {shlex.quote(_login_script(command, cwd, None))}"
        completed = self._ssh(remote, timeout=timeout)
        if completed.returncode != 0:
            raise OSError(completed.stderr.decode("utf-8", errors="replace").strip() or f"command failed: {command}")
        return completed.stdout

    def write_bytes(self, path: str, data: bytes) -> None:
        quoted = shlex.quote(path)
        completed = self._ssh(
            f"mkdir -p -- {shlex.quote(str(Path(path).parent))} && cat > {quoted}",
            timeout=DEFAULT_TIMEOUT,
            stdin=data,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr.decode("utf-8", errors="replace").strip() or f"failed to write {path}")

    def exists(self, path: str) -> bool:
        completed = self._ssh(f"test -e {shlex.quote(path)}", timeout=DEFAULT_CONNECT_TIMEOUT)
        return completed.returncode == 0

    def is_directory(self, path: str) -> bool:
        completed = self._ssh(f"test -d {shlex.quote(path)}", timeout=DEFAULT_CONNECT_TIMEOUT)
        return completed.returncode == 0

    def home_directory(self) -> str:
        if self._home_directory is None:
            completed = self._ssh('printf %s "$HOME"', timeout=DEFAULT_CONNECT_TIMEOUT)
            home = completed.stdout.decode("utf-8", errors="replace").strip()
            if completed.returncode != 0 or not home:
                raise OSError(
                    completed.stderr.decode("utf-8", errors="replace").strip()
                    or f"could not resolve $HOME on {self.alias}"
                )
            self._home_directory = home
        return self._home_directory

    def resolve(self, base_directory: str, file_path: str) -> str:
        path = file_path.strip()
        if path == "~" or path.startswith("~/"):
            path = self.home_directory() + path[1:]
        if not path.startswith("/"):
            path = f"{base_directory.rstrip('/')}/{path}"
        return posixpath.normpath(path)

    def glob_files(self, base_directory: str, pattern: str, limit: int) -> list[str]:
        # List the remote tree once (bounded, .git excluded) and apply the same
        # glob semantics as the local Path.glob, server-side, so patterns behave
        # identically on both kinds of location.
        listing = self.run(
            f"find . -type f -not -path '*/.git/*' 2>/dev/null | head -{MAXIMUM_REMOTE_LISTING}",
            base_directory,
        )
        if listing.returncode != 0 and not listing.stdout:
            raise FileNotFoundError(listing.stderr.strip() or f"Directory does not exist: {base_directory}")
        regex = re.compile(glob_to_regex(pattern))
        relative = [line[2:] for line in listing.stdout.splitlines() if line.startswith("./")]
        matched = [path for path in relative if regex.fullmatch(path)][:limit]
        # Newest-first, matching the local contract. xargs -0 chunks very large
        # sets (sorting within chunks only), which is acceptable at this cap.
        if len(matched) > 1:
            stdin = "\0".join(matched).encode("utf-8")
            sorted_run = self._ssh(
                f"cd {shlex.quote(base_directory)} && xargs -0 ls -1td -- 2>/dev/null",
                timeout=DEFAULT_TIMEOUT,
                stdin=stdin,
            )
            sorted_lines = [
                line for line in sorted_run.stdout.decode("utf-8", errors="replace").splitlines() if line
            ]
            if len(sorted_lines) == len(matched):
                matched = sorted_lines
        base = base_directory.rstrip("/")
        return [path if path.startswith("/") else f"{base}/{path}" for path in matched]

    def grep(self, pattern: str, target: str, include: str | None, max_results: int) -> list[str]:
        # Prefer ripgrep on the remote so the regex dialect matches the local tool;
        # otherwise fall back to POSIX ERE via `grep -E` (never BRE, whose unescaped
        # `+`/`?` silently match nothing and read as false "not found" results).
        if self._ripgrep_available is None:
            probe = self._ssh("command -v rg", timeout=DEFAULT_CONNECT_TIMEOUT)
            self._ripgrep_available = probe.returncode == 0
        quoted_pattern = shlex.quote(pattern)
        quoted_target = shlex.quote(target)
        if self._ripgrep_available:
            include_flag = f"--glob {shlex.quote(include)} " if include else ""
            command = (
                f"rg --line-number --no-heading --color=never "
                f"--max-count {MAXIMUM_GREP_MATCHES_PER_FILE} {include_flag}"
                f"-e {quoted_pattern} -- {quoted_target}"
            )
        else:
            include_flag = f"--include={shlex.quote(include)} " if include else ""
            command = (
                f"grep -rEn -m {MAXIMUM_GREP_MATCHES_PER_FILE} {include_flag}"
                f"-e {quoted_pattern} -- {quoted_target}"
            )
        completed = self._ssh(f"bash -lc {shlex.quote(command)}", timeout=DEFAULT_TIMEOUT)
        stdout = completed.stdout.decode("utf-8", errors="replace")
        # Both rg and grep exit 1 for "no matches" and >1 for a real error.
        if completed.returncode not in (0, 1):
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(stderr or "search failed")
        return stdout.splitlines()[:max_results]

    def connect(self, *, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> CommandResult:
        """Establish (or reuse) the ControlMaster and confirm the host is reachable and
        authenticated — the pre-flight run when a project is opened. Interactive auth is
        allowed to surface here (no BatchMode); a failure returns a non-zero result whose
        stderr explains why (unreachable, auth required, …)."""
        completed = self._ssh("true", timeout=timeout, extra_options=["-o", f"ConnectTimeout={int(timeout)}"])
        return CommandResult(
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
        )

    def is_connected(self) -> bool:
        """Whether a live ControlMaster already exists (no new connection attempted)."""
        completed = subprocess.run(
            ["ssh", *self._mux_options(), "-O", "check", self.alias],
            capture_output=True,
            timeout=DEFAULT_CONNECT_TIMEOUT,
            check=False,
        )
        return completed.returncode == 0

    def disconnect(self) -> None:
        """Tear down the multiplexed master, if any."""
        subprocess.run(
            ["ssh", *self._mux_options(), "-O", "exit", self.alias],
            capture_output=True,
            timeout=DEFAULT_CONNECT_TIMEOUT,
            check=False,
        )

    def terminal_argv(self, base_directory: str) -> list[str]:
        """The ssh argv for an interactive login shell on the remote, in ``base_directory``,
        over the shared multiplexed connection (`-tt` forces a PTY). Reuses this host's
        ControlMaster, so it shares the connection with tool execution."""
        remote_command = f"cd {shlex.quote(base_directory)} 2>/dev/null; exec ${{SHELL:-/bin/bash}} -l"
        return ["ssh", "-tt", *self._mux_options(), self.alias, remote_command]
