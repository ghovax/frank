# Agents, skills, memory, and MCP

Everything that shapes how Daisy behaves — its agents, their reusable skills, its memory,
and its tool servers — is **plain Markdown and JSON on disk**. There are two layers, and
they merge by name:

- **Global:** `~/.agents/` — available everywhere.
- **Project-local:** `.agents/` in the working directory you point an agent at.

A project-local entry **overrides** a global one with the same name, so a repo can ship
its own agents and skills without touching your global setup. The server also bundles a
base set that is always present.

```
.agents/
├── agents/<id>/agent.md            # profile: frontmatter + prompt body
├── agents/<id>/configuration.json  # model preset, tools, permissions
├── skills/<id>/SKILL.md            # a reusable capability, loaded on demand
├── memories/*.md                   # persistent project memory
└── mcp.json                        # MCP server configuration
```

## Agents

An agent is a directory with a Markdown profile and a JSON configuration.

**`agent.md`** — YAML frontmatter followed by the system-prompt body:

```markdown
---
name: senior-researcher
title: Senior researcher
description: A rigorous, skeptical researcher that pushes back before it builds.
role: primary
enabled: true
connection-type: internal
---

You are the senior researcher. You do not take bullshit...
```

**`configuration.json`** — the model preset, enabled tools, and permission rules:

```json
{
  "preset": { "model": "mimo-v2.5", "provider": "opencode", "reasoningEffort": "high" },
  "permissionMode": "default",
  "tools": {
    "enabledBuiltinTools": [],
    "bash": {
      "enabled": true,
      "backgroundAllowed": true,
      "permissions": { "sudo *": "deny", "rm *": "ask" }
    }
  }
}
```

Each agent is served as its own [A2A](https://github.com/google/A2A) endpoint, and an agent
can **delegate** a sub-task to another agent (the `spawn_agent` tool). Bundled agents:

| Agent | Role |
|-------|------|
| `general-assistant` | A capable default for everyday tasks. |
| `senior-researcher` | Skeptical planning and verification before building. |
| `code-investigator` | Reads and explains a codebase without changing it. |
| `code-implementer` | Writes and edits code against a clear plan. |

## Skills

A skill is a `SKILL.md` — a focused capability the agent loads **only when relevant**, so
the system prompt stays lean. Frontmatter carries a `description` the model uses to decide
when to load it (via the `load_skill` tool):

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

Bundled skills include `coding`, `data-visualization`, `literature-search`,
`harness-configuration`, and `context7-mcp`.

## Memory

`.agents/memories/*.md` are persistent notes. Only their metadata is injected into the
prompt; the agent reads a memory's body **on demand**, keeping context small while letting
knowledge accumulate across sessions.

## MCP servers

`.agents/mcp.json` registers [Model Context Protocol](https://modelcontextprotocol.io)
servers under `mcpServers`. Both `stdio` and `streamable_http` transports are supported:

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
      "args": ["run", "python", "examples/mcp/example/server.py"],
      "env": { "UV_CACHE_DIR": "/private/tmp/uv-cache" },
      "cwd": "."
    }
  }
}
```

Their tools and resources appear to the agent through the `call_mcp_tool`,
`list_mcp_tools`, `list_mcp_resources`, and `read_mcp_resource` tools. An example stdio
server lives in [`examples/mcp/`](../examples/mcp/).
