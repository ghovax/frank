# Frank — Documentation

Detailed guides for installing, configuring, understanding, and developing Frank. For a
high-level overview, start with the [project README](../README.md).

**Start with the one that matches how you use it.** The harness is the same underneath. These are three faces of it, not three products.

| If you want to… | Read |
|---|---|
| **Embed the harness in your own program** — `import frank`, no daemon, no socket | [As a library](library.md) |
| **Drive it from a terminal** — create, send, attach, approve | [The `frank` command](cli.md) |
| **Use the macOS app** | [Installation](installation.md) |

Then the rest, in roughly the order they become relevant:

| Guide | What's in it |
|-------|--------------|
| [Installation](installation.md) | Download and Gatekeeper, or building from source |
| [As a library](library.md) | `frank.Session` in your own process, and every seam you can replace — model, checkpoints, jobs, approvals, observer, sandbox, catalogue, peers |
| [The `frank` command](cli.md) | Every verb, the session states, JSON and exit codes |
| [Configuration](configuration.md) | Providers, keys, permissions, MCP, and every config key |
| [Architecture](architecture.md) | Sessions as processes, the daemon, the CLI, the app, and how they connect |
| [Agents and skills](agents-and-skills.md) | Authoring agents, skills, memory, and MCP servers |
| [Tools](tools.md) | The full tool surface, including screen control (`control_screen`) |
| [Development](development.md) | The dev environment, running the pieces, building the app |

## The shortest thing that works

```python
import asyncio
from frank import Session

async def main() -> None:
    async with Session("general-assistant", directory=".") as session:
        print(await session.ask("what does this project do?"))

asyncio.run(main())
```

That writes nothing to your home directory, and it starts no daemon. A library that installs a database because you imported it is a library you cannot embed. Every durable seam therefore defaults to memory.

To swap one, pass an object with the right methods. Each seam is a `typing.Protocol`: no base class to inherit, and no import of Frank in your type. [As a library](library.md) has the full table and a worked Redis checkpoint store.

Design plans for larger changes — the sequential, commit-associated record of how the harness evolved — live in [Plans](plans/README.md). Those are proposals and design history, distinct from the current-state guides above.

## The shape of the project

**`src/frank/`** — the Python harness, layered:

| Module | What lives there |
|---|---|
| `__init__.py` | The library surface: `frank.Session` and its seams |
| `__main__.py` | argv dispatch: `frank`, `frankd`, `prototype` |
| `base/` | Configuration, XDG paths, skills, ports, the catalogue |
| `protocol/` | A2A cards, DTOs, the wire contract |
| `cli/` | The `frank` command and its renderers |
| `workspace/` | Projects, locations, settings, terminals — no supervision |
| `daemon/` | `frankd`: registry, lifecycle, prototype client, turn store |
| `worker/` | A session process, and the prototype it is forked from |
| `runtime/` | The agent loop, prompts, tools, models |
| `computer/` | macOS screen-control bridges: native apps and Chrome |
| `locations/` | Where files live: local, SSH, containers |
| `rest/` | The REST surface the browser uses; never imports `daemon` |

**Everything else:**

| Path | What lives there |
|---|---|
| `.agents/` | Bundled agents, skills, memories, MCP configuration |
| `web/` | The desktop app: Next.js UI, and the Tauri shell in `src-tauri/` |
| `packaging/` | PyInstaller freeze and signing, plus `entry.py` for the frozen build |
| `scripts/` | Layering, import and translation checks; the verification battery |
| `examples/` | Example MCP servers |

The layering is `base` → `protocol` → `computer`/`locations` → `runtime` → `worker`, with `workspace` beside them and `rest` above it. Two rules do most of the work.

**The daemon never imports the runtime.** That keeps the control plane small. It is also why the *prototype* lives in `worker/` and is reached over a socket, rather than being a function the daemon calls. Every session is forked out of the prototype. Whatever forks a session must already have paid for the runtime import, and the daemon must never be that.

**`rest` never imports `daemon`.** The browser surface reaches `workspace` — projects, locations, settings, agents, terminals — and none of that supervises anything. It used to live inside the daemon, which is the only reason a GUI surface had to import the process that supervises agents. Where a workspace change has a supervision consequence, the workspace calls a hook the composition root filled in.

Runtime state never lives in the repository. Frank follows the XDG convention:

- Configuration in **`~/.config/frank/`**
- Durable state, including `history.db`, in **`~/.local/share/frank/`**
- Sockets in the runtime directory
- Logs in **`~/.local/state/frank/`**
- Caches in **`~/.cache/frank/`**

The [Configuration guide](configuration.md) is the reference for it.
