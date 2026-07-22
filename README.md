<div align="center">

# 🌼 Daisy

**An open agent harness you can modify.**

The harness is the code between the model and your machine — turn loop, tools, prompts, permissions — and in Daisy all of it is yours to edit. Host it anywhere, and drive it from a bundled macOS app or your own code.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) ![Platform: macOS (Apple Silicon)](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-black) ![Built with Tauri, Next.js, LangChain](https://img.shields.io/badge/built%20with-Tauri%2C%20Next.js%2C%20LangChain-6E56CF)

</div>

## What it is

Daisy is two things, kept apart:

- **The harness** — the Python agent runtime. Import and drive it directly, or run it as a server that exposes it over [A2A](https://github.com/google/A2A) and REST. Same runtime either way.
- **The app** — a native macOS client (Tauri + Next.js). It bundles a harness, so it works out of the box.

The two halves talk only over HTTP, so the app stays a thin native front-end while the compute, files, and network live wherever you host the harness. Configure several locations and pick one per session; tools take a location, so a session can span machines. See [Run the server anywhere](#run-the-server-anywhere).

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
| **Reach** | One macOS app and a server | Terminal, VS Code, JetBrains, desktop, web, mobile, Slack, CI, GitHub review; macOS and Windows | CLI, IDEs, desktop, cloud/web, Chrome, GitHub review; macOS and Windows |

Two design choices distinguish Daisy:

- **Structure, not screenshots.** It reads the screen as a semantic search over the accessibility tree and DOM, returning a few ranked elements where the rivals reason over screenshots — a query costs a handful of elements, not a downscaled image.
- **A composed script, not a click-by-click loop.** `control_screen` runs a Python program whose primitives (`click`, `type`, `scroll`, `evaluate`, …) are the *same* on native apps and in the browser. A whole task — loop over rows, branch on what you find, call the page's own API in one line — is a single call, not a screenshot‑decide‑act round trip per click. Far fewer model turns to finish the job.

The trade-off: it needs an accessibility tree or DOM to read, where a screenshot approach works on anything drawn on screen. See [Tools](documentation/tools.md).

Elsewhere they lead: more polish, more places to run, deeper ecosystems — Claude Code's subagents, hooks, plugins, and Agent SDK; Codex's cloud tasks, 90+ plugins, and automatic PR review. All three gate actions behind approvals and a sandbox. Daisy is the small, open, model-agnostic option you host yourself; for a mature multi-surface agent on a vendor's cloud, use theirs.

## Install

Daisy targets **macOS on Apple Silicon**. Download the latest `.dmg` from the [Releases](https://github.com/ghovax/daisy/releases) page and drag **Daisy** to Applications; the build is self-signed, so Gatekeeper warns on first launch. Or build from source with the Nix-pinned toolchain.

See the [Installation guide](documentation/installation.md) for both paths in full.

## Quickstart

1. **Launch Daisy.** The bundled server starts automatically; the app connects to it.
2. **Add a model key.** Open **Settings → Providers**, paste a key for any provider (or sign in with ChatGPT), and pick a model. Keys live in your Daisy configuration file — see the [Example configuration](configuration.example.yaml).
3. **Start a conversation.** Type a task. Approve tool calls as they come up, or relax the [permission mode](documentation/configuration.md#permission-modes) once you trust a flow.

The screen-control tools need a one-time Accessibility grant and Chrome's remote-debugging toggle — see the [Installation guide](documentation/installation.md#permissions-the-app-may-ask-for).

> [!NOTE]
> Opt in and the system prompt also carries a snapshot of how you work, sent to your model provider along with the rest of the prompt. It is off by default; see [what the agent sends to your model provider](SECURITY.md#what-the-agent-sends-to-your-model-provider).

## Run the server anywhere

The app defaults to a bundled local server, but any client can point at any harness — local, a remote URL, or over SSH. The compute, files, and network live wherever you host the harness while the interface stays native and local. See the [Architecture guide](documentation/architecture.md#connections-local-remote-ssh) for the connection modes.

> [!WARNING]
> The harness has no built-in authentication. Behind `localhost` that is fine; if you expose it, put your own auth and transport security in front. See the [Security notes](SECURITY.md).

## Documentation

The full guides — installation, configuration, architecture, authoring agents and skills, the tool surface, and development — live in the **[Documentation](documentation/README.md)**, which indexes them and sketches the project layout.

## Built with

[Tauri](https://tauri.app), [Next.js](https://nextjs.org), [Chakra UI](https://chakra-ui.com), [LangChain](https://www.langchain.com) / [LangGraph](https://langchain-ai.github.io/langgraph/), [LiteLLM](https://litellm.ai), [FastAPI](https://fastapi.tiangolo.com), [Model Context Protocol](https://modelcontextprotocol.io), and [A2A](https://github.com/google/A2A)

## Contributing

Contributions are welcome — see the [Contributing guide](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © Giovanni Gravili
