"""FastAPI route modules, split by concern. Each exposes a ``router`` that
:mod:`harness.server.asgi` registers; handlers reach shared runtime state through the
:mod:`harness.server.boot` module and pull stable helpers by name, and request/response
DTOs from :mod:`harness.server.models` — never from the entry module, so there is no cycle."""
