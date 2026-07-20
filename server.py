"""Launch entry point for the harness FastAPI server.

The server body lives in :mod:`harness.server` (``boot`` builds the app and owns the shared
state; ``asgi`` mounts the routers and exposes the ASGI ``app``) so it is importable as a
normal module — run directly (``python server.py``) or frozen by PyInstaller, this file only
starts it. Keeping the body out of ``__main__`` is what lets the route modules import that
shared state without spawning a second, half-initialized copy of it.
"""

from harness.server.asgi import app, run_server

__all__ = ["app", "run_server"]

if __name__ == "__main__":
    run_server()
