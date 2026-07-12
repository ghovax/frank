"""Launch entry point for the harness FastAPI server.

The server body lives in :mod:`harness.server.app` so it is importable as a normal module
— run directly (``python server.py``) or frozen by PyInstaller, this file only starts it,
while ``harness.server.app`` owns the app, its shared runtime state, and the route modules.
Keeping the body out of ``__main__`` is what lets route modules import that shared state
without spawning a second, half-initialized copy of it.
"""

from harness.server.app import app, run_server

__all__ = ["app", "run_server"]

if __name__ == "__main__":
    run_server()
