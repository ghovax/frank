"""Mounting the GUI's REST surface."""

from __future__ import annotations

import importlib

from fastapi import FastAPI

# Imported by name rather than bound into this module's namespace: several route modules share a name with something else in scope (`sessions`, `settings`), and a plain `from .routes import settings` would shadow it.
ROUTE_MODULES = (
    "agents", "dictation", "filesystem", "machines", "mcp", "preferences",
    "workspaces", "schedules", "remote_agents", "sessions", "settings", "terminals", "uploads",
)


def mount(app: FastAPI) -> None:
    """Add every GUI route to an application."""
    for name in ROUTE_MODULES:
        app.include_router(importlib.import_module(f"frank.rest.routes.{name}").router)


__all__ = ["ROUTE_MODULES", "mount"]
