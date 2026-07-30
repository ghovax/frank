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
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The package name both directories contribute to. One namespace, so a script never has to know
#: which of the two a workflow came from in order to import it.
PACKAGE = "workflows"

PROJECT_ROOT = ".agents"
PERSONAL_ROOT = "~/.agents"


def import_roots(project_directory: str = "") -> list[str]:
    """The directories to put on a script's import path, in precedence order.

    The project first, so a project workflow shadows a personal one of the same name — the layering
    skills already use, and the one a person would expect."""
    roots: list[Path] = []
    if project_directory:
        roots.append(Path(project_directory).expanduser() / PROJECT_ROOT)
    roots.append(Path(PERSONAL_ROOT).expanduser())
    return [str(root.resolve()) for root in roots if root.is_dir()]


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
    for root in import_roots(project_directory):
        directory = Path(root) / PACKAGE
        if not directory.is_dir():
            continue
        scope = "project" if Path(root).name == PROJECT_ROOT and project_directory else "personal"
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
