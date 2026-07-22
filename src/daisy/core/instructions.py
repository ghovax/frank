"""Load instruction files (AGENTS.md / CLAUDE.md / CONTEXT.md) that layer
project- and user-level conventions on top of an agent's system prompt.

Mirrors opencode's instruction loading: global files from well-known config
locations, plus the first match walking up from the working directory. The
result is returned as a JSON array (``[{"path", "content"}, ...]``) and dropped
into the system prompt through the ``{{ instructions }}`` PromptLoader
substitution — the model reads the JSON directly, so no formatted markdown is
hand-assembled in code.
"""

from __future__ import annotations

import json
from pathlib import Path

# Global instruction files, loaded in order. These are the well-known locations
# for user-wide rules on this setup: opencode's own global AGENTS.md, Claude
# Code's global CLAUDE.md, a harness-native AGENTS.md under ~/.agents, and the
# home-level AGENTS.md. Duplicates (by resolved path) are de-duplicated.
_GLOBAL_INSTRUCTION_PATHS = (
    Path.home() / ".config" / "opencode" / "AGENTS.md",
    Path.home() / ".claude" / "CLAUDE.md",
    Path.home() / ".agents" / "AGENTS.md",
    Path.home() / "AGENTS.md",
)

# Project-level instruction files, searched in this priority order at each
# ancestor of the working directory. The first existing file wins; ancestors are
# not stacked (matching opencode's "first match up the tree" behavior).
_PROJECT_INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md", "CONTEXT.md")


def _find_project_instruction(working_directory: str) -> Path | None:
    start = Path(working_directory).expanduser().resolve() if working_directory else Path.cwd().resolve()
    home = Path.home().resolve()
    current = start
    # Walk up from the working directory; stop at the first ancestor that has a
    # match (no stacking across ancestors). Cap at the home directory so a search
    # starting outside the home tree does not scan the whole filesystem.
    while True:
        for name in _PROJECT_INSTRUCTION_NAMES:
            candidate = current / name
            if candidate.is_file():
                return candidate
        if current == current.parent or current == home:
            return None
        current = current.parent


def load_instructions(working_directory: str) -> str:
    """Return the instruction files as a JSON array string.

    Each entry is ``{"path": <resolved path>, "content": <file body>}``. The
    string is meant for the ``{{ instructions }}`` placeholder; an empty list
    renders as ``[]``.
    """
    entries: list[dict[str, str]] = []
    seen: set[Path] = set()

    for path in _GLOBAL_INSTRUCTION_PATHS:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if not resolved.is_file() or resolved in seen:
            continue
        seen.add(resolved)
        entries.append({"path": str(resolved), "content": resolved.read_text(errors="ignore").strip()})

    project = _find_project_instruction(working_directory)
    if project is not None:
        try:
            resolved = project.resolve()
        except OSError:
            resolved = project
        if resolved not in seen:
            entries.append({"path": str(resolved), "content": resolved.read_text(errors="ignore").strip()})

    return json.dumps(entries)
