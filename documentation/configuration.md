# Configuration

Runtime configuration lives in **`$XDG_CONFIG_HOME/frank/configuration.yaml`** (`~/.config/frank/configuration.yaml` unless you have set `XDG_CONFIG_HOME`). It is created on first run from a built-in template and is the source of truth for credentials, permissions, and feature toggles. The repository never contains a filled-in copy.

Three ways to change it, all writing the same file:

- `frank configure` from the terminal:

  - `frank configure --all` lists every setting that exists, with what it is for, what it ships at, and what this machine runs on.
  - `frank configure` alone lists only what you changed.
  - `frank configure <setting>` reads one setting.
  - `frank configure <setting> <value>` sets it, and `--unset` removes it.
 A name the schema does not define, or a value it would reject, is refused with the reason rather than written;
- **Settings** in the desktop app;
- editing the file directly, which the daemon watches and picks up live.

This document is the reference for the file itself.

> [!IMPORTANT]
> Every credential can also be set through an environment variable, which takes precedence over the file. That lets you run a daemon without writing any secret to disk. Never commit a filled-in configuration or a `.env` — see [Security notes](../SECURITY.md).

A change applies to whatever starts **next**. A running session keeps the configuration it was built with. That is the same guarantee its permission mode carries. Some settings are the exception: the daemon pushes them out, and the sandbox, computer control, and the user-context snapshot each ask live sessions to rebuild.

**`frank configure --all` is the complete reference.** It prints every setting the schema defines. Each one shows what it is for, what it ships at, and what your machine runs on now. There is deliberately no checked-in file that says the same thing. A second copy of the defaults is a second thing to keep true. The one this repository carried had drifted: it documented renamed settings under their old names, and it missed settings that someone added. The command reads the running code, so it cannot.

This document is the *narrative* — what the settings mean and how they relate. The command is the exhaustive list.

Names the schema does not define are **refused**, not ignored. A setting that cannot take effect should say so where it is written, rather than being discovered when the behaviour never changes.

## Where everything lives

Frank follows the XDG Base Directory convention rather than one dot-directory:

| Path | What is there |
|------|---------------|
| `$XDG_CONFIG_HOME/frank/` | `configuration.yaml` |
| `$XDG_DATA_HOME/frank/` | `history.db`, uploads, the file-URL signing secret |
| `$XDG_STATE_HOME/frank/` | logs |
| `$XDG_CACHE_HOME/frank/` | caches |
| `$XDG_RUNTIME_DIR/frank/` | the daemon's socket, port and token, and one socket per session |

The runtime directory is `0700`, and the token files inside it are `0600`. On a shared machine, file permissions keep another user out of your sessions. When `XDG_RUNTIME_DIR` is unset — as on macOS — the fallback is a per-user directory under the system temporary directory.

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

Around forty providers are registered. They include Cerebras, Together, Fireworks, Perplexity, Moonshot, Nebius, Cloudflare and GitHub Copilot. The registry in `src/frank/base/providers.py` is the full list, with the environment variable each one reads.

You can also **sign in with a ChatGPT or a Cursor subscription** instead of pasting a key (Settings → Providers). Neither is a LiteLLM route, and neither appears in the block above, because neither has a key to store. `chatgpt` calls Codex's Responses endpoint with an OAuth token. `cursor` calls Cursor's agent service with one. Both live in the data directory's `oauths/` folder, one file per provider: `oauths/chatgpt.json` and `oauths/cursor.json`. They are written mode 0600, inside a 0700 directory.

They stay out of `configuration.yaml` deliberately. That file is digest-synced, and it would thrash on every silent token refresh.

Nothing reads any older location. An upgrade from a version that kept tokens elsewhere therefore signs you out once. Sign in again, and the token lands in the folder. Which models each plan actually serves is discovered live from the account, so a model the plan does not include stays greyed in the picker. The `cursor` provider lists nothing until you sign in. Its models, their names, and their context windows all come from the account. No list of them ships in the code. Both are unofficial routes that the vendor can withdraw at any time.

**Which model a session uses** is not set here — it belongs to the agent profile, in that agent's `configuration.json` under `preset`. See [Agents and skills](agents-and-skills.md#agents). A profile pinned to a provider you have no credentials for fails on its first call. It does not borrow another profile's model. Its own configuration defines an agent, and nothing else does.

## Web search and retrieval

```yaml
exa:       { api_key: "" }          # search_web — env: EXA_API_KEY
jina:      { api_key: "" }          # fetch_url, free tier — env: JINA_API_KEY
firecrawl: { api_key: "", api_url: "" }  # env: FIRECRAWL_API_KEY, FIRECRAWL_API_URL
web_fetch: { proxy_url: "" }        # outbound proxy — env: FRANK_FETCH_PROXY
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

When you enable Composio, it joins the ordinary MCP set. It is not a second path. Tool gating and the client both see it as another server.

## Execution and permissions

```yaml
sandbox:   { enforce: "required" }   # what a tool child may do — see below
workspace: { strategy: "none" }
agent:     { permission_mode: "default" }
computer_control: { enabled: false } # macOS screen tools (control_screen); opt-in — see below
user_context:     { enabled: false } # a snapshot of how you work, in the prompt; opt-in
```

### Confinement

What a session's tool children may do: a `bash` command, or a `control_screen` script. The operating system enforces this. The harness does not infer it from the text of a command.

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

Almost every field is a Unix primitive under its own name. `limits` are [`setrlimit(2)`](https://man7.org/linux/man-pages/man2/setrlimit.2.html) constants, and they take the integers that call takes. `umask` is `umask(2)`, and `nice` is `nice(2)`. Only the filesystem and the network have no POSIX spelling, and they are the two that need a platform behind them.

**The filesystem.** The system stays readable — `/usr` and `/etc` are not secrets, and denying them breaks every command while protecting nothing. The lists govern *your home*, which is closed by default. `readable` is the allowlist that keeps toolchains working. `writable` is narrower still, and `deny` wins over both.

The shipped defaults keep credential and configuration directories readable. To break `git push` in order to protect a key is a bad trade. What the defaults close is the personal data that no toolchain touches. `$WORKSPACE` is the session's own directory.

**The backend.** macOS uses [`sandbox-exec`](https://keith.github.io/xcode-man-pages/sandbox-exec.1.html) with a generated Seatbelt profile; Linux uses [Landlock](https://docs.kernel.org/userspace-api/landlock.html) plus a network namespace. Apple has **deprecated `sandbox-exec` since 10.15**, and Frank depends on it anyway. Nothing else on macOS confines a single child process:

- App Sandbox applies to a whole signed application. It would confine the harness out of the files it exists to reach.
- Endpoint Security observes; it does not bound.
- A separate uid, or a container, stops the agent from acting as you. If Apple removes it, the boot-time probe fails and `enforce` decides what happens — which is why that setting exists.

**`enforce`.** `required` (the default) refuses to create a session when no backend is available, naming what is missing. `preferred` runs with the POSIX half only — limits, mask, priority, a scoped environment — which is hygiene, not a boundary. `off` does not confine. The daemon logs which backend it found at startup, and a machine with none says so before the first session fails.

The harness resolves a session's confinement when it **creates** the session, and nothing widens it afterwards. That is exactly like its permission mode.

It also clamps the confinement against the session that created it. Path sets intersect, so a peer never gets a wider filesystem than its creator holds. An agent profile may narrow it further with its own `sandbox:` block.

> [!NOTE]
> Commands run against a **remote location** are not confined: they execute on another machine, where a boundary drawn by this process has no meaning.

`workspace.strategy` is one of `none`, `branch`, or `worktree`. The harness resolves it once, when it creates the session. A `worktree` session runs its tools in its own git worktree, so parallel sessions on one repository do not tread on each other.

`agent.permission_mode` is the mode a session gets when none is asked for. It is a default, not a ceiling — `frank create --mode` overrides it, and a child is clamped against its parent either way.

### Permission modes

| Mode | Behaviour |
|------|-----------|
| `default` | Ask before risky actions; allow safe ones. |
| `auto` | Auto-approve low-risk actions, ask for the rest. |
| `read_only` | Allow reads; deny writes and side effects. |

There is **no bypass mode**, and no standing "always allow": the only runtime decisions are allow-once and deny. A session's mode is fixed when the harness creates it, and nothing changes it afterwards. A session created by another is never looser than its parent.

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

How much of a model's context tool output may occupy, and how patient the tools are. Size and count caps are token budgets, derived from the **live** model context window. A small model therefore gets tight caps, and a large one gets room. `context_share` says what proportion of that window one result may fill. Timeouts do not depend on the window and answer only to `timeout_multiplier`.

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

Those three move whole families. `defaults` is the escape hatch for a single value. Its keys are the names in `frank.base.tuning.Tunable`, which is the same idea as `sandbox.limits` using `setrlimit` constant names. An unknown name is an error at load. It is not a line that looks applied and is not. An override replaces the value the code *ships with*, so `context_share` and `timeout_multiplier` still apply on top: `action_timeout_ms: 10000` under `timeout_multiplier: 2.0` resolves to twenty seconds.

The names are lowercase because they are not constants. Each one is a default the file may replace, and the casing is the first thing that says so.

`frank configure --all` lists every tunable with what it is for, what it ships at, and what this machine currently runs on.

Settling — how long a screen surface is given to stop changing after an action — lives with the surface rather than here, under [`computer_control.settle`](#screen-control).

## Screen control

```yaml
computer_control:
  enabled: false                    # drive native macOS apps and your own Chrome; opt-in
  settle:
    poll_seconds: 0.05              # how often to re-check whether the surface has settled
    give_up_seconds: 1.5            # the longest to wait before reading it anyway
```

After an action, the harness *polls* a surface until it stops changing. It does not sleep for a fixed guess. A fast page therefore costs one interval, and a slow one costs the ceiling. These two sit here rather than under `tuning` because settling is something a **surface** does, not a budget a tool spends.

## MCP servers

`mcp.servers` mirrors what `.agents/mcp.json` declares and is normally edited there — see [Agents and skills](agents-and-skills.md#mcp-servers). A folder's own servers join the shared pool when a session in that folder starts. The pool only grows, so no other session loses its servers.

## Remote peers

```yaml
remote_agents:
  agents: {}                        # normally written to .agents/remote-agents.json
```

Agents on other hosts, resolved by their A2A card and reached with `frank remote`. Normally registered in `~/.agents/remote-agents.json` or from Settings rather than written here. A remote agent is not a session. Frank does not own its lifecycle, cannot set its permission mode, and keeps no transcript of it. It therefore has its own verb, and does not share `send`.

## Telemetry

Off by default. When enabled, spans and token usage are exported over OTLP to an endpoint you choose — Frank ships nothing anywhere on its own.

```yaml
telemetry:
  enabled: false
  exporter: { endpoint: "", protocol: "http/protobuf", headers: {} }
  sample_ratio: 1.0
```

**There is no default agent setting**, here or anywhere. `frank create --agent` is required, and no profile is the one to fall back to. A default would run work under an agent nobody chose. It would also make every other profile's behaviour depend on that one. Which agent runs is always stated. Add your own under `~/.agents/agents/<id>/` or `.agents/agents/<id>/` in a working directory — see [Agents and skills](agents-and-skills.md).
