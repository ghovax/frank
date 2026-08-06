# Frank — Documentation

Detailed guides for installing, configuring, understanding, and developing Frank. For a high-level overview, start with the [project README](../README.md).

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
| **Reach it from your phone** | [`frank reach`](cli.md#reaching-it-from-a-phone) |

Then the rest, in the order they build on each other. [Architecture](architecture.md) defines the words the others use, so it comes first:

| Guide | What's in it |
|-------|--------------|
| [Architecture](architecture.md) | The vocabulary, the four layers, how a message becomes work, and [what is recorded about prompt caching](architecture.md#prompt-caching-and-what-is-recorded-about-it) |
| [Installation](installation.md) | Download and Gatekeeper, or building from source |
| [As a library](library.md) | `frank.Session` in your own process, and every seam you can replace |
| [The `frank` command](cli.md) | Every verb, the session states, JSON and exit codes |
| [The desktop app](app.md) | The window, decisions, environments, and screen control |
| [Agents and skills](agents-and-skills.md) | Authoring agents, skills, memory, and MCP servers |
| [Configuration](configuration.md) | Providers, keys, permissions, MCP, and every config key |
| [Tools](tools.md) | The full tool surface, including screen control (`control_screen`) |
| [Development](development.md) | The dev environment, running the pieces, building the app |
| [Security](../SECURITY.md) | What the agent sends, and what confines it |

## The shortest thing that works

```python
import asyncio
from frank import AgentConfiguration, Catalogue, FilesystemConfiguration, SandboxConfiguration, Session

reviewer = AgentConfiguration(
    name="reviewer",
    system_prompt="You review changes. Name the risk, or say there is none.",
    sandbox=SandboxConfiguration(filesystem=FilesystemConfiguration(writable=[])),
    provider="anthropic",
    model="claude-opus-4-5",
)

async def main() -> None:
    async with Session(
        reviewer,
        directory="/srv/checkout",
        catalogue=Catalogue(agents={"reviewer": reviewer}),
        providers={"anthropic": "sk-ant-…"},
    ) as session:
        print(await session.ask("What would break if I removed the retry loop?"))

asyncio.run(main())
```

That reads nothing from your home directory, writes nothing to it, and starts no daemon. A library that installs a database because you imported it is a library you cannot embed, so every durable seam defaults to memory.

To swap one, pass an object with the right methods. Each seam is a `typing.Protocol`: no base class to inherit, and no import of Frank in your type. [As a library](library.md) has the full table and a worked Redis checkpoint store.


## Where Frank keeps your things

Runtime state never lives in the repository. Frank follows the XDG convention:

- Configuration in **`~/.config/frank/`**
- Durable state, including `history.db`, in **`~/.local/share/frank/`**
- Sockets in the runtime directory
- Logs in **`~/.local/state/frank/`**
- Caches in **`~/.cache/frank/`**

The [Configuration guide](configuration.md) explains the settings worth explaining, and the [configuration reference](configuration-reference.md) lists every one of them.
