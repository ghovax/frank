"""The harness FastAPI server package.

:mod:`harness.server.runtime` owns the runtime — the FastAPI ``app``, its shared singleton
state, the ORM models, and the operations the routes call — and imports no route module.
:mod:`harness.server.app` is the thin ASGI entry that mounts the split routers onto that
``app`` and exposes ``run_server``. :mod:`harness.server.models` holds the request/response
DTOs. The route modules under ``routes/`` import the runtime and the models by name, never
the entry module, so there is no import cycle.
"""
