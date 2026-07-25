"""Shadow-git version store for agent-produced artifacts.

Every file the agent explicitly touches at a ``(location, path)`` is snapshotted into a
**xeac-owned** git repository that lives under ``<location-home>/.xeac/versions/`` and
is *never* the user's own repo. The trick that keeps it from ever touching the user's
``.git`` is to always drive git with an explicit ``--git-dir`` (our shadow repo) and
``--work-tree`` (the user's real directory): git then does no repository discovery and
writes only inside our shadow git directory.

Crucially, we **only ever stage the specific paths the agent touches** — never a whole
folder. Working in a huge directory (say ``~/Downloads`` with gigabytes of unrelated
files) costs nothing: git hashes and stores only the handful of files the agent actually
wrote, and diffs each against its own previous version. There is no baseline and no
``git add -A`` survey of the folder.

What gets tracked:

* **Structured writes** (``write_file``/``edit_file``) and **opened artifacts**
  (``open_artifact``) add their exact path to the tracked set (``git add -f -- <path>``).
* The **first time** we track a pre-existing file that is being *edited*, its original
  bytes are committed as version 1 (from the tool's ``before`` content, since the file on
  disk is already edited by the time we run), so the pre-agent state is restorable.
* After a **bash** call we run ``git add -u`` — this restages only files that are *already
  tracked* (cheap: it stats the tracked set, never the folder) and ignores everything
  else, so a bash *regeneration* of a tracked file is caught while a brand-new bash file
  is deliberately not auto-discovered.

Everything runs through a :class:`LocationExecutor`, so the exact same plumbing works on
the home machine (``LocalExecutor``) and on a remote host over SSH (``SshExecutor``).

Invariants: **plumbing only, HEAD never moves.** Each session stages into its own index
file and commits with ``write-tree`` → ``commit-tree`` → ``update-ref`` on a per-session
branch, so parallel sessions never collide; we never ``git checkout``/``commit``/``reset``.
The only index-touching commands are ``add``/``rm``/``update-index``/``read-tree`` (all
index-only). Restore is append-only. Staged blobs over ``maximum_bytes`` become placeholder
versions rather than being stored.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
from dataclasses import dataclass, field

from xeac.locations.executor import CommandResult, LocationExecutor

# A git identity for commit-tree. The shadow history is machine-authored, so a fixed
# identity is correct (and keeps commit-tree from failing when no user.* is configured).
_GIT_IDENTITY_ENVIRONMENT = (
    "GIT_AUTHOR_NAME=XEAC GIT_AUTHOR_EMAIL=xeac@localhost "
    "GIT_COMMITTER_NAME=XEAC GIT_COMMITTER_EMAIL=xeac@localhost"
)

# 128 MiB — matches the settings default; blobs above this become placeholder versions.
DEFAULT_MAXIMUM_BYTES = 128 * 1024 * 1024


@dataclass
class CapturedFile:
    """One file that changed in a captured version."""

    relative_path: str
    absolute_path: str
    blob_sha: str  # git blob sha of the new content; "" for deletions and placeholders
    change_type: str  # "A" | "M" | "D"
    size: int
    is_placeholder: bool = False  # bytes exceeded maximum_bytes → excluded from the commit


@dataclass
class CommitResult:
    """A recorded version (a commit on the session branch)."""

    commit_sha: str
    sequence: int  # 1-based position on the session branch
    files: list[CapturedFile] = field(default_factory=list)


class VersionStoreError(RuntimeError):
    pass


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def safe_reference_component(value: str) -> str:
    """Sanitize a session id into a git-ref-safe branch component."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-.") or "session"
    return cleaned


def branch_reference(session_id: str) -> str:
    return f"refs/heads/xeac/session/{safe_reference_component(session_id)}"


def _index_file(git_directory: str, session_id: str) -> str:
    return posixpath.join(git_directory, f"xeac-index-{safe_reference_component(session_id)}")


def git_directory_for(location_home: str, project_id: str, work_tree: str) -> str:
    """The shadow git directory for a ``(location, work-tree)``: one repo per real
    directory, keyed by a hash of the work-tree so different directories on the same
    machine don't collide, grouped under the project."""
    key = hashlib.sha256(work_tree.encode("utf-8")).hexdigest()[:16]
    project = safe_reference_component(project_id or "no-project")
    return posixpath.join(location_home, ".xeac", "versions", project, f"{key}.git")


def _run(executor: LocationExecutor, command: str, working_directory: str, *, allow_failure: bool = False) -> CommandResult:
    result = executor.run(command, working_directory)
    if not allow_failure and result.returncode != 0:
        raise VersionStoreError((result.stderr or result.stdout or "git command failed").strip())
    return result


def _git_command(git_directory: str, work_tree: str | None = None) -> str:
    prefix = f"git --git-dir={_shell_quote(git_directory)}"
    if work_tree is not None:
        prefix += f" --work-tree={_shell_quote(work_tree)}"
    return prefix


def ensure_repository(executor: LocationExecutor, git_directory: str, work_tree: str) -> None:
    """Create + configure the shadow repo if absent. Idempotent and cheap to call before
    every capture."""
    if not executor.exists(posixpath.join(git_directory, "HEAD")):
        _run(
            executor,
            f"mkdir -p {_shell_quote(git_directory)} && {_git_command(git_directory)} init -q "
            f"&& {_git_command(git_directory)} config core.bare false "
            f"&& {_git_command(git_directory)} config gc.auto 0 "
            f"&& {_git_command(git_directory)} config commit.gpgsign false",
            work_tree,
        )


def resolve_reference(executor: LocationExecutor, git_directory: str, reference: str) -> str:
    """Resolve a ref/commit to a sha, or "" if it doesn't exist."""
    result = executor.run(f"{_git_command(git_directory)} rev-parse --verify -q {_shell_quote(reference)}", git_directory)
    return result.stdout.strip() if result.returncode == 0 else ""


def _read_tree(executor: LocationExecutor, git_directory: str, index_file: str, commit: str) -> None:
    """Reset the per-session index to a commit's tree, so staging works from the current
    tracked set even after the disposable index file is gone (e.g. a server restart)."""
    _run(executor, f"GIT_INDEX_FILE={_shell_quote(index_file)} {_git_command(git_directory)} read-tree {_shell_quote(commit)}", git_directory)


def _write_tree(executor: LocationExecutor, git_directory: str, work_tree: str, index_file: str) -> str:
    result = _run(executor, f"GIT_INDEX_FILE={_shell_quote(index_file)} {_git_command(git_directory, work_tree)} write-tree", work_tree)
    return result.stdout.strip()


def _commit_tree(executor: LocationExecutor, git_directory: str, work_tree: str, tree: str, parent: str, message: str) -> str:
    parent_flag = f"-p {_shell_quote(parent)} " if parent else ""
    command = (
        f"{_GIT_IDENTITY_ENVIRONMENT} {_git_command(git_directory, work_tree)} commit-tree {_shell_quote(tree)} {parent_flag}"
        f"-m {_shell_quote(message)}"
    )
    return _run(executor, command, work_tree).stdout.strip()


def _tree_of(executor: LocationExecutor, git_directory: str, commit: str) -> str:
    return _run(executor, f"{_git_command(git_directory)} rev-parse {_shell_quote(commit)}^{{tree}}", git_directory).stdout.strip()


def _tree_blob_sizes(executor: LocationExecutor, git_directory: str, tree: str) -> dict[str, tuple[str, int]]:
    """Map ``relative_path -> (blob_sha, size)`` for every blob in a tree (one git call)."""
    result = _run(executor, f"{_git_command(git_directory)} ls-tree -r -l --full-tree {_shell_quote(tree)}", git_directory)
    sizes: dict[str, tuple[str, int]] = {}
    for line in result.stdout.splitlines():
        # "<mode> <type> <sha> <size>\t<path>"
        metadata, _, relative_path = line.partition("\t")
        if not relative_path:
            continue
        fields = metadata.split()
        if len(fields) < 4 or fields[1] != "blob":
            continue
        blob_sha, size_text = fields[2], fields[3]
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        sizes[relative_path] = (blob_sha, size)
    return sizes


def _unstage(executor: LocationExecutor, git_directory: str, work_tree: str, index_file: str, relative_path: str) -> None:
    _run(
        executor,
        f"GIT_INDEX_FILE={_shell_quote(index_file)} {_git_command(git_directory, work_tree)} rm --cached --quiet --ignore-unmatch -- {_shell_quote(relative_path)}",
        work_tree,
        allow_failure=True,
    )


def _apply_size_cap(executor: LocationExecutor, git_directory: str, work_tree: str, index_file: str, maximum_bytes: int) -> list[CapturedFile]:
    """Unstage any staged blob over ``maximum_bytes``; return placeholder records for them."""
    tree = _write_tree(executor, git_directory, work_tree, index_file)
    placeholders: list[CapturedFile] = []
    for relative_path, (_blob_sha, size) in _tree_blob_sizes(executor, git_directory, tree).items():
        if size > maximum_bytes:
            _unstage(executor, git_directory, work_tree, index_file, relative_path)
            placeholders.append(
                CapturedFile(
                    relative_path=relative_path,
                    absolute_path=posixpath.join(work_tree, relative_path),
                    blob_sha="",
                    change_type="M",
                    size=size,
                    is_placeholder=True,
                )
            )
    return placeholders


def _changed_files(
    executor: LocationExecutor,
    git_directory: str,
    work_tree: str,
    parent: str,
    commit: str,
    sizes: dict[str, tuple[str, int]],
) -> list[CapturedFile]:
    """Files changed between ``parent`` (may be "") and ``commit``, via ``diff-tree``."""
    root_flag = "--root " if not parent else ""
    command = f"{_git_command(git_directory)} diff-tree -r {root_flag}{_shell_quote(parent) + ' ' if parent else ''}{_shell_quote(commit)}"
    result = _run(executor, command, git_directory)
    files: list[CapturedFile] = []
    for line in result.stdout.splitlines():
        # ":<mode1> <mode2> <sha1> <sha2> <status>\t<path>"
        if not line.startswith(":"):
            continue
        metadata, _, relative_path = line.partition("\t")
        fields = metadata[1:].split()
        if len(fields) < 5 or not relative_path:
            continue
        new_blob_sha, status = fields[3], fields[4][0]
        blob_sha = "" if status == "D" or new_blob_sha.strip("0") == "" else new_blob_sha
        size = sizes.get(relative_path, ("", 0))[1]
        files.append(
            CapturedFile(
                relative_path=relative_path,
                absolute_path=posixpath.join(work_tree, relative_path),
                blob_sha=blob_sha,
                change_type=status,
                size=size,
            )
        )
    return files


def _commit_sequence(executor: LocationExecutor, git_directory: str, commit: str) -> int:
    """1-based position of a commit on its branch (the number of ancestor commits including
    this one)."""
    result = _run(executor, f"{_git_command(git_directory)} rev-list --count {_shell_quote(commit)}", git_directory)
    try:
        return max(1, int(result.stdout.strip()))
    except ValueError:
        return 1


def _hash_object(executor: LocationExecutor, git_directory: str, session_id: str, content: bytes) -> str:
    """Write given bytes into the object store and return the blob sha, without touching
    the work tree (used to commit a file's pre-edit *original* bytes)."""
    temporary = posixpath.join(git_directory, f"xeac-blob-tmp-{safe_reference_component(session_id)}")
    executor.write_bytes(temporary, content)
    try:
        result = _run(executor, f"{_git_command(git_directory)} hash-object -w {_shell_quote(temporary)}", git_directory)
        return result.stdout.strip()
    finally:
        _run(executor, f"rm -f {_shell_quote(temporary)}", git_directory, allow_failure=True)


def _commit_index(
    executor: LocationExecutor,
    git_directory: str,
    work_tree: str,
    index_file: str,
    parent: str,
    message: str,
    maximum_bytes: int,
) -> CommitResult | None:
    """Cap oversized staged blobs, write the tree, and commit it on the branch if it
    differs from ``parent`` (or something was capped). Returns the recorded version."""
    placeholders = _apply_size_cap(executor, git_directory, work_tree, index_file, maximum_bytes)
    tree = _write_tree(executor, git_directory, work_tree, index_file)
    if parent and tree == _tree_of(executor, git_directory, parent) and not placeholders:
        return None
    # The caller owns the ref update (it knows the branch), so this only builds the commit.
    commit = _commit_tree(executor, git_directory, work_tree, tree, parent, message)
    sizes = _tree_blob_sizes(executor, git_directory, tree)
    files = _changed_files(executor, git_directory, work_tree, parent, commit, sizes)
    files.extend(placeholders)
    return CommitResult(commit_sha=commit, sequence=_commit_sequence(executor, git_directory, commit), files=files)


def track_paths(
    executor: LocationExecutor,
    git_directory: str,
    work_tree: str,
    session_id: str,
    relative_paths: list[str],
    *,
    original_contents: dict[str, str] | None = None,
    maximum_bytes: int = DEFAULT_MAXIMUM_BYTES,
    message: str = "capture",
) -> list[CommitResult]:
    """Version exactly ``relative_paths`` by staging only those paths — never the folder.

    When a path is being tracked for the first time and an ``original_contents`` entry is
    given for it (a structured edit of a pre-existing file), its original bytes are
    committed as a first version before the current on-disk content, so the pre-agent state
    is restorable. Returns the versions created (0, 1, or 2)."""
    if not relative_paths:
        return []
    ensure_repository(executor, git_directory, work_tree)
    branch = branch_reference(session_id)
    index_file = _index_file(git_directory, session_id)
    head = resolve_reference(executor, git_directory, branch)
    if head:
        _read_tree(executor, git_directory, index_file, head)
    results: list[CommitResult] = []

    # 1. Original (pre-edit) bytes for first-tracked, pre-existing files.
    originals = {
        path: content
        for path, content in (original_contents or {}).items()
        if content is not None and path in relative_paths and (not head or not blob_at(executor, git_directory, head, path))
    }
    if originals:
        for path, content in originals.items():
            blob = _hash_object(executor, git_directory, session_id, content.encode("utf-8"))
            _run(
                executor,
                f"GIT_INDEX_FILE={_shell_quote(index_file)} {_git_command(git_directory)} update-index --add --cacheinfo {_shell_quote(f'100644,{blob},{path}')}",
                work_tree,
            )
        recorded = _commit_index(executor, git_directory, work_tree, index_file, head, f"original: {message}", maximum_bytes)
        if recorded is not None:
            _run(executor, f"{_git_command(git_directory)} update-ref {_shell_quote(branch)} {_shell_quote(recorded.commit_sha)}", work_tree)
            results.append(recorded)
            head = recorded.commit_sha

    # 2. Current on-disk content of the paths (forced, so an explicitly written/opened file
    #    is tracked even if the host repo's .gitignore would exclude it).
    pathspec = " ".join(_shell_quote(path) for path in relative_paths)
    _run(executor, f"GIT_INDEX_FILE={_shell_quote(index_file)} {_git_command(git_directory, work_tree)} add -f -- {pathspec}", work_tree)
    recorded = _commit_index(executor, git_directory, work_tree, index_file, head, message, maximum_bytes)
    if recorded is not None:
        _run(executor, f"{_git_command(git_directory)} update-ref {_shell_quote(branch)} {_shell_quote(recorded.commit_sha)}", work_tree)
        results.append(recorded)
    return results


def recheck_tracked(
    executor: LocationExecutor,
    git_directory: str,
    work_tree: str,
    session_id: str,
    *,
    maximum_bytes: int = DEFAULT_MAXIMUM_BYTES,
    message: str = "bash",
) -> CommitResult | None:
    """Restage only the files already tracked in this session (``git add -u``) and commit
    if any changed. Cheap: it stats the tracked set, never the folder — so a bash command
    that regenerated a tracked file is versioned, without discovering the ambient folder."""
    if not executor.exists(posixpath.join(git_directory, "HEAD")):
        return None
    branch = branch_reference(session_id)
    head = resolve_reference(executor, git_directory, branch)
    if not head:
        return None  # nothing tracked yet
    index_file = _index_file(git_directory, session_id)
    _read_tree(executor, git_directory, index_file, head)
    _run(executor, f"GIT_INDEX_FILE={_shell_quote(index_file)} {_git_command(git_directory, work_tree)} add -u", work_tree)
    recorded = _commit_index(executor, git_directory, work_tree, index_file, head, message, maximum_bytes)
    if recorded is not None:
        _run(executor, f"{_git_command(git_directory)} update-ref {_shell_quote(branch)} {_shell_quote(recorded.commit_sha)}", work_tree)
    return recorded


def read_blob(executor: LocationExecutor, git_directory: str, blob_sha: str) -> bytes:
    """Raw bytes of a blob (binary-safe)."""
    return executor.run_bytes(f"{_git_command(git_directory)} cat-file blob {_shell_quote(blob_sha)}", git_directory)


def blob_at(executor: LocationExecutor, git_directory: str, commit_sha: str, relative_path: str) -> str:
    """The blob sha of ``relative_path`` as of ``commit_sha`` ("" if absent)."""
    result = executor.run(f"{_git_command(git_directory)} rev-parse -q --verify {_shell_quote(commit_sha + ':' + relative_path)}", git_directory)
    return result.stdout.strip() if result.returncode == 0 else ""


def diff(executor: LocationExecutor, git_directory: str, relative_path: str, from_commit: str, to_commit: str) -> str:
    """Unified diff of ``relative_path`` between two commits (plumbing ``diff-tree -p``)."""
    command = f"{_git_command(git_directory)} diff-tree -p {_shell_quote(from_commit)} {_shell_quote(to_commit)} -- {_shell_quote(relative_path)}"
    return _run(executor, command, git_directory, allow_failure=True).stdout


def restore(
    executor: LocationExecutor,
    git_directory: str,
    work_tree: str,
    session_id: str,
    relative_path: str,
    commit_sha: str,
    *,
    maximum_bytes: int = DEFAULT_MAXIMUM_BYTES,
) -> list[CommitResult]:
    """Restore ``relative_path`` to its content at ``commit_sha`` — append-only: capture
    the current on-disk state first (so nothing is lost), write the old bytes back, then
    capture again so the restore itself becomes a new version."""
    track_paths(executor, git_directory, work_tree, session_id, [relative_path], maximum_bytes=maximum_bytes, message=f"pre-restore {relative_path}")
    blob_sha = blob_at(executor, git_directory, commit_sha, relative_path)
    if not blob_sha:
        raise VersionStoreError(f"{relative_path} does not exist at {commit_sha[:12]}")
    data = read_blob(executor, git_directory, blob_sha)
    executor.write_bytes(posixpath.join(work_tree, relative_path), data)
    return track_paths(
        executor, git_directory, work_tree, session_id, [relative_path],
        maximum_bytes=maximum_bytes, message=f"restore {relative_path} to {commit_sha[:12]}",
    )


def prune_session(executor: LocationExecutor, git_directory: str, session_id: str) -> None:
    """Drop a session's branch + index (session delete)."""
    if not executor.exists(posixpath.join(git_directory, "HEAD")):
        return
    _run(executor, f"{_git_command(git_directory)} update-ref -d {_shell_quote(branch_reference(session_id))}", git_directory, allow_failure=True)
    _run(executor, f"rm -f {_shell_quote(_index_file(git_directory, session_id))}", git_directory, allow_failure=True)


def prune_repository(executor: LocationExecutor, git_directory: str) -> None:
    """Remove an entire shadow repo (project delete)."""
    _run(executor, f"rm -rf {_shell_quote(git_directory)}", posixpath.dirname(git_directory) or "/", allow_failure=True)
