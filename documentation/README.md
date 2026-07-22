# Daisy 🌼 — Documentation

Detailed guides for installing, configuring, understanding, and developing Daisy. For a high-level overview, start with the [project README](../README.md).

| Guide | What's in it |
|-------|--------------|
| [Installation](installation.md) | Download and Gatekeeper, or building from source |
| [Configuration](configuration.md) | Providers, keys, permissions, MCP, and every config key |
| [Architecture](architecture.md) | The library-and-server harness, the app, and how they connect |
| [Agents and skills](agents-and-skills.md) | Authoring agents, skills, memory, and MCP servers |
| [Tools](tools.md) | The full tool surface, including screen control (`search_screen`/`control_screen`) |
| [Development](development.md) | The dev environment, running the pieces, building the app |

Design plans for larger changes — the sequential, commit-associated record of how the harness evolved — live in [`plans/`](plans/README.md); those are proposals and design history, distinct from the current-state guides above.

## The shape of the project

```
daisy/
├── server.py                 # launch shim for the harness (FastAPI app)
├── src/daisy/              # the runtime: agent loop, tools, permissions, A2A, REST
│   ├── core/                 # configuration, agent, prompts
│   ├── tools/                # tool implementations and descriptions
│   ├── computer/             # macOS screen-control bridges (native apps + Chrome)
│   └── server/               # HTTP routes
├── .agents/                  # bundled agents, skills, memories, MCP config
├── web/                      # the desktop app (Next.js UI + Tauri shell in src-tauri/)
├── packaging/                # PyInstaller freeze + signing for the packaged app
├── examples/                 # example MCP servers
└── configuration.example.yaml
```

State the harness reads and writes at runtime lives in **`~/.daisy/`**, never in the repository: `configuration.yaml` (credentials, selected model, settings) and `history.db` (chat history). It is created on first run and is the single source of truth.
