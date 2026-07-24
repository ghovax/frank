"""Turning API payloads into something readable in a terminal.

Every command also has a ``--json`` mode, so this is free to be human-first: aligned columns,
a tree that shows what spawned what, and a live stream that reads as prose rather than as
event frames. Nothing here is a data format — anything scripted should be reading the JSON.
"""

from __future__ import annotations

import sys
from typing import Any

# Colour is applied only when stdout is a terminal, so piping produces clean text.
_TTY = sys.stdout.isatty()

_STATUS_COLOURS = {
    "working": "\033[32m",   # green
    "running": "\033[32m",   # green
    "starting": "\033[33m",  # yellow
    "idle": "\033[36m",      # cyan
    "failed": "\033[31m",    # red
    "exited": "\033[90m",    # grey
}
_RESET = "\033[0m"
_DIM = "\033[90m"
_BOLD = "\033[1m"


def _colour(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}" if _TTY and code else text


def _status(record: dict) -> str:
    """What a session is doing, in the order a person needs to know it.

    A session parked on a permission request is not "running", it is waiting for them — that
    goes first. After that, "running" describes the process, which stays up between messages,
    so a live session is reported as working or idle depending on whether a turn is actually
    in flight. Nothing else could tell those two apart from the outside."""
    if record.get("awaiting_input"):
        return _colour("waiting", "\033[35m")
    status = str(record.get("status", ""))
    if status == "running":
        status = "working" if record.get("busy") else "idle"
    return _colour(status, _STATUS_COLOURS.get(status, ""))


def _short(value: Any, width: int) -> str:
    text = str(value or "")
    return text if len(text) <= width else text[: width - 1] + "…"


def session_table(sessions: list[dict]) -> None:
    if not sessions:
        print("no sessions")
        return
    rows = [
        (
            _short(record.get("id"), 22),
            _short(record.get("agent"), 14),
            _status(record),
            _short(record.get("title") or record.get("working_directory"), 44),
        )
        for record in sessions
    ]
    header = ("SESSION", "AGENT", "STATE", "TITLE")
    # Widths are computed off the uncoloured text, so escape codes do not skew the columns.
    widths = [max(len(header[index]), *(len(_strip(row[index])) for row in rows)) for index in range(4)]
    print("  ".join(_colour(header[index].ljust(widths[index]), _DIM) for index in range(4)))
    for row in rows:
        print("  ".join(_pad(row[index], widths[index]) for index in range(4)))


def _strip(text: str) -> str:
    out, skipping = [], False
    for character in text:
        if character == "\033":
            skipping = True
        elif skipping and character == "m":
            skipping = False
        elif not skipping:
            out.append(character)
    return "".join(out)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - len(_strip(text)))


def session_detail(record: dict) -> None:
    print(f"{_colour(record.get('id', ''), _BOLD)}  {_status(record)}")
    for label, key in (
        ("agent", "agent"),
        ("parent", "parent"),
        ("mode", "permission_mode"),
        ("directory", "working_directory"),
        ("pid", "pid"),
        ("created", "created_at"),
        ("ended because", "exit_reason"),
    ):
        value = record.get(key)
        if value:
            print(f"  {_colour(label.ljust(14), _DIM)}{value}")


def session_tree(payload: dict) -> None:
    """The session and what it spawned.

    Worth showing as a tree rather than a list because spawning is now how agents compose: a
    flat listing loses the one relationship that explains why the other sessions exist."""
    root = payload.get("session") or {}
    descendants = payload.get("descendants") or []
    children: dict[str, list[dict]] = {}
    for record in descendants:
        children.setdefault(str(record.get("parent") or ""), []).append(record)

    def walk(record: dict, prefix: str, last: bool, top: bool) -> None:
        connector = "" if top else ("└─ " if last else "├─ ")
        label = f"{record.get('id', '')}  {_colour(str(record.get('agent', '')), _DIM)}  {_status(record)}"
        print(f"{prefix}{connector}{label}")
        kids = children.get(str(record.get("id")), [])
        extension = "" if top else ("   " if last else "│  ")
        for index, child in enumerate(kids):
            walk(child, prefix + extension, index == len(kids) - 1, False)

    walk(root, "", True, True)


def daemon_status(payload: dict) -> None:
    sessions = payload.get("sessions", {})
    pool = payload.get("pool", {})
    print(f"{_colour('xeacd', _BOLD)}  {_colour('running', _STATUS_COLOURS['running'])}")
    print(f"  {_colour('sessions'.ljust(12), _DIM)}{sessions.get('live', 0)} live, {sessions.get('total', 0)} total")
    print(f"  {_colour('workers'.ljust(12), _DIM)}{pool.get('warm', 0)} warm, {pool.get('assigned', 0)} assigned")
    print(f"  {_colour('socket'.ljust(12), _DIM)}{payload.get('socket', '')}")
    print(f"  {_colour('port'.ljust(12), _DIM)}{payload.get('port', '')}")


def daemon_endpoint(payload: dict) -> None:
    """Where this daemon listens and the token that opens it — the pair a desktop client
    needs to attach to it across an SSH tunnel."""
    print(f"  {_colour('port'.ljust(12), _DIM)}{payload.get('port', '')}")
    print(f"  {_colour('token'.ljust(12), _DIM)}{payload.get('token', '')}")


def remote_agents(agents: list[dict]) -> None:
    """Peers on other hosts. Health first: a registered peer that cannot be resolved is the
    thing worth noticing, since it looks identical to a working one until you send to it."""
    if not agents:
        print("no remote agents registered")
        return
    width = max(len(str(agent.get("name", ""))) for agent in agents)
    for agent in agents:
        health = str(agent.get("health", ""))
        colour = _STATUS_COLOURS["running"] if health == "healthy" else _STATUS_COLOURS["failed"]
        detail = agent.get("description") or agent.get("card_url") or ""
        print(f"{str(agent.get('name', '')).ljust(width)}  {_colour(health, colour)}  {_short(detail, 52)}")
        if agent.get("error"):
            print(f"{' ' * width}  {_colour(str(agent['error']), _DIM)}")


def stream_frame(frame: dict) -> None:
    """One frame of a live attach, rendered as it arrives.

    Only the parts a person reading along cares about are printed: prose, what the session is
    doing, and anything that needs them. Token counts and bookkeeping stay in ``--json``."""
    kind = frame.get("kind")
    if kind == "snapshot":
        print(_colour(f"— {len(frame.get('tasks') or [])} earlier turn(s) —", _DIM))
        return
    if kind == "done":
        print(_colour("— session ended —", _DIM))
        return
    part = (frame.get("part") or {}).get("data") or {}
    event = part.get("kind")
    if event == "text":
        sys.stdout.write(part.get("text", ""))
        sys.stdout.flush()
    elif event == "tool_call":
        print(_colour(f"\n· {part.get('tool_name', 'tool')}", _DIM))
    elif event == "permission_request":
        print(
            _colour("\n! needs approval: ", "\033[35m")
            + f"{part.get('command') or part.get('justification') or ''}\n"
            + _colour(f"  xeac approve <session> {part.get('request_id', '')}", _DIM)
        )
    elif event == "question":
        print(_colour(f"\n? {part.get('questions')}", "\033[35m"))
    elif event == "error":
        print(_colour(f"\n✗ {part.get('message', 'error')}", _STATUS_COLOURS["failed"]))


def history(tasks: list[dict]) -> None:
    for task in tasks:
        status = (task.get("status") or {}).get("state", "")
        print(f"{_colour(task.get('id', ''), _BOLD)}  {status}")
        for artifact in task.get("artifacts") or []:
            for part in artifact.get("parts") or []:
                text = (part.get("text") or "").strip()
                if text:
                    print(f"  {text}")


def last_result(tasks: list[dict]) -> None:
    """What a session produced, for `wait` — the deliverable, not the transcript."""
    for task in reversed(tasks):
        for artifact in task.get("artifacts") or []:
            for part in artifact.get("parts") or []:
                text = (part.get("text") or "").strip()
                if text:
                    print(text)
                    return
