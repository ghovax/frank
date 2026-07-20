"""FastAPI route modules, one per concern. Each exposes a ``router`` that
:mod:`harness.server.asgi` registers. Handlers are thin adapters: they pull domain helpers
from :mod:`harness.server.services`, shared singletons from :mod:`harness.server.state`, and
request/response DTOs from :mod:`harness.server.models` — never the entry module, so there is
no import cycle."""
