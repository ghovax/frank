"""File-based skills.

A skill is just a file: ``skills/<name>.md`` with a ``name`` and ``description``
in its frontmatter and instructions in its body. Skills are auto-discovered,
broadcast on every agent's A2A AgentCard, and listed in every agent's system
context so agents are aware of them by default. To use a skill, an agent reads
its file (progressive disclosure) and follows the instructions.
"""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


class Skill(BaseModel):
    name: str  # stable identifier (the file stem by default)
    label: str = ""  # human-friendly display name, advertised on cards and the UI
    description: str = ""
    body: str = ""
    path: str = ""


def _parse_skill(path: Path) -> Skill:
    content = path.read_text()
    match = _FRONTMATTER.match(content)
    if match:
        frontmatter = yaml.safe_load(match.group(1)) or {}
        body = match.group(2).strip()
        name = str(frontmatter.get("name") or path.stem)
        label = str(frontmatter.get("label") or name)
        description = str(frontmatter.get("description", ""))
    else:
        name = path.stem
        label = name
        description = ""
        body = content.strip()
    return Skill(name=name, label=label, description=description, body=body, path=str(path))


def load_skills(skills_directory: str | Path) -> list[Skill]:
    """Discover every skill file in the skills directory."""
    directory = Path(skills_directory)
    if not directory.is_dir():
        return []
    return [_parse_skill(path) for path in sorted(directory.glob("*.md"))]


def skills_for_agent(skills: list[Skill], allowed_names: list[str]) -> list[Skill]:
    """The skills available to an agent: all of them by default, or only the
    named subset if the agent restricts itself via its ``skills`` frontmatter."""
    if not allowed_names:
        return skills
    wanted = set(allowed_names)
    return [skill for skill in skills if skill.name in wanted]


def skills_payload(skills: list[Skill]) -> list[dict]:
    """The structured skills data injected into an agent's system context — name,
    label, description, and the path to read for full instructions."""
    return [
        {"name": skill.name, "label": skill.label, "description": skill.description, "path": skill.path}
        for skill in skills
    ]
