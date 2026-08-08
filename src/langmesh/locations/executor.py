"""Run shell commands and move files against a location, local or reached over multiplexed SSH."""

from __future__ import annotations

from langmesh.base.paths import ssh_control_directory, ssh_control_identifier

import abc
import os
import posixpath
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from langmesh.base.tuning import Tunable, active_tuning

# Baseline command and connect ceilings, scaled at each subprocess boundary by the active timeout knob.
DEFAULT_TIMEOUT = 120.0
DEFAULT_CONNECT_TIMEOUT = 16.0
# Keep the multiplexed master alive briefly after the last use, so bursts of calls reuse one connection.
CONTROL_PERSIST_SECONDS = 120

# The per-file and listing budgets scale with the live context window and are read per call.


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _login_script(command: str, cwd: str, env: dict[str, str] | None) -> str:
    """One shell script: change into the base directory, export any extra environment, run the command."""
    prefix = ""
    if env:
        prefix = "".join(
            f"export {name}={shlex.quote(str(value))}; " for name, value in env.items()
        )
    return f"cd {shlex.quote(cwd)} && {prefix}{command}"


def glob_to_regex(pattern: str) -> str:
    """Translate a `Path.glob` pattern to a regex over relative paths, so remote globbing mirrors local semantics."""
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
    """Translate a simple filename glob to a regex, since `include` matches names rather than paths."""
    translated = []
    for char in pattern:
        if char == "*":
            translated.append("[^/]*")
        elif char == "?":
            translated.append("[^/]")
        else:
            translated.append(re.escape(char))
    return "".join(translated)


def _prune_gitignored(base: Path, paths: list[Path]) -> list[Path]:
    """Drop the paths `.gitignore` excludes, asking `git` itself so the answer matches what the user sees."""
    if not paths:
        return paths
    try:
        completed = subprocess.run(
            ["git", "-C", str(base), "check-ignore", "--stdin"],
            input="\n".join(str(path) for path in paths),
            capture_output=True,
            text=True,
            timeout=active_tuning().duration(Tunable.ripgrep),
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return paths
    # git check-ignore: 0 => some paths ignored (printed), 1 => none, 128 => not a repo.
    if completed.returncode not in (0, 1):
        return paths
    ignored = {line for line in completed.stdout.splitlines() if line}
    return [path for path in paths if str(path) not in ignored]


class LocationExecutor(abc.ABC):
    """Run commands and read/write/search files against one location."""

    #: Whether this executor operates on the home server's own filesystem.
    is_local: bool = False

    @abc.abstractmethod
    def run(
        self,
        command: str,
        cwd: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...

    @abc.abstractmethod
    def read_bytes(self, path: str) -> bytes: ...

    @abc.abstractmethod
    def run_bytes(self, command: str, cwd: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        """Run a command and return its raw stdout bytes, raising on a non-zero exit."""

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
        """Resolve a possibly-relative path against the location's base directory, expanding `~`."""

    @abc.abstractmethod
    def glob_files(
        self, base_directory: str, pattern: str, limit: int, include_ignored: bool = False
    ) -> list[str]:
        """Absolute paths of files matching the glob, newest first and capped, honouring `.gitignore` unless told not to."""

    @abc.abstractmethod
    def grep(
        self,
        pattern: str,
        target: str,
        include: str | None,
        maximum_results: int,
        include_ignored: bool = False,
    ) -> list[str]:
        """`path:line:content` matches of a regex under a path, optionally filtered by a filename glob."""

    def read_text(self, path: str) -> str:
        return self.read_bytes(path).decode("utf-8", errors="replace")

    def write_text(self, path: str, content: str) -> None:
        self.write_bytes(path, content.encode("utf-8"))


class LocalExecutor(LocationExecutor):
    """Executes on the home server's own filesystem."""

    is_local = True

    def run(
        self,
        command: str,
        cwd: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            ["bash", "-lc", _login_script(command, cwd, None)],
            capture_output=True,
            text=True,
            timeout=active_tuning().scale_timeout(timeout),
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
            timeout=active_tuning().scale_timeout(timeout),
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(
                completed.stderr.decode("utf-8", errors="replace").strip()
                or f"command failed: {command}"
            )
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

    def glob_files(
        self, base_directory: str, pattern: str, limit: int, include_ignored: bool = False
    ) -> list[str]:
        base = Path(base_directory) if base_directory else Path.cwd()
        if not base.exists():
            raise FileNotFoundError(f"Directory does not exist: {base}")
        regex = re.compile(glob_to_regex(pattern))
        if shutil.which("rg"):
            # ripgrep does the walk, honouring the whole ignore chain and sorting newest-first; we match the glob ourselves.
            command = ["rg", "--files", "--sortr", "modified"]
            if include_ignored:
                # Reach what the project excludes, both gitignored and hidden, though `.git` internals still never appear.
                command += ["--no-ignore", "--hidden"]
            result = subprocess.run(
                command,
                cwd=str(base),
                capture_output=True,
                text=True,
                timeout=active_tuning().duration(Tunable.ripgrep),
            )
            # rg exits 1 when the tree has no files, >1 on a real error (IO failure).
            if result.returncode not in (0, 1):
                raise ValueError((result.stderr or "").strip() or "glob failed")
            matched: list[str] = []
            for line in (result.stdout or "").splitlines():
                relative = line[2:] if line.startswith("./") else line
                if relative and regex.fullmatch(relative):
                    matched.append(str(base / relative))
                    if len(matched) >= limit:
                        break
            return matched
        # Fallback without ripgrep: `Path.glob`, dropping `.git` and asking `git check-ignore` about the rest.
        candidates = [
            match
            for match in base.glob(pattern)
            if not match.is_dir() and ".git" not in match.parts
        ]
        if not include_ignored:
            candidates = _prune_gitignored(base, candidates)
        candidates.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
        return [str(match) for match in candidates[:limit]]

    def grep(
        self,
        pattern: str,
        target: str,
        include: str | None,
        maximum_results: int,
        include_ignored: bool = False,
    ) -> list[str]:
        if shutil.which("rg"):
            try:
                return self._grep_with_ripgrep(
                    pattern, target, include, maximum_results, include_ignored
                )
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
        return self._grep_python(pattern, target, include, maximum_results, include_ignored)

    def _grep_with_ripgrep(
        self,
        pattern: str,
        target: str,
        include: str | None,
        maximum_results: int,
        include_ignored: bool = False,
    ) -> list[str]:
        command = [
            "rg",
            "--line-number",
            "--no-heading",
            "--color=never",
            "--max-count",
            str(active_tuning().amount(Tunable.grep_per_file)),
        ]
        if include_ignored:
            command += [
                "--no-ignore",
                "--hidden",
            ]  # reach gitignored + hidden files; .git stays out
        if include:
            command += ["--glob", include]
        command += ["-e", pattern, "--", target]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=active_tuning().duration(Tunable.ripgrep),
        )
        # rg exits 1 on "no matches", 2 on a real error (bad pattern, IO failure).
        if result.returncode not in (0, 1):
            raise ValueError((result.stderr or "").strip() or "search failed")
        return (result.stdout or "").splitlines()[:maximum_results]

    def _grep_python(
        self,
        pattern: str,
        target: str,
        include: str | None,
        maximum_results: int,
        include_ignored: bool = False,
    ) -> list[str]:
        """Fallback grep using a pure-Python walk (used when ripgrep is unavailable)."""
        per_file_limit = active_tuning().amount(Tunable.grep_per_file)
        try:
            regex = re.compile(pattern)
        except re.error as exception:
            raise ValueError(f"Invalid regular expression: {exception}") from exception
        include_re = re.compile(_include_glob_to_regex(include)) if include else None
        root = Path(target)
        if root.is_file():
            candidates = [root]
        else:
            walked = [
                path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts
            ]
            # Honour `.gitignore` on the fallback path too, so ripgrep's presence never changes what a search can see.
            candidates = walked if include_ignored else _prune_gitignored(root, walked)
        results: list[str] = []
        for file in candidates:
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
                    if len(results) >= maximum_results:
                        return results
                    if matches_in_file >= per_file_limit:
                        break
        return results


class SshExecutor(LocationExecutor):
    """Executes on a remote host over a multiplexed SSH connection, named by its `~/.ssh/config` alias."""

    is_local = False

    def __init__(self, alias: str, control_directory: Path | None = None):
        self.alias = alias
        # Runtime state, so the control socket has the lifetime the OS gives the runtime directory.
        self._control_directory = control_directory or ssh_control_directory()
        self._control_directory.mkdir(parents=True, exist_ok=True)
        self._home_directory: str | None = None
        self._ripgrep_available: bool | None = None

    def _mux_options(self) -> list[str]:
        # Our own digest of the alias rather than ssh's, whose full hash overran the unix socket path limit.
        control_path = str(self._control_directory / ssh_control_identifier(self.alias))
        return [
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={control_path}",
            "-o",
            f"ControlPersist={CONTROL_PERSIST_SECONDS}",
        ]

    def _ssh(
        self,
        remote_command: str,
        *,
        timeout: float,
        extra_options: list[str] | None = None,
        stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess:
        argv = ["ssh", *self._mux_options(), *(extra_options or []), self.alias, remote_command]
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=active_tuning().scale_timeout(timeout),
            check=False,
        )

    def ssh_argv(self, command: str, cwd: str, env: dict[str, str] | None = None) -> list[str]:
        """The exact ssh argv a `run` would execute, so the local bash machinery can drive a remote command."""
        remote = f"bash -lc {shlex.quote(_login_script(command, cwd, env))}"
        return ["ssh", *self._mux_options(), self.alias, remote]

    def run(
        self,
        command: str,
        cwd: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
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
            raise OSError(
                completed.stderr.decode("utf-8", errors="replace").strip()
                or f"failed to read {path}"
            )
        return completed.stdout

    def run_bytes(self, command: str, cwd: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        remote = f"bash -lc {shlex.quote(_login_script(command, cwd, None))}"
        completed = self._ssh(remote, timeout=timeout)
        if completed.returncode != 0:
            raise OSError(
                completed.stderr.decode("utf-8", errors="replace").strip()
                or f"command failed: {command}"
            )
        return completed.stdout

    def write_bytes(self, path: str, data: bytes) -> None:
        quoted = shlex.quote(path)
        completed = self._ssh(
            f"mkdir -p -- {shlex.quote(str(Path(path).parent))} && cat > {quoted}",
            timeout=DEFAULT_TIMEOUT,
            stdin=data,
        )
        if completed.returncode != 0:
            raise OSError(
                completed.stderr.decode("utf-8", errors="replace").strip()
                or f"failed to write {path}"
            )

    def exists(self, path: str) -> bool:
        completed = self._ssh(f"test -e {shlex.quote(path)}", timeout=DEFAULT_CONNECT_TIMEOUT)
        return completed.returncode == 0

    def is_directory(self, path: str) -> bool:
        completed = self._ssh(f"test -d {shlex.quote(path)}", timeout=DEFAULT_CONNECT_TIMEOUT)
        return completed.returncode == 0

    def home_directory(self) -> str:
        home = self._home_directory
        if home is None:
            completed = self._ssh('printf %s "$HOME"', timeout=DEFAULT_CONNECT_TIMEOUT)
            home = completed.stdout.decode("utf-8", errors="replace").strip()
            if completed.returncode != 0 or not home:
                raise OSError(
                    completed.stderr.decode("utf-8", errors="replace").strip()
                    or f"could not resolve $HOME on {self.alias}"
                )
            self._home_directory = home
        return home

    def resolve(self, base_directory: str, file_path: str) -> str:
        path = file_path.strip()
        if path == "~" or path.startswith("~/"):
            path = self.home_directory() + path[1:]
        if not path.startswith("/"):
            path = f"{base_directory.rstrip('/')}/{path}"
        return posixpath.normpath(path)

    def _has_ripgrep(self) -> bool:
        """Whether ripgrep is on the remote, memoized, so a remote location honours its ignore chain as the local one does."""
        if self._ripgrep_available is None:
            probe = self._ssh("command -v rg", timeout=DEFAULT_CONNECT_TIMEOUT)
            self._ripgrep_available = probe.returncode == 0
        return self._ripgrep_available

    def glob_files(
        self, base_directory: str, pattern: str, limit: int, include_ignored: bool = False
    ) -> list[str]:
        regex = re.compile(glob_to_regex(pattern))
        if self._has_ripgrep():
            # ripgrep does the walk on the remote too, and we match the glob against its results ourselves.
            no_ignore = " --no-ignore --hidden" if include_ignored else ""
            listing = self.run(f"rg --files --sortr modified{no_ignore}", base_directory)
            if listing.returncode not in (0, 1):  # 1 == the tree has no files
                raise FileNotFoundError(
                    listing.stderr.strip() or f"Directory does not exist: {base_directory}"
                )
            base = base_directory.rstrip("/")
            paths: list[str] = []
            for line in listing.stdout.splitlines():
                relative = line.strip()
                if not relative:
                    continue
                relative = relative[2:] if relative.startswith("./") else relative
                if regex.fullmatch(relative):
                    paths.append(relative if relative.startswith("/") else f"{base}/{relative}")
                    if len(paths) >= limit:
                        break
            return paths
        # Fallback without ripgrep: list the tree with `find` and glob-match locally, without the rest of the ignore chain.
        listing = self.run(
            f"find . -type f -not -path '*/.git/*' 2>/dev/null | head -{active_tuning().amount(Tunable.remote_listing)}",
            base_directory,
        )
        if listing.returncode != 0 and not listing.stdout:
            raise FileNotFoundError(
                listing.stderr.strip() or f"Directory does not exist: {base_directory}"
            )
        relative = [line[2:] for line in listing.stdout.splitlines() if line.startswith("./")]
        matched = [path for path in relative if regex.fullmatch(path)][:limit]
        # Newest-first, matching the local contract, with `xargs -0` chunking very large sets.
        if len(matched) > 1:
            stdin = "\0".join(matched).encode("utf-8")
            sorted_run = self._ssh(
                f"cd {shlex.quote(base_directory)} && xargs -0 ls -1td -- 2>/dev/null",
                timeout=DEFAULT_TIMEOUT,
                stdin=stdin,
            )
            sorted_lines = [
                line
                for line in sorted_run.stdout.decode("utf-8", errors="replace").splitlines()
                if line
            ]
            if len(sorted_lines) == len(matched):
                matched = sorted_lines
        base = base_directory.rstrip("/")
        return [path if path.startswith("/") else f"{base}/{path}" for path in matched]

    def grep(
        self,
        pattern: str,
        target: str,
        include: str | None,
        maximum_results: int,
        include_ignored: bool = False,
    ) -> list[str]:
        # Prefer ripgrep so the regex dialect matches, and otherwise use `grep -E` rather than basic expressions.
        quoted_pattern = shlex.quote(pattern)
        quoted_target = shlex.quote(target)
        per_file_limit = active_tuning().amount(Tunable.grep_per_file)
        if self._has_ripgrep():
            include_flag = f"--glob {shlex.quote(include)} " if include else ""
            no_ignore = "--no-ignore --hidden " if include_ignored else ""
            command = f"rg --line-number --no-heading --color=never --max-count {per_file_limit} {no_ignore}{include_flag}-e {quoted_pattern} -- {quoted_target}"
        else:
            include_flag = f"--include={shlex.quote(include)} " if include else ""
            command = f"grep -rEn -m {per_file_limit} --exclude-dir=.git {include_flag}-e {quoted_pattern} -- {quoted_target}"
        completed = self._ssh(f"bash -lc {shlex.quote(command)}", timeout=DEFAULT_TIMEOUT)
        stdout = completed.stdout.decode("utf-8", errors="replace")
        # Both rg and grep exit 1 for "no matches" and >1 for a real error.
        if completed.returncode not in (0, 1):
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(stderr or "search failed")
        return stdout.splitlines()[:maximum_results]

    def connect(self, *, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> CommandResult:
        """Establish or reuse the master and confirm the host is reachable, letting interactive auth surface."""
        completed = self._ssh(
            "true", timeout=timeout, extra_options=["-o", f"ConnectTimeout={int(timeout)}"]
        )
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
        """The ssh argv for an interactive login shell on the remote, sharing this host's multiplexed connection."""
        remote_command = (
            f"cd {shlex.quote(base_directory)} 2>/dev/null; exec ${{SHELL:-/bin/bash}} -l"
        )
        return ["ssh", "-tt", *self._mux_options(), self.alias, remote_command]
