# agentic-harness

A configurable multi-agent harness built on the A2A protocol. Each agent profile is a markdown file; every agent is served as an independently addressable A2A endpoint with tools, permissions, sub-agent delegation, skills, and a web UI.

- `server.py` — FastAPI app serving all agents over A2A (JSON-RPC) plus a small REST API for the UI. Defaults to `127.0.0.1:8822`.
- `agents/` — agent profiles (markdown with YAML frontmatter).
- `skills/` — reusable, auto-discovered skill instructions.
- `web/` — Next.js chat UI.
- `src/harness/` — the runtime: agent loop, tool dispatch, permissions, A2A bridge.
- `configuration.yaml` — API endpoint, default agent, directories.

Run with `uv run python server.py`. Secrets (API keys) are read from environment variables, falling back to `configuration.yaml`.
