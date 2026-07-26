# Development

XEAC has three parts: the **Python harness** (one executable entered as `xeac`, `xeacd`, or a worker), the **Next.js web UI**, and the **Tauri desktop shell**. In development you run the daemon and the UI directly. The packaged app is built only for releases.

## Toolchain

The repo ships a **Nix flake devshell** that pins bun, Rust, `cargo-tauri`, and `pkg-config`. With [direnv](https://direnv.net):

```sh
direnv allow            # loads the devshell on entry; or run `nix develop`
```

The Python harness runs from a local virtualenv managed with [uv](https://docs.astral.sh/uv/):

```sh
uv sync                 # create .venv and install the project + dependencies
```

## Running it

The CLI starts the daemon on its first command, so usually there is nothing to launch:

```sh
uv run xeac create --agent general-assistant --directory ~/code/project
uv run xeac send <id> "what does this project do?" --wait
```

The [`xeac` command](cli.md) is the full surface. To run the daemon in the foreground instead — the fastest way to watch a traceback — start it by name:

```sh
uv run python -m xeac xeacd
```

One image, three entry points, chosen by the first argument: `xeac` (the CLI), `xeacd` (the daemon), `worker` (a session). A bare launch lands in the CLI, which is why the daemon has to be asked for. `xeac daemon stop` takes down a foreground daemon and its sessions with it.

It listens on a unix socket in your runtime directory and on an ephemeral loopback port for GUI clients; `xeac daemon endpoint` reports the port and the capability token. State follows the XDG convention — configuration in `~/.config/xeac/`, durable state in `~/.local/share/xeac/`, logs in `~/.local/state/xeac/` — all created on first run. Add provider keys via `xeac configure`, the configuration file, or environment variables; see the [Configuration guide](configuration.md).

## Running the web UI

```sh
cd web
bun install
bun run dev             # http://localhost:3000, talks to the daemon's loopback port
```

Useful scripts (in `web/`):

- `bun run lint` — lint the UI.
- `bun run tauri:dev` / `bun run tauri:build` — the desktop shell (see below).
- `bun run build` — production static export (to `web/out`).
- `bun run build:events` — regenerate the TypeScript event schema from the Python models (`scripts/generate_event_schema.py`). Run this whenever the event contract changes.

Outside `web/`, `scripts/check_layers.py` enforces the package layering (`base` → `protocol` → `computer`/`locations` → `runtime` → `worker`, with the daemon never importing the runtime) and the invariant that `computer/` is never imported at module level — a parked worker that has loaded PyObjC is not safe to fork.

`scripts/generate_configuration_reference.py` writes `configuration.example.yaml` from the configuration schema, and `--check` fails when the committed file has drifted from it. Run it after adding a setting, renaming one, changing a default, or editing a `Field(description=...)` or a `Default(...)` — the reference is generated precisely so it cannot describe a setting the code does not have.

## Running the desktop app in dev

```sh
cd web
bun run tauri:dev       # launches the Tauri window against the dev UI + a local daemon
```

## Building and signing

```sh
cd web
bun run tauri:build
```

This runs `packaging/build-sidecar.sh` (freezes the harness into a bundled helper with PyInstaller — a no-op when nothing changed, and it smoke-tests the frozen daemon before the build proceeds) and produces `web/src-tauri/target/release/bundle/macos/XEAC.app` plus a `.dmg` under `bundle/dmg/`.

### Stable code-signing (recommended)

The screen-control tools (`control_screen`) need the macOS **Accessibility** grant, which is tied to the app's code identity. Every session worker is a re-exec of the same signed binary for exactly this reason — one grant covers the fleet. To keep that grant across rebuilds, sign with the persistent local identity:

```sh
# once: create the self-signed identity in your login keychain
packaging/create-signing-cert.sh

# after each build: restore symlinks (undo Tauri's dereferencing) and sign
packaging/sign-app.sh web/src-tauri/target/release/bundle/macos/XEAC.app
```

`sign-app.sh` also restores the frozen helper's symlink layout, which brings the app back from ~440 MB to ~230 MB (Tauri's resource copier otherwise dereferences PyInstaller's symlinks and doubles the bundle). The identity is self-signed, so Gatekeeper still warns on other machines until a build is Apple-notarized.

## Tests

The repository ships **no committed test suite** — changes are verified ad hoc (lint with `uv run ruff check`, run `scripts/check_layers.py`, and drive the affected path through the CLI directly). `pyproject.toml` is already set up for `pytest` (`testpaths = ["tests"]`, `asyncio_mode = "auto"`), so if you add a `tests/` directory `uv run pytest` will pick it up.

## Project layout

See the [documentation index](README.md#the-shape-of-the-project) for the directory map. The harness is in `src/xeac/` (with `server.py` as the frozen build's entry point), the UI in `web/src/`, and the Tauri shell in `web/src-tauri/`.
