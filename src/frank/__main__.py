"""One executable, four entry points.

`frank` is the command a person runs, `frankd` is the daemon, `prototype` is the process the
daemon starts to make sessions out of, and `session` is one session worker. They are the same
image entered differently rather than four binaries, for two reasons: packaging stays a single
specification, and — the load-bearing one — a process launched as a re-exec of this executable
carries the same code identity as the signed application bundle it lives in. On macOS that is
what keeps one Accessibility grant covering every session instead of prompting the user once
per process.

A session is `fork()` **and then `exec()`** back into this file. The exec is not incidental. A
forked child that never execs inherits its parent's copy of every Apple framework the parent
happened to touch, and several of those are documented as unusable in that state — the
Objective-C runtime, and Network.framework, which sits behind `getaddrinfo`. They do not fail
politely: every session died of `SIGSEGV` inside `getaddrinfo` on its first model call, which
reads as a network fault and is nothing of the kind. `exec()` rebuilds that state from
nothing, which is the only thing that makes it safe.

The identity property survives the change, because the child execs *this* executable rather
than a bare interpreter: same path, same signature, same grant. What does not survive is the
warm start that forking a pre-imported image bought — a session now pays its own runtime
import. That was the trade, made knowingly: a fast session that segfaults is worth less than
a slower one that answers.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    # The daemon, the prototype and a session are internal entry points, addressed by a leading word.
    if arguments and arguments[0] == "prototype":
        from frank.worker.prototype import main as prototype_main

        sys.argv = [sys.argv[0], *arguments[1:]]
        return prototype_main()

    # One session worker, exec'd by the prototype.
    if arguments and arguments[0] == "session":
        from frank.worker.session_entry import main as session_main

        return session_main(arguments[1:])

    # `frankd`, not `daemon`: the CLI has its own `daemon` verb for inspecting and starting the service, and routing that here would mean `frank daemon status` silently tried to start a second daemon instead of reporting on the running one.
    if arguments and arguments[0] == "frankd":
        from frank.daemon.__main__ import main as daemon_main

        sys.argv = [sys.argv[0], *arguments[1:]]
        return daemon_main()

    from frank.cli.__main__ import main as cli_main

    return cli_main(arguments)


if __name__ == "__main__":
    sys.exit(main())
