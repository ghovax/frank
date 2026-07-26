---
name: harness-layout
title: Repository layout — packages, and the .agents protocol
description: Where Daisy's code lives, what each package may import, and this repository's .agents layout for project-local agents, skills, memories, and MCP servers.
importance: high
tags: configuration, dotagents, mcp, layering
---

## The package tree

One executable, three entry points, selected by the first argument in `src/daisy/__main__.py`: `daisy` (the CLI), `daisyd` (the daemon), and `worker` (a session). `packaging/entry.py` at the repository root is the same entry for the frozen build.

```
src/daisy/
├── base/        configuration, XDG paths, skills, permission modes, MCP client, file leases
├── protocol/    A2A cards, DTOs, the wire contract
├── cli/         the `daisy` command and its renderers
├── daemon/      daisyd: registry, lifecycle, worker pool, brokers, persistence
├── worker/      a session process: its socket server and its executor
├── runtime/     the agent loop, prompts, tools, models
├── computer/    macOS screen-control bridges (native apps + Chrome)
├── locations/   where files live (local, SSH, containers)
└── rest/        the REST surface the desktop app uses
```

`scripts/check_layers.py` enforces the layering — `base` → `protocol` → `computer`/`locations` → `runtime` → `worker`, with `rest` above the daemon — and two invariants: **the daemon never imports `runtime`** (that weight is what the pre-started worker exists to carry), and **`computer/` is never imported at module level** (it pulls in PyObjC, which a pre-warmed worker must not have loaded). A package's `__main__.py` is exempt from the layer table: it is the composition root, and assembling a program means reaching across layers.

## The .agents protocol layout

Two layers merge by name — `~/.agents/` (global) and `.agents/` in the working directory (project-local, which wins).

- `.agents/agents/<name>/agent.md` — the profile: YAML frontmatter plus the system-prompt body. `AGENT.md` is also accepted.
- `.agents/agents/<name>/configuration.json` — runtime settings: the model `preset`, `permissionMode`, and `tools` (**`configuration.json`, not `config.json`**).
- `.agents/skills/<name>/SKILL.md` — a capability loaded on demand.
- `.agents/memories/*.md` — these notes. Metadata is injected; the body is read on demand. Not shipped in the packaged app.
- `.agents/mcp.json` — MCP servers, not auto-discovered from any folder.
- `.agents/remote-agents.json` — peers on other hosts. Read from every `.agents` root, but the Settings editor only ever writes the home one.

Non-trivial local MCP examples live in `examples/mcp/<server-id>/` with a `packaging/entry.py` and sibling templates or assets.

`enabledBuiltinTools` is an allow-list over the **whole** tool surface, not a subset of optional extras: an empty list means no restriction, and naming one tool denies every other. It is narrowed at build time to tools that exist, so a profile naming a tool that has since been removed does not silently disarm itself.

## Runtime state

XDG, never the repository: configuration in `~/.config/daisy/`, `history.db` and uploads in `~/.local/share/daisy/`, logs in `~/.local/state/daisy/`, caches in `~/.cache/daisy/`, and the daemon's socket, port, token and per-session sockets in the runtime directory (`0700`, cleared by the OS on logout).
