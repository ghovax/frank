"""One executable, four entry points."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    # The internal entry points are addressed by a leading word, stripped before the rest is handed on.
    if arguments and arguments[0] == "prototype":
        from langmesh.worker.prototype import main as prototype_main

        sys.argv = [sys.argv[0], *arguments[1:]]
        return prototype_main()

    # One session worker, whose assignment arrives on an inherited pipe rather than in `argv`.
    if arguments and arguments[0] == "session":
        from langmesh.worker.session_entry import main as session_main

        return session_main(arguments[1:])

    # `langmeshd` rather than `daemon`, since the command line has its own `daemon` verb.
    if arguments and arguments[0] == "langmeshd":
        from langmesh.daemon.__main__ import main as daemon_main

        sys.argv = [sys.argv[0], *arguments[1:]]
        return daemon_main()

    from langmesh.cli.__main__ import main as cli_main

    return cli_main(arguments)


if __name__ == "__main__":
    sys.exit(main())
