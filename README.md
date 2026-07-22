<div align="center">

# 🌼 Daisy

**A local-first desktop workspace for AI agents.**

Daisy pairs a native macOS app with an open agent runtime. Agents can run shell commands, read and write files, search and fetch the web, control your Mac, and drive your browser — with a permission system in front of every action and your choice of model behind it.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) ![Platform: macOS (Apple Silicon)](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-black) ![Built with Tauri, Next.js, LangChain](https://img.shields.io/badge/built%20with-Tauri%2C%20Next.js%2C%20LangChain-6E56CF)

</div>

## What it is

Daisy is two things that are deliberately kept apart:

- **The harness** — a standalone Python server (FastAPI + LangChain) that runs the agents, dispatches tools, enforces permissions, and persists history. It speaks the [A2A](https://github.com/google/A2A) protocol and a small REST API.
- **The app** — a native macOS client (Tauri + Next.js) that is a polished interface to a harness. It ships with a harness bundled in, so it works out of the box with nothing to configure.

Because the two halves talk over HTTP, **the server does not have to run on your Mac.** Deploy the harness on a workstation, a VM, or a container, and point the app at it — the app becomes a thin, native front-end to a backend that lives wherever your compute, files, and network access should be. The bundled local server is simply the zero-configuration default. See [Run the server anywhere](#run-the-server-anywhere).

## What's different

Most agent harnesses are a CLI or a web chat bolted to a code sandbox. Daisy shares the basics with them — shell, file edits, semantic code search, web search and fetch, MCP, tasks, skills, memory, delegation. Where it diverges:

| | Daisy | The usual harness |
|---|---|---|
| **Interface** | Native macOS app, a thin client over the harness | A terminal or a browser tab |
| **Where it runs** | The harness is separable: keep it local, put it on a remote box, or reach it over SSH — the app just points at it | Tied to the host and process it launched in |
| **Models** | Any provider, or a ChatGPT login, switchable per session | Usually one provider, wired in |
| **Your machine** | Drives your real macOS apps and your own signed-in Chrome — actual logins and sessions | A throwaway headless browser with no session |
| **Reading the screen** | A plain-language search returns the few relevant elements; a script then acts on them | A full accessibility/DOM dump each step, or raw screenshots |
| **Permissions** | An approval gate with per-action risk levels sits in front of every action | An all-or-nothing sandbox |
| **State** | Lives on your disk in `~/.daisy` — your keys, your history | Often a hosted account |

The screen tools deserve the detail: `search_screen` ranks native-app or Chrome elements from a description of what you want, and `control_screen` composes a short Python script to act on them — so a twenty-row task is one script, not twenty round-trips. Both are opt-in and off until you turn them on. See [Tools](documentation/tools.md).

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

## Run the server anywhere

The app defaults to a bundled local server, but any Daisy client can point at any Daisy harness:

- **Local (default)** — the app manages a server on `127.0.0.1:8822`. Nothing to set up.
- **Remote URL** — run `python server.py` on another host and add its URL under **Settings → Connections**.
- **Over SSH** — add an SSH host and Daisy tunnels to the remote harness, so the server can live on a box you reach only over SSH.

This is what makes Daisy more than a desktop toy: the agent, its tools, and its file and network access run wherever you put the harness, while the interface stays native and local. See [`documentation/architecture.md`](documentation/architecture.md).

> [!WARNING]
> The harness has no built-in authentication. If you expose it beyond `localhost`, put it behind your own auth and transport security and never open it to the public internet. See [`SECURITY.md`](SECURITY.md).

## Documentation

Detailed guides live in [`documentation/`](documentation/):

| Guide | What's in it |
|-------|--------------|
| [Installation](documentation/installation.md) | Download, Gatekeeper, building from source |
| [Configuration](documentation/configuration.md) | Providers, keys, permissions, MCP, all config keys |
| [Architecture](documentation/architecture.md) | The client/server split, the harness, the app |
| [Agents & skills](documentation/agents-and-skills.md) | Authoring agents, skills, memory, MCP servers |
| [Tools](documentation/tools.md) | The full tool surface, including screen control (`search_screen`/`control_screen`) |
| [Development](documentation/development.md) | Dev environment, running the pieces, building the app |

## Built with

[Tauri](https://tauri.app), [Next.js](https://nextjs.org), [Chakra UI](https://chakra-ui.com), [LangChain](https://www.langchain.com) / [LangGraph](https://langchain-ai.github.io/langgraph/), [LiteLLM](https://litellm.ai), [FastAPI](https://fastapi.tiangolo.com), [Model Context Protocol](https://modelcontextprotocol.io), and [A2A](https://github.com/google/A2A)

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © Giovanni Gravili
