---
name: nix-workflow
title: Nix system management and development workflow
description: How this machine is managed with Nix, how to set up development environments with direnv, and which tooling to use in each language domain.
importance: high
tags: nix, nix-darwin, direnv, uv, bun, development
---

## System management

This machine (Apple Silicon `aarch64-darwin`) is managed **declaratively** with **nix-darwin** + **home-manager** (as a nix-darwin module) + **nix-homebrew**. The config repo is `~/.config/nix-darwin` (flake-based, git-tracked).

**Apply changes:**
- `rebuild` — runs `sudo darwin-rebuild switch --flake ~/.config/nix-darwin`
- `update` — bumps all flake inputs and rebuilds
- Rollback: `sudo darwin-rebuild rollback`

**Hard rule:** never install software imperatively. No `brew install`, `npm i -g`, `pip install` (global), `cargo install`, or `curl ... | sh`. Add CLI tools to `home.nix` → `home.packages`, GUI apps to `darwin.nix` → `homebrew.casks`, then `rebuild`.

## Per-project environments with direnv

Each project has a `flake.nix` (isolated devshell) and `.envrc`:

- `.envrc` uses `use flake` to load the project's devshell on directory entry and unload on exit.
- `direnv allow` once after copying `.envrc` into a project.
- **Never install runtimes globally.** Language runtimes (node, python, go, rust, etc.) belong in the per-project devshell or are invoked through the project flake's tools.
- Local secrets (API keys) are loaded from a gitignored `.env` via `dotenv_if_exists .env`.

**This project (langmesh):**
- Root `flake.nix` provides `bun` (the JS runtime/package-manager for the web UI).
- `web/flake.nix` provides `nodejs_22` (for the web UI build toolchain).
- Python is managed via `uv` through `pyproject.toml`.

## Tooling conventions by language

**JavaScript / TypeScript / Web UI:**
- Use `bun` for running scripts, installing dependencies, linting, building. Not `npm` or `yarn`.
- Commands: `bun run dev`, `bun run build`, `bun run lint`, `bun run typecheck`.

**Python:**
- Use `uv` and `uvx` preferentially over bare `python` or `pip`.
- `uv run python` — run a Python script in the project's virtual environment.
- `uv run pytest` — run tests.
- `uv run ruff` — lint/format.
- `uvx <tool>` — run an ephemeral CLI tool (e.g. `uvx jq`, `uvx black`).
- Fall back to bare `python` only when `uv` is unavailable.
