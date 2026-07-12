# Installation

Daisy targets **macOS on Apple Silicon (`aarch64`)**. The computer-use tool and the
packaged app are macOS-specific; the harness itself is portable Python, but the desktop
experience is built for the Mac.

## Option 1 — Download the app

1. Open the [**Releases**](https://github.com/ghovax/daisy/releases) page and download the
   latest `Daisy_<version>_aarch64.dmg`.
2. Open the `.dmg` and drag **Daisy** into **Applications**.
3. Launch it.

### Gatekeeper

The app is **self-signed, not Apple-notarized**. macOS Gatekeeper will refuse the first
launch with an "unidentified developer" or "damaged" message. This is expected. Clear it
once, either way:

- **Right-click** `Daisy.app` → **Open** → **Open** in the dialog, or
- from a terminal:

  ```sh
  xattr -dr com.apple.quarantine /Applications/Daisy.app
  ```

Notarized builds are planned; until then this one-time step is required.

### Permissions the app may ask for

- **Accessibility** — required for the computer-use tool (controlling native apps). Daisy
  prompts you and deep-links to the right settings pane. Grant it to Daisy.
- **Chrome remote debugging** — required for the browser tool. Daisy shows a one-click
  prompt that opens `chrome://inspect`; enable the remote-debugging toggle once.

Neither is needed for plain chat or the file, shell, and web tools.

## Option 2 — Build from source

You need the [Nix](https://nixos.org) package manager (the repo ships a flake devshell
that pins bun, Rust, and the Tauri CLI) and, ideally, [direnv](https://direnv.net).

```sh
git clone https://github.com/ghovax/daisy.git
cd daisy

# Enter the toolchain (bun, rustc, cargo, cargo-tauri, pkg-config)
direnv allow            # or, without direnv:  nix develop

# Install web dependencies
cd web && bun install && cd ..

# Build the desktop app (also freezes the Python harness into a bundled helper)
cd web/src-tauri && cargo tauri build
```

The build produces `web/src-tauri/target/release/bundle/macos/Daisy.app` and a `.dmg`
under `bundle/dmg/`. For a stable code-signing identity (so the computer-use Accessibility
grant survives rebuilds) and packaging details, see
[development.md](development.md#building-and-signing).

You also need a local Python environment for the harness itself (a `.venv` with the
project installed) — see [development.md](development.md#running-the-harness).
