"""The screen as an object a Python program can hold — so a workflow can be a file.

Until this existed, a ``control_screen`` script was not a program. Its primitives were synthesized
into a bare namespace by the runner, so the text that drove a window was valid only inside one
``exec`` and nowhere else: saved to disk it read ``NameError: name 'wait_for' is not defined``. No
editor could check it, no test could exercise it, nothing could import it, and every workflow was
therefore re-derived from scratch every time somebody wanted it again — differently each time.

The missing piece was an import. With one, a workflow is an ordinary Python module:

    from frank.screen import Screen

    def open_topic(screen: Screen, topic: str) -> list[str]:
        field = screen.wait_for("Search help text field", clickable=True)
        screen.type(field, topic, submit=True)
        screen.wait_for(f"{topic} heading", seconds=5)
        return screen.read()

That file lints, type-checks, diffs in version control, and can be read by somebody who knows
nothing about this harness. Inside a ``control_screen`` script a bound ``screen`` is already in
scope, so calling it costs an import and a line.

**The vocabulary is not declared here.** Which primitives exist depends on what the target is — a
window and a page answer different sets — and on the session's permission mode, since a read-only
session is never offered the acting ones. The authority is
:meth:`frank.computer.surface.Surface.signatures`, computed from the surfaces themselves and
delivered in the model's context each turn. Restating it here would be a second statement of the
same fact, and the two would drift; so attribute access forwards whatever it is given and an
unknown name fails against the live surface, which is the only thing that knows.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

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
                raise NotDriving(
                    f"screen.{name}() needs a live session. Inside a control_screen script one is "
                    f"already bound as `screen`; on its own, open one with `frank.screen.place()`."
                )
            return _bridge(name, list(arguments), keywords)

        call.__name__ = name
        return call
