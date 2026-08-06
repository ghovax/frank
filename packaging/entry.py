"""Entry point for the frozen build, where one executable serves every role."""

import os
import runpy
import sys


def _bundle_root() -> str:
    """Where the frozen build keeps its sources: unpacked in a one-file layout, beside the executable otherwise."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))


def _run_bundled_script(relative_path: str, arguments: list) -> int:
    """Run a bundled source file as `__main__`, with nothing of this project imported first."""
    script = os.path.join(_bundle_root(), *relative_path.split("/"))
    sys.argv = [script, *arguments]
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    # A spawned process re-execs this binary, so its bootstrap arrives on the command line rather than as a script.
    import multiprocessing

    multiprocessing.freeze_support()

    # And the other half of the same contract, for the resource tracker started with the interpreter's own flags.
    if "-c" in sys.argv[1:4]:
        marker = sys.argv.index("-c")
        source = sys.argv[marker + 1] if len(sys.argv) > marker + 1 else ""
        sys.argv = ["-c", *sys.argv[marker + 2:]]
        exec(compile(source, "<string>", "exec"), {"__name__": "__main__"})
        sys.exit(0)

    role = sys.argv[1] if len(sys.argv) > 1 else ""
    # The `control_screen` child, stdlib-only by design and thrown away when the script ends.
    if role == "control-child":
        sys.exit(_run_bundled_script("langmesh/computer/control_child.py", sys.argv[2:]))
    # The system's proxy configuration, read out of process so the parent never loads SystemConfiguration.
    if role == "read-proxies":
        import json
        import urllib.request

        print(json.dumps(urllib.request.getproxies()))
        sys.exit(0)

    from langmesh.__main__ import main

    sys.exit(main())
