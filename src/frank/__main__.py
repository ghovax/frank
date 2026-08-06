"""One executable, four entry points."""

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
