<h1>
  <img src="documentation/assets/frank-lockup.svg" alt="Frank" height="45">
</h1>

**An open coding agent you run yourself, and a harness you can edit.**

A coding agent needs more than a model. Something has to write the system prompt, give the model its tools, decide what it may run without asking, and keep the conversation from overflowing. That layer is the harness, and it decides more about the result than the model does. In most products it is closed. In Frank it is the code you are reading, and you can change it.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) ![Platform: macOS (Apple Silicon)](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-black) ![Built with Tauri, Next.js, LangChain](https://img.shields.io/badge/built%20with-Tauri%2C%20Next.js%2C%20LangChain-6E56CF)

## What it is

One conversation with an agent is a **session**. You create one, send it work, and it answers over its life. That is the only object in Frank, and everything below is a way of running one.

Frank is four layers. Each one uses the layer under it and adds a single thing:

1. **The library** — `import frank`. `frank.Session` runs an agent in your own process. You give it the agent, the model, the working directory and the credentials; it reads no file you did not name. This is the harness itself, and the three layers above are all built on it. See [As a library](documentation/library.md).
2. **The machine loaders** — `frank.daemon.machine`. These read your configuration file and the agents in your `.agents` directory, and turn them into what the library takes. This is the first layer that knows your home directory exists.
3. **The daemon** — `frankd`. It gives each session its own OS process, with a pid you can kill and a socket you can reach. It keeps the register of what exists, owns the databases, and relays every call. That buys three things the library alone cannot. A session outlives the program that made it, another machine can reach it, and its crash takes nothing else down.
4. **The clients** — the `frank` command, the macOS app, and a phone. All three talk to the daemon and contain no harness of their own. Anything one can do, the others can. The phone reaches it over `frank reach`, which is the one surface here that is meant to leave the machine — a stable address and a durable token, opt-in and authenticated. See [The phone](documentation/mobile.md).

An agent can use these too. When a session needs help it creates a second session and messages it, over the same API your terminal uses. The helper appears in `frank ps`, you can watch it, and it ends when its parent does. Its answer arrives as a message, in its own words.

## Why own the harness

The harness writes the system prompt, defines the tools, manages context, and sets what the agent may do. The same model does different work under different harnesses — OpenCode versus Claude Code or Codex, say. Frank lets you change that layer:

- **Tune the guardrails.** Permission modes and per-command rules are configuration. The engine that enforces them is open code. When the settings are not enough, you can change how permissioning works ([Permissions](documentation/configuration.md#permission-modes)).
- **The agent can work on Frank itself.** Its prompt says that it runs Frank. Open the Frank repository as the project. The agent then reads and edits the harness, and you rebuild ([Architecture](documentation/architecture.md)).
- **The agent can start with context about you** — an opt-in snapshot of your machine and habits, off by default ([What it sends](SECURITY.md#what-the-agent-sends-to-your-model-provider)).


## Install

Frank runs on **macOS on Apple Silicon**. It ships as two downloads:

- **The daemon bundle**, which carries the harness, the daemon and the `frank` command in one signed image.
- **The app**, which is the window that talks to it.

Download the latest release, install both, and run `frank app`. The build is self-signed, so Gatekeeper warns you at the first launch. You can also build from source with the Nix-pinned toolchain.

See the [Installation guide](documentation/installation.md) for both paths in full.

## Quickstart

The same harness, reached three ways. Start at the layer you want: an object in your own
program, a session you can address from a terminal, or a window.

### As a library

No daemon, no socket, and nothing read from or written to your home directory. The agent, its prompt and its credentials are values in your program:

```python
import asyncio
from frank import AgentConfiguration, Session

reviewer = AgentConfiguration(
    name="reviewer",
    description="Reads a change and reports what it would break.",
    system_prompt="You review changes. Name the risk, or say there is none.",
    permission_mode="read_only",
    provider="anthropic",
    model="claude-opus-4-5",
)

async def main() -> None:
    async with Session(
        reviewer,
        directory="/srv/checkout",
        providers={"anthropic": "sk-ant-…"},
    ) as session:
        print(await session.ask("What would break if I removed the retry loop in the fetcher?"))

asyncio.run(main())
```

`stream()` instead of `ask()` gives the turn as it happens: text chunks, tool calls, tool results, suspensions. `model=` takes any LangChain chat model, and the same session composes with it:

```python
from langchain_anthropic import ChatAnthropic

async with Session(reviewer, directory="/srv/checkout", model=ChatAnthropic(model="claude-opus-4-5")) as session:
    async for event in session.stream("Summarise what the test suite covers."):
        print(event)
```

Every durable thing is a seam: `checkpoints`, `jobs`, `transcript`, `approvals`, `observer`, `sandbox`, `catalogue`, and `peers`. Each one defaults to something a library may safely do, which for anything durable means *in memory*.

Three more sit around the turn: bound it, wrap its tools, decide how its history folds. Each one is an object with a method or two, so your own is as short as the ones that ship:

```python
from frank import KeepRecentTurns, MaximumToolCalls, Session

class RefuseNetworkTools:
    """A hook. Sees the batch the permission rules approved, and narrows it."""

    async def before_tools(self, calls):
        return [call for call in calls if call["name"] not in ("fetch_url", "search_web")]

class Timed:
    """Middleware. Wraps every tool call, yours and the harness's alike."""

    async def run(self, call, proceed):
        started = time.monotonic()
        try:
            return await proceed(call)
        finally:
            metrics.timing("frank.tool", time.monotonic() - started, tags={"tool": call.name})

async with Session(
    reviewer,
    directory="/srv/checkout",
    hooks=[MaximumToolCalls(20), RefuseNetworkTools()],
    pipeline=[Timed()],
    compaction=KeepRecentTurns(20),
) as session:
    ...
```

A hook narrows and can never widen: `before_tools` runs after the permission barrier, so it sees only calls the rules already allowed. [As a library](documentation/library.md) has the rest.

A program that *is* running on someone's machine can ask for that machine's agents deliberately, through `frank.daemon.machine`. [As a library](documentation/library.md) is the reference: the full seam table, a worked Redis checkpoint store, and what you give up by not using the daemon.

### From the terminal

| Command | What it does |
|---|---|
| `frank create --agent general-assistant --directory ~/code/project` | Creates a session and prints its id |
| `frank send <id> "What does this project do?" --wait` | Sends it work and waits for the answer |
| `frank ps` | Shows what runs, and what waits on you |
| `frank attach <id>` | Follows it live |

A session composes over the API, not over this command. `create_session` makes a peer and gives it a brief, `message_session` reaches a session in either direction, and `end_session` stops one.

These use the same daemon, the same sockets, and the same tree. The tool carries the caller's identity, which an argv string cannot do. A peer is therefore always a child of whoever made it, and its answer arrives as a message.

The daemon starts itself on the first command.

### From the app

1. **Launch Frank.** The daemon starts automatically; the app connects to it.
2. **Add a model key.** Open **Settings → Providers** and paste a key for any provider. You can also sign in with a ChatGPT or Cursor subscription. Then pick a model. Keys live in your Frank configuration file — see the [Configuration guide](documentation/configuration.md), or run `frank configure --all` to see every setting there is.
3. **Start a conversation.** Type a task. Approve tool calls as they come up, or relax the [permission mode](documentation/configuration.md#permission-modes) once you trust a flow.

The screen-control tools need a one-time Accessibility grant and Chrome's remote-debugging toggle — see the [Installation guide](documentation/installation.md#permissions-the-app-may-ask-for).

> [!NOTE]
> You can opt in to send a snapshot of how you work. The system prompt then carries it to your model provider. This is off by default. See [what the agent sends to your model provider](SECURITY.md#what-the-agent-sends-to-your-model-provider).

## How it compares

The closest tools are [Claude Code](https://code.claude.com) and [OpenAI Codex](https://github.com/openai/codex). Both are more mature than Frank. In 2026 both also drive a real browser and control native macOS apps. Codex is open source too, and it runs on models that are not OpenAI's. This table compares approaches. It does not list things that only Frank does.

| | Frank | Claude Code | OpenAI Codex |
|---|---|---|---|
| **License** | Open source (MIT) | Proprietary | Open-source CLI (Apache-2.0); cloud and models are OpenAI's |
| **Models** | Any provider, or a ChatGPT or Cursor login, per session — the screen tools included | Claude first; third-party providers for coding on the CLI and VS Code, but its browser and computer use need an Anthropic plan | GPT-5 Codex by default; the CLI can also point at OpenRouter, Ollama, LM Studio, or any compatible endpoint |
| **Where it runs** | A harness you self-host — local, a VM, a container, or over SSH — with a native app pointed at it | Proprietary client; long tasks run on Anthropic's cloud | Local CLI, IDEs, and a desktop app; async tasks run on OpenAI's cloud |
| **Screen control** | Native macOS apps and your own Chrome, read as ranked accessibility/DOM elements from a plain-language search — screenshots only when you ask | Your real Chrome session, plus macOS computer use driven by downscaled screenshots (research preview, Pro/Max) | In-app and Chrome-extension browser, plus background macOS computer use driven by screenshots |
| **Reach** | Terminal-first (`frank`), plus a desktop app over the same API; every session is scriptable and attachable | Terminal, VS Code, JetBrains, desktop, web, mobile, Slack, CI, GitHub review; macOS and Windows | CLI, IDEs, desktop, cloud/web, Chrome, GitHub review; macOS and Windows |

Three design choices distinguish Frank:

- **Structure, not screenshots.** Frank reads the screen as a semantic search over the accessibility tree and the DOM. It returns a few ranked elements. The other tools reason over screenshots. A query here costs a few elements, not an image.
- **A session is a process, not a coroutine.** Each session runs in its own OS process behind its own socket. It is therefore crash-isolated, addressable, and killable. To make a peer, a session creates another session and messages it. It uses the API that a person uses.
- **A composed script, not a click-by-click loop.** `control_screen` runs a Python program. Its primitives (`click`, `type`, `scroll`, `evaluate`) are the same on native apps and in the browser. One call can loop over rows, branch on what it finds, and call the page's own API. The other tools need one round trip for each click. Frank needs far fewer model turns.

The trade-off: it needs an accessibility tree or DOM to read, where a screenshot approach works on anything drawn on screen. See [Tools](documentation/tools.md).

Elsewhere they lead. They have more polish, more places to run, and deeper ecosystems. Claude Code has subagents, hooks, plugins, and an Agent SDK. Codex has cloud tasks, more than 90 plugins, and automatic PR review. All three tools gate actions behind approvals and a sandbox.

Frank is the small, open, model-agnostic option that you host yourself. For a mature agent on a vendor's cloud, use theirs.

## Where things live

Frank follows the XDG convention. It does not use a single dot-directory:

- Configuration in `~/.config/frank`
- Durable state in `~/.local/share/frank`
- Sockets in the runtime directory
- Logs in `~/.local/state/frank`
- Caches in `~/.cache/frank`

The OS clears the runtime directory when you log out. A crashed daemon therefore leaves nothing behind.

Only the holder of a session's handle can reach it. `create` mints a capability token. Every call to a session's socket must present that token. The daemon guards its own API the same way, with a token that it writes 0600 into the runtime directory.

That token does not say *which* session is calling. A session runs as the same user and could read the file. So on the unix socket the daemon asks the kernel for the peer's pid. It resolves the pid to a session through the process session that every worker leads. A call is therefore attributed to whoever made it.

> [!NOTE]
> A session's permission mode is fixed when the session is created. You cannot change it afterwards. A child gets a mode no looser than its parent's. There is no bypass mode and no standing "always allow". The only decisions at runtime are allow-once and deny. See the [Security notes](SECURITY.md).

## Documentation

The full guides live in the **[Documentation](documentation/README.md)**. It indexes them and sketches the project layout. They cover the architecture and its vocabulary, installation, [the library](documentation/library.md), the [`frank` command](documentation/cli.md), [the desktop app](documentation/app.md), agents and skills, configuration, the tool surface, and development.

## Built with

[Tauri](https://tauri.app), [Next.js](https://nextjs.org), [Chakra UI](https://chakra-ui.com), [LangChain](https://www.langchain.com), [LiteLLM](https://litellm.ai), [FastAPI](https://fastapi.tiangolo.com), [Model Context Protocol](https://modelcontextprotocol.io), and [A2A](https://github.com/google/A2A)

## Contributing

Contributions are welcome — see the [Contributing guide](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © Giovanni Gravili
