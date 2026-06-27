---
name: configure-harness
title: Configure the agentic harness
description: Configure the agentic harness — API credentials and model, sub-agents, skills, MCP servers, and memories. Use when the user wants to add/change an agent, add a skill, connect an MCP server, set API keys, or change the model/endpoint.
enabled: true
---

# Configure the Agentic Harness

Use this skill when the user wants to change how the harness itself is set up: credentials/model, sub-agents, skills, MCP servers, or memories. Read the relevant existing file before editing — most of these are conventions, and copying an existing entry is the safest way to add a new one.

## Where configuration lives

- `~/.harness/configuration.yaml` — the single source of truth for runtime config: API endpoint/model/keys, the Exa key, the default agent, and the `.agents` discovery directories. Created on first run from `configuration.yaml.example` in the repo root (`GlobalConfiguration.load`).
- `~/.harness/history.db` — chat history (SQLite). Not configuration; never edit by hand.
- `.agents/` (project) and `~/.agents/` (global) — agents, skills, MCP servers, and memories. Project entries override global entries with the same name.

Read the model: `src/harness/core/configuration.py`. The server wires it up in `server.py` (`lifespan`).

## API credentials and model

`configuration.yaml`:

```yaml
api:
  endpoint: "https://opencode.ai/zen/go/v1"
  model: "deepseek-v4-flash"
  api_key: ""        # or set OPENCODE_API_KEY in the environment
exa:
  api_key: ""        # or set EXA_API_KEY; enables web_search
default_agent: assistant
```

`effective_api_key` prefers the environment variable over the file (`OPENCODE_API_KEY`, `EXA_API_KEY`). Keys can also be set live from the UI **Settings** dialog (`POST /settings`), which writes them into `configuration.yaml` and reloads them without a restart. Changing `endpoint`/`model` by editing the file needs a server restart.

## Sub-agents

One agent per directory: `.agents/agents/<name>/agent.md` (or `~/.agents/agents/<name>/agent.md`). Frontmatter + a Markdown body that is the agent's system prompt:

```markdown
---
name: reviewer
title: Reviewer
aliases: [code-reviewer]
description: Reviews a diff for correctness and risk, read-only
role: delegation-target        # or "primary" for a default chat agent
enabled: true
connection-type: internal
permission_mode: read_only     # default | read_only | bypass
---

You are the reviewer. ...
```

`name` is the route/slug (used for A2A routing and `spawn_agent`); `title` is the human label shown in the UI. Optional runtime overrides can live in a sibling `config.json` (model, reasoning effort, enabled tools). Agents reload live — no restart needed.

## Skills

A skill is `.agents/skills/<name>/SKILL.md` with frontmatter (`name`, `title`, `description`, `enabled`) and a Markdown body of instructions. `name` is the stable lowercase slug used for lookup and filtering. `title` is the UI-facing label and should be a descriptive action phrase, not a short category name: prefer a verb + object shape such as "Create and update OpenStreetMap map artifacts" or "Research current web sources". The `description` is what makes the agent decide to read it, so make it specific. An agent can restrict which skills it sees via a `skills:` list in its frontmatter; empty means all. Skills reload live.

## MCP servers

Configured in `.agents/mcp.json` (project) or `~/.agents/mcp.json` (global) — **not** auto-discovered from any folder. Each entry names a server the agent can reach via `list_mcp_tools` / `call_mcp_tool`.

Local (stdio) server — the harness launches it as a subprocess. Put non-trivial servers in their own folder under `examples/mcp/<server-id>/`, with `server.py` plus any templates/assets the server returns or reads:

```json
{
  "mcpServers": {
    "openstreetmap": {
      "transport": "stdio",
      "command": "uv",
      "args": ["run", "python", "examples/mcp/openstreetmap/server.py"],
      "stateful": true,
      "env": {},
      "cwd": "."
    }
  }
}
```

Remote (HTTP) server:

```json
{
  "mcpServers": {
    "maps": {
      "transport": "streamable_http",
      "url": "https://mcp.example.com/v1",
      "headers": { "Authorization": "Bearer ..." },
      "stateful": true,
      "timeout_seconds": 30
    }
  }
}
```

Notes: `enabled: false` keeps an entry but turns it off; the `"type"` key is accepted as an alias for `"transport"`. MCP servers default to `stateful: true`, so the harness keeps the initialized session open. For `stdio`, that keeps the subprocess alive across tool calls; for `streamable_http`, it preserves the MCP session id and listens to the server's GET SSE stream. MCP progress/notification events are forwarded into the active A2A stream. Set `stateful: false` only for servers that require one fresh session per operation. **MCP config is read once at startup** (the live watcher does not watch `mcp.json`), so adding or changing a server requires a **server restart**. Discovery/connection live in `src/harness/core/mcp_client.py`.

### MCP render artifacts

MCP tools can return renderable artifacts in a top-level `artifacts` array. The harness currently renders `html`, `iframe`, `image`, and `link` artifacts. For artifacts that should be refreshed by later tool calls, use the generic artifact update fields:

```json
{
  "artifacts": [
    {
      "artifact_id": "map-rome",
      "artifact_update_mode": "replace",
      "artifact_target_id": "map-rome",
      "type": "html",
      "title": "Rome itinerary",
      "html": "<!doctype html>..."
    }
  ]
}
```

`artifact_id` identifies the artifact being returned. `artifact_update_mode` controls UI behavior: `append` renders a new artifact, `replace`/`update` refreshes an existing artifact, and `upsert` replaces when the target exists or appends otherwise. `artifact_target_id` names the existing artifact to refresh; omit it when the target is the returned `artifact_id`. The backend normalizes common aliases such as `artifactId`, `artifactTargetId`, `targetArtifactId`, `updateMode`, and `artifactUpdateMode`.

## Memories

Durable project/user context: `.agents/memories/*.md` and `~/.agents/memories/*.md`, injected into agent prompts. Use for stable facts, not commands.

## What reloads live vs. needs a restart

- **Live (no restart):** agent files, skill files, memories, and API keys set via the Settings dialog.
- **Restart required:** `mcp.json` changes, and `configuration.yaml` edits made directly on disk (endpoint, model, default agent, directories).

After any change, verify: for agents/skills, confirm they appear via the `/agents/cards` or `/mcp/tools` endpoints (or the UI capabilities panel); for credentials, send a message and confirm the turn completes instead of failing with a credentials error.
