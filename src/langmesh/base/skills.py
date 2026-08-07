"""File-based skills: a name, a title and a description in front matter, with instructions in the body."""

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
    from langmesh.base.file_cache import parsed_file

    content = parsed_file(path, lambda each: each.read_text()) or ""
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
    return Skill(
        name=identifier,
        title=title,
        description=description,
        enabled=enabled,
        body=body,
        path=str(path.resolve()),
    )


def _as_directories(directories: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(directories, (str, Path)):
        return [Path(directories).expanduser()]
    return [Path(directory).expanduser() for directory in directories]


def load_skills(skills_directory: str | Path | Iterable[str | Path]) -> list[Skill]:
    """Discover skill files, deduplicated by name, later directories overriding earlier ones."""
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
    """The skills available to an agent: all of them, or the subset its ``skills`` front matter names."""
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
