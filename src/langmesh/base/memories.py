"""Durable notes an agent keeps about a project, loaded from its memories directory."""

from __future__ import annotations

from collections.abc import Iterable
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


class Memory(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    importance: str = ""
    tags: list[str] = []
    body: str = ""
    path: str = ""


def _as_directories(directories: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(directories, (str, Path)):
        return [Path(directories).expanduser()]
    return [Path(directory).expanduser() for directory in directories]


def _parse_memory(path: Path) -> Memory:
    from langmesh.base.file_cache import parsed_file

    content = parsed_file(path, lambda each: each.read_text()) or ""
    match = _FRONTMATTER.match(content)
    if not match:
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
        return Memory(
            name=path.stem,
            title=path.stem,
            description=first_line[:240],
            body=content.strip(),
            path=str(path),
        )
    frontmatter = yaml.safe_load(match.group(1)) or {}
    raw_tags = frontmatter.get("tags", [])
    if isinstance(raw_tags, str):
        tags = [tag.strip() for tag in raw_tags.split(",") if tag.strip()]
    else:
        tags = [str(tag) for tag in raw_tags]
    return Memory(
        name=str(frontmatter.get("name") or path.stem),
        title=str(frontmatter.get("title") or path.stem),
        description=str(frontmatter.get("description") or ""),
        importance=str(frontmatter.get("importance", "")),
        tags=tags,
        body=match.group(2).strip(),
        path=str(path),
    )


def load_memories(memory_directories: str | Path | Iterable[str | Path]) -> list[Memory]:
    memories: dict[str, Memory] = {}
    for directory in _as_directories(memory_directories):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            memory = _parse_memory(path)
            memories[memory.name] = memory
    return [memories[identifier] for identifier in sorted(memories)]


def memories_payload(memories: list[Memory]) -> list[dict]:
    return [
        {
            "name": memory.name,
            "title": memory.title,
            "description": memory.description,
            "importance": memory.importance,
            "tags": memory.tags,
            "path": memory.path,
            "read_hint": "Read this path with bash if the description indicates it is relevant.",
        }
        for memory in memories
    ]
