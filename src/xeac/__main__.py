"""One executable, three entry points.

`xeac` is the command a person runs, `xeacd` is the daemon, and `worker` is what the daemon
re-execs to make a session. They are the same image entered differently rather than three
binaries, for two reasons: packaging stays a single specification, and — the load-bearing one
— a worker launched as a re-exec of this executable carries the same code identity as the
signed application bundle it lives in. On macOS that is what keeps one Accessibility grant
covering every session instead of prompting the user once per worker.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    # The daemon and the worker are internal entry points, addressed by a leading word. Both
    # are stripped before the rest is handed on, so neither sees it as a subcommand.
    if arguments and arguments[0] == "worker":
        from xeac.worker.__main__ import main as worker_main

        sys.argv = [sys.argv[0], *arguments[1:]]
        return worker_main()

    # `xeacd`, not `daemon`: the CLI has its own `daemon` verb for inspecting and starting the
    # service, and routing that here would mean `xeac daemon status` silently tried to start a
    # second daemon instead of reporting on the running one.
    if arguments and arguments[0] == "xeacd":
        from xeac.daemon.__main__ import main as daemon_main

        sys.argv = [sys.argv[0], *arguments[1:]]
        return daemon_main()

    from xeac.cli.__main__ import main as cli_main

    return cli_main(arguments)


if __name__ == "__main__":
    sys.exit(main())
