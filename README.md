# agentic-harness

A configurable multi-agent harness built on the A2A protocol. Each agent profile is a markdown file; every agent is served as an independently addressable A2A endpoint with tools, permissions, sub-agent delegation, skills, and a web UI.

- `server.py` — FastAPI app serving all agents over A2A (JSON-RPC) plus a small REST API for the UI. Defaults to `127.0.0.1:8822`.
- `.agents/agents/<id>/agent.md` — project-local agent profiles with frontmatter and prompt body.
- `.agents/agents/<id>/config.json` — per-agent model, tool, and permission settings.
- `.agents/skills/<id>/skill.md` — project-local reusable skill instructions.
- `.agents/mcp.json` — MCP server configuration using `mcpServers`.
- `.agents/memories/*.md` — persistent project memory injected into agent prompts.
- `~/.agents/agents/` and `~/.agents/skills/` — optional global profiles and skills. Project-local entries override global entries with the same name.
- `examples/mcp/echo_server.py` — tiny stdio MCP server used by the default `demo` MCP configuration for smoke tests.
- `web/` — Next.js chat UI.
- `src/harness/` — the runtime: agent loop, tool dispatch, permissions, A2A bridge.
- `configuration.yaml` — API endpoint, default agent, local agent/skill discovery directories, and MCP servers.

Run with `uv run python server.py`. Secrets (API keys) are read from environment variables, falling back to `configuration.yaml`.
