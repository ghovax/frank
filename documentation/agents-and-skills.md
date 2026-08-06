# Agents, skills, memory, and MCP

Everything that shapes how Frank behaves — its agents, their reusable skills, its memory, and its tool servers — is **plain Markdown and JSON on disk**. There are two layers, and they merge by name:

- **Global:** `~/.agents/` — available everywhere.
- **Project-local:** `.agents/` in the working directory you point an agent at.

A project-local entry **overrides** a global one with the same name. A repository can therefore ship its own agents and skills, and leave your global setup alone. The server also bundles a base set, which is always present.

Everything sits under `.agents/`:

| Path | What it holds |
|---|---|
| `agents/<id>/AGENT.md` | The whole profile: frontmatter and the prompt body |
| `skills/<id>/SKILL.md` | A reusable capability, loaded on demand |
| `memories/*.md` | Persistent project memory |
| `mcp.json` | MCP server configuration |

## Agents

An agent is a directory with one file in it: **`AGENT.md`**, spelled that way for the same reason a skill is `SKILL.md`. YAML frontmatter says everything the agent *is*; the body is its system prompt.

```markdown
---
name: senior-researcher
title: Senior researcher
description: A rigorous, skeptical researcher that pushes back before it builds.
role: primary
enabled: true
model: mimo-v2.5
provider: opencode
reasoning_effort: high
permission_mode: ask
tools:
  bash:
    enabled: true
    background_allowed: true
    permissions:
      sudo *: deny
      rm *: ask
  mcp:
    permissions:
      "*.delete_*": deny
---

You are the senior researcher. You do not take bullshit...
```

There used to be a `configuration.json` beside it holding the model preset, the permission ceiling and the tool toggles — in camelCase, while the frontmatter was snake_case. Loading an agent meant merging the two, so the same fact had two spellings and two places to be wrong. Settings edits this file directly, and leaves the prompt body untouched.

Each agent is a profile a session can be created with, and a running session serves [A2A](https://github.com/google/A2A) on its own socket. A session that needs a peer creates one with its `create_session` tool, over the control plane your terminal uses. See [Tools](tools.md#composing-with-other-sessions) for how a peer reports back.

The peer answers the same way, by messaging the session that created it. Bundled agents:

| Agent | Role |
|-------|------|
| `general-assistant` | A capable default for everyday tasks. |
| `senior-researcher` | Skeptical planning and verification before building. |
| `code-investigator` | Reads and explains a codebase without changing it. |
| `code-implementer` | Writes and edits code against a clear plan. |

## Skills

A skill is a `SKILL.md` — a focused capability the agent loads **only when relevant**, keeping the system prompt lean. Frontmatter carries a `description` the model uses to decide when to load it (via the `load_skill` tool):

```markdown
---
name: coding
title: Code patterns, conventions, and implementation discipline
description: Load before writing, editing, refactoring, or reviewing code.
enabled: true
---

# Coding Patterns and Implementation Discipline
...
```

Bundled skills include `coding`, `data-visualization`, `literature-search`, `harness-configuration`, and `context7-mcp`.

## Memory

`.agents/memories/*.md` are persistent notes. Only their metadata is injected into the prompt; the agent reads a memory's body **on demand**, so context stays small while knowledge accumulates across sessions.

## MCP servers

`.agents/mcp.json` registers [Model Context Protocol](https://modelcontextprotocol.io) servers under `mcpServers`. Both `stdio` and `streamable_http` transports are supported:

```json
{
  "mcpServers": {
    "context7": {
      "enabled": true,
      "transport": "streamable_http",
      "url": "https://mcp.context7.com/mcp",
      "stateful": true
    },
    "my-tool": {
      "enabled": true,
      "transport": "stdio",
      "command": "uv",
      "args": ["run", "python", "examples/mcp/echo/server.py"],
      "env": { "UV_CACHE_DIR": "/private/tmp/uv-cache" },
      "cwd": "."
    }
  }
}
```

Their tools and resources appear to the agent through the `call_mcp_tool`, `list_mcp_tools`, `list_mcp_resources`, and `read_mcp_resource` tools. An example stdio server lives in [`examples/mcp/`](../examples/mcp/).
