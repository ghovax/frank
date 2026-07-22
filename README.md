<div align="center">

# 🌼 Daisy

**An open agent harness you can modify.**

Daisy is an agent harness — the code between the model and your machine that runs the turn loop, defines the tools, holds the prompts, and enforces permissions. All of it is editable, and that matters: the harness shapes the result as much as the model does. Run it as a server you host — laptop, VM, container, or over SSH — driven by a bundled macOS app or your own code.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) ![Platform: macOS (Apple Silicon)](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-black) ![Built with Tauri, Next.js, LangChain](https://img.shields.io/badge/built%20with-Tauri%2C%20Next.js%2C%20LangChain-6E56CF)

</div>

## What it is

Daisy is two things that are deliberately kept apart:

- **The harness** — the Python agent runtime. Import and drive it directly, or run it as a server that exposes it over [A2A](https://github.com/google/A2A) and REST. Same runtime either way.
- **The app** — a native macOS client (Tauri + Next.js) that is a polished interface to a harness. It ships with a harness bundled in, so it works out of the box with nothing to configure.

The two halves talk only over HTTP, so the harness runs detached: the app stays a thin native front-end while the compute, files, and network live wherever you host it. Configure several locations, local and remote, and pick one per session — tools take a location, so a single session can span machines. See [Run the server anywhere](#run-the-server-anywhere).

## Why own the harness

The harness writes the system prompt, defines the tools, manages context, and sets what the agent may do. The same model does different work under different harnesses — OpenCode versus Claude Code or Codex, say. Daisy lets you change that layer:

- **Permission rules are code, not fixed config** — edit them to give the model more or less latitude ([Permissions](documentation/configuration.md#permissions)).
- **The agent can work on Daisy itself.** Its prompt says it's running Daisy; open the Daisy repo as the project and it can read and edit the harness, then you rebuild ([Architecture](documentation/architecture.md)).
- **The agent can start with context about you** — an opt-in snapshot of your machine and habits, off by default ([What it sends](SECURITY.md#what-the-agent-sends-to-your-model-provider)).

## How it compares

The two closest tools are [Claude Code](https://code.claude.com) and [OpenAI Codex](https://github.com/openai/codex). Both are more mature than Daisy, and as of 2026 both do the things that once made Daisy unusual: each drives a real, logged-in browser and controls native macOS apps, and Codex — like Daisy — is open source and can run on non-OpenAI models. This is not a list of things only Daisy does.

| | Daisy | Claude Code | OpenAI Codex |
|---|---|---|---|
| **License** | Open source (MIT) | Proprietary | Open-source CLI (Apache-2.0); cloud and models are OpenAI's |
| **Models** | Any provider, or a ChatGPT login, per session — the screen tools included | Claude first; third-party providers for coding on the CLI and VS Code, but its browser and computer use need an Anthropic plan | GPT-5 Codex by default; the CLI can also point at OpenRouter, Ollama, LM Studio, or any compatible endpoint |
| **Where it runs** | A harness you self-host — local, a VM, a container, or over SSH — with a native app pointed at it | Proprietary client; long tasks run on Anthropic's cloud | Local CLI, IDEs, and a desktop app; async tasks run on OpenAI's cloud |
| **Screen control** | Native macOS apps and your own Chrome, read as ranked accessibility/DOM elements from a plain-language search — screenshots only when you ask | Your real Chrome session, plus macOS computer use driven by downscaled screenshots (research preview, Pro/Max) | In-app and Chrome-extension browser, plus background macOS computer use driven by screenshots |
| **Reach** | One macOS app and a server | Terminal, VS Code, JetBrains, desktop, web, mobile, Slack, CI, GitHub review; macOS and Windows | CLI, IDEs, desktop, cloud/web, Chrome, GitHub review; macOS and Windows |

Two design choices distinguish Daisy:

- **Structure, not screenshots.** It reads the screen as a semantic search over the accessibility tree and DOM that returns a few ranked elements, where both rivals' computer use reasons over screenshots — so a query costs a handful of elements instead of a downscaled image.
- **A composed script, not a click-by-click loop.** `control_screen` runs an ordinary Python program whose primitives (`click`, `type`, `scroll`, `evaluate`, …) are the *same* on a native app and in the browser, so a whole task — loop over every row, branch on what you find, pull a page's own API in a single line — is one call rather than a screenshot‑decide‑act round trip per click. One shared abstraction over both surfaces, and far fewer, leaner model turns to finish the job.

The trade-off: this depends on there being an accessibility tree or DOM to read, whereas a screenshot approach works on anything drawn on screen, structure or not. See [Tools](documentation/tools.md).

On everything else, Claude Code and Codex lead: they are further along on polish, run in many more places, and carry deep ecosystems — Claude Code's subagents, hooks, plugins, and Agent SDK; Codex's cloud tasks, 90+ plugins, and automatic PR review. All three gate actions behind approvals and a sandbox. Daisy is the small, open, model-agnostic option you host yourself; for a mature multi-surface agent backed by a big vendor's cloud, use theirs.

## Install

Daisy targets **macOS on Apple Silicon**.

### Download

Grab the latest `.dmg` from the [**Releases**](https://github.com/ghovax/daisy/releases) page, open it, and drag **Daisy** to Applications.

> [!NOTE]
> The app is **self-signed, not Apple-notarized**, so Gatekeeper will warn on first launch. Right-click **Daisy.app → Open** and confirm once, or run `xattr -dr com.apple.quarantine /Applications/Daisy.app`. Notarized builds are planned.

### Build from source

See the [Development guide](documentation/development.md). In short:

```sh
git clone https://github.com/ghovax/daisy.git
cd daisy
direnv allow                       # or: nix develop  (provides bun, rust, cargo-tauri)
cd web && bun install && cd ..
cd web/src-tauri && cargo tauri build
```

## Quickstart

1. **Launch Daisy.** The bundled server starts automatically; the app connects to it.
2. **Add a model key.** Open **Settings → Providers**, paste a key for any provider (or sign in with ChatGPT), and pick a model. Keys live in your Daisy configuration file — see the [Example configuration](configuration.example.yaml).
3. **Start a conversation.** Type a task. Approve tool calls as they come up, or relax the [permission mode](documentation/configuration.md#permissions) once you trust a flow.

To enable the distinctive tools:

- **Screen control** (`search_screen`/`control_screen`) needs macOS Accessibility permission for native apps (Daisy prompts you).
- Driving **your own Chrome** needs Chrome's remote-debugging toggle enabled once (`chrome://inspect`). Daisy shows a one-click prompt.

> [!NOTE]
> So it fits your setup from the first turn, the agent's system prompt carries a snapshot of your machine and — only if you opt in — of how you work (identity, locale, frequent files and apps, and the like). Whatever is in the prompt goes to your configured model provider, so that snapshot sends personal data there. It is a deliberate choice and the user snapshot is opt-in; see [what the agent sends to your model provider](SECURITY.md#what-the-agent-sends-to-your-model-provider) for the reasoning and how to shape it.

## Run the server anywhere

The app defaults to a bundled local server, but any Daisy client can point at any Daisy harness:

- **Local (default)** — the app manages a server on `127.0.0.1:8822`. Nothing to set up.
- **Remote URL** — run `python server.py` on another host and add its URL under **Settings → Connections**.
- **Over SSH** — add an SSH host and Daisy tunnels to the remote harness, so the server can live on a box you reach only over SSH.

This is what makes Daisy more than a desktop toy: the agent, its tools, and its file and network access run wherever you put the harness, while the interface stays native and local. See the [Architecture guide](documentation/architecture.md).

> [!WARNING]
> The harness has no built-in authentication. If you expose it beyond `localhost`, put it behind your own auth and transport security and never open it to the public internet. See the [Security notes](SECURITY.md).

## Documentation

The full guides — installation, configuration, architecture, authoring agents and skills, the tool surface, and development — live in the **[Documentation](documentation/README.md)**, which indexes them and sketches the project layout. Quick jumps:

- [Set up providers and permissions](documentation/configuration.md)
- [How the pieces fit together](documentation/architecture.md)
- [The tool surface and screen control](documentation/tools.md)

## Built with

[Tauri](https://tauri.app), [Next.js](https://nextjs.org), [Chakra UI](https://chakra-ui.com), [LangChain](https://www.langchain.com) / [LangGraph](https://langchain-ai.github.io/langgraph/), [LiteLLM](https://litellm.ai), [FastAPI](https://fastapi.tiangolo.com), [Model Context Protocol](https://modelcontextprotocol.io), and [A2A](https://github.com/google/A2A)

## Contributing

Contributions are welcome — see the [Contributing guide](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © Giovanni Gravili
