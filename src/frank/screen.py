"""The screen as an object a Python program can hold — so a workflow can be a file.

Until this existed, a ``control_screen`` script was not a program. Its primitives were synthesized
into a bare namespace by the runner, so the text that drove a window was valid only inside one
``exec`` and nowhere else: saved to disk it read ``NameError: name 'wait_for' is not defined``. No
editor could check it, no test could exercise it, nothing could import it, and every workflow was
therefore re-derived from scratch every time somebody wanted it again — differently each time.

The missing piece was an import. With one, a workflow is an ordinary Python module::

    from frank.screen import Screen

    def open_topic(screen: Screen, topic: str) -> list[str]:
        field = screen.wait_for("Search help text field", clickable=True)
        screen.type(field, topic, submit=True)
        screen.wait_for(f"{topic} heading", seconds=5)
        return screen.read()

That file lints, type-checks, diffs in version control, and can be read by somebody who knows
nothing about this harness. An inline script says the same thing on its first line::

    from frank.screen import screen

and is thereafter the same program, which is the point: there is one way to reach the screen and
it is written down in the file that uses it. The runner used to put a bound ``screen`` into the
namespace instead, which read pleasantly and meant a script declared nothing it depended on —
so the inline form and the saved form were two different languages, and only one of them could
be moved to the other.

**Importing this module is not authority.** ``screen`` and :func:`place` are ordinary objects
anywhere; what makes them act is a bridge the ``control_screen`` runner installs in the process
it controls. A program that imports this and runs some other way — from a shell, from a test —
gets :class:`NotDriving` on every call. That is deliberate, and it is what keeps driving
somebody's signed-in windows behind the one tool that asks their permission first.

**The vocabulary is not declared here.** Which primitives exist depends on what the target is — a
window and a page answer different sets — and on the session's permission mode, since a read-only
session is never offered the acting ones. The authority is
:meth:`frank.computer.surface.Surface.signatures`, computed from the surfaces themselves and
delivered in the model's context each turn. Restating it here would be a second statement of the
same fact, and the two would drift; so attribute access forwards whatever it is given and an
unknown name fails against the live surface, which is the only thing that knows.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional


def _message(name: str, **variables: str) -> str:
    """One of this module's messages, from ``messages/<name>.md``.

    Read here rather than through :class:`frank.base.configuration.PromptLoader` because this
    module is imported by the ``control_screen`` child on every screen call, and that child is
    kept thin on purpose — the loader would bring pydantic and yaml with it for the sake of one
    file with no frontmatter. Prose belongs in a file either way: a paragraph wrapped across four
    string literals is a paragraph nobody edits."""
    try:
        text = (Path(__file__).parent / "messages" / f"{name}.md").read_text().strip()
    except OSError:
        return ""
    for key, value in variables.items():
        text = text.replace("{{ " + key + " }}", value)
    return text

#: How a call reaches the live surface. The runner installs this before a script runs; a program
#: importing this module outside a session finds it unset, and says so rather than doing nothing.
_bridge: Optional[Callable[[str, list, dict], Any]] = None


def install_bridge(bridge: Callable[[str, list, dict], Any]) -> None:
    """Point this module at a live surface. Called by the runner, not by a script."""
    global _bridge
    _bridge = bridge


class NotDriving(RuntimeError):
    """Raised when a screen call is made outside a session that can perform it."""


class Screen:
    """One place — a window or a browser tab — and everything it can be told to do.

    Methods are not enumerated: :meth:`__getattr__` forwards any name to the surface, which
    answers with what it has. Reaching for something a place does not implement fails there,
    naming what it does have, rather than failing here against a list that has gone stale.
    """

    def __init__(self, target: str = "") -> None:
        self.target = target

    def __repr__(self) -> str:
        return f"Screen({self.target!r})" if self.target else "Screen()"

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*arguments: Any, **keywords: Any) -> Any:
            if _bridge is None:
                raise NotDriving(_message("not_driving", primitive=name))
            return _bridge(name, list(arguments), keywords)

        call.__name__ = name
        return call


def place(target: str = "") -> Screen:
    """The place this script is driving.

    Here because a script should say where its capabilities come from. A ``control_screen``
    script used to open with a ``screen`` that was never declared anywhere — a name the runner
    put into the namespace — so the text was not a program: nothing in it said what ``screen``
    was, no reader could follow it, and pasted into a file it read as a ``NameError``. Now the
    first line is an import, like any other program's.

    ``target`` is accepted and ignored. Which place a script drives is settled by the tool
    argument before the script starts, for the whole of it, and cannot be changed from inside —
    so this takes the parameter a reader would expect to be there and does not pretend it is a
    choice being made here.
    """
    return Screen(target)


#: The place, for ``from frank.screen import screen``. Bound to whatever the tool call named,
#: because the bridge that carries every call was installed pointing at it before this module
#: was reached. Outside a session it is an ordinary object whose every method raises
#: :class:`NotDriving`, which is what keeps screen control reachable only through the tool that
#: asks permission for it.
screen = Screen()
