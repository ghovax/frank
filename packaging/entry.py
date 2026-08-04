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
    # A process this one spawns re-execs *this binary*, because frozen there is no separate
    # interpreter to re-exec. `multiprocessing` therefore hands its own bootstrap on the command
    # line, and without this the CLI's parser reads it as a subcommand and exits — the same
    # mistake as the two roles below, and the reason dictation could not start in the packaged
    # app: the worker was never reached, so it logged nothing, and the interface reported a
    # failure whose explanation was in a log that had no entry.
    #
    # Called before anything else. It answers the child bootstrap and never returns in that
    # case; in the parent it does nothing at all.
    import multiprocessing

    multiprocessing.freeze_support()

    # And the other half of the same contract. `multiprocessing` starts its resource tracker
    # with `sys.executable -E -c <code>`, which is the interpreter's own interface and not
    # this program's. Unanswered, the tracker died on every launch and was relaunched forever —
    # a warning a second, and shared memory nobody was tracking. Honouring `-c` here means
    # anything that reasonably expects `sys.executable` to behave like an interpreter gets what
    # it expects, rather than each caller needing a bespoke role invented for it.
    if "-c" in sys.argv[1:4]:
        marker = sys.argv.index("-c")
        source = sys.argv[marker + 1] if len(sys.argv) > marker + 1 else ""
        sys.argv = ["-c", *sys.argv[marker + 2:]]
        exec(compile(source, "<string>", "exec"), {"__name__": "__main__"})
        sys.exit(0)

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
