"""`xeac`: the command.

The verbs mirror the API exactly — create a session, send it a message, read it, watch it,
list what exists, kill a tree. The CLI adds no capability of its own; it is the ergonomic
face of the same surface the desktop client and agents use, which is why an agent spawning a
peer runs the same command a person would.

`create` is the only place a session's configuration is set. `send` only does work. That
split is the permission model made visible: there is no verb that loosens a running session,
because there is no such operation.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from xeac.cli import render
from xeac.cli.client import DaemonError, call, daemon_is_up, ensure_daemon, stream


def _print(payload: Any, as_json: bool, renderer=None) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    elif renderer is not None:
        renderer(payload)
    else:
        print(payload)


def _command_create(arguments: argparse.Namespace) -> int:
    result = call(
        "session.create",
        agent=arguments.agent or "",
        working_directory=arguments.directory or "",
        permission_mode=arguments.mode or "",
        project_id=arguments.project or "",
        parent=arguments.parent or "",
        title=arguments.title or "",
    )
    if arguments.json:
        print(json.dumps(result, indent=2))
    else:
        # The bare id on stdout is what makes `id=$(xeac create ...)` work, which is how an
        # agent spawns a peer from a shell.
        print(result["id"])
    return 0


def _command_send(arguments: argparse.Namespace) -> int:
    text = arguments.message
    if text == "-":
        text = sys.stdin.read()
    result = call("session.send", id=arguments.session, parts=[{"kind": "text", "text": text}])
    if arguments.wait:
        return _follow(arguments.session, arguments.json, until_idle=True)
    _print(result, arguments.json, lambda payload: print(payload.get("task_id", "")))
    return 0


def _command_get(arguments: argparse.Namespace) -> int:
    result = call("session.get", id=arguments.session)
    _print(result["session"], arguments.json, render.session_detail)
    return 0


def _command_wait(arguments: argparse.Namespace) -> int:
    return _follow(arguments.session, arguments.json, until_idle=True, quiet=True)


def _command_attach(arguments: argparse.Namespace) -> int:
    return _follow(arguments.session, arguments.json, until_idle=False)


def _follow(session_id: str, as_json: bool, *, until_idle: bool, quiet: bool = False) -> int:
    """Watch a session's stream, optionally stopping once it goes idle.

    `attach` follows until interrupted; `wait` follows until the session stops working and
    then prints what it produced. Both read the same stream, so waiting is not polling."""
    try:
        for frame in stream(f"/sessions/{session_id}/attach"):
            if as_json:
                print(json.dumps(frame))
            elif not quiet:
                render.stream_frame(frame)
            if until_idle and frame.get("kind") == "done":
                break
    except KeyboardInterrupt:
        return 130
    if until_idle:
        result = call("session.history", id=session_id, limit=1)
        if quiet and not as_json:
            render.last_result(result.get("tasks") or [])
    return 0


def _command_ps(arguments: argparse.Namespace) -> int:
    result = call("session.list", all=arguments.all)
    _print(result["sessions"], arguments.json, render.session_table)
    return 0


def _command_tree(arguments: argparse.Namespace) -> int:
    result = call("session.tree", id=arguments.session)
    _print(result, arguments.json, render.session_tree)
    return 0


def _command_approve(arguments: argparse.Namespace) -> int:
    decision = "deny" if arguments.deny else "allow_once"
    result = call("session.respond", id=arguments.session, request_id=arguments.request, decision=decision)
    _print(result, arguments.json, lambda payload: print("resolved" if payload.get("resolved") else "no matching request"))
    return 0


def _command_kill(arguments: argparse.Namespace) -> int:
    result = call("session.kill", id=arguments.session)
    _print(result, arguments.json, lambda payload: print(f"killed {payload['killed']} ({payload['reaped']} reaped)"))
    return 0


def _command_history(arguments: argparse.Namespace) -> int:
    result = call("session.history", id=arguments.session, limit=arguments.limit or 0)
    _print(result["tasks"], arguments.json, render.history)
    return 0


def _command_configure(arguments: argparse.Namespace) -> int:
    from xeac.cli.commands import configure

    if arguments.unset:
        if not arguments.setting:
            print("xeac: --unset needs a setting to remove", file=sys.stderr)
            return 1
        return configure.run_unset(arguments)
    return configure.run(arguments)


def _command_daemon(arguments: argparse.Namespace) -> int:
    if arguments.action == "status":
        # Reporting must not start anything: `status` is what a person runs to find out, and a
        # status check that silently launches a service is a status check that can never
        # report the absence it was asked about. `--start` opts into the other behaviour.
        if not daemon_is_up() and not arguments.start:
            print("xeacd is not running (start it with `xeac daemon start`)")
            return 1
        _print(call("daemon.status"), arguments.json, render.daemon_status)
        return 0
    if arguments.action == "start":
        ensure_daemon()
        print("xeacd is running")
        return 0
    if arguments.action == "stop":
        # A signal rather than an API call: a daemon wedged badly enough to need stopping may
        # not be answering its own socket, and this must work then too. The group is signalled
        # so the sessions go with it — a worker whose daemon is gone cannot persist anything.
        import os
        import signal

        from xeac.base.paths import runtime_directory

        pidfile = runtime_directory() / "xeacd.pid"
        try:
            pid = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            print("xeacd does not appear to be running")
            return 1
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            print("xeacd does not appear to be running")
            return 1
        except PermissionError:
            print("xeac: not permitted to stop that process", file=sys.stderr)
            return 1
        print("stopping xeacd; its sessions are reaped with it")
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xeac", description="Drive XEAC sessions.")
    parser.add_argument("-j", "--json", action="store_true", help="emit raw JSON instead of formatted output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        """A subcommand that also accepts `--json` after the verb.

        `xeac ps --json` is what a person types; argparse would otherwise only accept the flag
        before the verb, and reject the natural form with an unhelpful error."""
        subparser = subparsers.add_parser(name, help=help_text)
        subparser.add_argument("-j", "--json", action="store_true", default=argparse.SUPPRESS,
                               help="emit raw JSON instead of formatted output")
        return subparser

    create = add("create", "create a session (the only place its configuration is set)")
    create.add_argument("-a", "--agent", help="agent profile to run")
    create.add_argument("-C", "--directory", help="working directory")
    create.add_argument("-m", "--mode", choices=["default", "auto", "read_only"],
                        help="permission mode, fixed for the session's life")
    create.add_argument("-p", "--project", help="project identifier")
    create.add_argument("-P", "--parent", help="parent session; the child is clamped to no looser a mode")
    create.add_argument("-t", "--title", help="a human label for the session list")
    create.set_defaults(handler=_command_create)

    send = add("send", "send a message to a session")
    send.add_argument("session")
    send.add_argument("message", help="the message, or - to read stdin")
    send.add_argument("-w", "--wait", action="store_true", help="follow until the session goes idle")
    send.set_defaults(handler=_command_send)

    get = add("get", "show a session")
    get.add_argument("session")
    get.set_defaults(handler=_command_get)

    wait = add("wait", "wait for a session to go idle, then print its result")
    wait.add_argument("session")
    wait.set_defaults(handler=_command_wait)

    attach = add("attach", "follow a session live")
    attach.add_argument("session")
    attach.set_defaults(handler=_command_attach)

    ps = add("ps", "list sessions")
    ps.add_argument("-a", "--all", action="store_true", help="include sessions that have ended")
    ps.set_defaults(handler=_command_ps)

    tree = add("tree", "show a session and everything it spawned")
    tree.add_argument("session")
    tree.set_defaults(handler=_command_tree)

    approve = add("approve", "answer a session's pending permission request")
    approve.add_argument("session")
    approve.add_argument("request")
    approve.add_argument("-d", "--deny", action="store_true", help="deny instead of allowing")
    approve.set_defaults(handler=_command_approve)

    kill = add("kill", "end a session and everything under it")
    kill.add_argument("session")
    kill.set_defaults(handler=_command_kill)

    history = add("history", "print a session's turns")
    history.add_argument("session")
    history.add_argument("-n", "--limit", type=int, help="only the last N turns")
    history.set_defaults(handler=_command_history)

    configure = add("configure", "read or change what new sessions and daemons start with")
    configure.add_argument("setting", nargs="?", help="dotted path, e.g. agent.permission_mode")
    configure.add_argument("value", nargs="?", help="the new value; omit to read it")
    configure.add_argument("-u", "--unset", action="store_true", help="remove the setting instead")
    configure.set_defaults(handler=_command_configure)

    daemon = add("daemon", "inspect or start the daemon")
    daemon.add_argument("action", choices=["status", "start", "stop"], nargs="?", default="status")
    daemon.add_argument("-s", "--start", action="store_true", help="start the daemon if it is not running")
    daemon.set_defaults(handler=_command_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    # SUPPRESS leaves the attribute unset unless the flag appeared after the verb, so the
    # top-level value stands in when it did not.
    if not hasattr(arguments, "json"):
        arguments.json = False
    try:
        return arguments.handler(arguments)
    except DaemonError as error:
        print(f"xeac: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
