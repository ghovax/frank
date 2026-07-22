# Configuration

All runtime configuration lives in **`~/.daisy/configuration.yaml`**. It is created on first run from a built-in template. It is the source of truth for credentials, the selected model, permissions, and feature toggles. The repository never contains a filled-in copy — `~/.daisy/` sits outside the repo.

Most settings are editable from **Settings** in the app. This document is the reference for the file itself, which you need for headless or remote deployments.

> [!IMPORTANT]
> Every credential can also be set through an environment variable (listed below), which takes precedence over the file. This lets you run a harness without writing any secret to disk. Never commit a filled-in config or a `.env` — see [Security notes](../SECURITY.md).

A fully-commented template lives at [Example configuration](../configuration.example.yaml).

## Model providers

Set an `api_key` for the providers you use. First-party providers resolve through LiteLLM's built-in endpoints. Any OpenAI-compatible provider may also set `base_url`.

```yaml
providers:
  anthropic:   { api_key: "" }      # env: ANTHROPIC_API_KEY
  openai:      { api_key: "" }      # env: OPENAI_API_KEY
  google:      { api_key: "" }      # env: GEMINI_API_KEY
  openrouter:  { api_key: "" }      # env: OPENROUTER_API_KEY
  xai:         { api_key: "" }      # env: XAI_API_KEY
  deepseek:    { api_key: "" }      # env: DEEPSEEK_API_KEY
  groq:        { api_key: "" }      # env: GROQ_API_KEY
  mistral:     { api_key: "" }      # env: MISTRAL_API_KEY
  opencode:    { api_key: "", base_url: "https://opencode.ai/zen/go/v1" }
  custom:      { api_key: "", base_url: "" }   # any OpenAI-compatible endpoint

selected_provider: ""   # usually set from the UI
selected_model: ""
```

You can also **sign in with a ChatGPT subscription** from **Settings → Providers** instead of pasting a key.

## Web search and retrieval

```yaml
exa:       { api_key: "" }          # search_web fallback — env: EXA_API_KEY
jina:      { api_key: "" }          # fetch_url free tier — env: JINA_API_KEY
firecrawl: { api_key: "" }          # fetch_url fallback — env: FIRECRAWL_API_KEY
web_fetch: { proxy_url: "" }        # optional outbound proxy — env: DAISY_FETCH_PROXY
```

`fetch_url` uses a tiered engine: Jina Reader (free) first, then Firecrawl, then a direct fetch. Each tier is optional; an unset key skips that tier.

## Hosted integrations

```yaml
composio:
  enabled: false
  url: "https://connect.composio.dev/mcp"
  api_key: ""                       # env: COMPOSIO_API_KEY
  server_name: "composio"
```

When enabled, Composio is exposed as a normal MCP server. The agent discovers its tools and authorizes accounts (Gmail, Notion, …) on first use.

## Execution and permissions

```yaml
sandbox:   { enabled: true }        # confine bash to the active workspace
workspace: { strategy: "none" }     # "none" | "branch" | "worktree"
agent:     { permission_mode: "default" }
computer_control: { enabled: false } # macOS screen tools (search_screen/control_screen); opt-in
user_context:     { enabled: false } # persistent context about you across sessions; opt-in
```

### Permission modes

| Mode | Behavior |
|------|----------|
| `default` | Ask before risky actions; allow safe ones. |
| `auto` | Auto-approve low-risk actions, ask for the rest. |
| `read_only` | Allow reads; deny writes and side effects. |
| `bypass` | Approve everything. Use only when you fully trust the task. |

Bash additionally honors per-command rules defined on each agent (for example `sudo *: deny`, `rm *: ask`) — see [Agents and skills guide](agents-and-skills.md).

## Agents

```yaml
default_agent: "general-assistant"
```

The primary agent for new sessions. Bundled options: `general-assistant`, `senior-researcher`, `code-investigator`, `code-implementer`. Add your own under `~/.agents/agents/<id>/` or `.agents/agents/<id>/` in a working directory — see [Agents and skills guide](agents-and-skills.md).
