# Daisy

**An open agent harness where every session is a process you can address.**

The harness is the code between the model and your machine — turn loop, tools, prompts, permissions — and in Daisy all of it is yours to edit. Drive it from the terminal, from the bundled macOS app, or from another agent.

A session here is *executable*, because it is a real OS process with a pid you can kill; *addressable*, because it has its own unix socket and its own capability token; and *composable*, because sessions create and message each other through the same control plane you use. Those three properties are the whole design, and everything below follows from them.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) ![Platform: macOS (Apple Silicon)](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-black) ![Built with Tauri, Next.js, LangChain](https://img.shields.io/badge/built%20with-Tauri%2C%20Next.js%2C%20LangChain-6E56CF)

## What it is

Everything in Daisy is a **session**: one OS process running one agent, created empty and then driven by messages over its life. A session serves [A2A](https://github.com/google/A2A) on its own unix socket, and every client — you from the terminal, the desktop app, another session — reaches it through the daemon, which relays. One path in, so a caller is identified and scoped in exactly one place.

Three parts, kept apart:

- **`daisyd`** — a thin daemon. It keeps the registry of sessions, supervises their processes, owns the databases as the sole writer, brokers the shared resources, and parks a couple of warm workers so spawning a session is usually a socket write rather than a Python cold start. It runs no agents itself.
- **`daisy`** — the command. `create` a session, `send` it work, `ps` what is running, `attach` to watch, `tree` to see what created what, `approve` what it asks for, `configure` what the next one starts with, `kill` to end a subtree. It adds nothing the control plane does not have; it is the ergonomic face of it — see the [CLI guide](documentation/cli.md).
- **The app** — a native macOS client (Tauri + Next.js) over the same API.

Sessions compose the same way you do. A session that needs a peer calls `create_session`, which reaches the same control plane your terminal does — one API, whether the caller is a person, the desktop app, or an agent. The peer reports back by sending its parent a message, so an answer is a message rather than something reconstructed from a transcript. A child is a real session: it appears in `daisy ps`, you can attach to it, and it is reaped when its parent ends.

## Why own the harness

The harness writes the system prompt, defines the tools, manages context, and sets what the agent may do. The same model does different work under different harnesses — OpenCode versus Claude Code or Codex, say. Daisy lets you change that layer:

- **Tune the guardrails.** Permission modes and per-command allow/deny rules are config; the engine that enforces them is open code, so you can change how permissioning works when the settings aren't enough ([Permissions](documentation/configuration.md#permission-modes)).
- **The agent can work on Daisy itself.** Its prompt says it's running Daisy; open the Daisy repo as the project and it can read and edit the harness, then you rebuild ([Architecture](documentation/architecture.md)).
- **The agent can start with context about you** — an opt-in snapshot of your machine and habits, off by default ([What it sends](SECURITY.md#what-the-agent-sends-to-your-model-provider)).

## How it compares

The closest tools are [Claude Code](https://code.claude.com) and [OpenAI Codex](https://github.com/openai/codex), both more mature than Daisy. As of 2026 both also drive a real, logged-in browser and control native macOS apps, and Codex is likewise open source and runs on non-OpenAI models — so this compares approaches, not things only Daisy does.

| | Daisy | Claude Code | OpenAI Codex |
|---|---|---|---|
| **License** | Open source (MIT) | Proprietary | Open-source CLI (Apache-2.0); cloud and models are OpenAI's |
| **Models** | Any provider, or a ChatGPT login, per session — the screen tools included | Claude first; third-party providers for coding on the CLI and VS Code, but its browser and computer use need an Anthropic plan | GPT-5 Codex by default; the CLI can also point at OpenRouter, Ollama, LM Studio, or any compatible endpoint |
| **Where it runs** | A harness you self-host — local, a VM, a container, or over SSH — with a native app pointed at it | Proprietary client; long tasks run on Anthropic's cloud | Local CLI, IDEs, and a desktop app; async tasks run on OpenAI's cloud |
| **Screen control** | Native macOS apps and your own Chrome, read as ranked accessibility/DOM elements from a plain-language search — screenshots only when you ask | Your real Chrome session, plus macOS computer use driven by downscaled screenshots (research preview, Pro/Max) | In-app and Chrome-extension browser, plus background macOS computer use driven by screenshots |
| **Reach** | Terminal-first (`daisy`), plus a desktop app over the same API; every session is scriptable and attachable | Terminal, VS Code, JetBrains, desktop, web, mobile, Slack, CI, GitHub review; macOS and Windows | CLI, IDEs, desktop, cloud/web, Chrome, GitHub review; macOS and Windows |

Three design choices distinguish Daisy:

- **Structure, not screenshots.** It reads the screen as a semantic search over the accessibility tree and DOM, returning a few ranked elements where the rivals reason over screenshots — a query costs a handful of elements, not a downscaled image.
- **A session is a process, not a coroutine.** Each session runs in its own OS process behind its own socket, so it is crash-isolated, addressable, and killable. A session creates a peer by creating another session and messaging it over the same API a person uses, instead of through a bespoke in-process delegation tool.
- **A composed script, not a click-by-click loop.** `control_screen` runs a Python program whose primitives (`click`, `type`, `scroll`, `evaluate`, …) are the *same* on native apps and in the browser. A whole task — loop over rows, branch on what you find, call the page's own API in one line — is a single call, not a screenshot‑decide‑act round trip per click. Far fewer model turns to finish the job.

The trade-off: it needs an accessibility tree or DOM to read, where a screenshot approach works on anything drawn on screen. See [Tools](documentation/tools.md).

Elsewhere they lead: more polish, more places to run, deeper ecosystems — Claude Code's subagents, hooks, plugins, and Agent SDK; Codex's cloud tasks, 90+ plugins, and automatic PR review. All three gate actions behind approvals and a sandbox. Daisy is the small, open, model-agnostic option you host yourself; for a mature multi-surface agent on a vendor's cloud, use theirs.

## Install

Daisy targets **macOS on Apple Silicon**. Download the latest `.dmg` from the [Releases](https://github.com/ghovax/daisy/releases) page and drag **Daisy** to Applications; the build is self-signed, so Gatekeeper warns on first launch. Or build from source with the Nix-pinned toolchain.

See the [Installation guide](documentation/installation.md) for both paths in full.

## Quickstart

From the terminal:

```
daisy create --agent general-assistant --directory ~/code/project   # prints a session id
daisy send <id> "what does this project do?" --wait
daisy ps                                                            # what is running, and what waits on you
daisy attach <id>                                                   # follow it live
```

A session composes over the same API rather than over this command: `create_session` makes a peer and hands it a brief, `message_session` reaches a session in either direction, `end_session` stops one. Same daemon, same sockets, same tree — the tool carries the caller's identity, which an argv string cannot, so a peer is always a child of whoever made it, and its answer comes back as a message.

The daemon starts itself on the first command. From the app:

1. **Launch Daisy.** The daemon starts automatically; the app connects to it.
2. **Add a model key.** Open **Settings → Providers**, paste a key for any provider (or sign in with ChatGPT), and pick a model. Keys live in your Daisy configuration file — see the [Configuration guide](documentation/configuration.md), or run `daisy configure --all` to see every setting there is.
3. **Start a conversation.** Type a task. Approve tool calls as they come up, or relax the [permission mode](documentation/configuration.md#permission-modes) once you trust a flow.

The screen-control tools need a one-time Accessibility grant and Chrome's remote-debugging toggle — see the [Installation guide](documentation/installation.md#permissions-the-app-may-ask-for).

> [!NOTE]
> Opt in and the system prompt also carries a snapshot of how you work, sent to your model provider along with the rest of the prompt. It is off by default; see [what the agent sends to your model provider](SECURITY.md#what-the-agent-sends-to-your-model-provider).

## Where things live

Daisy follows the XDG convention rather than a single dot-directory: configuration in `~/.config/daisy`, durable state in `~/.local/share/daisy`, sockets in the runtime directory (which the OS clears on logout, so a crashed daemon leaves nothing behind), logs in `~/.local/state/daisy`, and caches in `~/.cache/daisy`.

Sessions are reachable only by whoever holds their handle: `create` mints a capability token, and every call to a session's socket must present it. The daemon's own API is guarded the same way, by a token it writes 0600 into the runtime directory. *Which* session is calling is not left to that token, though — a session runs as the same user and could read the file. On the unix socket the daemon asks the kernel for the peer's pid and resolves it to a session through the process session every worker leads, so a call is attributed to whoever actually made it.

> [!NOTE]
> A session's permission mode is fixed when it is created and cannot be changed afterwards, and a child is clamped to no looser a mode than its parent. There is no bypass mode and no standing "always allow" — the only runtime decisions are allow-once and deny. See the [Security notes](SECURITY.md).

## Documentation

The full guides — installation, the [`daisy` command](documentation/cli.md), configuration, architecture, authoring agents and skills, the tool surface, and development — live in the **[Documentation](documentation/README.md)**, which indexes them and sketches the project layout.

## Built with

[Tauri](https://tauri.app), [Next.js](https://nextjs.org), [Chakra UI](https://chakra-ui.com), [LangChain](https://www.langchain.com), [LiteLLM](https://litellm.ai), [FastAPI](https://fastapi.tiangolo.com), [Model Context Protocol](https://modelcontextprotocol.io), and [A2A](https://github.com/google/A2A)

## Contributing

Contributions are welcome — see the [Contributing guide](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © Giovanni Gravili
