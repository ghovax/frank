"""Entry point for the frozen build.

One executable serves all three roles — `daisy`, `daisyd`, and the prototype the daemon
re-execs — so packaging stays a single specification and every process carries the same code
identity as the signed bundle it runs from. Sessions inherit it for free: each is a `fork()` of
the prototype rather than a fresh exec, and a forked child carries its parent's signature.
"""

import sys

from daisy.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
