"""One executable, three entry points.

`daisy` is the command a person runs, `daisyd` is the daemon, and `prototype` is the process
the daemon starts to fork sessions out of. They are the same image entered differently rather
than three binaries, for two reasons: packaging stays a single specification, and — the
load-bearing one — a process launched as a re-exec of this executable carries the same code
identity as the signed application bundle it lives in. On macOS that is what keeps one
Accessibility grant covering every session instead of prompting the user once per process.

That property extends to the sessions themselves for free, and it is worth saying why. A
session is a `fork()` of the prototype rather than a fresh exec, and a forked child inherits
its parent's code signature exactly. So the fleet is one code identity no matter how many
sessions run, without any of them being launched from here.

There is deliberately no `worker` entry point. There was one, back when the daemon started
worker processes and told them over stdin what session to be; nothing execs a worker now, so
an entry point for it would be a way of starting a process the architecture never starts.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    # The daemon and the prototype are internal entry points, addressed by a leading word.
    # Both are stripped before the rest is handed on, so neither sees it as a subcommand.
    if arguments and arguments[0] == "prototype":
        from daisy.worker.prototype import main as prototype_main

        sys.argv = [sys.argv[0], *arguments[1:]]
        return prototype_main()

    # `daisyd`, not `daemon`: the CLI has its own `daemon` verb for inspecting and starting the
    # service, and routing that here would mean `daisy daemon status` silently tried to start a
    # second daemon instead of reporting on the running one.
    if arguments and arguments[0] == "daisyd":
        from daisy.daemon.__main__ import main as daemon_main

        sys.argv = [sys.argv[0], *arguments[1:]]
        return daemon_main()

    from daisy.cli.__main__ import main as cli_main

    return cli_main(arguments)


if __name__ == "__main__":
    sys.exit(main())
