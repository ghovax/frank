"""The location execution primitive: run shell commands and move files against a
location, whether it is the home server's own filesystem or a remote reached over SSH.

The remote executor shells out to the system ``ssh`` with a per-host **ControlMaster**
socket, so the first connection is reused (multiplexed) by every later command and file
transfer — matching the "multiplexed OpenSSH, nothing installed on the remote" model.
Commands run through a login shell (``bash -lc``) in the location's base directory, so
the remote's own environment (PATH, tool shims) is in effect — the same reasoning as the
local terminal login-env work.

Beyond raw command execution, the executor is the *filesystem abstraction* the file
tools (``read_file``, ``edit_file``, ``write_file``, ``bash``)
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

from xeac.base.tuning import Limit, active_tuning

# Baseline command and connect ceilings. These are safety valves against a dead process or link,
# not accuracy caps; the active timeout knob scales them at each subprocess boundary
# (``active_tuning().scale_timeout``), so a slow machine or link can widen them from the config.
DEFAULT_TIMEOUT = 120.0
DEFAULT_CONNECT_TIMEOUT = 16.0
# Keep the multiplexed master alive briefly after the last use so bursts of tool calls
# reuse one connection without holding it open forever.
CONTROL_PERSIST_SECONDS = 120

# How many matches a single file may contribute and how many remote paths are listed before glob
# matching are listing budgets that scale with the live model context window; they are read per
# call from ``active_tuning()`` (grep_per_file / remote_listing). The total per-search match cap
# likewise comes from the tuning policy and is passed in by the file tools.


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


def _prune_gitignored(base: Path, paths: list[Path]) -> list[Path]:
    """Drop the paths excluded by ``base``'s ``.gitignore`` chain, asking ``git`` itself
    so the answer matches what the user sees. Only the non-ripgrep fallback needs this —
    ripgrep applies the ignore rules while it walks. A no-op outside a git repo or when
    ``git`` is unavailable, so a plain directory still globs normally."""
    if not paths:
        return paths
    try:
        completed = subprocess.run(
            ["git", "-C", str(base), "check-ignore", "--stdin"],
            input="\n".join(str(path) for path in paths),
            capture_output=True,
            text=True,
            timeout=active_tuning().duration(Limit.RIPGREP_SECONDS),
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
    def glob_files(self, base_directory: str, pattern: str, limit: int, include_ignored: bool = False) -> list[str]:
        """Absolute paths of files under ``base_directory`` matching the glob
        ``pattern``, newest (by mtime) first, capped at ``limit``. Honors the
        location's ``.gitignore`` unless ``include_ignored`` is set."""

    @abc.abstractmethod
    def grep(self, pattern: str, target: str, include: str | None, maximum_results: int, include_ignored: bool = False) -> list[str]:
        """``path:line:content`` matches of regex ``pattern`` under the absolute
        ``target`` path, optionally filtered by an ``include`` filename glob. Honors
        the location's ``.gitignore`` unless ``include_ignored`` is set."""

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

    def glob_files(self, base_directory: str, pattern: str, limit: int, include_ignored: bool = False) -> list[str]:
        base = Path(base_directory) if base_directory else Path.cwd()
        if not base.exists():
            raise FileNotFoundError(f"Directory does not exist: {base}")
        regex = re.compile(glob_to_regex(pattern))
        if shutil.which("rg"):
            # ripgrep does the walk: `rg --files` honors the full .gitignore/.ignore chain
            # and skips .git and hidden files, and `--sortr modified` yields newest-first.
            # We match the caller's glob against those results ourselves rather than through
            # `rg -g`, because an rg glob is a whitelist that overrides .gitignore — a broad
            # pattern like `**/*` would otherwise drag build output and dependencies back in.
            command = ["rg", "--files", "--sortr", "modified"]
            if include_ignored:
                # Reach what the project excludes — both gitignored and hidden (dot) files.
                # rg keeps .git internals out even with --hidden, so those still never appear.
                command += ["--no-ignore", "--hidden"]
            result = subprocess.run(
                command,
                cwd=str(base),
                capture_output=True,
                text=True,
                timeout=active_tuning().duration(Limit.RIPGREP_SECONDS),
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
        # Fallback when ripgrep is unavailable: Path.glob, dropping .git always and (unless
        # include_ignored) the gitignored paths, via `git check-ignore`.
        candidates = [match for match in base.glob(pattern) if not match.is_dir() and ".git" not in match.parts]
        if not include_ignored:
            candidates = _prune_gitignored(base, candidates)
        candidates.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
        return [str(match) for match in candidates[:limit]]

    def grep(self, pattern: str, target: str, include: str | None, maximum_results: int, include_ignored: bool = False) -> list[str]:
        if shutil.which("rg"):
            try:
                return self._grep_with_ripgrep(pattern, target, include, maximum_results, include_ignored)
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
        return self._grep_python(pattern, target, include, maximum_results, include_ignored)

    def _grep_with_ripgrep(self, pattern: str, target: str, include: str | None, maximum_results: int, include_ignored: bool = False) -> list[str]:
        command = [
            "rg", "--line-number", "--no-heading", "--color=never",
            "--max-count", str(active_tuning().amount(Limit.GREP_PER_FILE)),
        ]
        if include_ignored:
            command += ["--no-ignore", "--hidden"]  # reach gitignored + hidden files; .git stays out
        if include:
            command += ["--glob", include]
        command += ["-e", pattern, "--", target]
        result = subprocess.run(command, capture_output=True, text=True, timeout=active_tuning().duration(Limit.RIPGREP_SECONDS))
        # rg exits 1 on "no matches", 2 on a real error (bad pattern, IO failure).
        if result.returncode not in (0, 1):
            raise ValueError((result.stderr or "").strip() or "search failed")
        return (result.stdout or "").splitlines()[:maximum_results]

    def _grep_python(self, pattern: str, target: str, include: str | None, maximum_results: int, include_ignored: bool = False) -> list[str]:
        """Fallback grep using a pure-Python walk (used when ripgrep is unavailable)."""
        per_file_limit = active_tuning().amount(Limit.GREP_PER_FILE)
        try:
            regex = re.compile(pattern)
        except re.error as exception:
            raise ValueError(f"Invalid regular expression: {exception}") from exception
        include_re = re.compile(_include_glob_to_regex(include)) if include else None
        root = Path(target)
        if root.is_file():
            candidates = [root]
        else:
            walked = [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts]
            # Honor .gitignore on the fallback path too (unless include_ignored), so ripgrep's
            # presence never changes which files a search can see.
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
            timeout=active_tuning().scale_timeout(timeout),
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
        """Whether ripgrep is on the remote (memoized). Both glob and grep prefer it so a
        remote location honors its .gitignore chain exactly as the local one does."""
        if self._ripgrep_available is None:
            probe = self._ssh("command -v rg", timeout=DEFAULT_CONNECT_TIMEOUT)
            self._ripgrep_available = probe.returncode == 0
        return self._ripgrep_available

    def glob_files(self, base_directory: str, pattern: str, limit: int, include_ignored: bool = False) -> list[str]:
        regex = re.compile(glob_to_regex(pattern))
        if self._has_ripgrep():
            # ripgrep does the walk: `rg --files` honors the remote's .gitignore/.ignore
            # chain (and skips .git and hidden files) and `--sortr modified` yields
            # newest-first. As on the local side we match the glob against the results
            # ourselves — an `rg -g` glob overrides .gitignore, so `**/*` would drag the
            # excluded tree back in.
            no_ignore = " --no-ignore --hidden" if include_ignored else ""
            listing = self.run(f"rg --files --sortr modified{no_ignore}", base_directory)
            if listing.returncode not in (0, 1):  # 1 == the tree has no files
                raise FileNotFoundError(listing.stderr.strip() or f"Directory does not exist: {base_directory}")
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
        # Fallback without ripgrep: list the tree with find (.git excluded) and glob-match
        # locally. Without ripgrep the rest of the .gitignore chain is not applied.
        listing = self.run(
            f"find . -type f -not -path '*/.git/*' 2>/dev/null | head -{active_tuning().amount(Limit.REMOTE_LISTING)}",
            base_directory,
        )
        if listing.returncode != 0 and not listing.stdout:
            raise FileNotFoundError(listing.stderr.strip() or f"Directory does not exist: {base_directory}")
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

    def grep(self, pattern: str, target: str, include: str | None, maximum_results: int, include_ignored: bool = False) -> list[str]:
        # Prefer ripgrep on the remote so the regex dialect matches the local tool and the
        # .gitignore chain is honored; otherwise fall back to POSIX ERE via `grep -E` (never
        # BRE, whose unescaped `+`/`?` silently match nothing and read as false "not found").
        quoted_pattern = shlex.quote(pattern)
        quoted_target = shlex.quote(target)
        per_file_limit = active_tuning().amount(Limit.GREP_PER_FILE)
        if self._has_ripgrep():
            include_flag = f"--glob {shlex.quote(include)} " if include else ""
            no_ignore = "--no-ignore --hidden " if include_ignored else ""
            command = (
                f"rg --line-number --no-heading --color=never "
                f"--max-count {per_file_limit} {no_ignore}{include_flag}"
                f"-e {quoted_pattern} -- {quoted_target}"
            )
        else:
            include_flag = f"--include={shlex.quote(include)} " if include else ""
            command = (
                f"grep -rEn -m {per_file_limit} --exclude-dir=.git {include_flag}"
                f"-e {quoted_pattern} -- {quoted_target}"
            )
        completed = self._ssh(f"bash -lc {shlex.quote(command)}", timeout=DEFAULT_TIMEOUT)
        stdout = completed.stdout.decode("utf-8", errors="replace")
        # Both rg and grep exit 1 for "no matches" and >1 for a real error.
        if completed.returncode not in (0, 1):
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(stderr or "search failed")
        return stdout.splitlines()[:maximum_results]

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
