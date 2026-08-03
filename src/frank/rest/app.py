"""Mounting the GUI's REST surface.

The desktop client cannot open a unix socket, so it drives everything over the daemon's
loopback listener — which means these routes hang off the same application, behind the same
capability token, as the control plane. The CLI never touches them; it has the socket.

This lives in `rest` rather than in the daemon because `rest` is the layer above: it may
reach down into the daemon's services, and the daemon must not reach up into it. The daemon's
entry point calls this when it assembles its app, which is the one place both are in view.
"""

from __future__ import annotations

import importlib

from fastapi import FastAPI

# Imported by name rather than bound into this module's namespace: several route modules
# share a name with something else in scope (`sessions`, `settings`), and a plain
# `from .routes import settings` would shadow it.
ROUTE_MODULES = (
    "agents", "dictation", "filesystem", "machines", "mcp", "preferences",
    "workspaces", "schedules", "remote_agents", "sessions", "settings", "terminals", "uploads",
)


def mount(app: FastAPI) -> None:
    """Add every GUI route to an application."""
    for name in ROUTE_MODULES:
        app.include_router(importlib.import_module(f"frank.rest.routes.{name}").router)


__all__ = ["ROUTE_MODULES", "mount"]
