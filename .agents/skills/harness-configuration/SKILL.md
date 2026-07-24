---
name: harness-configuration
title: Configure the agentic harness via the built-in files and patterns
description: Configure the agentic harness — provider credentials and model, permission modes, sandbox, agents, skills, MCP servers, and memories. Use when the user wants to add/change a provider key or model, add/change an agent or skill, connect an MCP server, set the Composio/Exa key, toggle the sandbox, or switch permission behavior.
enabled: true
---

# Configure the Agentic Harness

Use this skill when the user wants to change how the harness itself is set up. The harness has **two configuration surfaces**: the YAML file (`~/.config/xeac/configuration.yaml`) and the running UI (Settings + model picker), which writes back to that file for live changes. Always read the relevant existing file before editing.

The authoritative models live in `src/harness/core/configuration.py` (`GlobalConfiguration`, `AgentConfiguration`), `src/harness/core/providers.py` (the provider registry + key/base-url resolution), and `server.py` (`lifespan`, where it is all wired up).

## Where things live

- `~/.config/xeac/configuration.yaml` — runtime configuration: provider credentials, selected model/provider, Exa, sandbox, Composio, default agent, and discovery directories. Seeded on first run from the packaged `src/harness/core/configuration.yaml`. The UI writes settings back here.
- `~/.config/xeac/history.db` — chat history (SQLite, WAL mode). Not configuration; never edit by hand. If its schema ever goes stale after an upgrade, stop the server and delete it — it rebuilds on next start (history is replayable transcripts, not irreplaceable state).
- `.agents/` (project) and `~/.agents/` (global) — agents, skills, MCP servers, and memories. Project entries override global entries with the same name.

## Providers, credentials, and the model

The harness is multi-provider. Credentials are keyed by **provider id** under a top-level `providers:` map; the selected model is the pair `selected_provider` + `selected_model` (the factory recombines them into the `provider/model` form LiteLLM expects).

```yaml
providers:
  opencode:                         # OpenAI-compatible — takes a base_url
    api_key: ""
    base_url: "https://opencode.ai/zen/go/v1"
  anthropic:  { api_key: "" }        # first-party clouds omit base_url (LiteLLM knows the endpoint)
  openai:     { api_key: "" }
  google:     { api_key: "" }
  openrouter: { api_key: "" }
  xai:        { api_key: "" }
  deepseek:   { api_key: "" }
  groq:       { api_key: "" }
  mistral:    { api_key: "" }
  custom:                            # any other OpenAI-compatible endpoint
    api_key: ""
    base_url: ""

selected_model: "deepseek-v4-flash"
selected_provider: "opencode"
```

**Key/base-url resolution** (`providers.py`): an explicit configured value (file or UI) **wins**; otherwise the provider's conventional env var is read (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`/`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `XAI_API_KEY`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`). `base_url` only matters for the OpenAI-compatible providers (`opencode`, `custom`); first-party clouds ignore it.

**Two ways to change this at runtime, both live (no restart):**
- The **Settings** dialog writes credentials and the selected model into the YAML and reloads.
- The **model picker** (provider dropdown → model dropdown, with a per-provider API-key field) sets a **per-session model override** (`PUT /sessions/{id}/model`). A session runs on its override, falling back to the globally selected model.

Editing `configuration.yaml` on disk by hand needs a server restart.

## Permission modes

Permission behavior is one of four modes, set per-agent in frontmatter (`permission_mode:`) and overridable per-session from the UI:

- `default` — user-configured per-command permission rules (allow / ask / deny) from the bash allow-rules.
- `auto` — the user-configured rules **plus** an LLM classifier that auto-approves bash calls it judges safe and escalates the rest to the user. It is permission-rule-aware (a configured `deny` stays a hard deny; `read_only` stays a hard block) and conservative — classifier failure falls back to escalation. The classifier prompt lives in `src/harness/core/prompts/bash_permission_classifier.md`.
- `read_only` — hard-block every write (investigation/review agents).
- `bypass` — allow everything.

## Sandbox

`sandbox.enabled` (default `true`) confines bash commands to the working directory; access outside it needs approval. Toggle it from the UI (live) or the YAML (`sandbox: { enabled: true }`, restart).

## Agents

One agent per directory: `.agents/agents/<name>/agent.md` (or `~/.agents/agents/<name>/agent.md`). YAML frontmatter + a Markdown body that is the agent's system prompt. Fields mirror `AgentConfiguration`:

```markdown
---
name: reviewer                       # route/slug — used for A2A routing and spawn_agent
title: Reviewer                      # human label shown in the UI
aliases: [code-reviewer]
color: purple
description: Reviews a diff for correctness and risk, read-only
role: delegation-target              # or "primary" for a default chat agent
enabled: true
connection_type: internal
skills: []                           # skill slugs this agent may use; empty = all
model: null                          # override the global default (provider/model)
provider: null
reasoning_effort: high               # minimal | low | medium | high
maximum_iterations: 25               # safety bound on the per-turn tool-calling loop
permission_mode: read_only           # default | auto | read_only | bypass
tools_enabled: []                    # restrict the built-in tools; empty = all
system_prompt: ""
---

You are the reviewer. ...
```

Optional runtime overrides can live in a sibling `config.json` (`model`/`provider`, `reasoningEffort`, `permissionMode`, `toolsEnabled`). Agents reload live — no restart needed.

## Skills

A skill is `.agents/skills/<name>/SKILL.md` with frontmatter (`name`, `title`, `description`, `enabled`) and a Markdown body of instructions. `name` is the stable lowercase slug used for lookup and filtering. `title` is the UI-facing label and should be a descriptive action phrase, not a short category name: prefer a verb + object shape such as "Create and update OpenStreetMap map artifacts" or "Research current web sources". The `description` is what makes the agent decide to read it, so make it specific. An agent restricts which skills it sees via its `skills:` list (empty means all). Skills reload live.

## MCP servers

Configured in `.agents/mcp.json` (project) or `~/.agents/mcp.json` (global) — **not** auto-discovered from any folder. Each entry names a server the agent reaches via `list_mcp_tools` / `call_mcp_tool`. Put non-trivial stdio servers in their own folder under `examples/mcp/<server-id>/` (`server.py` plus templates/assets).

Local (stdio) — launched as a subprocess:

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

Remote (HTTP):

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

`enabled: false` keeps an entry but turns it off; `"type"` is accepted as an alias for `"transport"`. Servers default to `stateful: true`: for `stdio` the subprocess stays alive across calls; for `streamable_http` the MCP session id is preserved and the server's GET SSE stream is listened to. MCP progress/notification events are forwarded into the active A2A stream. Set `stateful: false` only for servers that require one fresh session per operation. **MCP config is read once at startup** (the live watcher does not watch `mcp.json`), so adding or changing a server requires a **server restart**. Discovery/connection live in `src/harness/core/mcp_client.py`.

### MCP render artifacts

MCP tools can return renderable artifacts in a top-level `artifacts` array. The harness renders `html`, `iframe`, `image`, and `link` artifacts. To refresh an existing artifact from a later call:

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

`artifact_update_mode` controls UI behavior: `append` renders a new artifact, `replace`/`update` refresh an existing one, `upsert` replaces when the target exists or appends otherwise. `artifact_target_id` names the existing artifact to refresh; omit it when the target is the returned `artifact_id`. The backend normalizes camelCase aliases (`artifactId`, `artifactTargetId`, `updateMode`, …).

Note: in the chat UI only **one** live preview (iframe/html) is mounted at a time — the newest auto-activates and the rest collapse to click-to-open placeholders, so many previews never pile up live iframes.

## Composio (optional)

Hosted MCP integration under `composio:` in the YAML. When `enabled`, the harness points at Composio's "connect" MCP URL and exposes its tools through the normal MCP path (`call_mcp_tool`) under `server_name`. The API key may come from `COMPOSIO_API_KEY` (env wins). Which toolkits are available is set in the Composio dashboard.

```yaml
composio:
  enabled: false
  url: "https://connect.composio.dev/mcp"
  api_key: ""
  server_name: "composio"
```

## Memories

Durable project/user context: `.agents/memories/*.md` and `~/.agents/memories/*.md`, injected into agent prompts. Use for stable facts, not commands.

## What reloads live vs. needs a restart

- **Live (no restart):** agent files, skill files, memories, API keys/base-url/model set via the Settings dialog or model picker, per-session model overrides, and the sandbox toggle.
- **Restart required:** `mcp.json` changes, Composio changes, and `configuration.yaml` edits made directly on disk (directory roots, default agent, delegation depth, etc.).

## Verifying a change

- Agents/skills: confirm they appear via `GET /agents/cards` (or the UI capabilities panel).
- Providers/model: `GET /models` lists available models grouped by provider; a provider's models are unlocked once its key resolves.
- MCP: `GET /mcp/tools?working_directory=<path>` lists the servers and tools the folder sees.
- Credentials/model end-to-end: send a message and confirm the turn completes (a missing/empty key fails the turn with a credentials error rather than silently hanging).
