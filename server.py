"""Entry point for the frozen build.

One executable serves all three roles — `xeac`, `xeacd`, and the worker the daemon re-execs —
so packaging stays a single specification and every worker carries the same code identity as
the signed bundle it runs from.
"""

import sys

from xeac.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
