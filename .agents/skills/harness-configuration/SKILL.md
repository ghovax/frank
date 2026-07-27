---
name: harness-configuration
title: Configure the agentic harness via the built-in files and patterns
description: Configure the agentic harness — provider credentials and model, permission modes, sandbox, agents, skills, MCP servers, and memories. Use when the user wants to add/change a provider key or model, add/change an agent or skill, connect an MCP server, set the Composio/Exa key, toggle the sandbox, or switch permission behavior.
enabled: true
---

# Configure the Agentic Harness

Use this skill when the user wants to change how the harness itself is set up. There are **three surfaces onto one file** (`~/.config/daisy/configuration.yaml`): `daisy configure` from the terminal, Settings in the desktop app, and editing the file directly — the daemon watches it and picks up a hand edit live. Always read the relevant existing file before editing.

**Start with `daisy configure --all`.** It walks the schema, not the file, so it lists every setting that exists — including the ones nobody has written down — each with what it is for, what it ships at, and what this machine currently runs on. Reading the file only ever shows the part already known about. A name the schema does not define is refused rather than written and ignored, so a typo fails where it is made.

The authoritative models live in `src/daisy/base/configuration.py` (`GlobalConfiguration`, `AgentConfiguration`) and `src/daisy/base/providers.py` (the provider registry and key/base-url resolution). Each setting's explanation is the `Field(description=...)` beside it, and each tunable's is the `Default(...)` beside it in `src/daisy/base/tuning.py` — those strings are exactly what `daisy configure` prints, so a new setting is documented by writing one and needs nothing else. There is no reference file to regenerate and no listing to add it to. The daemon's composition root — where the shared resources are wired up — is `src/daisy/daemon/composition.py`.

## Where things live

- `~/.config/daisy/configuration.yaml` — provider credentials, Exa, sandbox, Composio, permissions, tuning, telemetry. Seeded on first run from the packaged `src/daisy/base/configuration.yaml`, which is deliberately almost empty: everything has a default in the code, and a seed restating those defaults would freeze them, so an installation would keep overriding an improved default with a copy of the old value. Note what is *not* there: no default agent. Naming the profile is required — `--agent` from the CLI, the `agent` argument of a session's `create_session` tool — and no profile is nominated to stand in for an unstated one.
- There is no checked-in reference file. `daisy configure --all` is the complete list, read from the running code, so it cannot describe a setting the code does not have or miss one it does.
- `~/.local/share/daisy/history.db` — session transcripts (SQLite, WAL). Not configuration; never edit by hand, and never open it from a session: the daemon is the sole writer, and workers persist by posting to it. If the schema ever goes stale after an upgrade, `daisy daemon stop` and delete it — it rebuilds (transcripts are replayable, not irreplaceable).
- The rest is XDG too: logs in `~/.local/state/daisy/`, caches in `~/.cache/daisy/`, and sockets, the daemon's port and its token in the runtime directory.
- `.agents/` (project) and `~/.agents/` (global) — agents, skills, MCP servers, and memories. Project entries override global entries with the same name.

## Providers, credentials, and the model

The harness is multi-provider. Credentials are keyed by **provider id** under a top-level `providers:` map. Which model a session runs is **not** set here — it belongs to the agent profile, under `preset` in that agent's `configuration.json`, as a `provider` + `model` pair the factory recombines into the `provider/model` form LiteLLM expects.

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
```

**Key/base-url resolution** (`providers.py`): an explicit configured value (file or UI) **wins**; otherwise the provider's conventional env var is read (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`/`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `XAI_API_KEY`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`). `base_url` only matters for the OpenAI-compatible providers (`opencode`, `custom`); first-party clouds ignore it.

**Three ways to change this, all live:** `daisy configure providers.anthropic.api_key <key>`, the Settings dialog, or editing the file — the daemon watches it and reloads. A credential change asks running sessions to rebuild their runtime on the next turn, so it takes effect without restarting anything.

A profile pinned to a provider you have no credentials for fails on its first call. It does not borrow another profile's model: an agent is defined by its own configuration, and nothing else's.

## Permission modes

Permission behaviour is one of **three** modes, defaulted per-agent in frontmatter (`permission_mode:`) and fixed for a session when it is created:

- `default` — the user-configured per-command rules (allow / ask / deny) from the bash allow-rules.
- `auto` — those rules **plus** a classifier that auto-approves bash calls it judges safe and escalates the rest. It is rule-aware (a configured `deny` stays a hard deny; `read_only` stays a hard block) and conservative — a classifier failure falls back to escalation. Its prompt is `src/daisy/runtime/prompts/permission_classifier.md`.
- `read_only` — hard-block every write (investigation and review sessions).

There is **no bypass mode** and no standing "always allow": the only runtime decisions are allow-once and deny. A session's mode cannot be changed after creation, and a session created by another is clamped to no looser a mode than its parent — so a peer can never be used to escape the mode you are in.

## Sandbox

`sandbox:` is what a session's tool children may actually do, enforced by the OS (`sandbox-exec` on macOS, Landlock plus a network namespace on Linux) rather than guessed from the text of a command. `enforce` is `required` (refuse to create a session where no backend can enforce it), `preferred` (POSIX limits only) or `off`; `filesystem.readable`/`writable`/`deny` govern the home directory, which is closed by default while the system stays readable; `limits` are `setrlimit(2)` constants under their own names. Set it from the UI, with `daisy configure sandbox.enforce off`, or in the YAML. Unlike most settings this is **not** live: a session's confinement is fixed when it is created and clamped against its creator, so a change reaches the next session rather than a running one — the same guarantee the permission mode carries.

## Agents

One agent per directory: `.agents/agents/<name>/agent.md` (or `~/.agents/agents/<name>/agent.md`). YAML frontmatter + a Markdown body that is the agent's system prompt. Fields mirror `AgentConfiguration`:

```markdown
---
name: reviewer                       # slug — what `--agent`, `create_session` and the card name it by
title: Reviewer                      # human label shown in the UI
aliases: [code-reviewer]
color: purple
description: Reviews a diff for correctness and risk, read-only
role: peer                           # "primary" to be talked to directly, "peer" for scoped work
enabled: true
connection_type: internal
skills: []                           # skill slugs this agent may use; empty = all
model: null                          # override the global default (provider/model)
provider: null
reasoning_effort: high               # minimal | low | medium | high
permission_mode: read_only           # default | auto | read_only
tools_enabled: []                    # allow-list over the WHOLE tool surface; empty = no restriction
system_prompt: ""
---

You are the reviewer. ...
```

Runtime overrides live in a sibling **`configuration.json`** (not `config.json`): `preset` (`model`, `provider`, `reasoningEffort`), `permissionMode`, and `tools` (`enabledBuiltinTools`, `bash`). This is the file the UI writes when you pick a model for an agent. Agents reload live — the daemon watches the `.agents` roots.

`enabledBuiltinTools` is an allow-list over the **whole** tool surface: empty means no restriction, and naming one tool denies every other. It is narrowed at load to tools that exist, so an entry for a removed tool does not silently disarm the profile.

## Skills

A skill is `.agents/skills/<name>/SKILL.md` with frontmatter (`name`, `title`, `description`, `enabled`) and a Markdown body of instructions. `name` is the stable lowercase slug used for lookup and filtering. `title` is the UI-facing label and should be a descriptive action phrase, not a short category name: prefer a verb + object shape such as "Create and update OpenStreetMap maps" or "Research current web sources". The `description` is what makes the agent decide to read it, so make it specific. An agent restricts which skills it sees via its `skills:` list (empty means all). Skills reload live.

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

`enabled: false` keeps an entry but turns it off; `"type"` is accepted as an alias for `"transport"`. Servers default to `stateful: true`: for `stdio` the subprocess stays alive across calls; for `streamable_http` the MCP session id is preserved and the server's GET SSE stream is listened to. MCP progress/notification events are forwarded into the active A2A stream. Set `stateful: false` only for servers that require one fresh session per operation. **`mcp.json` is watched and reloads live** — the daemon watches the `.agents` roots recursively, so adding or changing a server takes effect without restarting anything. Discovery and connection live in `src/daisy/base/mcp_client.py`. Note that the daemon keeps its own pool for the GUI's server browser while each session connects its own for its tool calls: connections are stateful, and a stdio server cannot be shared across processes.

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

## What reaches a running session, and what does not

The daemon watches the configuration file and the `.agents` roots, so **everything reloads live** — agents, skills, memories, `mcp.json`, `remote-agents.json`, credentials, and the sandbox, computer-control and user-context toggles, whether changed through `daisy configure`, the Settings dialog, or a hand edit. A change that affects a session's runtime asks live sessions to rebuild it on their next turn.

Two things are deliberately **not** live, because they are fixed when a session is created and cannot be widened afterwards: its **permission mode** and its **working directory**. Changing the configured defaults affects the next session, never a running one. That is the guarantee, not a limitation — a session's authority is decided once, in the open, at `daisy create`.

## Verifying a change

- Agents and skills: `daisy create --agent <name>` refuses an unknown profile and lists the ones that exist. A session's `create_session` tool enumerates the same catalogue in its schema, so an unknown name cannot be asked for at all. The GUI reads it from `GET /agents/cards`.
- Configuration: `daisy configure --all` prints every setting the schema defines as a JSON object of dotted path to `{about, default, current}`; `daisy configure` alone prints only what has been changed. Credentials are included in both — it reads a file the user owns.
- Providers and models: `GET /models` lists them grouped by provider; a provider's models unlock once its key resolves.
- MCP: `GET /mcp/tools?working_directory=<path>` lists the servers and tools that folder sees. An unreachable server is listed with no tools and an `error` rather than failing the call.
- End to end: `daisy create`, then `daisy send <id> "…" --wait`. A missing key fails the turn with a credentials error rather than hanging.
