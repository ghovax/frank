"""FastAPI route modules, split out of harness.server.app by concern. Each exposes a
``router`` that app.py registers; handlers reach shared runtime state through the
``harness.server.app`` module and pull stable helpers/models from it by name."""
