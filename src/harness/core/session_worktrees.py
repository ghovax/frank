"""Per-session Git worktree isolation.

Each chat session gets its own checkout when the selected project lives inside a
Git repository. The original project path remains the discovery/configuration
scope; the runtime path is the isolated worktree path where shell and file tools
execute.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness.core.configuration import harness_home_directory


@dataclass(frozen=True)
class SessionWorkspace:
    source_working_directory: str
    runtime_working_directory: str
    isolated: bool
    worktree_path: str = ""
    worktree_branch: str = ""
    source_repository_root: str = ""
    runtime_repository_root: str = ""
    head: str = ""
    isolation_error: str = ""

    def model_dump(self) -> dict[str, str | bool]:
        return {
            "source_working_directory": self.source_working_directory,
            "runtime_working_directory": self.runtime_working_directory,
            "isolated": self.isolated,
            "worktree_path": self.worktree_path,
            "worktree_branch": self.worktree_branch,
            "source_repository_root": self.source_repository_root,
            "runtime_repository_root": self.runtime_repository_root,
            "head": self.head,
            "isolation_error": self.isolation_error,
        }


class SessionWorktreeManager:
    def __init__(self, root_directory: Path | None = None):
        self._root_directory = root_directory or (harness_home_directory() / "worktrees")
        self._root_directory.mkdir(parents=True, exist_ok=True)
        self._lock_path = self._root_directory / ".lock"

    async def prepare(self, session_id: str, source_working_directory: str) -> SessionWorkspace:
        return await asyncio.to_thread(self.prepare_sync, session_id, source_working_directory)

    def prepare_sync(self, session_id: str, source_working_directory: str) -> SessionWorkspace:
        source = Path(source_working_directory or Path.home()).expanduser().resolve(strict=False)
        if not source.is_dir():
            raise FileNotFoundError(f"Working directory does not exist: {source}")

        repository_root = self._git_text(source, "rev-parse", "--show-toplevel")
        if not repository_root:
            return SessionWorkspace(
                source_working_directory=str(source),
                runtime_working_directory=str(source),
                isolated=False,
                isolation_error="Selected directory is not inside a Git repository.",
            )

        source_repository_root = Path(repository_root).resolve(strict=False)
        try:
            source_relative = source.relative_to(source_repository_root)
        except ValueError:
            source_relative = Path()

        repository_key = self._repository_key(source_repository_root)
        session_root = self._root_directory / repository_key / self._safe_session_id(session_id)
        worktree_root = session_root / "checkout"
        runtime_directory = worktree_root / source_relative
        branch = f"codex/session/{self._safe_branch_component(session_id)}"
        head = self._git_text(source_repository_root, "rev-parse", "HEAD") or "HEAD"

        with self._process_lock():
            if not (worktree_root / ".git").exists():
                if worktree_root.exists():
                    shutil.rmtree(worktree_root)
                worktree_root.parent.mkdir(parents=True, exist_ok=True)
                self._add_worktree(source_repository_root, worktree_root, branch, head)

        return SessionWorkspace(
            source_working_directory=str(source),
            runtime_working_directory=str(runtime_directory),
            isolated=True,
            worktree_path=str(worktree_root),
            worktree_branch=branch,
            source_repository_root=str(source_repository_root),
            runtime_repository_root=str(worktree_root),
            head=head,
        )

    def _process_lock(self):
        manager = self

        class _Lock:
            def __enter__(self):
                manager._lock_path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = manager._lock_path.open("a+")
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                self._handle.close()

        return _Lock()

    def _add_worktree(self, repository_root: Path, worktree_root: Path, branch: str, head: str) -> None:
        result = self._run_git(
            repository_root,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_root),
            head,
            check=False,
        )
        if result.returncode == 0:
            return
        branch_exists = self._run_git(
            repository_root,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        ).returncode == 0
        if branch_exists:
            result = self._run_git(
                repository_root,
                "worktree",
                "add",
                str(worktree_root),
                branch,
                check=False,
            )
            if result.returncode == 0:
                return
        stderr = (result.stderr or result.stdout or "unknown git worktree error").strip()
        raise RuntimeError(f"Could not create session worktree: {stderr}")

    def _git_text(self, cwd: Path, *args: str) -> str:
        result = self._run_git(cwd, *args, check=False)
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def _run_git(self, cwd: Path, *args: str, check: bool) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=check,
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )

    def _repository_key(self, repository_root: Path) -> str:
        name = repository_root.name or "repository"
        digest = hashlib.sha256(str(repository_root).encode()).hexdigest()[:12]
        safe_name = "".join(character if character.isalnum() or character in "-_." else "-" for character in name)
        return f"{safe_name}-{digest}"

    def _safe_session_id(self, session_id: str) -> str:
        safe = "".join(character if character.isalnum() or character in "-_." else "-" for character in session_id)
        return safe or hashlib.sha256(session_id.encode()).hexdigest()[:16]

    def _safe_branch_component(self, session_id: str) -> str:
        safe = "".join(character if character.isalnum() or character in "-_." else "-" for character in session_id)
        return safe.strip("-") or hashlib.sha256(session_id.encode()).hexdigest()[:16]
