<div align="center">

# 🌼 Daisy

**A local-first desktop workspace for AI agents.**

Daisy pairs a native macOS app with an open agent runtime. Agents can run shell commands, read and write files, search and fetch the web, control your Mac, and drive your browser — with a permission system in front of every action and your choice of model behind it.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) ![Platform: macOS (Apple Silicon)](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-black) ![Built with Tauri, Next.js, LangChain](https://img.shields.io/badge/built%20with-Tauri%2C%20Next.js%2C%20LangChain-6E56CF)

<img alt="Daisy — an agent working through a task with live tool calls" src="documentation/assets/screenshots/hero.png" width="820">

</div>

## What it is

Daisy is two things that are deliberately kept apart:

- **The harness** — a standalone Python server (FastAPI + LangChain) that runs the agents, dispatches tools, enforces permissions, and persists history. It speaks the [A2A](https://github.com/google/A2A) protocol and a small REST API.
- **The app** — a native macOS client (Tauri + Next.js) that is a polished interface to a harness. It ships with a harness bundled in, so it works out of the box with nothing to configure.

Because the two halves talk over HTTP, **the server does not have to run on your Mac.** Deploy the harness on a workstation, a VM, or a container, and point the app at it — the app becomes a thin, native front-end to a backend that lives wherever your compute, files, and network access should be. The bundled local server is simply the zero-configuration default. See [Run the server anywhere](#run-the-server-anywhere).

## Highlights

- **Bring your own model.** Anthropic, OpenAI, Google, OpenRouter, xAI, DeepSeek, Groq, Mistral, any OpenAI-compatible endpoint — or sign in with a ChatGPT subscription. Switch per session.
- **A real tool surface.** Shell, file read/edit/write/search, web search, tiered URL fetching, file downloads, MCP tools and resources, tasks and goals, skills, and rendered artifacts.
- **Controls your Mac.** A computer-use tool drives native apps through the macOS accessibility tree, and a browser tool drives *your own* Chrome — real logins, real sessions.
- **Permissions in front of everything.** Every risky action can pause for approval, with per-action risk levels and modes from ask-always to fully autonomous. Bash runs sandboxed to the workspace by default.
- **Multiple agents, delegation, and skills.** Ship-with profiles for research and coding, agent-to-agent delegation, reusable `SKILL.md` capabilities, and persistent per-project memory — all plain Markdown you can edit.
- **MCP-native.** Add any [Model Context Protocol](https://modelcontextprotocol.io) server; hosted integrations like Composio are first-class.
- **Local-first.** State lives in `~/.daisy`. Your keys, your history, your machine.

## Screenshots

<table>
  <tr>
    <td width="50%">
      <img alt="Model providers and settings" src="documentation/assets/screenshots/providers.png">
      <p align="center"><b>Bring your own model</b><br/>Any provider or a ChatGPT subscription.</p>
    </td>
    <td width="50%">
      <img alt="Computer-use and browser control" src="documentation/assets/screenshots/computer-use.png">
      <p align="center"><b>Controls your Mac</b><br/>Native apps and your own browser.</p>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <img alt="Artifacts and projects" src="documentation/assets/screenshots/artifacts.png">
      <p align="center"><b>Artifacts &amp; projects</b><br/>Rendered output, organized workspaces.</p>
    </td>
    <td width="50%">
      <img alt="Permission approval for a tool call" src="documentation/assets/screenshots/permissions.png">
      <p align="center"><b>Permissions in front</b><br/>Approve, always-allow, or deny.</p>
    </td>
  </tr>
</table>

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

- **Computer-use** needs macOS Accessibility permission (Daisy prompts you).
- **Browser control** needs Chrome's remote-debugging toggle enabled once (`chrome://inspect`). Daisy shows a one-click prompt.

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
| [Tools](documentation/tools.md) | The full tool surface, including computer-use and browser |
| [Development](documentation/development.md) | Dev environment, running the pieces, building the app |

## Built with

[Tauri](https://tauri.app), [Next.js](https://nextjs.org), [Chakra UI](https://chakra-ui.com), [LangChain](https://www.langchain.com) / [LangGraph](https://langchain-ai.github.io/langgraph/), [LiteLLM](https://litellm.ai), [FastAPI](https://fastapi.tiangolo.com), [Model Context Protocol](https://modelcontextprotocol.io), and [A2A](https://github.com/google/A2A)

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © Giovanni Gravili
