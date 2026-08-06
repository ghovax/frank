"""The screen as an object a Python program can hold — so a workflow can be a file."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional


def _message(name: str, **variables: str) -> str:
    """One of this module's messages, from ``messages/<name>.md``."""
    try:
        text = (Path(__file__).parent / "messages" / f"{name}.md").read_text().strip()
    except OSError:
        return ""
    for key, value in variables.items():
        text = text.replace("{{ " + key + " }}", value)
    return text

#: How a call reaches the live surface.
_bridge: Optional[Callable[[str, list, dict], Any]] = None


def install_bridge(bridge: Callable[[str, list, dict], Any]) -> None:
    """Point this module at a live surface. Called by the runner, not by a script."""
    global _bridge
    _bridge = bridge


class NotDriving(RuntimeError):
    """Raised when a screen call is made outside a session that can perform it."""


class Screen:
    """One place — a window or a browser tab — and everything it can be told to do."""

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
    """The place this script is driving."""
    return Screen(target)


#: The place, for ``from frank.screen import screen``.
screen = Screen()
