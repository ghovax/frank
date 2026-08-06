"""`frank`: the command.

The verbs mirror the API exactly — create a session, send it a message, read it, watch it,
list what exists, kill a tree. The CLI adds no capability of its own; it is the ergonomic
face of the same surface the desktop client and sessions use. A session reaches that surface
through its own tools rather than through this command — a typed call carries the caller's
identity, which an argv string cannot — but it is the same API underneath.

`create` is the only place a session's configuration is set. `send` only does work. That
split is the permission model made visible: there is no verb that loosens a running session,
because there is no such operation.

**Everything on stdout is plumbing.** A read prints the API's payload as JSON; a stream
prints one JSON object per line; a verb whose answer *is* a single value prints that value
bare, so `id=$(frank create …)` works. There is no formatting layer, no colour, and no
alternate human mode to choose between — anything that wants a table can pipe to `jq`, and
anything that wants to parse this never has to guess which mode it is in. Diagnostics go to
stderr and outcomes go to the exit code, so neither can ever contaminate the data.

It is minified, and every JSON object is exactly one line. Pretty-printing exists for a reader
who does not have `jq`, and this output has no such reader: agents drive these verbs constantly
and pay for the indentation by the token. `jq .` puts it back for a person.
"""

from __future__ import annotations

import argparse
import logging
import contextlib
import sys
from typing import Any

from tenacity import Retrying, RetryError, retry_if_exception_type, stop_after_delay, wait_fixed

from frank.cli.client import DaemonError, call, daemon_is_up, ensure_daemon, stream
from frank.base.serialization import compact
from frank.base.tuning import Tunable, active_tuning


class _StillRunning(Exception):
    """A process being waited on has not exited yet. Raised so a retry keeps waiting, rather
    than being a return value a caller could forget to check."""


logger = logging.getLogger("frank")


def _emit(payload: Any) -> None:
    """One structured answer on stdout, on one line."""
    print(compact(payload))


def _emit_line(payload: Any) -> None:
    """One frame of a stream. Identical to `_emit` but flushed, so a reader consumes the
    stream as it arrives rather than in whatever chunks the buffer decides on — which is the
    whole point of watching a session live."""
    _emit(payload)
    sys.stdout.flush()


def _note(message: str) -> None:
    """A diagnostic. Never stdout — that carries data, and a reader must not have to filter
    prose out of it.

    Through the logger rather than around it, so that everything this program says goes out one
    way. `main` configures the handler to write the bare message to stderr: at a terminal a
    timestamp and a level in front of every sentence is noise, and the sentences here are
    addressed to a person, not to a log."""
    logger.info(message)


def _command_create(arguments: argparse.Namespace) -> int:
    result = call(
        "session.create",
        agent=arguments.agent,
        working_directory=arguments.directory or "",
        permission_mode=arguments.mode or "",
        workspace_id=arguments.workspace or "",
        # Run from inside a session's shell, this command creates a *child* of that session by
        # default. Without it a session that reached for the CLI created an orphan: outside the
        # tree, outside the reaper, and outside the permission clamp, which is skipped entirely
        # when there is no parent to clamp against.
        #
        # Read only when it is shaped like one of ours. The variable is a plain name that a
        # person may already have exported for their own purposes, and this is the one place a
        # value from the environment decides whose child a session is.
        parent=arguments.parent or _session_from_environment(),
        read_only=bool(getattr(arguments, "read_only", False)),
        title=arguments.title or "",
    )
    # The bare id, because the answer is one value: this is what makes `id=$(frank create …)`
    # work in a shell script. The token and socket are in `frank get`.
    print(result["id"])
    return 0


def _command_send(arguments: argparse.Namespace) -> int:
    text = sys.stdin.read() if arguments.message == "-" else arguments.message
    result = call("session.send", id=arguments.session, parts=[{"kind": "text", "text": text}])
    # A session parked on a decision takes nothing, and says so in the body rather than by
    # failing — the call succeeded, the message did not land. Reported here as what it is: the
    # message was not delivered, so this exits non-zero, and `--wait` does not follow a turn
    # that was never started. It used to fall straight through to `_follow` with an empty
    # `turn_id`, which then waited for the session to go idle — which it already was.
    if result.get("accepted") is False:
        waiting_on = str(result.get("waiting_on") or "a decision from the user")
        _note(f"frank: not sent — the session is waiting on {waiting_on}")
        return 1
    if arguments.wait:
        # Waiting on *this* turn, not merely on the session going quiet: a session can have a
        # compaction or an autonomous wake open alongside the message just sent, and returning
        # when one of those ends would hand back a turn the caller never asked about.
        return _follow(arguments.session, until_idle=True, frames=False,
                       turn_id=str(result.get("turn_id") or ""))
    _emit(result)
    return 0


def _command_get(arguments: argparse.Namespace) -> int:
    _emit(call("session.get", id=arguments.session)["session"])
    return 0


def _command_wait(arguments: argparse.Namespace) -> int:
    return _follow(arguments.session, until_idle=True, frames=False)


def _command_attach(arguments: argparse.Namespace) -> int:
    return _follow(arguments.session, until_idle=False, frames=True)


# A turn the session is still driving on its own. Anything else — completed, failed,
# canceled, or parked on a human with `input-required` — means it will not progress without
# something happening, which is exactly when a waiter should be handed back control.
_IN_FLIGHT = {"submitted", "working"}


def _still_working(turn: dict) -> bool:
    return str((turn.get("status") or {}).get("state") or "") in _IN_FLIGHT


def _follow(session_id: str, *, until_idle: bool, frames: bool, turn_id: str = "") -> int:
    """Watch a session's stream.

    `attach` prints every frame and follows until the session ends or you interrupt it.
    `wait` prints nothing while it waits and emits the session's last turn once it goes idle —
    both read the same stream, so waiting is not polling.

    The snapshot the stream opens with is what makes waiting race-free. It is sent *after* the
    subscription exists, so a turn that ends from that point on cannot be missed, and a turn
    that had already ended is visible in the snapshot itself. Checking the session's state
    separately could fall between the two and wait for an edge that had already gone by."""
    try:
        for frame in stream(f"/sessions/{session_id}/attach"):
            if frames:
                _emit_line(frame)
            if not until_idle:
                continue
            kind = frame.get("kind")
            if kind == "snapshot":
                turns = frame.get("turns") or []
                if turn_id and not any(turn.get("id") == turn_id for turn in turns):
                    # Our own turn has not been persisted yet. It is in flight by definition —
                    # the send was accepted — so keep waiting rather than reading its absence
                    # as an idle session and returning somebody else's last turn.
                    continue
                if not any(_still_working(turn) for turn in turns):
                    break
            elif kind == "turn" and not frame.get("running"):
                break
            elif kind == "done":
                break
    except KeyboardInterrupt:
        return 130
    if until_idle:
        result = call("session.history", id=session_id, limit=1)
        _emit(result.get("turns") or [])
    return 0


def _session_from_environment() -> str:
    """The session this command is running inside, according to the environment, or ``""``.

    Imports inside the function, like every other one in this module: the CLI's startup time is
    the time a person waits before anything appears, and this file keeps its imports where they
    are used for that reason."""
    import os

    from frank.base import environment_variables
    from frank.base.identifiers import is_id

    value = os.environ.get(environment_variables.SESSION_ID, "").strip()
    return value if is_id(value, "session") else ""


def _command_ps(arguments: argparse.Namespace) -> int:
    _emit(call("session.list", all=arguments.all)["sessions"])
    return 0


def _command_tree(arguments: argparse.Namespace) -> int:
    _emit(call("session.tree", id=arguments.session))
    return 0


def _command_allow(arguments: argparse.Namespace) -> int:
    _emit(call("session.respond", id=arguments.session, request_id=arguments.request, decision="allow_once"))
    return 0


def _command_deny(arguments: argparse.Namespace) -> int:
    _emit(call("session.respond", id=arguments.session, request_id=arguments.request, decision="deny"))
    return 0


def _command_kill(arguments: argparse.Namespace) -> int:
    _emit(call("session.end", id=arguments.session))
    return 0


def _command_history(arguments: argparse.Namespace) -> int:
    _emit(call("session.history", id=arguments.session, limit=arguments.limit or 0)["turns"])
    return 0


def _command_configure(arguments: argparse.Namespace) -> int:
    from frank.cli.commands import configure

    if arguments.all and (arguments.setting or arguments.unset):
        _note("frank: --all lists everything and takes no setting")
        return 1
    if arguments.unset:
        if not arguments.setting:
            _note("frank: --unset needs a setting to remove")
            return 1
        if arguments.value is not None:
            # Caught here rather than inside `run`, which never sees this: `--unset` is
            # dispatched before it. Left to itself the value was silently discarded and the
            # setting removed — the opposite of what `configure x true --unset` looks like.
            _note("frank: pass either a value or --unset, not both")
            return 1
        return configure.run_unset(arguments)
    return configure.run(arguments)


def _command_remote(arguments: argparse.Namespace) -> int:
    """Registered peers on other hosts: list them, or hand one a message.

    Deliberately not `send`. A remote agent runs on someone else's machine, at their cost,
    with no shared history and no access to this filesystem — a different bargain from a local
    peer, and one a caller should never be unsure it made."""
    if not arguments.name:
        _emit(call("remote.list")["agents"])
        return 0
    if not arguments.message:
        _note("frank: give a message to send, or no name to list")
        return 1
    text = sys.stdin.read() if arguments.message == "-" else arguments.message
    _emit(call("remote.send", name=arguments.name, text=text))
    return 0


def _command_daemon(arguments: argparse.Namespace) -> int:
    if arguments.action == "status":
        # Reporting must not start anything: `status` is what a person runs to find out, and a
        # status check that silently launches a service is a status check that can never
        # report the absence it was asked about. `--start` opts into the other behaviour.
        if not daemon_is_up() and not arguments.start:
            _note("frankd is not running (start it with `frank serve`)")
            return 1
        _emit(call("daemon.status"))
        return 0
    if arguments.action == "endpoint":
        # The two values a GUI needs to attach to *this* daemon: where it listens, and the
        # token that authorises talking to it. The port is ephemeral and the token is minted
        # per boot, so neither can be guessed — and over SSH there is no runtime directory
        # to read them from, which is what makes this worth a verb.
        from frank.base.paths import daemon_port_path, daemon_token_path

        try:
            port = daemon_port_path().read_text().strip()
            token = daemon_token_path().read_text().strip()
        except OSError:
            _note("frankd does not appear to be running")
            return 1
        _emit({"port": int(port), "token": token})
        return 0
    if arguments.action == "stop":
        # A signal rather than an API call: a daemon wedged badly enough to need stopping may
        # not be answering its own socket, and this must work then too. The group is signalled
        # so the sessions go with it — a worker whose daemon is gone cannot persist anything.
        import os
        import signal

        from frank.base.paths import runtime_directory

        pidfile = runtime_directory() / "frankd.pid"
        try:
            pid = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            _note("frankd does not appear to be running")
            return 1
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            _note("frankd does not appear to be running")
            return 1
        except PermissionError:
            _note("frank: not permitted to stop that process")
            return 1
        _emit({"stopping": pid})
        return 0
    if arguments.action == "restart":
        # Stop, wait for the socket to go, start again.
        #
        # This ends every live session, and says so rather than letting it be discovered:
        # workers are the daemon's children, and shutdown reaps them. It exists because macOS
        # caches the Accessibility trust check per process, so a daemon that was running before
        # the grant never sees it — and now that the desktop app no longer owns the daemon,
        # restarting the window is not a way to restart the harness.
        import os
        import signal

        from frank.base.paths import runtime_directory

        pidfile = runtime_directory() / "frankd.pid"
        try:
            pid = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            # Nothing to restart is not a failure when the intent is "be running afterwards".
            ensure_daemon()
            _emit({"restarted": False, "running": True})
            return 0
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), signal.SIGTERM)

        # Wait for the *process*, not for its socket. The socket file goes early in shutdown
        # while the daemon is still reaping sessions and still holding `frankd.lock` through an
        # open descriptor, and a successor started in that window dies on "Another frankd holds
        # the lock but never started serving" — leaving nothing running at all, which is the one
        # outcome a restart must not produce. A pid that no longer exists is the real signal.
        tuning = active_tuning()

        def check_exited() -> None:
            """Return once the process is gone; raise while it is still there, which is what the
            retry below retries on."""
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            raise _StillRunning

        try:
            for attempt in Retrying(
                retry=retry_if_exception_type(_StillRunning),
                wait=wait_fixed(tuning.duration(Tunable.daemon_probe_interval_seconds)),
                stop=stop_after_delay(tuning.duration(Tunable.daemon_startup_seconds)),
            ):
                with attempt:
                    check_exited()
        except RetryError:
            _note(f"frank: frankd ({pid}) did not exit; not starting a second one")
            return 1
        ensure_daemon()
        _emit({"restarted": True, "running": True})
        return 0
    return 1


# The desktop app's bundle identifier, which is how it is launched. Addressing it by identifier
# rather than by name means the application can be renamed or moved and this still finds it.
APPLICATION_BUNDLE_ID = "com.ghovax.frank"


def _command_serve(arguments: argparse.Namespace) -> int:
    """Make Frank available: a control plane, and the interface in front of it.

    One verb rather than two, because starting the daemon and serving the interface were never
    separately useful. Every other command starts a daemon on demand anyway, so a command whose
    only job was to start one early was a command for a problem nobody had — and having both
    `serve` and `web` meant two names for "make this reachable", which is exactly the kind of
    second vocabulary this surface exists to remove.

    It runs in the foreground, because it is a server. That is also what a supervisor wants, so
    the old `--foreground` flag has nothing left to mean.

    The interface **proxies** the daemon rather than pointing a browser at it: the capability
    token is attached here and never reaches the page, the daemon's ephemeral port stays this
    process's business, and everything is one origin, so there is no CORS to configure.

    Imported on use — it pulls in uvicorn and starlette, which every other verb has no reason
    to pay for.
    """
    from frank.cli.commands import serve

    return serve.run(arguments)


def _command_reach(arguments: argparse.Namespace) -> int:
    """Make Frank reachable from somewhere that is not this machine.

    `serve` is the same proxy for a browser on the same host, and stops at loopback because that
    is all a browser on the same host needs. This one is the case that surface cannot cover: a
    phone, on a network, tomorrow. What it adds is the two things that turn an address into an
    endpoint — authentication, so binding past loopback is not a hole, and a token that survives
    the reboot that gives the daemon a new one.
    """
    from frank.cli.commands import reach

    return reach.run(arguments)


def _command_run(arguments: argparse.Namespace) -> int:
    """One turn, in this process, with no daemon at all.

    The shortest possible use of the harness, and the first consumer of the library surface:
    everything below is `frank.Session`. That is the point of it being here — a library nothing
    uses is a library nobody can trust, and this is the same code path an embedder takes.

    No daemon means no session record, no address and no crash isolation. Reach for `create`
    and `send` when you want any of those; this is for a question with an answer.
    """
    import asyncio

    prompt = arguments.prompt
    if prompt == "-" or prompt is None:
        prompt = sys.stdin.read()
    prompt = (prompt or "").strip()
    if not prompt:
        _note("frank: nothing to run (pass a prompt, or - to read stdin)")
        return 1

    async def drive() -> int:
        from frank import Approval, Session

        class AllowEverything:
            """Answers every gate with yes. Only reachable through `--allow`, which is a
            deliberate act: unattended means nobody is watching what it agrees to."""

            async def decide(self, _gate):
                return Approval(allow=True, reason="--allow was passed")

        # The CLI is a program for a person on a machine, so it reads the machine — visibly,
        # here, rather than inside the library. `Session` takes the resolved profile.
        from pathlib import Path

        from frank.daemon.machine import load_agent, load_catalogue, load_configuration

        configuration = load_configuration(seed=False)
        directory = str(Path(arguments.directory).resolve())
        session = Session(
            load_agent(arguments.agent, directory, configuration=configuration),
            directory=directory,
            configuration=configuration,
            catalogue=load_catalogue(configuration, directory),
            permission_mode=arguments.permission_mode,
            approvals=AllowEverything() if arguments.allow else None,
        )
        try:
            from frank.runtime.turn_events import Done, Suspended, TextChunk

            answer = ""
            async for event in session.stream(prompt):
                if arguments.json:
                    _emit(event.to_dict() if hasattr(event, "to_dict") else {"event": type(event).__name__})
                    continue
                if isinstance(event, TextChunk):
                    sys.stdout.write(event.text)
                    sys.stdout.flush()
                elif isinstance(event, Suspended):
                    _note(
                        "\nfrank: this turn needs a decision and nothing is watching. "
                        "Re-run with --allow, or with a permission mode that does not gate it."
                    )
                    return 2
                elif isinstance(event, Done):
                    answer = event.text or answer
            if not arguments.json and not answer.endswith("\n"):
                sys.stdout.write("\n")
            return 0
        except Exception as error:  # noqa: BLE001 — a person gets a sentence, not a traceback
            # The common cases are a provider with no credential and a model the account
            # cannot serve, and neither is a bug to be reported with a stack. The detail is
            # kept because it is usually the whole answer.
            _note(f"\nfrank: the turn failed — {type(error).__name__}: {error}")
            return 1
        finally:
            await session.aclose()

    return asyncio.run(drive())


def _command_auth(arguments: argparse.Namespace) -> int:
    """Sign in to a provider that uses an account rather than an API key.

    Only ChatGPT works this way today. It existed only in the browser interface, which meant a
    headless install could not reach the one provider that needs no key — so this is a gap
    being closed rather than a second way to do something.
    """
    import asyncio

    from frank.base.credentials import ChatGPTAuthError, ChatGPTLoginFlow, clear_tokens, load_tokens

    if arguments.action == "status":
        tokens = load_tokens()
        if tokens is None:
            _emit({"signed_in": False})
            return 1
        _emit({"signed_in": True, "account_id": tokens.account_id, "expires_at": tokens.expires_at})
        return 0

    if arguments.action == "logout":
        clear_tokens()
        _emit({"signed_in": False})
        return 0

    async def login() -> int:
        flow = ChatGPTLoginFlow()
        try:
            await flow.start()
        except OSError as error:
            # Port 1455 is the redirect target OpenAI's consent screen sends the browser to,
            # so it cannot be chosen: another sign-in already holding it is the whole message.
            _note(f"frank: could not listen for the sign-in callback ({error}). "
                  "Another Frank or Codex sign-in may be in progress.")
            return 1
        _note("frank: open this in a browser to sign in:")
        print(flow.authorize_url)
        # Best effort. On a headless box there is nothing to open and the printed URL is the
        # whole point of this command, so a failure here is not a failure.
        with contextlib.suppress(Exception):
            import webbrowser

            webbrowser.open(flow.authorize_url)
        try:
            tokens = await flow.wait()
        except ChatGPTAuthError as error:
            _note(f"frank: sign-in failed ({error})")
            return 1
        _emit({"signed_in": True, "account_id": tokens.account_id})
        return 0

    return asyncio.run(login())


def _command_open(arguments: argparse.Namespace) -> int:
    """Bring the daemon up and launch the desktop app.

    The dependency runs this way — command line to window — and that is the whole reason this
    is comfortable. The app used to start the daemon, which meant carrying a frozen copy of the
    harness inside itself and owning its lifetime. Launching an application is not owning it:
    nothing is bundled, nothing is supervised, and if the app is not installed this says so and
    exits rather than half-working.

    The daemon comes up first because the app is useless without one, and starting it is
    something the command line does anyway. Unconditionally: `ensure_daemon` returns at once when
    one is already running and never starts a second, so the `--no-daemon` that used to be here
    could only ever turn a window that would have worked into one that opens onto nothing."""
    import shutil
    import subprocess

    ensure_daemon()
    launcher = shutil.which("open")
    if launcher is None:
        _note("frank: `open` is not available; the desktop app is macOS-only")
        return 1
    result = subprocess.run(
        [launcher, "-b", APPLICATION_BUNDLE_ID],
        capture_output=True,
        text=True,
        timeout=active_tuning().duration(Tunable.open_url_seconds),
    )
    if result.returncode != 0:
        # `open -b` resolves through LaunchServices, which knows about applications in the
        # standard locations. A freshly built Frank.app sitting in the Tauri target directory
        # has usually never been registered, so this failure most often means "built but not
        # installed" rather than "not built" — worth saying, because the two look identical
        # from here and the fix is one `ditto`.
        _note(
            f"frank: nothing on this system claims {APPLICATION_BUNDLE_ID}. If you have built "
            "Frank.app but not installed it, macOS will not find it by identifier — move it to "
            "/Applications first. See documentation/installation.md."
        )
        return 1
    _emit({"opened": APPLICATION_BUNDLE_ID, "daemon": True})
    return 0


def _local_timezone() -> str:
    """This machine's IANA zone, so a cron line means what a person meant by it.

    Read from where the system keeps it rather than from ``datetime``: an aware datetime carries
    a fixed *offset*, not a zone, so `astimezone().tzinfo` answers "+02:00" and has no name to
    give — which silently produced UTC for everybody, and a schedule an hour or two off.

    Defaulted rather than required because "nine every weekday" almost always means nine where
    the person typing it is. Written into the record either way, so a machine that later moves
    or changes its own zone does not quietly take the schedule with it."""
    from pathlib import Path

    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        parts = localtime.resolve().parts
        if "zoneinfo" in parts:
            return "/".join(parts[parts.index("zoneinfo") + 1:]) or "UTC"
    return "UTC"


def _resolve_workspace(reference: str) -> str:
    """A workspace id, or the id of the workspace owning a path.

    Taking a path is what lets `--workspace ~/code/thing` read the way somebody would say it.
    Resolved here rather than in the daemon because it is a convenience of *this* interface —
    the RPC takes an id, as every other caller does."""
    reference = (reference or "").strip()
    if not reference:
        return ""
    if not ("/" in reference or reference.startswith("~") or reference == "."):
        return reference
    import os

    wanted = os.path.realpath(os.path.expanduser(reference))
    for workspace in call("workspace.list").get("workspaces", []):
        for location in workspace.get("locations", []):
            base = location.get("base_directory") or location.get("path") or ""
            if base and os.path.realpath(os.path.expanduser(base)) == wanted:
                return str(workspace.get("id") or "")
    raise DaemonError(f"No workspace has a location at {reference}.")


def _command_schedule_create(arguments: argparse.Namespace) -> int:
    result = call(
        "schedule.create",
        workspace_id=_resolve_workspace(arguments.workspace),
        name=arguments.name,
        cron=arguments.cron,
        prompt=arguments.prompt,
        agent=arguments.agent,
        permission_mode=arguments.mode,
        timezone=arguments.timezone,
        working_directory=arguments.directory or "",
    )
    _emit_line(result.get("id", ""))
    return 0


def _command_schedule_list(arguments: argparse.Namespace) -> int:
    _emit(call("schedule.list", workspace_id=_resolve_workspace(arguments.workspace or "")))
    return 0


def _command_schedule_show(arguments: argparse.Namespace) -> int:
    _emit(call("schedule.get", id=arguments.schedule))
    return 0


def _command_schedule_pause(arguments: argparse.Namespace) -> int:
    _emit(call("schedule.enable", id=arguments.schedule, enabled=False))
    return 0


def _command_schedule_resume(arguments: argparse.Namespace) -> int:
    _emit(call("schedule.enable", id=arguments.schedule, enabled=True))
    return 0


def _command_schedule_delete(arguments: argparse.Namespace) -> int:
    _emit(call("schedule.delete", id=arguments.schedule))
    return 0


def _command_schedule_run(arguments: argparse.Namespace) -> int:
    _emit(call("schedule.run", id=arguments.schedule))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="frank", description="Drive Frank sessions.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add = subparsers.add_parser

    create = add("create", help="create a session (the only place its configuration is set)")
    create.add_argument("-a", "--agent", required=True,
                        help="agent profile to run; required, because nothing can guess it for you")
    create.add_argument("-C", "--directory", help="working directory")
    create.add_argument("-m", "--mode", choices=["ask", "auto"],
                        help="the permission mode this session starts under; it can be changed later, and the change reaches the turn in flight")
    create.add_argument("-w", "--workspace", help="workspace the session belongs to — the set of locations it may act in")
    create.add_argument("-P", "--parent", help="parent session; the child is clamped to no looser a mode")
    create.add_argument("--read-only", action="store_true",
                        help="give the session a confinement with nowhere writable, so the operating system refuses every write")
    create.add_argument("-t", "--title", help="a human label for the session list")
    create.set_defaults(handler=_command_create)

    schedule = add("schedule", help="run a prompt on a recurring schedule, unattended")
    schedule_actions = schedule.add_subparsers(dest="schedule_command", required=True)

    schedule_create = schedule_actions.add_parser("create", help="write down a recurring prompt")
    schedule_create.add_argument("name", help="how you will recognise it in the list")
    schedule_create.add_argument("--cron", required=True,
                                 help='when to run, as cron — e.g. "0 9 * * MON-FRI"')
    schedule_create.add_argument("--prompt", required=True, help="what to ask, each time it fires")
    schedule_create.add_argument("-a", "--agent", required=True, help="agent profile to run")
    schedule_create.add_argument("-w", "--workspace", required=True,
                                 help="workspace id, or a path inside one")
    schedule_create.add_argument("-m", "--mode", required=True,
                                 choices=["ask", "auto"],
                                 help="permission mode; required, because nobody is watching when "
                                      "this runs and an unstated mode is one nobody chose")
    schedule_create.add_argument("--timezone", default=_local_timezone(),
                                 help="IANA timezone the cron line is read in (default: this machine's)")
    schedule_create.add_argument("-C", "--directory", help="working directory for the session")
    schedule_create.set_defaults(handler=_command_schedule_create)

    schedule_list = schedule_actions.add_parser("list", help="every schedule, and when each next fires")
    schedule_list.add_argument("-w", "--workspace", help="only this workspace (id or a path inside one)")
    schedule_list.set_defaults(handler=_command_schedule_list)

    schedule_show = schedule_actions.add_parser("show", help="one schedule, including its last run")
    schedule_show.add_argument("schedule")
    schedule_show.set_defaults(handler=_command_schedule_show)

    schedule_pause = schedule_actions.add_parser("pause", help="stop it firing, without deleting it")
    schedule_pause.add_argument("schedule")
    schedule_pause.set_defaults(handler=_command_schedule_pause)

    schedule_resume = schedule_actions.add_parser("resume", help="let it fire again")
    schedule_resume.add_argument("schedule")
    schedule_resume.set_defaults(handler=_command_schedule_resume)

    schedule_delete = schedule_actions.add_parser("delete", help="remove it")
    schedule_delete.add_argument("schedule")
    schedule_delete.set_defaults(handler=_command_schedule_delete)

    schedule_run = schedule_actions.add_parser(
        "run", help="fire it now, without moving its next window — for trying it out")
    schedule_run.add_argument("schedule")
    schedule_run.set_defaults(handler=_command_schedule_run)

    send = add("send", help="send a message to a session")
    send.add_argument("session")
    send.add_argument("message", help="the message, or - to read stdin")
    send.add_argument("-w", "--wait", action="store_true", help="follow until the session goes idle")
    send.set_defaults(handler=_command_send)

    get = add("get", help="show a session")
    get.add_argument("session")
    get.set_defaults(handler=_command_get)

    wait = add("wait", help="wait for a session to go idle, then print its last turn")
    wait.add_argument("session")
    wait.set_defaults(handler=_command_wait)

    attach = add("attach", help="follow a session live, one JSON frame per line")
    attach.add_argument("session")
    attach.set_defaults(handler=_command_attach)

    ps = add("ps", help="list sessions")
    ps.add_argument("-a", "--all", action="store_true", help="include sessions that have ended")
    ps.set_defaults(handler=_command_ps)

    tree = add("tree", help="show a session and everything it created")
    tree.add_argument("session")
    tree.set_defaults(handler=_command_tree)

    # Two verbs, because there are two answers and they are the two words used everywhere else:
    # the decision on the wire is `allow_once` or `deny`, the reviewer answers `allow` or `deny`,
    # and the app's buttons say the same.
    allow = add("allow", help="allow a session's pending permission request")
    allow.add_argument("session")
    allow.add_argument("request")
    allow.set_defaults(handler=_command_allow)

    deny = add("deny", help="deny a session's pending permission request")
    deny.add_argument("session")
    deny.add_argument("request")
    deny.set_defaults(handler=_command_deny)

    kill = add("kill", help="end a session and everything under it")
    kill.add_argument("session")
    kill.set_defaults(handler=_command_kill)

    history = add("history", help="print a session's turns")
    history.add_argument("session")
    history.add_argument("-n", "--limit", type=int, help="only the last N turns")
    history.set_defaults(handler=_command_history)

    configure = add("configure", help="read or change what new sessions and daemons start with")
    configure.add_argument("setting", nargs="?", help="dotted path, e.g. agent.permission_mode")
    configure.add_argument("value", nargs="?", help="the new value; omit to read it")
    configure.add_argument("-u", "--unset", action="store_true", help="remove the setting instead")
    configure.add_argument(
        "-a", "--all", action="store_true",
        help="list every setting the schema defines, with what it is for and what it ships at",
    )
    configure.set_defaults(handler=_command_configure)

    remote = add("remote", help="list peers on other hosts, or hand one a message")
    remote.add_argument("name", nargs="?", help="the registered peer; omit to list them")
    remote.add_argument("message", nargs="?", help="the message, or - to read stdin")
    remote.set_defaults(handler=_command_remote)

    serve = add("serve", help="make Frank available: the control plane and the browser interface")
    serve.add_argument("-p", "--port", type=int, default=8824, help="port to listen on (default 8824)")
    serve.add_argument(
        "--host", default="127.0.0.1",
        help="address to bind (default 127.0.0.1; this surface drives the daemon, so keep it local)",
    )
    serve.add_argument(
        "--open", dest="open_browser", action="store_true",
        help="also open a browser at the served address (off by default: serving is not a "
             "reason to take over the screen, and this may not be the machine you are looking at)",
    )
    serve.set_defaults(handler=_command_serve)

    reach = add("reach", help="make Frank reachable from a phone, over your tailnet")
    reach.add_argument(
        "action", choices=["serve", "pair", "rotate"], nargs="?", default="serve",
        help="serve the endpoint (default), print a pairing code for it, or mint a new token",
    )
    reach.add_argument(
        "-p", "--port", type=int, default=8825,
        help="the loopback port Tailscale proxies to (default 8825). Nothing listens on a "
             "network interface; only change this if something else already has the port",
    )
    reach.add_argument(
        "--interface", nargs="?", const="http://127.0.0.1:3000", default="",
        help="serve the interface from a running dev server instead of the built export, so a "
             "change reaches the phone without `bun run build`. Defaults to Next's own port",
    )
    reach.set_defaults(handler=_command_reach)

    open_app = add("app", help="start the daemon and launch the desktop app")
    open_app.set_defaults(handler=_command_open)

    run = add("run", help="run one turn and print the answer, without a daemon")
    run.add_argument("prompt", nargs="?", help="what to ask, or - to read stdin")
    run.add_argument("-a", "--agent", default="general-assistant", help="which agent profile to run")
    run.add_argument("-C", "--directory", default=".", help="where the agent works (default: here)")
    run.add_argument(
        "--permission-mode", default="",
        help="who answers when a call asks to reach past its confinement: ask, or auto",
    )
    run.add_argument(
        "--allow", action="store_true",
        help="answer every permission gate with yes, for unattended use",
    )
    run.add_argument("--json", action="store_true", help="print every turn event as JSON instead of prose")
    run.set_defaults(handler=_command_run)

    auth = add("auth", help="sign in to a model provider that uses an account rather than a key")
    auth.add_argument("action", choices=["login", "logout", "status"], nargs="?", default="status")
    auth.set_defaults(handler=_command_auth)

    daemon = add("daemon", help="inspect a running daemon (start one with `frank serve`)")
    daemon.add_argument(
        "action", choices=["status", "stop", "restart", "endpoint"],
        nargs="?", default="status",
    )
    daemon.add_argument("-s", "--start", action="store_true", help="start the daemon if it is not running")
    daemon.set_defaults(handler=_command_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Prose, on stderr, with nothing in front of it. The daemon's format — timestamp, level,
    # logger name — is right for a file somebody greps a week later and wrong for a command
    # answering somebody who is watching. `force` because a library on the way in may already
    # have installed a handler of its own, and then this one would be ignored.
    #
    # The root stays at `WARNING` and only Frank's own logger is lowered. Lowering the root as
    # well let every library share the channel, and httpx narrating `POST http://daemon/rpc
    # "HTTP/1.1 200 OK"` before each answer is not what somebody running `frank ps` asked to
    # read.
    logging.basicConfig(
        level=logging.WARNING, format="%(message)s",
        handlers=[logging.StreamHandler(sys.stderr)], force=True,
    )
    logging.getLogger("frank").setLevel(logging.INFO)
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments)
    except DaemonError as error:
        _note(f"frank: {error}")
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `frank ps | head` closes the pipe while we are still writing to it. That is a normal
        # way to use a command, not a failure, so it must not print a traceback. Redirecting
        # stdout to /dev/null first is what stops the interpreter from raising the same error
        # again while flushing at exit, which would print one anyway.
        import os

        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        # 128 + SIGPIPE, the exit status a shell expects from a program a pipe closed under.
        return 141


if __name__ == "__main__":
    sys.exit(main())
