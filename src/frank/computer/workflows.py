"""Workflows somebody saved: what exists, where it lives, and how to call it.

A workflow is an ordinary Python module that takes a :class:`frank.screen.Screen` and does
something with it. Two directories hold them, following the layering skills and memories already
use — a project's own beside the project, a person's own beside the person:

* ``.agents/workflows/`` in the project — about *this* codebase's application. Versioned with it,
  shared with whoever else works on it.
* ``~/.agents/workflows/`` — about the person's own tools. Available in every project and
  committed to none, which is not a filing preference: a workflow that drives somebody's mail
  carries their account names and habits, and has no business in a shared repository.

Both are on the import path as one ``workflows`` namespace package, so Python merges them and a
script writes ``from workflows.invoice import run`` without caring which directory answered. Where
the same module name exists in both, the project's wins — the same precedence skills use — and the
listing says so rather than letting the personal one vanish quietly.

**Discovered by reading, never by importing.** A listing runs on every turn, and importing user
code to find out what it is would execute it every turn — arbitrary code, for a question about
names. So each file is parsed to an AST and read: the functions it defines, their signatures, and
the first line of their docstrings. Nothing here runs anything.
"""

from __future__ import annotations

import ast
import logging
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The package name both directories contribute to. One namespace, so a script never has to know
#: which of the two a workflow came from in order to import it.
PACKAGE = "workflows"

PROJECT_ROOT = ".agents"
PERSONAL_ROOT = "~/.agents"

#: Where skills live under either of those, and therefore where their ``scripts/`` packages do.
SKILLS_ROOT = "skills"


def import_roots(project_directory: str = "") -> list[str]:
    """The directories to put on a script's import path, in precedence order.

    The project first, so a project workflow shadows a personal one of the same name — the layering
    skills already use, and the one a person would expect.

    Skills contribute too, and that is the point of the second half. A skill is instructions plus,
    very often, a ``scripts/`` directory holding a real Python package with its own
    ``pyproject.toml`` — which is how capability is packaged for an agent generally, not something
    invented here. Putting those directories on the path is what lets a skill ship screen work:
    the package is written, tested and versioned like any other, and a script reaches it with an
    ordinary import instead of having the whole of it pasted in as text.
    """
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
    """The ``site-packages`` of every skill that has its own environment.

    A skill's ``scripts/`` is a real project with a ``pyproject.toml`` and, very often, real
    dependencies that ``uv`` has installed into a ``.venv`` beside it. Putting the package
    directory on the path makes the package importable and its dependencies *not*, which fails in
    the least helpful way available: the import of the skill succeeds and something three levels
    down raises ``ModuleNotFoundError`` for a name the script never mentioned.

    Only when the interpreter versions match. A ``site-packages`` built for another Python holds
    extension modules this one cannot load, and a confident ``ImportError`` about a name that is
    plainly installed is worse than the honest absence.

    Kept apart from :func:`import_roots` because the two want opposite ends of ``sys.path``: a
    skill's own package must win, and a skill's *dependencies* must not — one pinning an old copy
    of something should never decide what the rest of this process imports.

    The packages are borrowed rather than activated — this process stays the session's
    interpreter — so anything an environment would normally set up for them has to be arranged
    here too. :func:`library_roots` is the other half: a wheel that ships its own shared library
    finds it through an rpath that assumes its own environment, and without that the import fails
    on a missing ``.dylib`` rather than on anything the script did.
    """
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    found: list[str] = []
    for scripts in _skill_script_roots(agent_roots(project_directory)):
        packages = Path(scripts) / ".venv" / "lib" / version / "site-packages"
        if packages.is_dir():
            found.append(str(packages.resolve()))
    return found


@lru_cache(maxsize=8)
def _libraries_under(root: str) -> tuple[str, ...]:
    """Directories inside one ``site-packages`` that hold a shared library of their own.

    Cached because it is a directory walk and the answer changes only when something is
    installed, which does not happen while a script is running.
    """
    base = Path(root)
    found: list[str] = []
    for package in sorted(base.iterdir()) if base.is_dir() else []:
        if not package.is_dir():
            continue
        # The package itself, and the directory `delocate` puts vendored libraries in when a
        # wheel is built for macOS.
        for candidate in (package, package / ".dylibs"):
            if not candidate.is_dir():
                continue
            # `lib*.so` as well as `*.dylib`: a wheel may ship either, and the `lib` prefix is
            # what tells a shared library from a Python extension module, which is also `.so`.
            if any(candidate.glob("*.dylib")) or any(candidate.glob("lib*.so")):
                found.append(str(candidate))
    return tuple(found)


def library_roots(project_directory: str = "") -> list[str]:
    """Where to look for shared libraries a skill's dependencies bring with them.

    A wheel that ships a compiled library links it as ``@rpath/libwhatever.dylib`` and records
    rpaths that assume the environment it was installed into. Borrowing the packages does not
    reproduce that, so the extension module loads and the library it needs does not — the import
    fails naming a ``.dylib`` the script never mentioned, which reads as a broken skill rather
    than a missing search path. ``PyMuPDF`` is the case in hand: its ``libmupdf.dylib`` sits
    beside the extension that wants it, and the recorded rpath points nine directories above
    ``site-packages`` at nothing.

    Handed to the child as ``DYLD_FALLBACK_LIBRARY_PATH`` rather than ``DYLD_LIBRARY_PATH``,
    which matters: the fallback is consulted only once normal resolution has failed, so a package
    shipping its own copy of a common library cannot displace the system's for everything else in
    the process.
    """
    return [directory for root in dependency_roots(project_directory)
            for directory in _libraries_under(root)]


def _skill_script_roots(agent_roots: list[Path]) -> list[str]:
    """Each skill's ``scripts/`` directory, in the same precedence order.

    Read off the filesystem rather than from the loaded skill list, because this runs where the
    directories are known and the skills are not — and a directory either exists or it does not,
    which is the whole of the question. A skill without scripts contributes nothing.
    """
    found: list[str] = []
    for root in agent_roots:
        skills = root / SKILLS_ROOT
        if not skills.is_dir():
            continue
        for scripts in sorted(skills.glob("*/scripts")):
            if scripts.is_dir():
                found.append(str(scripts.resolve()))
    return found


def _summarise(function: ast.FunctionDef | ast.AsyncFunctionDef, module: str, scope: str) -> Optional[dict[str, Any]]:
    """One callable, as the model reads it — or ``None`` when it is not a workflow.

    A workflow is recognised by its first parameter being ``screen``, which is the whole of the
    convention. A module is free to hold helpers beside its workflows; they are simply not listed,
    because a helper is not a thing anybody calls from a script."""
    parameters = [argument.arg for argument in function.args.args]
    if not parameters or parameters[0] != "screen" or function.name.startswith("_"):
        return None
    rendered = [ast.unparse(argument) for argument in function.args.args[1:]]
    for offset, default in enumerate(function.args.defaults[-len(rendered):] if rendered else []):
        rendered[len(rendered) - len(function.args.defaults or []) + offset] += f"={ast.unparse(default)}"
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
                # The project already defines this module name, so this one is unreachable. Said
                # aloud: a workflow that silently does not exist is worse than one that is missing.
                listed.append({"import": f"{PACKAGE}.{path.stem}", "scope": scope,
                               "error": "unreachable: the project defines this module name too"})
                continue
            seen.add(path.stem)
            try:
                tree = ast.parse(path.read_text())
            except (OSError, SyntaxError) as error:
                listed.append({"import": f"{PACKAGE}.{path.stem}", "scope": scope,
                               "error": f"could not be read: {error}"})
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    entry = _summarise(node, path.stem, scope)
                    if entry is not None:
                        listed.append(entry)
    return listed
