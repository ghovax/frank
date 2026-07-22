"""File-based skills.

A skill is just a file: ``.agents/skills/<name>.md`` or
``.agents/skills/<name>/SKILL.md`` with a stable ``name``, human-facing
``title``, and ``description`` in its frontmatter and instructions in its body.
Skills are auto-discovered, broadcast on every agent's A2A AgentCard, and listed
in every agent's system context so agents are aware of them by default. To use a
skill, an agent reads its file (progressive disclosure) and follows the
instructions.
"""

from __future__ import annotations

from collections.abc import Iterable
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


class Skill(BaseModel):
    name: str = ""  # stable identifier
    title: str = ""  # human-friendly display title
    description: str = ""
    enabled: bool = True
    body: str = ""
    path: str = ""

    @property
    def identifier(self) -> str:
        return self.name

    @property
    def display_title(self) -> str:
        return self.title or self.name


def _parse_skill(path: Path) -> Skill:
    content = path.read_text()
    match = _FRONTMATTER.match(content)
    if match:
        frontmatter = yaml.safe_load(match.group(1)) or {}
        body = match.group(2).strip()
        default_identifier = path.parent.name if path.name.upper() == "SKILL.MD" else path.stem
        identifier = str(frontmatter.get("name") or default_identifier)
        title = str(frontmatter.get("title") or identifier)
        description = str(frontmatter.get("description", ""))
        enabled = bool(frontmatter.get("enabled", True))
    else:
        identifier = path.parent.name if path.name.upper() == "SKILL.MD" else path.stem
        title = identifier
        description = ""
        enabled = True
        body = content.strip()
    return Skill(name=identifier, title=title, description=description, enabled=enabled, body=body, path=str(path.resolve()))


def _as_directories(directories: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(directories, (str, Path)):
        return [Path(directories).expanduser()]
    return [Path(directory).expanduser() for directory in directories]


def load_skills(skills_directory: str | Path | Iterable[str | Path]) -> list[Skill]:
    """Discover skill files, deduplicated by skill name.

    Directories are processed in order; later entries override earlier entries.
    This lets project-local ``.agents/skills`` replace a global ``~/.agents/skills``
    skill with the same name.

    Disabled skills are still returned (carrying ``enabled=False``) so the UI can
    show them greyed out; callers that build the agent's actual capability set use
    :func:`enabled_skills` to drop them.
    """
    skills: dict[str, Skill] = {}
    for directory in _as_directories(skills_directory):
        if not directory.is_dir():
            continue
        candidates = [
            *sorted(directory.glob("*.md")),
            *sorted(directory.glob("*/SKILL.md")),
        ]
        for path in candidates:
            skill = _parse_skill(path)
            skills[skill.identifier] = skill
    return [skills[name] for name in sorted(skills)]


def enabled_skills(skills: list[Skill]) -> list[Skill]:
    """The subset of skills that are enabled — what an agent may actually use."""
    return [skill for skill in skills if skill.enabled]


def skills_for_agent(skills: list[Skill], allowed_names: list[str]) -> list[Skill]:
    """The skills available to an agent: all of them by default, or only the
    named subset if the agent restricts itself via its ``skills`` frontmatter."""
    if not allowed_names:
        return skills
    wanted = set(allowed_names)
    return [skill for skill in skills if skill.identifier in wanted]


def skills_payload(skills: list[Skill]) -> list[dict]:
    """The structured skills data injected into an agent's system context."""
    return [
        {
            "name": skill.identifier,
            "title": skill.display_title,
            "description": skill.description,
            "path": skill.path,
        }
        for skill in skills
    ]
