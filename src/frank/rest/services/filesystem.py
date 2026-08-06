"""Filesystem domain: git status, change watching, directory validation, and the native folder picker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from watchfiles import DefaultFilter
import os
import platform
import re
import shutil
import subprocess


def _validate_directory_payload(directory: str) -> dict[str, object]:
    if not directory:
        return {
            "valid": False,
            "exists": False,
            "is_directory": False,
            "is_absolute": False,
            "is_git_repository": False,
            "repository_root": "",
            "git_branch": "",
            "git_head": "",
            "git_short_head": "",
            "git_dirty": False,
            "git_detached": False,
            "git_label": "",
            "git_commit_subject": "",
            "git_commit_author": "",
            "git_commit_author_email": "",
            "git_commit_author_date": "",
            "git_upstream": "",
            "git_ahead": 0,
            "git_behind": 0,
            "git_staged_count": 0,
            "git_unstaged_count": 0,
            "git_untracked_count": 0,
            "git_conflicted_count": 0,
            "path": "",
        }
    path = Path(directory).expanduser()
    valid = path.is_absolute() and path.exists() and path.is_dir()
    is_git_repository = False
    repository_root = ""
    git_branch = ""
    git_head = ""
    git_short_head = ""
    git_dirty = False
    git_detached = False
    git_label = ""
    git_commit_subject = ""
    git_commit_author = ""
    git_commit_author_email = ""
    git_commit_author_date = ""
    git_upstream = ""
    git_ahead = 0
    git_behind = 0
    git_staged_count = 0
    git_unstaged_count = 0
    git_untracked_count = 0
    git_conflicted_count = 0
    if valid:
        try:
            inside = _run_git_probe(path, "rev-parse", "--is-inside-work-tree")
            is_git_repository = inside.returncode == 0 and inside.stdout.strip() == "true"
            if is_git_repository:
                root = _run_git_probe(path, "rev-parse", "--show-toplevel")
                if root.returncode == 0:
                    repository_root = root.stdout.strip()
                branch = _run_git_probe(path, "symbolic-ref", "--quiet", "--short", "HEAD")
                git_branch = branch.stdout.strip() if branch.returncode == 0 else ""
                head = _run_git_probe(path, "rev-parse", "HEAD")
                git_head = head.stdout.strip() if head.returncode == 0 else ""
                short_head = _run_git_probe(path, "rev-parse", "--short", "HEAD")
                git_short_head = short_head.stdout.strip() if short_head.returncode == 0 else ""
                commit = _run_git_probe(path, "cat-file", "-p", "HEAD")
                if commit.returncode == 0:
                    commit_metadata = _git_commit_metadata(commit.stdout)
                    git_commit_subject = commit_metadata["subject"]
                    git_commit_author = commit_metadata["author"]
                    git_commit_author_email = commit_metadata["author_email"]
                    git_commit_author_date = commit_metadata["author_date"]
                upstream = _run_git_probe(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
                git_upstream = upstream.stdout.strip() if upstream.returncode == 0 else ""
                if git_upstream:
                    ahead_behind = _run_git_probe(path, "rev-list", "--left-right", "--count", "HEAD...@{u}")
                    if ahead_behind.returncode == 0:
                        counts = ahead_behind.stdout.strip().split()
                        if len(counts) == 2:
                            git_ahead = int(counts[0])
                            git_behind = int(counts[1])
                staged = _run_git_probe(path, "diff", "--cached", "--name-only")
                git_staged_count = len(staged.stdout.splitlines()) if staged.returncode == 0 else 0
                unstaged = _run_git_probe(path, "diff", "--name-only")
                git_unstaged_count = len(unstaged.stdout.splitlines()) if unstaged.returncode == 0 else 0
                untracked = _run_git_probe(path, "ls-files", "--others", "--exclude-standard")
                git_untracked_count = len(untracked.stdout.splitlines()) if untracked.returncode == 0 else 0
                conflicted = _run_git_probe(path, "diff", "--name-only", "--diff-filter=U")
                git_conflicted_count = len(conflicted.stdout.splitlines()) if conflicted.returncode == 0 else 0
                git_dirty = any(
                    count > 0
                    for count in (
                        git_staged_count,
                        git_unstaged_count,
                        git_untracked_count,
                        git_conflicted_count,
                    )
                )
                git_detached = bool(git_head and not git_branch)
                git_label = git_branch or git_short_head
        except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            is_git_repository = False
    return {
        "valid": valid,
        "exists": path.exists(),
        "is_directory": path.is_dir(),
        "is_absolute": path.is_absolute(),
        "is_git_repository": is_git_repository,
        "repository_root": repository_root,
        "git_branch": git_branch,
        "git_head": git_head,
        "git_short_head": git_short_head,
        "git_dirty": git_dirty,
        "git_detached": git_detached,
        "git_label": git_label,
        "git_commit_subject": git_commit_subject,
        "git_commit_author": git_commit_author,
        "git_commit_author_email": git_commit_author_email,
        "git_commit_author_date": git_commit_author_date,
        "git_upstream": git_upstream,
        "git_ahead": git_ahead,
        "git_behind": git_behind,
        "git_staged_count": git_staged_count,
        "git_unstaged_count": git_unstaged_count,
        "git_untracked_count": git_untracked_count,
        "git_conflicted_count": git_conflicted_count,
        "path": str(path),
    }


def _git_commit_metadata(commit_text: str) -> dict[str, str]:
    headers, separator, message = commit_text.partition("\n\n")
    metadata = {
        "subject": "",
        "author": "",
        "author_email": "",
        "author_date": "",
    }
    for line in headers.splitlines():
        if not line.startswith("author "):
            continue
        match = re.match(r"author (.+) <([^>]+)> (\d+) ([+-]\d{4})$", line)
        if not match:
            continue
        metadata["author"] = match.group(1)
        metadata["author_email"] = match.group(2)
        timestamp = int(match.group(3))
        timezone_text = match.group(4)
        timezone_offset = timezone(
            timedelta(
                hours=int(timezone_text[1:3]),
                minutes=int(timezone_text[3:5]),
            ) * (1 if timezone_text[0] == "+" else -1)
        )
        metadata["author_date"] = datetime.fromtimestamp(timestamp, timezone_offset).isoformat()
        break
    if not separator:
        return metadata
    for line in message.splitlines():
        subject = line.strip()
        if subject:
            metadata["subject"] = subject
            return metadata
    return metadata


def _run_git_probe(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )


def _git_status_key(payload: dict[str, object]) -> tuple[object, ...]:
    return (
        payload.get("valid"),
        payload.get("is_git_repository"),
        payload.get("repository_root"),
        payload.get("git_branch"),
        payload.get("git_head"),
        payload.get("git_short_head"),
        payload.get("git_dirty"),
        payload.get("git_detached"),
        payload.get("git_label"),
        payload.get("git_commit_subject"),
        payload.get("git_commit_author"),
        payload.get("git_commit_author_email"),
        payload.get("git_commit_author_date"),
        payload.get("git_upstream"),
        payload.get("git_ahead"),
        payload.get("git_behind"),
        payload.get("git_staged_count"),
        payload.get("git_unstaged_count"),
        payload.get("git_untracked_count"),
        payload.get("git_conflicted_count"),
    )


_GIT_STATUS_WATCH_FILTER = DefaultFilter(ignore_dirs=())


def _resolve_git_path(repository_root: Path, path_text: str) -> Path | None:
    if not path_text:
        return None
    path = Path(path_text)
    if not path.is_absolute():
        path = repository_root / path
    return path.resolve(strict=False)


def _git_status_watch_paths(directory: str, payload: dict[str, object]) -> list[str]:
    repository_root_text = str(payload.get("repository_root") or "")
    if not repository_root_text:
        return []
    repository_root = Path(repository_root_text).expanduser().resolve(strict=False)
    paths: list[Path] = [repository_root]
    for arguments in (("rev-parse", "--git-dir"), ("rev-parse", "--git-common-dir")):
        result = _run_git_probe(Path(directory), *arguments)
        if result.returncode != 0:
            continue
        path = _resolve_git_path(repository_root, result.stdout.strip())
        if path is not None:
            paths.append(path)

    seen: set[str] = set()
    existing_paths: list[str] = []
    for path in paths:
        path_text = str(path)
        if path_text in seen or not path.exists():
            continue
        seen.add(path_text)
        existing_paths.append(path_text)
    return existing_paths


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _git_status_changes_relevant(directory: str, payload: dict[str, object], changes: set[tuple[object, str]]) -> bool:
    repository_root_text = str(payload.get("repository_root") or "")
    if not repository_root_text:
        return True
    repository_root = Path(repository_root_text).expanduser().resolve(strict=False)
    git_paths = [
        path
        for path in (
            _resolve_git_path(repository_root, _run_git_probe(Path(directory), "rev-parse", "--git-dir").stdout.strip()),
            _resolve_git_path(repository_root, _run_git_probe(Path(directory), "rev-parse", "--git-common-dir").stdout.strip()),
        )
        if path is not None
    ]

    worktree_paths: list[str] = []
    for _change, path_text in changes:
        changed_path = Path(path_text).resolve(strict=False)
        if any(_is_relative_to(changed_path, git_path) for git_path in git_paths):
            return True
        if _is_relative_to(changed_path, repository_root):
            worktree_paths.append(str(changed_path.relative_to(repository_root)))

    if not worktree_paths:
        return False

    check_ignore = subprocess.run(
        ["git", "-C", str(Path(directory)), "check-ignore", "--stdin"],
        input="\n".join(worktree_paths),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        timeout=5,
    )
    if check_ignore.returncode == 128:
        return True
    ignored = set(check_ignore.stdout.splitlines())
    return any(path not in ignored for path in worktree_paths)


def _open_folder_picker() -> dict[str, object]:
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose a working directory")'],
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return _folder_picker_result(result)
        if system == "Windows":
            command = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$dialog.Description = 'Choose a working directory'; "
                "$dialog.ShowNewFolderButton = $true; "
                "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ $dialog.SelectedPath }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", command],
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return _folder_picker_result(result)
        result = _run_unix_folder_picker()
        if result is not None:
            return _folder_picker_result(result)
        return {
            "path": "",
            "cancelled": True,
            "error": "No supported graphical folder picker is available.",
        }
    except subprocess.TimeoutExpired:
        return {"path": "", "cancelled": True, "error": "Folder selection timed out."}
    except FileNotFoundError as exception:
        return {"path": "", "cancelled": True, "error": f"Folder picker is unavailable: {exception.filename}"}


def _folder_picker_result(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    if result.returncode != 0:
        return {"path": "", "cancelled": True, "error": result.stderr.strip()}
    selected_path = result.stdout.strip()
    if not selected_path:
        return {"path": "", "cancelled": True}
    return {"path": str(Path(selected_path).expanduser().resolve()), "cancelled": False}


def _run_unix_folder_picker() -> subprocess.CompletedProcess[str] | None:
    if shutil.which("zenity"):
        return subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Choose a working directory"],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    if shutil.which("kdialog"):
        return subprocess.run(
            ["kdialog", "--getexistingdirectory", str(Path.home())],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    return _run_tk_folder_picker()


def _run_tk_folder_picker() -> subprocess.CompletedProcess[str] | None:
    script = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "path = filedialog.askdirectory(title='Choose a working directory')\n"
        "print(path or '')\n"
        "root.destroy()\n"
    )
    try:
        return subprocess.run(
            ["python3", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None


def _accessibility_granted() -> bool:
    """Whether this process may control other apps. Imported lazily, since PyObjC makes the prototype unforkable."""
    try:
        from frank.computer import permissions

        return permissions.accessibility_granted()
    except Exception:
        return False


def _request_accessibility() -> None:
    """Surface the system prompt (and deep-link to the pane) if not yet trusted."""
    try:
        from frank.computer import permissions

        permissions.request_accessibility()
    except Exception:
        pass


def _open_accessibility_settings() -> None:
    try:
        from frank.computer import permissions

        permissions.open_accessibility_settings()
    except Exception:
        pass
