# daisy

A configurable multi-agent harness built on the A2A protocol. Each agent profile is a markdown file; every agent is served as an independently addressable A2A endpoint with tools, permissions, sub-agent delegation, skills, and a web UI.

- `server.py` — FastAPI app serving all agents over A2A (JSON-RPC) plus a small REST API for the UI. Defaults to `127.0.0.1:8822`.
- `.agents/agents/<id>/agent.md` — project-local agent profiles with frontmatter and prompt body.
- `.agents/agents/<id>/config.json` — per-agent model, tool, and permission settings.
- `.agents/skills/<id>/SKILL.md` — project-local reusable skill instructions.
- `.agents/mcp.json` — MCP server configuration using `mcpServers`.
- Composio — set `composio.enabled: true` in `configuration.yaml` with the hosted MCP (`url`) and `api_key` from the Composio dashboard, and the harness exposes its tools through the normal MCP path. No separate agent; the agent discovers tools dynamically and authorizes accounts (e.g. `gmail`, `notion`) on first use.
- `.agents/memories/*.md` — persistent project memory; prompt injection includes metadata only, and agents read the body on demand.
- `~/.agents/agents/` and `~/.agents/skills/` — optional global profiles and skills. Project-local entries override global entries with the same name.
- `examples/mcp/openstreetmap/` — stdio MCP server folder used by the default `openstreetmap` MCP configuration for map smoke tests.
- `web/` — Next.js chat UI.
- `src/harness/` — the runtime: agent loop, tool dispatch, permissions, A2A bridge.
- `~/.harness/` — the harness home directory and single source of truth for mutable state: `configuration.yaml` (provider credentials, selected model, default agent, local agent/skill discovery directories, and MCP servers) and `history.db` (chat history). The directory and config are created on first run from the packaged `src/harness/core/configuration.yaml`.

Run with `uv run python server.py`. Secrets (API keys) are read from environment variables, falling back to `~/.harness/configuration.yaml`.
