"""Load model prompts from markdown files in ``prompts/``.

Prompts live as their own ``.md`` files, never inlined in code, so they can be read
and edited as prose. A template may contain ``{{ variable }}`` placeholders that are
filled at load time; an unmatched placeholder is left verbatim rather than blanked.
This mirrors the harness core's prompt loader.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_PROMPT_DIRECTORY = Path(__file__).parent / "prompts"
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def load_prompt(name: str, /, **variables: str) -> str:
    """Return the prompt named ``name`` (a file ``prompts/<name>.md``) with its
    ``{{ variable }}`` placeholders replaced from ``variables``."""
    template = _read_template(name)

    def replace(match: re.Match) -> str:
        return variables.get(match.group(1), match.group(0))

    return _PLACEHOLDER.sub(replace, template).strip()


@lru_cache(maxsize=None)
def _read_template(name: str) -> str:
    path = _PROMPT_DIRECTORY / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")
