"""Where the prompt's material comes from, and the two obvious places it comes from.

:class:`FileCatalogue` is what the harness has always done — walk a set of `.agents` roots for
agents, skills, memories, instructions and prompt templates — moved behind the
:class:`~frank.base.ports.Catalogue` interface rather than being the only possibility. The
path-walking is *correct* for a person's machine; what changed is that it is now a choice.

:class:`DictCatalogue` is the other obvious one: everything supplied in code, nothing read from
disk. It exists because "build a session entirely in memory" is the thing an embedder most
often wants and the thing that was hardest to do, and because a default that only exists in
prose is a default nobody gets.

## The roots are an argument now

`FileCatalogue` takes the directories it searches rather than deriving them, and that is the
whole point of the file. The old resolution baked the home directory in at four levels, and
one of them read *other products'* configuration::

    Path.home() / ".config" / "opencode" / "AGENTS.md"
    Path.home() / ".claude" / "CLAUDE.md"

A library that acquires those because it was imported is not a library anyone can embed. So
`frankd` and the CLI construct a catalogue with the full set — there, the person running it is
the person those files belong to, and inheriting them is the feature — while
:func:`project_catalogue` gives an embedded harness the working directory and nothing else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from frank.base.instructions import Instruction, as_instructions
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

# Instruction files a project may carry, in the order they are preferred. The first match
# walking up from the working directory wins; ancestors are not stacked.
PROJECT_INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md", "CONTEXT.md")

# Well-known user-wide instruction files, including two that belong to other tools. Read only
# when a catalogue is explicitly given them, which the CLI and the daemon do and a library
# session does not.
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
    """The directories a :class:`FileCatalogue` searches, as a value the caller assembles.

    Separated from the catalogue itself so that "which roots" is a decision made once, by the
    layer that knows whether it is serving a person or embedded in a program, rather than
    re-derived at each lookup from configuration nobody passed in.
    """

    agents: tuple[Path, ...] = ()
    skills: tuple[Path, ...] = ()
    memories: tuple[Path, ...] = ()
    prompts: Optional[Path] = None
    # Where to start walking for AGENTS.md / CLAUDE.md / CONTEXT.md.
    project_directory: Optional[Path] = None
    # Whether to also read the well-known user-wide instruction files. False for a library,
    # because a program that imported us did not ask to inherit another tool's configuration.
    include_home_instructions: bool = False


def _as_paths(directories: Iterable[str | Path] | str | Path | None) -> tuple[Path, ...]:
    if directories is None:
        return ()
    if isinstance(directories, (str, Path)):
        return (Path(directories).expanduser(),)
    return tuple(Path(directory).expanduser() for directory in directories)


class FileCatalogue:
    """The `.agents` trees on disk: what Frank has always read, now behind the interface."""

    def __init__(self, roots: CatalogueRoots) -> None:
        self._roots = roots

    # Agents

    def agent(self, name: str) -> Any:
        from frank.base.configuration import load_agent_configuration

        if not self._roots.agents:
            return None
        try:
            return load_agent_configuration(name, list(self._roots.agents))
        except FileNotFoundError:
            return None

    def agents(self) -> Sequence[str]:
        from frank.base.configuration import list_agent_route_names

        if not self._roots.agents:
            return []
        return list_agent_route_names(list(self._roots.agents))

    # Skills and memories, re-read each call so an edit takes effect without a restart. That
    # liveness is a property of *this* implementation, not of the port: a catalogue built from
    # a dictionary is static, and neither is more correct than the other.

    def skills(self) -> Sequence[Any]:
        from frank.base.skills import load_skills

        if not self._roots.skills:
            return []
        return load_skills(list(self._roots.skills))

    def memories(self) -> Sequence[Any]:
        from frank.base.memories import load_memories

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
                # A home-wide document governs everything, because that is where it sits — and
                # that is also why it loses every disagreement with a project's own.
                entries.append(Instruction(
                    source=str(resolved), scope=str(resolved.parent),
                    content=resolved.read_text(errors="ignore").strip(),
                ))

        project = self._project_instruction()
        if project is not None and project not in seen:
            entries.append(Instruction(
                source=str(project), scope=str(project.parent),
                content=project.read_text(errors="ignore").strip(),
            ))

        return entries

    def _project_instruction(self) -> Optional[Path]:
        """The nearest instruction file at or above the project directory.

        Stops at the home directory so a search starting outside the home tree does not walk to
        the filesystem root, and takes the first ancestor that has one rather than stacking
        every ancestor's."""
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
        from frank.base.configuration import PromptLoader

        directory = self._roots.prompts
        if directory is None:
            return ""
        return PromptLoader(directory).load(name, dict(variables))


class Catalogue:
    """Everything supplied in code, nothing read from disk.

    For a program that builds its own agent, ships its own skills, or wants a session whose
    prompt it fully controls — and for a test that would otherwise depend on whatever happens
    to be in the developer's home directory.

    Not a dataclass, deliberately: the protocol answers `agents()`, `skills()`, `memories()`
    and `instructions()` as methods, and a caller writes those same words as keywords. Taking
    them in the constructor and holding them privately lets both be the plain word, rather
    than one of them carrying a suffix to get out of the other's way.

    Prompt templates fall back to the packaged ones unless `prompts` overrides them by name,
    because replacing the system prompt is a thing to opt into rather than a thing to be
    required to reproduce.
    """

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
        from frank.base.configuration import PromptLoader

        template = self._prompts.get(name)
        if template is not None:
            return PromptLoader.render(template, dict(variables), name)
        directory = self._fallback_prompts or packaged_prompts_directory()
        return PromptLoader(directory).load(name, dict(variables))


def packaged_prompts_directory() -> Path:
    """Where the harness's own prompt templates live."""
    return Path(__file__).resolve().parent.parent / "runtime" / "prompts"


def machine_catalogue(configuration: Any, working_directory: str = "") -> FileCatalogue:
    """The catalogue a person's machine has: every root, including the home ones.

    What `frankd` and the CLI use. Reading `~/.agents` and the well-known instruction files is
    the *feature* here — the person running the daemon is the person those files describe.
    """
    return FileCatalogue(CatalogueRoots(
        agents=_as_paths(configuration.agent_directories_for(working_directory)),
        skills=_as_paths(configuration.skill_directories_for(working_directory)),
        memories=_as_paths(configuration.memory_directories_for(working_directory)),
        prompts=packaged_prompts_directory(),
        project_directory=Path(working_directory).expanduser() if working_directory else None,
        include_home_instructions=True,
    ))


def project_catalogue(configuration: Any, working_directory: str) -> FileCatalogue:
    """The catalogue an embedded harness gets: the working directory, and nothing of $HOME.

    The bundled agents and skills are still there — they ship with the package and are what
    makes a default agent exist at all — but `~/.agents`, `~/.claude/CLAUDE.md` and
    `~/.config/opencode/AGENTS.md` are not. A program that imported Frank did not ask to
    inherit the machine's opinions, or another product's.
    """
    from frank.base.configuration import BUNDLED_DOTAGENTS_ROOT

    local = Path(working_directory).expanduser()
    return FileCatalogue(CatalogueRoots(
        agents=(BUNDLED_DOTAGENTS_ROOT / "agents", local / ".agents" / "agents", local / "agents"),
        skills=(BUNDLED_DOTAGENTS_ROOT / "skills", local / ".agents" / "skills", local / "skills"),
        memories=(local / ".agents" / "memories",),
        prompts=packaged_prompts_directory(),
        project_directory=local,
        include_home_instructions=False,
    ))


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
