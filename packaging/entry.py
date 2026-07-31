"""Entry point for the frozen build.

One executable serves all three roles — `frank`, `frankd`, and the prototype the daemon
re-execs — so packaging stays a single specification and every process carries the same code
identity as the signed bundle it runs from. Sessions inherit it for free: each is a `fork()` of
the prototype rather than a fresh exec, and a forked child carries its parent's signature.

Two further roles are answered *here*, above the import of `frank.__main__`, and the position is
the whole point. Importing that module imports the `frank` package with it, and the package body
pulls the runtime — seconds of work, for processes that need none of it. From a checkout both run
as plain stdlib programs the interpreter is handed directly; frozen, there is no separate
interpreter to hand anything to, so the executable has to be asked by role instead. Answering
below the import would make them correct and slow, and for a child spawned once per screen action
that is its own kind of broken.
"""

import os
import runpy
import sys


def _bundle_root() -> str:
    """Where the frozen build keeps its sources. The one-file layout unpacks them under
    `sys._MEIPASS`; a one-directory build has them beside the executable."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))


def _run_bundled_script(relative_path: str, arguments: list) -> int:
    """Run a bundled source file as `__main__`, with nothing of this project imported first.

    `runpy` rather than an import, because an import would pull the package the file lives in —
    which, for the screen-control child, is exactly the cost it exists to avoid."""
    script = os.path.join(_bundle_root(), *relative_path.split("/"))
    sys.argv = [script, *arguments]
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    # The `control_screen` child. Stdlib-only by design: it holds no Frank code, bridges every
    # primitive to its parent over a pipe, and is thrown away when the script ends. Asking the
    # binary for a *path* instead made it read that path as a subcommand, and every screen action
    # in the packaged app failed with `invalid choice: '.../control_child.py'`.
    if role == "control-child":
        sys.exit(_run_bundled_script("frank/computer/control_child.py", sys.argv[2:]))
    # The system's proxy configuration, read out of process so the parent never loads
    # SystemConfiguration. Frozen, `sys.executable -c …` was the same mistake with a quieter
    # ending: the binary rejected `-c`, the caller saw a non-zero exit, and every proxy the
    # machine had configured was silently treated as absent.
    if role == "read-proxies":
        import json
        import urllib.request

        print(json.dumps(urllib.request.getproxies()))
        sys.exit(0)

    from frank.__main__ import main

    sys.exit(main())
