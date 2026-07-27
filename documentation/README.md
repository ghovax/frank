# Frank — Documentation

Detailed guides for installing, configuring, understanding, and developing Frank. For a high-level overview, start with the [project README](../README.md); for the day-to-day surface, the [`frank` command](cli.md).

| Guide | What's in it |
|-------|--------------|
| [Installation](installation.md) | Download and Gatekeeper, or building from source |
| [The `frank` command](cli.md) | Every verb, the session states, JSON and exit codes |
| [Configuration](configuration.md) | Providers, keys, permissions, MCP, and every config key |
| [Architecture](architecture.md) | Sessions as processes, the daemon, the CLI, the app, and how they connect |
| [Agents and skills](agents-and-skills.md) | Authoring agents, skills, memory, and MCP servers |
| [Tools](tools.md) | The full tool surface, including screen control (`control_screen`) |
| [As a library](library.md) | Embedding the harness in your own program, and every seam you can replace |
| [Development](development.md) | The dev environment, running the pieces, building the app |

Design plans for larger changes — the sequential, commit-associated record of how the harness evolved — live in [Plans](plans/README.md). Those are proposals and design history, distinct from the current-state guides above.

## The shape of the project

```
frank/
├── packaging/entry.py                 # entry point for the frozen build (all three roles)
├── src/frank/
│   ├── __init__.py           # the library surface: frank.Session and its seams
│   ├── __main__.py           # argv dispatch: frank, frankd, prototype
│   ├── base/                 # configuration, XDG paths, skills, ports, the catalogue
│   ├── protocol/             # A2A cards, DTOs, the wire contract
│   ├── cli/                  # the `frank` command and its renderers
│   ├── workspace/            # projects, locations, settings, terminals — no supervision
│   ├── daemon/               # frankd: registry, lifecycle, prototype client, turn store
│   ├── worker/               # a session process, and the prototype it is forked from
│   ├── runtime/              # the agent loop, prompts, tools, models
│   ├── computer/             # macOS screen-control bridges (native apps + Chrome)
│   ├── locations/            # where files live (local, SSH, containers)
│   └── rest/                 # the REST surface the browser uses (never imports daemon)
├── .agents/                  # bundled agents, skills, memories, MCP config
├── web/                      # the desktop app (Next.js UI + Tauri shell in src-tauri/)
├── packaging/                # PyInstaller freeze + signing for the packaged app
├── scripts/                  # layering/import/translation checks, the verification battery
└── examples/                 # example MCP servers
```

The layering is `base` → `protocol` → `computer`/`locations` → `runtime` → `worker`, with `workspace` beside them and `rest` above it. Two rules do most of the work.

**The daemon never imports the runtime.** That keeps the control plane small, and it is why the *prototype* — the process every session is forked out of — lives in `worker/` and is reached over a socket rather than being a function the daemon calls: whatever forks a session must already have paid for the runtime import, and the daemon must never be that.

**`rest` never imports `daemon`.** The browser surface reaches `workspace` — projects, locations, settings, agents, terminals — and none of that supervises anything. It used to live inside the daemon, which is the only reason a GUI surface had to import the process that supervises agents. Where a workspace change has a supervision consequence, the workspace calls a hook the composition root filled in.

Runtime state never lives in the repository. Frank follows the XDG convention: configuration in **`~/.config/frank/`**, durable state (including `history.db`) in **`~/.local/share/frank/`**, sockets in the runtime directory, logs in **`~/.local/state/frank/`**, and caches in **`~/.cache/frank/`**. The [Configuration guide](configuration.md) is the reference for it.
