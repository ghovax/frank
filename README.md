<div align="center">

# 🌼 Daisy

**A local-first desktop workspace for AI agents.**

Daisy pairs a native macOS app with an open agent runtime. Agents can run shell commands, read and write files, search and fetch the web, control your Mac, and drive your browser — with a permission system in front of every action and your choice of model behind it.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) ![Platform: macOS (Apple Silicon)](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-black) ![Built with Tauri, Next.js, LangChain](https://img.shields.io/badge/built%20with-Tauri%2C%20Next.js%2C%20LangChain-6E56CF)

</div>

## What it is

Daisy is two things that are deliberately kept apart:

- **The harness** — first a Python library: the `daisy` package is the agent runtime that runs the turn loop, dispatches tools, enforces permissions, talks to models, and persists history (built on LangChain), and you can import and drive it directly. A thin server (FastAPI) wraps that library to expose it over the [A2A](https://github.com/google/A2A) protocol and a small REST API. Use the library in your own code, or run the server — same runtime underneath.
- **The app** — a native macOS client (Tauri + Next.js) that is a polished interface to a harness. It ships with a harness bundled in, so it works out of the box with nothing to configure.

Because the two halves talk over HTTP, **the harness runs detached, and you are not tied to one of them.** Run it locally for zero setup, on a workstation, a VM, or a container, or reach one over SSH — configure several locations, local and remote, and choose per session which the agent runs against. The app stays a thin, native front-end; the compute, files, and network access live wherever you put the harness. Its shell and file tools take a location too, so one agent can act across more than one machine in a single session. See [Run the server anywhere](#run-the-server-anywhere).

## How it compares

The closest tools are [Claude Code](https://www.anthropic.com/claude-code) and [OpenAI Codex](https://developers.openai.com/codex/) — both excellent, both further along than Daisy, and both sharing the coding basics with it: shell, file edits, semantic search, web search, MCP, and delegated agents. Being honest about where each stands:

| | Daisy | Claude Code | OpenAI Codex |
|---|---|---|---|
| **Model** | Any provider, or a ChatGPT login, switchable per session | Claude models; third-party providers on the CLI and VS Code | OpenAI's GPT-5 Codex models only |
| **Where it runs** | A harness you self-host — local, a VM, a container, or over SSH — with a native app pointed at it; one agent can span several configured locations in a session | Vendor client; long tasks run on Anthropic's cloud | Vendor client; async tasks run in OpenAI's cloud |
| **Your machine** | Drives native macOS apps and your own signed-in Chrome for real, logged-in tasks, read through plain-language element search | Chrome integration aimed at debugging web apps; no native-app control | Files, shell, and tests only; no desktop or browser control |
| **Source** | Open source (MIT); history and keys stay on your disk | Proprietary | Proprietary |

Where they lead, plainly: both meet you in far more places — terminal, several IDEs, web, mobile, Slack, and CI or GitHub review — and carry deeper ecosystems, from Claude Code's subagents, hooks, plugins, and Agent SDK to Codex's cloud tasks and automatic PR review. Both also gate actions behind approvals and a sandbox, as Daisy does. Daisy is narrower and younger: macOS-only, one app and a server, aimed at the rows above. If you want a polished coding agent tied to one vendor's models and cloud, use theirs. If you want an open, model-agnostic agent you host yourself that can act on your real desktop, that is the gap Daisy fills.

The screen tools are the clearest example: `search_screen` ranks native-app or Chrome elements from a description of what you want, and `control_screen` composes a short Python script to act on them, so a twenty-row task is one script rather than twenty round-trips. Both are opt-in and stay off until you turn them on. See [Tools](documentation/tools.md).

## Install

Daisy targets **macOS on Apple Silicon**.

### Download

Grab the latest `.dmg` from the [**Releases**](https://github.com/ghovax/daisy/releases) page, open it, and drag **Daisy** to Applications.

> [!NOTE]
> The app is **self-signed, not Apple-notarized**, so Gatekeeper will warn on first launch. Right-click **Daisy.app → Open** and confirm once, or run `xattr -dr com.apple.quarantine /Applications/Daisy.app`. Notarized builds are planned.

### Build from source

See [`documentation/development.md`](documentation/development.md). In short:

```sh
git clone https://github.com/ghovax/daisy.git
cd daisy
direnv allow                       # or: nix develop  (provides bun, rust, cargo-tauri)
cd web && bun install && cd ..
cd web/src-tauri && cargo tauri build
```

## Quickstart

1. **Launch Daisy.** The bundled server starts automatically; the app connects to it.
2. **Add a model key.** Open **Settings → Providers**, paste a key for any provider (or sign in with ChatGPT), and pick a model. Keys are stored in `~/.daisy/configuration.yaml` — see [`configuration.example.yaml`](configuration.example.yaml).
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

This is what makes Daisy more than a desktop toy: the agent, its tools, and its file and network access run wherever you put the harness, while the interface stays native and local. See [`documentation/architecture.md`](documentation/architecture.md).

> [!WARNING]
> The harness has no built-in authentication. If you expose it beyond `localhost`, put it behind your own auth and transport security and never open it to the public internet. See [`SECURITY.md`](SECURITY.md).

## Documentation

The full guides — installation, configuration, architecture, authoring agents and skills, the tool surface, and development — live in **[`documentation/`](documentation/README.md)**, which indexes them and sketches the project layout. Quick jumps: [set up providers and permissions](documentation/configuration.md), [how the pieces fit together](documentation/architecture.md), [the tool surface and screen control](documentation/tools.md).

## Built with

[Tauri](https://tauri.app), [Next.js](https://nextjs.org), [Chakra UI](https://chakra-ui.com), [LangChain](https://www.langchain.com) / [LangGraph](https://langchain-ai.github.io/langgraph/), [LiteLLM](https://litellm.ai), [FastAPI](https://fastapi.tiangolo.com), [Model Context Protocol](https://modelcontextprotocol.io), and [A2A](https://github.com/google/A2A)

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © Giovanni Gravili
