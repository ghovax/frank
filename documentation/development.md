# Development

XEAC has three parts: the **Python harness**, the **Next.js web UI**, and the **Tauri desktop shell**. In development you run the harness and the UI directly. The packaged app is built only for releases.

## Toolchain

The repo ships a **Nix flake devshell** that pins bun, Rust, `cargo-tauri`, and `pkg-config`. With [direnv](https://direnv.net):

```sh
direnv allow            # loads the devshell on entry; or run `nix develop`
```

The Python harness runs from a local virtualenv managed with [uv](https://docs.astral.sh/uv/):

```sh
uv sync                 # create .venv and install the project + dependencies
```

## Running the harness

```sh
uv run python -m xeac daemon
# or: PYTHONPATH=src .venv/bin/python server.py
```

It serves on `http://127.0.0.1:8822`. State is read from and written to `~/.config/xeac/` (created on first run). Add provider keys there or via environment variables — see [Configuration guide](configuration.md).

## Running the web UI

```sh
cd web
bun install
bun run dev             # http://localhost:3000, talks to the harness on :8822
```

Useful scripts (in `web/`):

- `bun run lint` — lint the UI.
- `bun run build` — production static export (to `web/out`).
- `bun run build:events` — regenerate the TypeScript event schema from the Python models (`scripts/generate_event_schema.py`). Run this whenever the event contract changes.

## Running the desktop app in dev

```sh
cd web/src-tauri
cargo tauri dev         # launches the Tauri window against the dev UI + a local harness
```

## Building and signing

```sh
cd web/src-tauri
cargo tauri build
```

This runs `packaging/build-sidecar.sh` (freezes the harness into a bundled helper with PyInstaller — a no-op when nothing changed) and produces `target/release/bundle/macos/XEAC.app` plus a `.dmg`.

### Stable code-signing (recommended)

The screen-control tools (`control_screen`) need the macOS **Accessibility** grant, which is tied to the app's code identity. To keep that grant across rebuilds, sign with the persistent local identity:

```sh
# once: create the self-signed identity in your login keychain
packaging/create-signing-cert.sh

# after each build: restore symlinks (undo Tauri's dereferencing) and sign
packaging/sign-app.sh web/src-tauri/target/release/bundle/macos/XEAC.app
```

`sign-app.sh` also restores the frozen helper's symlink layout, which brings the app back from ~440 MB to ~230 MB (Tauri's resource copier otherwise dereferences PyInstaller's symlinks and doubles the bundle). The identity is self-signed, so Gatekeeper still warns on other machines until a build is Apple-notarized.

## Tests

The repository ships **no committed test suite** — changes are verified ad hoc (compile and lint with `uv run ruff check`, import the server, and drive the affected path directly). `pyproject.toml` is already set up for `pytest` (`testpaths = ["tests"]`, `asyncio_mode = "auto"`), so if you add a `tests/` directory `uv run pytest` will pick it up.

## Project layout

See the [documentation index](README.md#the-shape-of-the-project) for the directory map. The harness runtime is in `src/xeac/` (with `server.py` as a thin launch shim), the UI in `web/src/`, and the Tauri shell in `web/src-tauri/`.
