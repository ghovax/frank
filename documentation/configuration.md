# Configuration

Runtime configuration lives in **`$XDG_CONFIG_HOME/xeac/configuration.yaml`** (`~/.config/xeac/configuration.yaml` unless you have set `XDG_CONFIG_HOME`). It is created on first run from a built-in template and is the source of truth for credentials, permissions, and feature toggles. The repository never contains a filled-in copy.

Three ways to change it, all writing the same file:

- `xeac configure` from the terminal — `xeac configure` lists everything, `xeac configure <setting>` reads one, `xeac configure <setting> <value>` sets it, `xeac configure <setting> --unset` removes it. A value the schema would reject is refused with the reason rather than written;
- **Settings** in the desktop app;
- editing the file directly, which the daemon watches and picks up live.

This document is the reference for the file itself.

> [!IMPORTANT]
> Every credential can also be set through an environment variable, which takes precedence over the file. That lets you run a daemon without writing any secret to disk. Never commit a filled-in configuration or a `.env` — see [Security notes](../SECURITY.md).

A change applies to whatever starts **next**. A running session keeps the configuration it was built with — the same guarantee its permission mode carries — except for settings the daemon explicitly pushes out (the sandbox, computer control, and the user-context snapshot each ask live sessions to rebuild).

A fully-commented template lives at [Example configuration](../configuration.example.yaml).

## Where everything lives

XEAC follows the XDG Base Directory convention rather than one dot-directory:

| Path | What is there |
|------|---------------|
| `$XDG_CONFIG_HOME/xeac/` | `configuration.yaml` |
| `$XDG_DATA_HOME/xeac/` | `history.db`, uploads, the file-URL signing secret |
| `$XDG_STATE_HOME/xeac/` | logs |
| `$XDG_CACHE_HOME/xeac/` | caches |
| `$XDG_RUNTIME_DIR/xeac/` | the daemon's socket, port and token, and one socket per session |

The runtime directory is `0700` and the token files inside it `0600`: on a shared machine, file permissions are what keep another user out of your sessions. When `XDG_RUNTIME_DIR` is unset — as on macOS — the fallback is a per-user directory under the system temporary directory.

## Model providers

Set an `api_key` for the providers you use. Most resolve through LiteLLM's built-in endpoints; any OpenAI-compatible provider may also set `base_url`.

```yaml
providers:
  anthropic:   { api_key: "" }      # env: ANTHROPIC_API_KEY
  openai:      { api_key: "" }      # env: OPENAI_API_KEY
  google:      { api_key: "" }      # env: GOOGLE_GENERATIVE_AI_API_KEY or GEMINI_API_KEY
  openrouter:  { api_key: "" }      # env: OPENROUTER_API_KEY
  xai:         { api_key: "" }      # env: XAI_API_KEY
  deepseek:    { api_key: "" }      # env: DEEPSEEK_API_KEY
  groq:        { api_key: "" }      # env: GROQ_API_KEY
  mistral:     { api_key: "" }      # env: MISTRAL_API_KEY
  opencode:    { api_key: "", base_url: "https://opencode.ai/zen/go/v1" }
  custom:      { api_key: "", base_url: "" }   # any OpenAI-compatible endpoint
```

Around forty providers are registered, including Cerebras, Together, Fireworks, Perplexity, Moonshot, Nebius, Cloudflare and GitHub Copilot; the registry in `src/xeac/base/providers.py` is the full list, with the environment variable each one reads.

You can also **sign in with a ChatGPT subscription** instead of pasting a key (Settings → Providers). That provider is not a LiteLLM route: it calls Codex's endpoint directly with an OAuth token from the shared token store.

**Which model a session uses** is not set here — it belongs to the agent profile, in that agent's `configuration.json` under `preset`. See [Agents and skills](agents-and-skills.md#agents). A profile pinned to a provider you have no credentials for fails on its first call rather than borrowing another profile's model: an agent is defined by its own configuration and nothing else.

## Web search and retrieval

```yaml
exa:       { api_key: "" }          # search_web — env: EXA_API_KEY
jina:      { api_key: "" }          # fetch_url, free tier — env: JINA_API_KEY
firecrawl: { api_key: "", api_url: "" }  # env: FIRECRAWL_API_KEY, FIRECRAWL_API_URL
web_fetch: { proxy_url: "" }        # outbound proxy — env: XEAC_FETCH_PROXY
```

`fetch_url` uses a tiered engine: Jina Reader first, then Firecrawl, then a direct fetch. Each tier is optional; an unset key skips it. `proxy_url` overrides the standard `HTTPS_PROXY`/`ALL_PROXY` for the fetch and download tools only.

## Hosted integrations

```yaml
composio:
  enabled: false
  url: "https://connect.composio.dev/mcp"
  api_key: ""                       # env: COMPOSIO_API_KEY
  server_name: "composio"
  timeout_seconds: 60
```

When enabled, Composio is folded into the ordinary MCP set rather than being a second path, so tool gating and the client both see it as just another server.

## Execution and permissions

```yaml
sandbox:   { enabled: true }         # confine bash to the active workspace
workspace: { strategy: "none", artifact_maximum_bytes: 134217728 }
agent:     { permission_mode: "default" }
computer_control: { enabled: false } # macOS screen tools (control_screen); opt-in
user_context:     { enabled: false } # a snapshot of how you work, in the prompt; opt-in
```

`workspace.strategy` is one of `none`, `branch`, or `worktree`, and is resolved once when a session is created: a `worktree` session runs its tools in its own git worktree, so parallel sessions on one repository do not tread on each other.

`agent.permission_mode` is the mode a session gets when none is asked for. It is a default, not a ceiling — `xeac create --mode` overrides it, and a child is clamped against its parent either way.

### Permission modes

| Mode | Behaviour |
|------|-----------|
| `default` | Ask before risky actions; allow safe ones. |
| `auto` | Auto-approve low-risk actions, ask for the rest. |
| `read_only` | Allow reads; deny writes and side effects. |

There is **no bypass mode**, and no standing "always allow": the only runtime decisions are allow-once and deny. A session's mode is fixed when it is created and cannot be changed afterwards, and a session created by another can never be looser than its parent.

Bash additionally honours per-command rules on each agent (`sudo *: deny`, `rm *: ask`, …) — see [Agents and skills](agents-and-skills.md).

## Conversation compaction

```yaml
compaction:
  auto: true                        # compact automatically as the context fills
  observer_context_fraction: 0.6
  reflector_observation_fraction: 0.3
  keep_recent_turns: 6              # turns kept verbatim after a compaction
```

## Tool tuning

How much of a model's context tool output may occupy, and how patient the tools are. Raise `timeout_scale` on a slow machine or a slow network; lower the fractions to spend less context on tool results.

```yaml
tuning:
  output_fraction: 0.25             # share of context one tool result may fill
  listing_fraction: 0.15            # share for a directory or search listing
  settle_interval_seconds: 0.05     # how often a screen action re-checks for settling
  settle_ceiling_seconds: 1.5       # how long it waits before giving up on settling
  timeout_scale: 1.0                # multiplier over every tool's default timeout
```

## MCP servers

`mcp.servers` mirrors what `.agents/mcp.json` declares and is normally edited there — see [Agents and skills](agents-and-skills.md#mcp-servers). A folder's own servers are added to the shared pool when a session in that folder starts; the pool only ever grows, so no other session loses its servers.

## Remote peers

```yaml
remote_agents:
  agents: {}                        # normally written to .agents/remote-agents.json
```

Agents on other hosts, resolved by their A2A card and reached with `xeac remote`. Normally registered in `~/.agents/remote-agents.json` or from Settings rather than written here. A remote agent is not a session — XEAC does not own its lifecycle, cannot set its permission mode, and keeps no transcript of it — which is why it has its own verb rather than sharing `send`.

## Inbound authentication

Only relevant if you expose a daemon beyond loopback. The daemon's own surfaces are already gated by its capability token; this configures A2A's inbound auth on top.

```yaml
a2a:
  api_key: ""
  api_key_header: "X-API-Key"
  oauth2_jwks_url: ""
  oauth2_issuer: ""
  oauth2_audience: ""
```

## Telemetry

Off by default. When enabled, spans and token usage are exported over OTLP to an endpoint you choose — XEAC ships nothing anywhere on its own.

```yaml
telemetry:
  enabled: false
  exporter: { endpoint: "", protocol: "http/protobuf", headers: {} }
  sample_ratio: 1.0
```

## History

```yaml
maximum_history_age_days: 30
```

How long a finished session's transcript is kept.

**There is no default agent setting**, here or anywhere. `xeac create --agent` is required, and no profile is nominated as the one to fall back to — a default would mean work running under an agent nobody chose, and would make every other profile's behaviour depend on that one. Which agent runs is always stated. Add your own under `~/.agents/agents/<id>/` or `.agents/agents/<id>/` in a working directory — see [Agents and skills](agents-and-skills.md).
