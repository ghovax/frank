# Development

Daisy has three parts: the **Python harness** (one executable entered as `daisy`, `daisyd`, or a worker), the **Next.js web UI**, and the **Tauri desktop shell**. In development you run the daemon and the UI directly. The packaged app is built only for releases.

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
uv run daisy create --agent general-assistant --directory ~/code/project
uv run daisy send <id> "what does this project do?" --wait
```

The [`daisy` command](cli.md) is the full surface. To run the daemon in the foreground instead — the fastest way to watch a traceback — start it by name:

```sh
uv run python -m daisy daisyd
```

One image, three entry points, chosen by the first argument: `daisy` (the CLI), `daisyd` (the daemon), `worker` (a session). A bare launch lands in the CLI, which is why the daemon has to be asked for. `daisy daemon stop` takes down a foreground daemon and its sessions with it.

It listens on a unix socket in your runtime directory and on an ephemeral loopback port for GUI clients; `daisy daemon endpoint` reports the port and the capability token. State follows the XDG convention — configuration in `~/.config/daisy/`, durable state in `~/.local/share/daisy/`, logs in `~/.local/state/daisy/` — all created on first run. Add provider keys via `daisy configure`, the configuration file, or environment variables; see the [Configuration guide](configuration.md).

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

A new setting needs nothing beyond its `Field(description=...)` — no reference file to update, no listing to add it to. `daisy configure --all` walks the schema, so a setting is discoverable from the moment it exists. Write the description as the sentence you would want printed at a terminal, because that is exactly where it goes.

## Running the desktop app in dev

```sh
daisy daemon start      # the app connects to a daemon; it does not start one
cd web
bun run tauri:dev       # launches the Tauri window against the dev UI
```

Start the daemon first, in either order but before you expect the window to work. The app is a client: with nothing listening it shows the connection picker and says what to run, rather than launching a harness of its own.

## Building and signing

There are **two artifacts**, built independently, because the app is a client of the daemon rather than its container. Building one never rebuilds the other.

```sh
# The daemon (and the CLI, and every worker — one image, three entry points).
packaging/build-daemon.sh          # FORCE=1 to rebuild when the freshness guard says it is current

# The desktop app: a Tauri shell, no Python in it at all.
cd web && bun run tauri:build
```

The first freezes the harness with PyInstaller into `packaging/dist/Daisy Computer Use.app`, smoke-tests it by launching `daisyd` and waiting for it to answer on its socket, and is a no-op when nothing that goes into it has changed. The second produces `web/src-tauri/target/release/bundle/macos/Daisy.app` plus a `.dmg` under `bundle/dmg/`.

### Stable code-signing (recommended)

The screen-control tools (`control_screen`) need the macOS **Accessibility** grant, which is tied to code identity. Every session worker is a re-exec of the daemon binary for exactly this reason — one grant covers the fleet. Both artifacts carry the same `CFBundleName` and identifier, so signing both with one persistent identity keeps them a single **Daisy** row that survives rebuilds:

```sh
# once: create the self-signed identity in your login keychain
packaging/create-signing-cert.sh

# after each build, either or both:
packaging/sign-app.sh "packaging/dist/Daisy Computer Use.app"
packaging/sign-app.sh web/src-tauri/target/release/bundle/macos/Daisy.app
```

The daemon is signed `--deep` with `packaging/Entitlements.plist` — it needs to send Apple Events for its login-items and running-apps probes, and to load PyInstaller's dylibs without library validation. The app needs neither and signs plain; both entitlements used to sit on the app only because it was the daemon's parent process. The identity is self-signed, so Gatekeeper still warns on other machines until a build is Apple-notarized.

### Installing the daemon

```sh
ditto "packaging/dist/Daisy Computer Use.app" "/Applications/Daisy Computer Use.app"
ln -sf "/Applications/Daisy Computer Use.app/Contents/MacOS/daisy" /usr/local/bin/daisy
```

The symlink is what puts `daisy` and `daisyd` on your `PATH`, both entering the same signed image. Running from a checkout (`uv run daisy …`) works for everything except a stable Accessibility grant, since the interpreter is then the code identity.

## Tests

The repository ships **no committed test suite** — changes are verified ad hoc (lint with `uv run ruff check`, run `scripts/check_layers.py`, and drive the affected path through the CLI directly). `pyproject.toml` is already set up for `pytest` (`testpaths = ["tests"]`, `asyncio_mode = "auto"`), so if you add a `tests/` directory `uv run pytest` will pick it up.

## Project layout

See the [documentation index](README.md#the-shape-of-the-project) for the directory map. The harness is in `src/daisy/` (with `packaging/entry.py` as the frozen build's entry point), the UI in `web/src/`, and the Tauri shell in `web/src-tauri/`.
