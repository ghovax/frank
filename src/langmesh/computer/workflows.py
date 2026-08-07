"""Workflows somebody saved: what exists, where it lives, and how to call it."""

from __future__ import annotations

import ast
import logging
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The package name both directories contribute to, so a script need not know which one a workflow came from.
PACKAGE = "workflows"

PROJECT_ROOT = ".agents"
PERSONAL_ROOT = "~/.agents"

#: Where skills live under either of those, and therefore where their ``scripts/`` packages do.
SKILLS_ROOT = "skills"


def import_roots(project_directory: str = "") -> list[str]:
    """The directories to put on a script's import path, project first so it shadows a personal one."""
    resolved = agent_roots(project_directory)
    return [str(root) for root in resolved] + _skill_script_roots(resolved)


def agent_roots(project_directory: str = "") -> list[Path]:
    """The ``.agents`` directories themselves, project first — what workflows are read from."""
    roots: list[Path] = []
    if project_directory:
        roots.append(Path(project_directory).expanduser() / PROJECT_ROOT)
    roots.append(Path(PERSONAL_ROOT).expanduser())
    return [root.resolve() for root in roots if root.is_dir()]


def dependency_roots(project_directory: str = "") -> list[str]:
    """The site-packages of every skill that has its own environment."""
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    found: list[str] = []
    for scripts in _skill_script_roots(agent_roots(project_directory)):
        packages = Path(scripts) / ".venv" / "lib" / version / "site-packages"
        if packages.is_dir():
            found.append(str(packages.resolve()))
    return found


@lru_cache(maxsize=8)
def _libraries_under(root: str) -> tuple[str, ...]:
    """Directories inside one site-packages that hold a shared library, cached because it is a directory walk."""
    base = Path(root)
    found: list[str] = []
    for package in sorted(base.iterdir()) if base.is_dir() else []:
        if not package.is_dir():
            continue
        # The package itself, and the directory a macOS wheel puts its vendored libraries in.
        for candidate in (package, package / ".dylibs"):
            if not candidate.is_dir():
                continue
            # `lib*.so` as well as `*.dylib`, since the prefix is what tells a shared library from an extension module.
            if any(candidate.glob("*.dylib")) or any(candidate.glob("lib*.so")):
                found.append(str(candidate))
    return tuple(found)


def library_roots(project_directory: str = "") -> list[str]:
    """Where to look for shared libraries a skill's dependencies bring with them."""
    return [
        directory
        for root in dependency_roots(project_directory)
        for directory in _libraries_under(root)
    ]


def _skill_script_roots(agent_roots: list[Path]) -> list[str]:
    """Each skill's `scripts/` directory, in the same precedence order, read off the filesystem."""
    found: list[str] = []
    for root in agent_roots:
        skills = root / SKILLS_ROOT
        if not skills.is_dir():
            continue
        for scripts in sorted(skills.glob("*/scripts")):
            if scripts.is_dir():
                found.append(str(scripts.resolve()))
    return found


def _summarise(
    function: ast.FunctionDef | ast.AsyncFunctionDef, module: str, scope: str
) -> Optional[dict[str, Any]]:
    """One callable as the model reads it, recognised as a workflow by its first parameter being `screen`."""
    parameters = [argument.arg for argument in function.args.args]
    if not parameters or parameters[0] != "screen" or function.name.startswith("_"):
        return None
    rendered = [ast.unparse(argument) for argument in function.args.args[1:]]
    for offset, default in enumerate(function.args.defaults[-len(rendered) :] if rendered else []):
        rendered[len(rendered) - len(function.args.defaults or []) + offset] += (
            f"={ast.unparse(default)}"
        )
    documentation = ast.get_docstring(function) or ""
    entry: dict[str, Any] = {
        "import": f"from {PACKAGE}.{module} import {function.name}",
        "call": f"{function.name}(screen{''.join(', ' + part for part in rendered)})",
        "scope": scope,
    }
    if documentation:
        entry["does"] = documentation.strip().splitlines()[0]
    return entry


def available(project_directory: str = "") -> list[dict[str, Any]]:
    """Every workflow a script could import, read off the files without running any of them."""
    listed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in agent_roots(project_directory):
        directory = root / PACKAGE
        if not directory.is_dir():
            continue
        scope = "project" if root.name == PROJECT_ROOT and project_directory else "personal"
        for path in sorted(directory.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            if path.stem in seen:
                # The project already defines this module name, so this one is unreachable, and it is said aloud.
                listed.append(
                    {
                        "import": f"{PACKAGE}.{path.stem}",
                        "scope": scope,
                        "error": "unreachable: the project defines this module name too",
                    }
                )
                continue
            seen.add(path.stem)
            try:
                tree = ast.parse(path.read_text())
            except (OSError, SyntaxError) as error:
                listed.append(
                    {
                        "import": f"{PACKAGE}.{path.stem}",
                        "scope": scope,
                        "error": f"could not be read: {error}",
                    }
                )
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    entry = _summarise(node, path.stem, scope)
                    if entry is not None:
                        listed.append(entry)
    return listed
