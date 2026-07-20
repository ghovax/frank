"""The harness FastAPI server package.

:mod:`daisy.server.boot` is the composition root: it builds the FastAPI ``app``, drives
its lifespan, mounts each agent's A2A sub-app, and runs the watchers — and imports no route
module. The shared singletons live in :mod:`daisy.server.state`, the ORM in
:mod:`daisy.server.database`, the request/response DTOs in :mod:`daisy.server.models`, and
the domain operations in :mod:`daisy.server.services`. :mod:`daisy.server.asgi` is the
thin ASGI entry that mounts the split routers onto the ``app`` and exposes ``run_server``. The
route modules under ``routes/`` import ``boot``, ``services``, ``state``, and ``models`` by
name, never the entry module, so there is no import cycle.
"""
