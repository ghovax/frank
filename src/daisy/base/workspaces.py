"""Session workspace strategies.

The source project remains the scope for project-local agents, skills,
instructions, memories, and MCP configuration. The runtime directory is where
shell and file tools execute.
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
from typing import Literal

from daisy.base.paths import workspaces_directory


WorkspaceStrategy = Literal["none", "branch", "worktree"]


@dataclass(frozen=True)
class SessionWorkspace:
    source_working_directory: str
    runtime_working_directory: str
    strategy: WorkspaceStrategy
    workspace_path: str = ""
    workspace_branch: str = ""
    source_repository_root: str = ""
    runtime_repository_root: str = ""
    head: str = ""
    error: str = ""

    @property
    def isolated(self) -> bool:
        return self.strategy == "worktree"

    def model_dump(self) -> dict[str, str | bool]:
        return {
            "source_working_directory": self.source_working_directory,
            "runtime_working_directory": self.runtime_working_directory,
            "workspace_strategy": self.strategy,
            "workspace_path": self.workspace_path,
            "workspace_branch": self.workspace_branch,
            "source_repository_root": self.source_repository_root,
            "runtime_repository_root": self.runtime_repository_root,
            "workspace_head": self.head,
            "workspace_error": self.error,
            "isolated": self.isolated,
        }


class SessionWorkspaceManager:
    def __init__(self, root_directory: Path | None = None):
        self._root_directory = root_directory or workspaces_directory()
        self._root_directory.mkdir(parents=True, exist_ok=True)
        self._lock_path = self._root_directory / ".lock"

    async def prepare(self, session_id: str, source_working_directory: str, strategy: WorkspaceStrategy) -> SessionWorkspace:
        return await asyncio.to_thread(self.prepare_sync, session_id, source_working_directory, strategy)

    def prepare_sync(self, session_id: str, source_working_directory: str, strategy: WorkspaceStrategy) -> SessionWorkspace:
        source = Path(source_working_directory or Path.home()).expanduser().resolve(strict=False)
        if not source.is_dir():
            raise FileNotFoundError(f"Working directory does not exist: {source}")
        if strategy == "none":
            return SessionWorkspace(
                source_working_directory=str(source),
                runtime_working_directory=str(source),
                strategy="none",
            )

        repository_root_text = self._git_text(source, "rev-parse", "--show-toplevel")
        if not repository_root_text:
            return SessionWorkspace(
                source_working_directory=str(source),
                runtime_working_directory=str(source),
                strategy="none",
                error=f"Selected directory is not inside a Git repository; requested {strategy}.",
            )

        source_repository_root = Path(repository_root_text).resolve(strict=False)
        try:
            source_relative = source.relative_to(source_repository_root)
        except ValueError:
            source_relative = Path()
        branch = f"daisy/session/{self._safe_branch_component(session_id)}"
        base_ref = self._base_ref(source_repository_root)
        head = self._git_text(source_repository_root, "rev-parse", base_ref) or "HEAD"

        if strategy == "branch":
            with self._process_lock():
                self._checkout_branch(source_repository_root, branch, head)
            return SessionWorkspace(
                source_working_directory=str(source),
                runtime_working_directory=str(source),
                strategy="branch",
                workspace_path=str(source_repository_root),
                workspace_branch=branch,
                source_repository_root=str(source_repository_root),
                runtime_repository_root=str(source_repository_root),
                head=head,
            )

        repository_key = self._repository_key(source_repository_root)
        session_root = self._root_directory / repository_key / self._safe_session_id(session_id)
        worktree_root = session_root / "checkout"
        runtime_directory = worktree_root / source_relative
        with self._process_lock():
            if not (worktree_root / ".git").exists():
                if worktree_root.exists():
                    shutil.rmtree(worktree_root)
                worktree_root.parent.mkdir(parents=True, exist_ok=True)
                self._add_worktree(source_repository_root, worktree_root, branch, head)

        return SessionWorkspace(
            source_working_directory=str(source),
            runtime_working_directory=str(runtime_directory),
            strategy="worktree",
            workspace_path=str(worktree_root),
            workspace_branch=branch,
            source_repository_root=str(source_repository_root),
            runtime_repository_root=str(worktree_root),
            head=head,
        )

    def _process_lock(self):
        manager = self

        class WorkspaceLock:
            def __enter__(self):
                manager._lock_path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = manager._lock_path.open("a+")
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
                return self

            def __exit__(self, _exception_type, _exception, _traceback):
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                self._handle.close()

        return WorkspaceLock()

    def _checkout_branch(self, repository_root: Path, branch: str, head: str) -> None:
        if self._branch_exists(repository_root, branch):
            result = self._run_git(repository_root, "checkout", branch, check=False)
        else:
            result = self._run_git(repository_root, "checkout", "-b", branch, head, check=False)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "unknown git checkout error").strip()
            raise RuntimeError(f"Could not prepare session branch: {stderr}")

    def _add_worktree(self, repository_root: Path, worktree_root: Path, branch: str, head: str) -> None:
        result = self._run_git(repository_root, "worktree", "add", "-b", branch, str(worktree_root), head, check=False)
        if result.returncode == 0:
            return
        if self._branch_exists(repository_root, branch):
            result = self._run_git(repository_root, "worktree", "add", str(worktree_root), branch, check=False)
            if result.returncode == 0:
                return
        stderr = (result.stderr or result.stdout or "unknown git worktree error").strip()
        raise RuntimeError(f"Could not create session worktree: {stderr}")

    def _base_ref(self, repository_root: Path) -> str:
        """Resolve the ref that new session branches and worktrees branch from.

        Sessions always branch from the repository's main line, never from the
        currently checked-out ref. Otherwise starting a session while a previous
        session branch is checked out would stack the new branch on top of it,
        chaining session branches indefinitely. Prefer a local ``main`` (then
        ``master``), fall back to the remote's default branch, and only as a last
        resort use the current ``HEAD``."""
        for candidate in ("main", "master"):
            if self._branch_exists(repository_root, candidate):
                return candidate
        origin_head = self._git_text(repository_root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if origin_head:
            return origin_head.replace("refs/remotes/", "", 1)
        return "HEAD"

    def _branch_exists(self, repository_root: Path, branch: str) -> bool:
        return self._run_git(
            repository_root,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        ).returncode == 0

    def _git_text(self, cwd: Path, *arguments: str) -> str:
        result = self._run_git(cwd, *arguments, check=False)
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def _run_git(self, cwd: Path, *arguments: str, check: bool) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            check=check,
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )

    def _repository_key(self, repository_root: Path) -> str:
        repository_name = repository_root.name or "repository"
        digest = hashlib.sha256(str(repository_root).encode()).hexdigest()[:12]
        safe_name = "".join(character if character.isalnum() or character in "-_." else "-" for character in repository_name)
        return f"{safe_name}-{digest}"

    def _safe_session_id(self, session_id: str) -> str:
        safe = "".join(character if character.isalnum() or character in "-_." else "-" for character in session_id)
        return safe or hashlib.sha256(session_id.encode()).hexdigest()[:16]

    def _safe_branch_component(self, session_id: str) -> str:
        safe = "".join(character if character.isalnum() or character in "-_." else "-" for character in session_id)
        return safe.strip("-") or hashlib.sha256(session_id.encode()).hexdigest()[:16]
