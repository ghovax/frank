"""Where the prompt's material comes from, and the two obvious places it comes from."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from langmesh.base.instructions import Instruction, as_instructions
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

# Instruction files a project may carry, in preference order, with the first match winning.
PROJECT_INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md", "CONTEXT.md")


# Well-known user-wide instruction files, read only when a catalogue is explicitly given them.
def home_instruction_paths() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".config" / "opencode" / "AGENTS.md",
        home / ".claude" / "CLAUDE.md",
        home / ".agents" / "AGENTS.md",
        home / "AGENTS.md",
    )


@dataclass(frozen=True)
class CatalogueRoots:
    """The directories a file catalogue searches, as a value the caller assembles."""

    agents: tuple[Path, ...] = ()
    skills: tuple[Path, ...] = ()
    memories: tuple[Path, ...] = ()
    prompts: Optional[Path] = None
    # Where to start walking for AGENTS.md / CLAUDE.md / CONTEXT.md.
    project_directory: Optional[Path] = None
    # Whether to also read the well-known user-wide instruction files, false for a library.
    include_home_instructions: bool = False


def _as_paths(directories: Iterable[str | Path] | str | Path | None) -> tuple[Path, ...]:
    if directories is None:
        return ()
    if isinstance(directories, (str, Path)):
        return (Path(directories).expanduser(),)
    return tuple(Path(directory).expanduser() for directory in directories)


class FileCatalogue:
    """The `.agents` trees on disk: what LangMesh has always read, now behind the interface."""

    def __init__(self, roots: CatalogueRoots) -> None:
        self._roots = roots

    # Agents

    def agent(self, name: str) -> Any:
        from langmesh.base.configuration import load_agent_configuration

        if not self._roots.agents:
            return None
        try:
            return load_agent_configuration(name, list(self._roots.agents))
        except FileNotFoundError:
            return None

    def agents(self) -> Sequence[str]:
        from langmesh.base.configuration import list_agent_route_names

        if not self._roots.agents:
            return []
        return list_agent_route_names(list(self._roots.agents))

    # Skills and memories, re-read each call so an edit takes effect without a restart.

    def skills(self) -> Sequence[Any]:
        from langmesh.base.skills import load_skills

        if not self._roots.skills:
            return []
        return load_skills(list(self._roots.skills))

    def memories(self) -> Sequence[Any]:
        from langmesh.base.memories import load_memories

        if not self._roots.memories:
            return []
        return load_memories(list(self._roots.memories))

    # Instructions

    def instructions(self) -> list[Instruction]:
        entries: list[Instruction] = []
        seen: set[Path] = set()

        if self._roots.include_home_instructions:
            for path in home_instruction_paths():
                try:
                    resolved = path.expanduser().resolve()
                except OSError:
                    continue
                if not resolved.is_file() or resolved in seen:
                    continue
                seen.add(resolved)
                # A home-wide document governs everything, which is also why it loses to a project's own.
                entries.append(
                    Instruction(
                        source=str(resolved),
                        scope=str(resolved.parent),
                        content=resolved.read_text(errors="ignore").strip(),
                    )
                )

        project = self._project_instruction()
        if project is not None and project not in seen:
            entries.append(
                Instruction(
                    source=str(project),
                    scope=str(project.parent),
                    content=project.read_text(errors="ignore").strip(),
                )
            )

        return entries

    def _project_instruction(self) -> Optional[Path]:
        """The nearest instruction file at or above the project directory, stopping at the home directory."""
        if self._roots.project_directory is None:
            return None
        try:
            current = self._roots.project_directory.expanduser().resolve()
            home = Path.home().resolve()
        except OSError:
            return None
        while True:
            for name in PROJECT_INSTRUCTION_NAMES:
                candidate = current / name
                if candidate.is_file():
                    return candidate
            if current == current.parent or current == home:
                return None
            current = current.parent

    # Prompt templates

    def prompt(self, name: str, variables: Mapping[str, str]) -> str:
        from langmesh.base.configuration import PromptLoader

        directory = self._roots.prompts
        if directory is None:
            return ""
        return PromptLoader(directory).load(name, dict(variables))


class Catalogue:
    """Everything supplied in code and nothing read from disk."""

    def __init__(
        self,
        agents: Optional[Mapping[str, Any]] = None,
        skills: Optional[Iterable[Any]] = None,
        memories: Optional[Iterable[Any]] = None,
        instructions: str | Iterable[Instruction] | None = None,
        prompts: Optional[Mapping[str, str]] = None,
        fallback_prompts: Optional[Path] = None,
    ) -> None:
        self._agents = dict(agents or {})
        self._skills = list(skills or ())
        self._memories = list(memories or ())
        self._instructions = as_instructions(instructions)
        self._prompts = dict(prompts or {})
        # Where unlisted templates come from. `None` means the packaged ones.
        self._fallback_prompts = fallback_prompts

    def agent(self, name: str) -> Any:
        return self._agents.get(name)

    def agents(self) -> Sequence[str]:
        return sorted(self._agents)

    def skills(self) -> Sequence[Any]:
        return list(self._skills)

    def memories(self) -> Sequence[Any]:
        return list(self._memories)

    def instructions(self) -> list[Instruction]:
        return list(self._instructions)

    def prompt(self, name: str, variables: Mapping[str, str]) -> str:
        from langmesh.base.configuration import PromptLoader

        template = self._prompts.get(name)
        if template is not None:
            return PromptLoader.render(template, dict(variables), name)
        directory = self._fallback_prompts or packaged_prompts_directory()
        return PromptLoader(directory).load(name, dict(variables))


def packaged_prompts_directory() -> Path:
    """Where the harness's own prompt templates live."""
    return Path(__file__).resolve().parent.parent / "runtime" / "prompts"


def machine_catalogue(configuration: Any, working_directory: str = "") -> FileCatalogue:
    """The catalogue a person's machine has, including the home roots."""
    return FileCatalogue(
        CatalogueRoots(
            agents=_as_paths(configuration.agent_directories_for(working_directory)),
            skills=_as_paths(configuration.skill_directories_for(working_directory)),
            memories=_as_paths(configuration.memory_directories_for(working_directory)),
            prompts=packaged_prompts_directory(),
            project_directory=Path(working_directory).expanduser() if working_directory else None,
            include_home_instructions=True,
        )
    )


def project_catalogue(configuration: Any, working_directory: str) -> FileCatalogue:
    """The catalogue an embedded harness gets: the working directory, and nothing of the home directory."""
    from langmesh.base.configuration import BUNDLED_DOTAGENTS_ROOT

    local = Path(working_directory).expanduser()
    return FileCatalogue(
        CatalogueRoots(
            agents=(
                BUNDLED_DOTAGENTS_ROOT / "agents",
                local / ".agents" / "agents",
                local / "agents",
            ),
            skills=(
                BUNDLED_DOTAGENTS_ROOT / "skills",
                local / ".agents" / "skills",
                local / "skills",
            ),
            memories=(local / ".agents" / "memories",),
            prompts=packaged_prompts_directory(),
            project_directory=local,
            include_home_instructions=False,
        )
    )


__all__ = [
    "Catalogue",
    "CatalogueRoots",
    "FileCatalogue",
    "PROJECT_INSTRUCTION_NAMES",
    "home_instruction_paths",
    "machine_catalogue",
    "packaged_prompts_directory",
    "project_catalogue",
]
