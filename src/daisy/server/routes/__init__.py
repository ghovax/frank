"""FastAPI route modules, one per concern. Each exposes a ``router`` that
:mod:`daisy.server.asgi` registers. Handlers are thin adapters: they pull domain helpers
from :mod:`daisy.server.services`, shared singletons from :mod:`daisy.server.state`, and
request/response DTOs from :mod:`daisy.server.models` — never the entry module, so there is
no import cycle."""
