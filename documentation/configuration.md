# Configuration

Runtime configuration lives in **`$XDG_CONFIG_HOME/frank/configuration.yaml`** (`~/.config/frank/configuration.yaml` unless you have set `XDG_CONFIG_HOME`). It is created on first run from a built-in template and is the source of truth for credentials, permissions, and feature toggles. The repository never contains a filled-in copy.

Three ways to change it, all writing the same file:

- `frank configure` from the terminal:

- `frank configure --all` lists every setting that exists, with what it ships at and what this machine runs on.
- `frank configure` alone lists only what you changed.
- `frank configure <setting>` reads one setting.
- `frank configure <setting> <value>` sets it, and `--unset` removes it. A name the schema does not define, or a value it would reject, is refused with the reason rather than written;
- **Settings** in the desktop app;
- editing the file directly, which the next thing to start reads.

This document is the reference for the file itself.

> [!IMPORTANT]
> Every credential can also be set through an environment variable, which takes precedence over the file. That lets you run a daemon without writing any secret to disk. Never commit a filled-in configuration or a `.env` — see [Security notes](../SECURITY.md).

A change applies to whatever starts **next**. A running session keeps the configuration it was built with. That is the same guarantee its permission mode carries. Some settings are the exception: the daemon pushes them out, and the sandbox, computer control, and the user-context snapshot each ask live sessions to rebuild.

Three places say something about a setting, and each says a different thing. **This document** is the narrative: what the settings mean and how they relate, for the ones worth explaining at length. The **[configuration reference](configuration-reference.md)** is the list: every setting there is, its type, what it ships at, and what it is for, in one row each. **`frank configure`** reads the running code, so it is the only one that can tell you what *this machine* is set to.

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
  anthropic:   { api_key: "" }
  openai:      { api_key: "" }
  google:      { api_key: "" }
  openrouter:  { api_key: "" }
  xai:         { api_key: "" }
  deepseek:    { api_key: "" }
  groq:        { api_key: "" }
  mistral:     { api_key: "" }
  opencode:    { api_key: "", base_url: "https://opencode.ai/zen/go/v1" }
  custom:      { api_key: "", base_url: "" }
```

Each provider also reads an environment variable, which takes precedence over the file:

| Provider | Environment variable |
|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `google` | `GOOGLE_GENERATIVE_AI_API_KEY`, or `GEMINI_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `xai` | `XAI_API_KEY` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `groq` | `GROQ_API_KEY` |
| `mistral` | `MISTRAL_API_KEY` |

`custom` takes any OpenAI-compatible endpoint, which is why it needs a `base_url` as well.

Around forty providers are registered. They include Cerebras, Together, Fireworks, Perplexity, Moonshot, Nebius, Cloudflare and GitHub Copilot. The registry in `src/frank/base/providers.py` is the full list, with the environment variable each one reads.

You can also **sign in with a ChatGPT or a Cursor subscription** instead of pasting a key (in Settings, under Providers). Neither is a LiteLLM route, and neither appears in the block above, because neither has a key to store. `chatgpt` calls Codex's Responses endpoint with an OAuth token. `cursor` calls Cursor's agent service with one. Both live in the data directory's `oauths/` folder, one file per provider: `oauths/chatgpt.json` and `oauths/cursor.json`. They are written mode 0600, inside a 0700 directory.

They stay out of `configuration.yaml` deliberately. That file is digest-synced, and it would thrash on every silent token refresh.



Which models each plan actually serves is discovered live from the account, so a model the plan does not include stays greyed in the picker. The `cursor` provider lists nothing until you sign in. Its models, their names, and their context windows all come from the account. No list of them ships in the code.

Both are unofficial routes that the vendor can withdraw at any time.

**Which model a session uses** is not set here — it belongs to the agent profile, in that agent's `AGENT.md` frontmatter (`model` and `provider`). See [Agents and skills](agents-and-skills.md#agents). A profile pinned to a provider you have no credentials for fails on its first call. It does not borrow another profile's model. Its own configuration defines an agent, and nothing else does.

## Web search and retrieval

```yaml
exa:       { api_key: "" }
jina:      { api_key: "" }
firecrawl: { api_key: "", api_url: "" }
web_fetch: { proxy_url: "" }
```

| Setting | What it serves | Environment variable |
|---|---|---|
| `exa` | `search_web` | `EXA_API_KEY` |
| `jina` | `fetch_url`, on the free tier | `JINA_API_KEY` |
| `firecrawl` | `fetch_url` | `FIRECRAWL_API_KEY`, `FIRECRAWL_API_URL` |
| `web_fetch` | An outbound proxy | `FETCH_PROXY` |

`fetch_url` uses a tiered engine: Jina Reader first, then Firecrawl, then a direct fetch. Each tier is optional; an unset key skips it. `proxy_url` overrides the standard `HTTPS_PROXY`/`ALL_PROXY` for the fetch and download tools only.

## Hosted integrations

```yaml
composio:
  enabled: false
  url: "https://connect.composio.dev/mcp"
  api_key: ""
  server_name: "composio"
  timeout_seconds: 60
```

`api_key` also reads `COMPOSIO_API_KEY` from the environment.

When you enable Composio, it joins the ordinary MCP set. It is not a second path. Tool gating and the client both see it as another server.

## Execution and permissions

```yaml
sandbox:   { enforce: "required" }
workspace: { strategy: "none" }
agent:     { permission_mode: "ask" }
computer_control: { enabled: false }
user_context:     { enabled: false }
toolbox:          { enabled: true }
```

`sandbox.enforce` sets what a tool child may do, and it is described below. `computer_control` turns on the macOS screen tools (`control_screen`), and it is opt-in; it is also described below. `user_context` puts a snapshot of how you work into the prompt, and it is opt-in too. `toolbox` is whether a session may install the tools it needs, and it is described below.

### The session toolbox

What a session may *reach* is the confinement's question. What a session *has* is this one, and until they were separated they gave the same answer: a missing tool and a forbidden path both came back as `Operation not permitted`, so an agent read a gap in its toolkit as a boundary and went looking for a way around it.

```yaml
toolbox:
  enabled: true
```

With it on, each session gets a package profile of its own at the front of its `PATH`, and `nix profile add nixpkgs#jq` installs into that profile with no flag and no path. Nothing reaches your machine: the packages come from the shared read-only store, what the session owns is a directory of symlinks under `~/.local/state/frank/sessions/<id>`, and that directory is deleted when the session is reaped. Your own profile is never written to, and the confinement is unchanged — a tool a session installed is still refused every path the sandbox refuses.

It needs [Nix](https://nixos.org). On a machine without it there is no toolbox, and the agent is told nothing about installing anything rather than being told about a capability it does not have.

### Confinement

What a session's tool children may do: a `bash` command, or a `control_screen` script. The operating system enforces this. The harness does not infer it from the text of a command.

```yaml
sandbox:
  enforce: required
  filesystem:
    readable:  ["~/.config", "~/.ssh", "~/.gitconfig", "~/.cargo", "~/.npmrc"]
    writable:  ["$WORKSPACE", "$TMPDIR", "/tmp", "$XDG_CACHE_HOME"]
    deny:      ["~/Documents", "~/Desktop", "~/Downloads", "~/Library/Mail"]
    grantable: []
  network: true
  limits:
    RLIMIT_CORE: 0
    RLIMIT_FSIZE: 8589934592
    RLIMIT_NPROC: 2048
  umask: "0077"
  nice: 0
```

`enforce` is one of `required`, `preferred`, or `off`. `limits` are POSIX rlimits, under their own names and in their own units.

Almost every field is a Unix primitive under its own name. `limits` are [`setrlimit(2)`](https://man7.org/linux/man-pages/man2/setrlimit.2.html) constants, and they take the integers that call takes. `umask` is `umask(2)`, and `nice` is `nice(2)`. Only the filesystem and the network have no POSIX spelling, and they are the two that need a platform behind them.

**The filesystem.** The system stays readable — `/usr` and `/etc` are not secrets, and denying them breaks every command while protecting nothing. The lists govern *your home*, which is closed by default. `readable` is the allowlist that keeps toolchains working. `writable` is narrower still, and `deny` wins over both.

The shipped defaults keep credential and configuration directories readable. To break `git push` in order to protect a key is a bad trade. What the defaults close is the personal data that no toolchain touches. `$WORKSPACE` is the session's own directory.

`/tmp` is listed beside `$TMPDIR` because on macOS the two are different places: `$TMPDIR` expands to a per-user directory under `/var/folders`. A writable set that named only `$TMPDIR` refused `/tmp`, which is the scratch path every convention points at and the first one anything reaches for.

**Asking for more.** An agent that needs a path outside these lists asks for it, on the call that needs it, with `access_request`. That request is the *only* thing that raises a prompt: work inside the confinement runs without interrupting anybody, because the boundary is already drawn and the operating system holds it. An approval holds for the rest of that session, and it never reaches a peer: a session it creates clamps against the *configured* profile, not the granted one.

**When a command hits the wall.** A command the operating system refuses is not simply failed. Its first run was confined and could not have been otherwise, so whatever it managed before the refusal it did inside the box — which makes it safe to offer a second run with more reach. Under `ask` you are shown the command and what the refusal looked like; under `auto` the reviewer answers. The offer is "let this one command reach past the workspace", because neither backend reports *which* path it refused, and there is nothing narrower to honestly offer. Your `deny` list still holds through it.

`grantable` lists the paths an agent may be given without a prompt. It is empty by default, so every request is asked about. A path under `deny` is never grantable, whatever `grantable` says — that list is what you declared off-limits before the session started, and nothing decided at runtime reaches past it.

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

A session's mode says **who answers** when a call asks to reach past its confinement. It says nothing about what the session may do — that is the `sandbox` block above, and the operating system enforces it.

| Mode | Behaviour |
|------|-----------|
| `ask` | The person running the session answers. The turn parks until they do. |
| `auto` | A reviewer answers: it allows the request or refuses it, and never asks. For work nobody is watching. A refusal reaches the agent as a refused tool call, with a reason it can work around. |

There is **no bypass mode**, and no standing "always allow": the only runtime decisions are allow-once and deny. A session's mode is chosen when the harness creates it and can be changed afterwards by the person running it; a session can never change its own. A session created by another is never looser than its parent, and tightening a session tightens the subtree it created.

**A read-only session** is not a mode. It is a confinement with nowhere writable — `frank create --read-only`, or a `sandbox:` block on the agent profile that lists no `writable` paths. Nothing about a command's text decides it, so there is no spelling of a write that gets past.

Three tools take per-call rules on each agent, and they are the three whose calls can be named: `bash` by its command (`sudo *: deny`, `rm -rf *: ask`, …), `mcp` by `server.tool` (`*.delete_*: deny`), and `screen` by the primitive a script reaches for (`evaluate: deny`). The longest matching pattern wins. A `deny` refuses the call outright in both modes — a reviewer may not overrule a rule you wrote. See [Agents and skills](agents-and-skills.md).

`bash` ships with a short list of prefixes already set to `ask` or `deny`, because the confinement answers "where can this reach" and not "how much of the workspace survives this": `rm -rf .` is entirely inside the boundary, and so is `git reset --hard`. Your own entry at the same pattern replaces the shipped one, so writing `"rm -rf *": allow` turns it off.

## Conversation compaction

```yaml
compaction:
  automatic: true
  reclaim_at_fraction: 0.85
  condense_log_at_fraction: 0.3
  output_reserve_fraction: 0.1
  recent_working_set_fraction: 0.25
```

When a conversation outgrows the window, the older half is folded into a dense observation log and the recent part is kept word for word:

- `output_reserve_fraction` is held back for the answer the model is about to write; everything else here is a share of what remains, so a fraction means what it says.
- `reclaim_at_fraction` is when the fold runs. It is late on purpose. A fold is the one thing that rewrites the conversation, and a rewritten prefix is a prompt cache thrown away — so it is paid for twice, once in the model calls it takes and again in the full-price re-read on the next call. Held context is cheap by comparison while the cache holds. Folding earlier costs more, and folding later is worse again, because a larger backlog is a larger thing for the Observer to read.
- `recent_working_set_fraction` is how much stays verbatim. Measured in tokens rather than turns, because an unattended run is one instruction and several hundred tool results, and a turn count reads that as nothing worth folding.
- `condense_log_at_fraction` is when the log itself is condensed, once it has grown large enough to be worth rewriting.

## Tool tuning

How much of a model's context tool output may occupy, and how patient the tools are. Size and count caps are token budgets, derived from the **live** model context window. A small model therefore gets tight caps, and a large one gets room. `context_share` says what proportion of that window one result may fill. Timeouts do not depend on the window and answer only to `timeout_multiplier`.

```yaml
tuning:
  context_share:
    text: 0.25
    results: 0.15
  timeout_multiplier: 1.0
  defaults:
    action_timeout_ms: 10000
    grep_results: 1024
```

| Setting | What it does |
|---|---|
| `context_share.text` | The share one result's text may fill: output, fetched pages |
| `context_share.results` | The share a set of results may fill: matches, lines, records |
| `timeout_multiplier` | `2.0` doubles every wait, for a slow machine. `1.0` is neutral |
| `defaults` | Overrides one value, by its own name and in its own unit |

Those three move whole families. `defaults` is the escape hatch for a single value. Its keys are the names in `frank.base.tuning.Tunable`, which is the same idea as `sandbox.limits` using `setrlimit` constant names. An unknown name is an error at load. It is not a line that looks applied and is not. An override replaces the value the code *ships with*, so `context_share` and `timeout_multiplier` still apply on top: `action_timeout_ms: 10000` under `timeout_multiplier: 2.0` resolves to twenty seconds.

The names are lowercase because they are not constants. Each one is a default the file may replace, and the casing is the first thing that says so.

`frank configure --all` lists every setting with what it ships at and what this machine runs on; what each one is *for* is in the [configuration reference](configuration-reference.md), which also carries the longer reasoning behind the eleven tunables that need it. [`configuration.example.yaml`](configuration.example.yaml) is the same surface as a file — every setting that exists, grouped and annotated, at its shipped value. Read it; do not copy it over your own configuration. Everything in it is already the default, so a copy changes nothing now and pins all of it later, which is exactly why the file you get on first run is nearly empty.

Settling — how long a screen surface is given to stop changing after an action — lives with the surface rather than here, under [`computer_control.settle`](#screen-control).

## Screen control

```yaml
computer_control:
  enabled: false
  settle:
    poll_seconds: 0.05
    give_up_seconds: 1.5
  retrieval:
    multilingual_rank_model: "minishlab/M2V_multilingual_output"
    english_rank_model: "minishlab/potion-base-32M"
    lexical_gate_short_words: 3
    lexical_gate_long_words: 7
```

`enabled` drives native macOS apps and your own Chrome, and it is opt-in. `poll_seconds` is how often to re-check whether the surface settled. `give_up_seconds` is the longest to wait before reading it anyway.

After an action, the harness *polls* a surface until it stops changing. It does not sleep for a fixed guess. A fast page therefore costs one interval, and a slow one costs the ceiling. These two sit here rather than under `tuning` because settling is something a **surface** does, not a budget a tool spends.

### How a screen is ranked

`find_one` and `find_many` score every element three ways and add the results: two static embeddings read what the query *means*, and a character similarity reads how it is *spelled*. `retrieval` is where those choices live.

| Setting | What it does |
|---|---|
| `multilingual_rank_model` | Ranks by meaning across languages. Also backs the relevance floor, because a floor needs a score comparable between calls and only a plain cosine is |
| `english_rank_model` | A second embedding, ranked *beside* the first. Better on queries that describe a purpose, weaker on exact labels |
| `lexical_gate_short_words` | At or below this, a query is a quoted label and its spelling counts in full |
| `lexical_gate_long_words` | At or above this, a query is a description and its spelling is ignored. Linear between the two |

The two models are added, not chosen between. Used alone the English one is worse on native windows, where a query usually quotes a label it can see; used together they beat either alone on every surface measured. Clearing a name turns that model off, and clearing both leaves BM25 — which is also how to get the older single-model ranking back if this one suits your work less.

The gate is what keeps the character similarity from doing harm. A short query is a label read off the screen, so its spelling is the strongest evidence available — it is what lets a retyped number, a changed case, or a label the interface truncated still find its element. A long query is a description that shares no spelling with anything, and a character similarity is never silent, so past `lexical_gate_long_words` it is dropped rather than left to rank by coincidence.

`find_one`'s willingness to answer at all is separate, under [`tuning.defaults.find_one_margin`](#tool-tuning) — and it is fitted against *this* ranking, so change one and re-fit the other.

## MCP servers

`mcp.servers` mirrors what `.agents/mcp.json` declares and is normally edited there — see [Agents and skills](agents-and-skills.md#mcp-servers). A folder's own servers join the shared pool when a session in that folder starts. The pool only grows, so no other session loses its servers.

## Remote peers

```yaml
remote_agents:
  agents: {}
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
