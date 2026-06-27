"""File-based skills.

A skill is just a file: ``.agents/skills/<name>.md`` or
``.agents/skills/<id>/SKILL.md`` with a ``name`` and ``description`` in its
frontmatter and instructions in its body. Skills are auto-discovered, broadcast
on every agent's A2A AgentCard, and listed in every agent's system context so
agents are aware of them by default. To use a skill, an agent reads its file
(progressive disclosure) and follows the instructions.
"""

from collections.abc import Iterable
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


class Skill(BaseModel):
    id: str = ""  # stable identifier
    name: str = ""  # human-friendly display name, advertised on cards and the UI
    description: str = ""
    enabled: bool = True
    body: str = ""
    path: str = ""

    @property
    def identifier(self) -> str:
        return self.id or self.name


def _parse_skill(path: Path) -> Skill:
    content = path.read_text()
    match = _FRONTMATTER.match(content)
    if match:
        frontmatter = yaml.safe_load(match.group(1)) or {}
        body = match.group(2).strip()
        default_identifier = path.parent.name if path.name.upper() == "SKILL.MD" else path.stem
        identifier = str(frontmatter.get("id") or default_identifier)
        name = str(frontmatter.get("name") or identifier)
        description = str(frontmatter.get("description", ""))
        enabled = bool(frontmatter.get("enabled", True))
    else:
        identifier = path.parent.name if path.name.upper() == "SKILL.MD" else path.stem
        name = identifier
        description = ""
        enabled = True
        body = content.strip()
    return Skill(id=identifier, name=name, description=description, enabled=enabled, body=body, path=str(path))


def _as_directories(directories: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(directories, (str, Path)):
        return [Path(directories).expanduser()]
    return [Path(directory).expanduser() for directory in directories]


def load_skills(skills_directory: str | Path | Iterable[str | Path]) -> list[Skill]:
    """Discover skill files, deduplicated by skill name.

    Directories are processed in order; later entries override earlier entries.
    This lets project-local ``.agents/skills`` replace a global ``~/.agents/skills``
    skill with the same name.
    """
    skills: dict[str, Skill] = {}
    for directory in _as_directories(skills_directory):
        if not directory.is_dir():
            continue
        candidates = [
            *sorted(directory.glob("*.md")),
            *sorted(directory.glob("*/SKILL.md")),
            *sorted(directory.glob("*/skill.md")),
        ]
        for path in candidates:
            skill = _parse_skill(path)
            if skill.enabled:
                skills[skill.identifier] = skill
    return [skills[name] for name in sorted(skills)]


def skills_for_agent(skills: list[Skill], allowed_names: list[str]) -> list[Skill]:
    """The skills available to an agent: all of them by default, or only the
    named subset if the agent restricts itself via its ``skills`` frontmatter."""
    if not allowed_names:
        return skills
    wanted = set(allowed_names)
    return [skill for skill in skills if skill.identifier in wanted]


def skills_payload(skills: list[Skill]) -> list[dict]:
    """The structured skills data injected into an agent's system context — name,
    name, description, and the path to read for full instructions."""
    return [
        {"id": skill.identifier, "name": skill.name, "description": skill.description, "path": skill.path}
        for skill in skills
    ]
