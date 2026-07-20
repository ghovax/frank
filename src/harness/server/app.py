"""ASGI entry point for the harness server.

The server runtime — the FastAPI ``app``, its shared state, and every operation the routes
call — lives in :mod:`harness.server.runtime`, which imports no route module. This entry
sits on top: it takes that ``app``, mounts the split route routers onto it, and exposes
``run_server``. Keeping the router mounting here (the one place that imports the route
modules) is what breaks the old ``app -> routes -> app`` import cycle: the routes import the
runtime, never this module.
"""

import importlib

from harness.server import state
from harness.server.runtime import app

# Register the split route modules WITHOUT binding their names into this module's namespace:
# several of them (notably `artifacts`) collide with module-level aliases used at runtime —
# e.g. `from harness.core import artifact_versioning as artifacts` — and a bare
# `from .routes import artifacts` would shadow that alias. Import each router module by path
# and include only its `router`.
for _route_name in (
    "agents", "artifacts", "chat", "filesystem", "mcp",
    "projects", "remote_agents", "sessions", "settings", "terminals", "uploads",
):
    app.include_router(importlib.import_module(f"harness.server.routes.{_route_name}").router)


def run_server(host: str = "127.0.0.1", port: int = 8822) -> None:
    # Record the bind host in shared state so the runtime's startup exposure check
    # (fail-closed on a non-loopback bind without inbound auth) sees the real target.
    state._BIND_HOST = host
    import uvicorn
    uvicorn.run(app, host=host, port=port)


__all__ = ["app", "run_server"]

if __name__ == "__main__":
    run_server()
