# Frank — Documentation

Detailed guides for installing, configuring, understanding, and developing Frank. For a
high-level overview, start with the [project README](../README.md).

**They are a stack, not separate products.** The library is the bottom of it, and everything else is built on top:

| Layer | What it is | What it knows about your machine |
|---|---|---|
| `frank.Session` | The harness: turn loop, tools, prompts, permissions | Nothing. Every value is one you passed |
| `frank.daemon.machine` | Turns a home directory into what `Session` takes | The XDG paths, and your `.agents` |
| `frankd` | Supervision: a process per session, a socket each, the databases | Everything, and it is the right place to |
| `frank`, and the app | Clients of the daemon | Where the daemon is |

Start with the layer you are actually using.

| If you want to… | Read |
|---|---|
| **Embed the harness in your own program** — `import frank`, no daemon, no socket | [As a library](library.md) |
| **Drive it from a terminal** — create, send, attach, approve | [The `frank` command](cli.md) |
| **Use the macOS app** | [The desktop app](app.md) |

Then the rest, in the order they build on each other. [Architecture](architecture.md) defines
the words the others use, so it comes first:

| Guide | What's in it |
|-------|--------------|
| [Architecture](architecture.md) | The vocabulary, the four layers, and how a message becomes work |
| [Installation](installation.md) | Download and Gatekeeper, or building from source |
| [As a library](library.md) | `frank.Session` in your own process, and every seam you can replace |
| [The `frank` command](cli.md) | Every verb, the session states, JSON and exit codes |
| [The desktop app](app.md) | The window, decisions, folders, and screen control |
| [Agents and skills](agents-and-skills.md) | Authoring agents, skills, memory, and MCP servers |
| [Configuration](configuration.md) | Providers, keys, permissions, MCP, and every config key |
| [Tools](tools.md) | The full tool surface, including screen control (`control_screen`) |
| [Development](development.md) | The dev environment, running the pieces, building the app |
| [Security](../SECURITY.md) | What the agent sends, and what confines it |

## The shortest thing that works

```python
import asyncio
from frank import AgentConfiguration, DictCatalogue, Session

reviewer = AgentConfiguration(
    name="reviewer",
    system_prompt="You review changes. Name the risk, or say there is none.",
    permission_mode="read_only",
    provider="anthropic",
    model="claude-opus-4-5",
)

async def main() -> None:
    async with Session(
        reviewer,
        directory="/srv/checkout",
        catalogue=DictCatalogue(agent_configurations={"reviewer": reviewer}),
        providers={"anthropic": "sk-ant-…"},
    ) as session:
        print(await session.ask("What would break if I removed the retry loop?"))

asyncio.run(main())
```

That reads nothing from your home directory, writes nothing to it, and starts no daemon. A library that installs a database because you imported it is a library you cannot embed, so every durable seam defaults to memory.

To swap one, pass an object with the right methods. Each seam is a `typing.Protocol`: no base class to inherit, and no import of Frank in your type. [As a library](library.md) has the full table and a worked Redis checkpoint store.

Design plans for larger changes — the sequential, commit-associated record of how the harness evolved — live in [Plans](plans/README.md). Those are proposals and design history, distinct from the current-state guides above.

## The shape of the project

**`src/frank/`** — the Python image, in the import order stated below:

| Module | What lives there |
|---|---|
| `base/` | Configuration, XDG paths, skills, ports, the catalogue |
| `protocol/` | A2A cards, DTOs, the wire contract |
| `computer/` | macOS screen-control bridges: native apps and Chrome |
| `locations/` | Where files live: local, SSH, containers |
| `runtime/` | The agent loop, prompts, tools, models |
| `worker/` | A session process, and the prototype it is forked from |
| `__init__.py` | The library surface: `frank.Session` and its seams |
| `workspace/` | Projects, locations, settings, terminals — beside the rest, not above |
| `daemon/` | `frankd`: registry, lifecycle, prototype client, machine loaders |
| `rest/` | The REST surface the browser uses; never imports `daemon` |
| `cli/` | The `frank` command and its renderers |
| `__main__.py` | argv dispatch: `frank`, `frankd`, `prototype`, `session` |

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

**`rest` never imports `daemon`.** The browser surface reaches `workspace`: projects, locations, settings, agents, terminals. None of that supervises anything, so a GUI surface never imports the process that supervises agents. Where a workspace change has a supervision consequence, the workspace calls a hook that the composition root filled in.

Runtime state never lives in the repository. Frank follows the XDG convention:

- Configuration in **`~/.config/frank/`**
- Durable state, including `history.db`, in **`~/.local/share/frank/`**
- Sockets in the runtime directory
- Logs in **`~/.local/state/frank/`**
- Caches in **`~/.cache/frank/`**

The [Configuration guide](configuration.md) is the reference for it.
