"""The ASGI entry point: the fully-assembled ``app`` an ASGI server imports and serves.

:mod:`harness.server.boot` builds the FastAPI ``app`` and owns the runtime; this module
takes that ``app``, mounts the split route routers onto it, and exposes ``run_server``.
Mounting the routers here — the one and only module that imports the route modules — is what
breaks the old ``app -> routes -> app`` cycle: the routes import the boot module and the
services, never this entry. Import target for a server: ``harness.server.asgi:app``.
"""

import importlib

from harness.server import state
from harness.server.boot import app

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
