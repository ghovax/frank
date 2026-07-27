# Daisy — Documentation

Detailed guides for installing, configuring, understanding, and developing Daisy. For a high-level overview, start with the [project README](../README.md); for the day-to-day surface, the [`daisy` command](cli.md).

| Guide | What's in it |
|-------|--------------|
| [Installation](installation.md) | Download and Gatekeeper, or building from source |
| [The `daisy` command](cli.md) | Every verb, the session states, JSON and exit codes |
| [Configuration](configuration.md) | Providers, keys, permissions, MCP, and every config key |
| [Architecture](architecture.md) | Sessions as processes, the daemon, the CLI, the app, and how they connect |
| [Agents and skills](agents-and-skills.md) | Authoring agents, skills, memory, and MCP servers |
| [Tools](tools.md) | The full tool surface, including screen control (`control_screen`) |
| [Development](development.md) | The dev environment, running the pieces, building the app |

Design plans for larger changes — the sequential, commit-associated record of how the harness evolved — live in [Plans](plans/README.md). Those are proposals and design history, distinct from the current-state guides above.

## The shape of the project

```
daisy/
├── packaging/entry.py                 # entry point for the frozen build (all three roles)
├── src/daisy/
│   ├── __main__.py           # argv dispatch: daisy, daisyd, worker
│   ├── base/                 # configuration, XDG paths, skills, permission modes
│   ├── protocol/             # A2A cards, DTOs, the wire contract
│   ├── cli/                  # the `daisy` command and its renderers
│   ├── daemon/               # daisyd: registry, lifecycle, prototype client, persistence
│   ├── worker/               # a session process: its socket server and executor
│   ├── runtime/              # the agent loop, prompts, tools, models
│   ├── computer/             # macOS screen-control bridges (native apps + Chrome)
│   ├── locations/            # where files live (local, SSH, containers)
│   └── rest/                 # the REST surface the desktop app uses
├── .agents/                  # bundled agents, skills, memories, MCP config
├── web/                      # the desktop app (Next.js UI + Tauri shell in src-tauri/)
├── packaging/                # PyInstaller freeze + signing for the packaged app
├── scripts/                  # layering check, event-schema generation
└── examples/                 # example MCP servers
```

The layering is enforced by `scripts/check_layers.py`: `base` → `protocol` → `computer`/`locations` → `runtime` → `worker`, and the daemon never imports the runtime. That is what keeps the control plane small, and it is why the *prototype* — the process every session is forked out of — lives in `worker/` and is reached over a socket rather than being a function the daemon calls.

Runtime state never lives in the repository. Daisy follows the XDG convention: configuration in **`~/.config/daisy/`**, durable state (including `history.db`) in **`~/.local/share/daisy/`**, sockets in the runtime directory, logs in **`~/.local/state/daisy/`**, and caches in **`~/.cache/daisy/`**. The [Configuration guide](configuration.md) is the reference for it.
