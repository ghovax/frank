"""Foundation for the blackboard.

The blackboard is a durable, append-only knowledge base the agent scratches
findings onto and queries freely. It lives entirely outside any skill or repo:
its SQLite database and cached artifacts are auto-created under ``~/.blackboard``
(override with the ``BLACKBOARD_HOME`` environment variable). Skill folders are
reference/execution only — nothing is written inside them.

Every persisted thing gets a random, globally-unique id (never a sequential
counter) so two records can never be confused for one another.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import uuid
from pathlib import Path


def blackboard_home() -> Path:
    """The ``~/.blackboard`` data directory (auto-created). This is where the
    board database and parse cache live — outside the skill and the repo, and
    gitignored by virtue of being in the home directory."""
    home = Path(os.environ.get("BLACKBOARD_HOME") or (Path.home() / ".blackboard")).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    return home


def board_database_path() -> Path:
    return blackboard_home() / "board.db"


def parse_cache_directory() -> Path:
    directory = blackboard_home() / "parse-cache"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def image_blob_directory(document_hash: str) -> Path:
    directory = blackboard_home() / "images" / (document_hash or "unhashed")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def new_id(prefix: str) -> str:
    """A random, globally-unique id with a readable prefix (e.g. ``anchor-a1b2…``).
    Never sequential: distinct things must never share confusable ids."""
    return f"{prefix}-{uuid.uuid4().hex}"


@contextlib.contextmanager
def board_write_lock():
    """Serialize writers across processes (several agents may share one board) with
    an exclusive file lock. Reads open the database read-only and take no lock."""
    lock_path = blackboard_home() / ".board.lock"
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
