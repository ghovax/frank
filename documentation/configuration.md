# Configuration

Runtime configuration lives in **`$XDG_CONFIG_HOME/daisy/configuration.yaml`** (`~/.config/daisy/configuration.yaml` unless you have set `XDG_CONFIG_HOME`). It is created on first run from a built-in template and is the source of truth for credentials, permissions, and feature toggles. The repository never contains a filled-in copy.

Three ways to change it, all writing the same file:

- `daisy configure` from the terminal — `daisy configure --all` lists every setting that exists with what it is for, what it ships at, and what this machine runs on; `daisy configure` alone lists only what you have changed; `daisy configure <setting>` reads one; `daisy configure <setting> <value>` sets it; `daisy configure <setting> --unset` removes it. A name the schema does not define, or a value it would reject, is refused with the reason rather than written;
- **Settings** in the desktop app;
- editing the file directly, which the daemon watches and picks up live.

This document is the reference for the file itself.

> [!IMPORTANT]
> Every credential can also be set through an environment variable, which takes precedence over the file. That lets you run a daemon without writing any secret to disk. Never commit a filled-in configuration or a `.env` — see [Security notes](../SECURITY.md).

A change applies to whatever starts **next**. A running session keeps the configuration it was built with — the same guarantee its permission mode carries — except for settings the daemon explicitly pushes out (the sandbox, computer control, and the user-context snapshot each ask live sessions to rebuild).

**`daisy configure --all` is the complete reference.** It prints every setting the schema defines, each with what it is for, what it ships at, and what your machine currently runs on. There is deliberately no checked-in file saying the same thing: a second copy of the defaults is a second thing to keep true, and the one this repository used to carry had drifted — documenting renamed settings under their old names and missing ones that had been added. The command reads the running code, so it cannot.

This document is the *narrative* — what the settings mean and how they relate. The command is the exhaustive list.

Names the schema does not define are **refused**, not ignored. A setting that cannot take effect should say so where it is written, rather than being discovered when the behaviour never changes.

## Where everything lives

Daisy follows the XDG Base Directory convention rather than one dot-directory:

| Path | What is there |
|------|---------------|
| `$XDG_CONFIG_HOME/daisy/` | `configuration.yaml` |
| `$XDG_DATA_HOME/daisy/` | `history.db`, uploads, the file-URL signing secret |
| `$XDG_STATE_HOME/daisy/` | logs |
| `$XDG_CACHE_HOME/daisy/` | caches |
| `$XDG_RUNTIME_DIR/daisy/` | the daemon's socket, port and token, and one socket per session |

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

Around forty providers are registered, including Cerebras, Together, Fireworks, Perplexity, Moonshot, Nebius, Cloudflare and GitHub Copilot; the registry in `src/daisy/base/providers.py` is the full list, with the environment variable each one reads.

You can also **sign in with a ChatGPT or a Cursor subscription** instead of pasting a key (Settings → Providers). Neither is a LiteLLM route and neither appears in the block above, because neither has a key to store: `chatgpt` calls Codex's Responses endpoint with an OAuth token from `~/.daisy/chatgpt_auth.json`, and `cursor` calls Cursor's agent service with one from `~/.daisy/cursor_auth.json`. Both files are written mode 0600 and kept out of `configuration.yaml` deliberately — that file is digest-synced and would thrash on every silent token refresh. Which models each plan actually serves is discovered live from the account, so a model the plan does not include stays greyed in the picker; and both are unofficial routes that the vendor can withdraw at any time.

**Which model a session uses** is not set here — it belongs to the agent profile, in that agent's `configuration.json` under `preset`. See [Agents and skills](agents-and-skills.md#agents). A profile pinned to a provider you have no credentials for fails on its first call rather than borrowing another profile's model: an agent is defined by its own configuration and nothing else.

## Web search and retrieval

```yaml
exa:       { api_key: "" }          # search_web — env: EXA_API_KEY
jina:      { api_key: "" }          # fetch_url, free tier — env: JINA_API_KEY
firecrawl: { api_key: "", api_url: "" }  # env: FIRECRAWL_API_KEY, FIRECRAWL_API_URL
web_fetch: { proxy_url: "" }        # outbound proxy — env: DAISY_FETCH_PROXY
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
sandbox:   { enforce: "required" }   # what a tool child may do — see below
workspace: { strategy: "none", artifact_maximum_bytes: 134217728 }
agent:     { permission_mode: "default" }
computer_control: { enabled: false } # macOS screen tools (control_screen); opt-in — see below
user_context:     { enabled: false } # a snapshot of how you work, in the prompt; opt-in
```

### Confinement

What a session's tool children — a `bash` command, a `control_screen` script — may actually do, enforced by the operating system rather than inferred from the text of a command.

```yaml
sandbox:
  enforce: required              # required | preferred | off
  filesystem:
    readable: ["~/.config", "~/.ssh", "~/.gitconfig", "~/.cargo", "~/.npmrc"]
    writable: ["$WORKSPACE", "$TMPDIR", "$XDG_CACHE_HOME"]
    deny:     ["~/Documents", "~/Desktop", "~/Downloads", "~/Library/Mail"]
  network: true
  limits:                        # POSIX rlimits, by their own names, in their own units
    RLIMIT_CORE: 0
    RLIMIT_FSIZE: 8589934592
    RLIMIT_NPROC: 2048
  umask: "0077"
  nice: 0
```

Almost every field is a Unix primitive under its own name: `limits` are [`setrlimit(2)`](https://man7.org/linux/man-pages/man2/setrlimit.2.html) constants taking the integers that call takes, `umask` is `umask(2)`, `nice` is `nice(2)`. Only the filesystem and the network have no POSIX spelling, and they are the two that need a platform behind them.

**The filesystem.** The system stays readable — `/usr` and `/etc` are not secrets, and denying them breaks every command while protecting nothing. What the lists govern is *your home*, which is closed by default: `readable` is the allowlist that keeps toolchains working, `writable` is narrower still, and `deny` wins over both. The shipped defaults keep credential and configuration directories readable, because breaking `git push` to protect a key is a bad trade; what they close is the personal data no toolchain touches. `$WORKSPACE` is the session's own directory.

**The backend.** macOS uses [`sandbox-exec`](https://keith.github.io/xcode-man-pages/sandbox-exec.1.html) with a generated Seatbelt profile; Linux uses [Landlock](https://docs.kernel.org/userspace-api/landlock.html) plus a network namespace. `sandbox-exec` has been **deprecated by Apple since 10.15** and is depended on anyway, because nothing else on macOS confines a single child process: App Sandbox applies to a whole signed application and would confine the harness out of the files it exists to reach, Endpoint Security observes rather than bounds, and a separate uid or a container stops the agent being able to act as you. If Apple removes it, the boot-time probe fails and `enforce` decides what happens — which is why that setting exists.

**`enforce`.** `required` (the default) refuses to create a session when no backend is available, naming what is missing. `preferred` runs with the POSIX half only — limits, mask, priority, a scoped environment — which is hygiene, not a boundary. `off` does not confine. The daemon logs which backend it found at startup, and a machine with none says so before the first session fails.

A session's confinement is resolved when it is **created** and cannot be widened afterwards, exactly like its permission mode — and it is clamped against the session that created it, so path sets intersect and a peer can never be handed a wider filesystem than its creator holds. An agent profile may narrow it further with its own `sandbox:` block.

> [!NOTE]
> Commands run against a **remote location** are not confined: they execute on another machine, where a boundary drawn by this process has no meaning.

`workspace.strategy` is one of `none`, `branch`, or `worktree`, and is resolved once when a session is created: a `worktree` session runs its tools in its own git worktree, so parallel sessions on one repository do not tread on each other.

`agent.permission_mode` is the mode a session gets when none is asked for. It is a default, not a ceiling — `daisy create --mode` overrides it, and a child is clamped against its parent either way.

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

How much of a model's context tool output may occupy, and how patient the tools are. Size and count caps are token budgets derived from the **live** model context window, so a small model gets tight caps and a large one gets room; `context_share` says what proportion of that window one result may fill. Timeouts do not depend on the window and answer only to `timeout_multiplier`.

```yaml
tuning:
  context_share:
    text: 0.25                      # share one result's text may fill — output, fetched pages
    results: 0.15                   # share a set of results may fill — matches, lines, records
  timeout_multiplier: 1.0           # 2.0 doubles every wait for a slow machine; 1.0 is neutral
  defaults:                         # override one value, by its own name and in its own unit
    action_timeout_ms: 10000
    grep_results: 1024
```

Those three move whole families. `defaults` is the escape hatch for a single value: the keys are the names in `daisy.base.tuning.Tunable` — the same idea as `sandbox.limits` using `setrlimit` constant names — and an unknown name is an error at load rather than a line that looks applied and is not. An override replaces the value the code *ships with*, so `context_share` and `timeout_multiplier` still apply on top: `action_timeout_ms: 10000` under `timeout_multiplier: 2.0` resolves to twenty seconds.

The names are lowercase because they are not constants. Each one is a default the file may replace, and the casing is the first thing that says so.

`daisy configure --all` lists every tunable with what it is for, what it ships at, and what this machine currently runs on.

Settling — how long a screen surface is given to stop changing after an action — lives with the surface rather than here, under [`computer_control.settle`](#screen-control).

## Screen control

```yaml
computer_control:
  enabled: false                    # drive native macOS apps and your own Chrome; opt-in
  settle:
    poll_seconds: 0.05              # how often to re-check whether the surface has settled
    give_up_seconds: 1.5            # the longest to wait before reading it anyway
```

After an action, a surface is *polled* until it stops changing rather than slept on for a fixed guess: a fast page costs one interval and a slow one costs the ceiling. These two sit here rather than under `tuning` because settling is something a **surface** does, not a budget a tool spends.

## The daemon

```yaml
daemon:
  warm_floor: 2                     # blank workers parked, so creating a session is a socket write
  warm_ceiling: 8                   # stop pre-warming once this many workers exist in total
```

`warm_ceiling` counts warm *and* assigned workers together and bounds pre-warming, not concurrency — a claim against an empty pool spawns on demand and never consults it, so a wide fan-out is always served; it just pays a cold start per child past the spares.

## MCP servers

`mcp.servers` mirrors what `.agents/mcp.json` declares and is normally edited there — see [Agents and skills](agents-and-skills.md#mcp-servers). A folder's own servers are added to the shared pool when a session in that folder starts; the pool only ever grows, so no other session loses its servers.

## Remote peers

```yaml
remote_agents:
  agents: {}                        # normally written to .agents/remote-agents.json
```

Agents on other hosts, resolved by their A2A card and reached with `daisy remote`. Normally registered in `~/.agents/remote-agents.json` or from Settings rather than written here. A remote agent is not a session — Daisy does not own its lifecycle, cannot set its permission mode, and keeps no transcript of it — which is why it has its own verb rather than sharing `send`.

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

Off by default. When enabled, spans and token usage are exported over OTLP to an endpoint you choose — Daisy ships nothing anywhere on its own.

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

**There is no default agent setting**, here or anywhere. `daisy create --agent` is required, and no profile is nominated as the one to fall back to — a default would mean work running under an agent nobody chose, and would make every other profile's behaviour depend on that one. Which agent runs is always stated. Add your own under `~/.agents/agents/<id>/` or `.agents/agents/<id>/` in a working directory — see [Agents and skills](agents-and-skills.md).
