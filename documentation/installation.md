# Installation

XEAC targets **macOS on Apple Silicon (`aarch64`)**. The screen-control tools (`control_screen`) and the packaged app are macOS-specific. The harness itself is portable Python, but the desktop experience is built for the Mac.

## Option 1 — Download the app

1. Open the [**Releases**](https://github.com/ghovax/daisy/releases) page and download the latest `XEAC_<version>_aarch64.dmg`.
2. Open the `.dmg` and drag **XEAC** into **Applications**.
3. Launch it.

### Gatekeeper

The app is **self-signed, not Apple-notarized**. macOS Gatekeeper refuses the first launch with an "unidentified developer" or "damaged" message. This is expected. Clear it once, either way:

- **Right-click** `XEAC.app` → **Open** → **Open** in the dialog, or
- from a terminal:

  ```sh
  xattr -dr com.apple.quarantine /Applications/XEAC.app
  ```

Notarized builds are planned. Until then this one-time step is required.

### Permissions the app may ask for

- **Accessibility** — required for the screen-control tools (`control_screen`) to read and act on native apps. XEAC prompts you and deep-links to the right settings pane. Grant it to XEAC.
- **Chrome remote debugging** — required for the screen-control tools to drive your own Chrome. XEAC shows a one-click prompt that opens `chrome://inspect`. Enable the remote-debugging toggle once.

Neither is needed for plain chat or the file, shell, and web tools.

## Option 2 — Build from source

You need the [Nix](https://nixos.org) package manager (the repo ships a flake devshell that pins bun, Rust, and the Tauri CLI), and optionally [direnv](https://direnv.net).

```sh
git clone https://github.com/ghovax/daisy.git
cd xeac

# Enter the toolchain (bun, rustc, cargo, cargo-tauri, pkg-config)
direnv allow            # or, without direnv:  nix develop

# Install web dependencies
cd web && bun install && cd ..

# Build the desktop app (also freezes the Python harness into a bundled helper)
cd web && bun run tauri:build
```

The build produces `web/src-tauri/target/release/bundle/macos/XEAC.app` and a `.dmg` under `bundle/dmg/`. For a stable code-signing identity (so the Accessibility grant for the screen-control tools survives rebuilds) and packaging details, see [Development guide](development.md#building-and-signing).

You also need a local Python environment for the harness itself (a `.venv` with the project installed) — see [Development guide](development.md#running-it).
