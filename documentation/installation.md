# Installation

LangMesh targets **macOS on Apple Silicon (`aarch64`)**. The screen-control tools (`control_screen`) and the packaged app are macOS-specific. The harness itself is portable Python, but the desktop experience is built for the Mac.

## Option 1 — Download the app

1. Open the [**Releases**](https://github.com/ghovax/langmesh/releases) page and download the latest `LANGMESH_<version>_aarch64.dmg`.
2. Open the `.dmg` and drag **LangMesh** into **Applications**.
3. Launch it.

### Gatekeeper

The app is **self-signed, not Apple-notarized**. macOS Gatekeeper refuses the first launch with an "unidentified developer" or "damaged" message. This is expected. Clear it once, either way:

- **Right-click** `LangMesh.app`, choose **Open**, then **Open** again in the dialog, or
- from a terminal:

  ```shell
  xattr -dr com.apple.quarantine /Applications/LangMesh.app
  ```

Notarized builds are planned. Until then this one-time step is required.

### Permissions the app may ask for

- **Accessibility** — required for the screen-control tools (`control_screen`) to read and act on native apps. LangMesh prompts you and deep-links to the right settings pane. Grant it to LangMesh.
- **Chrome remote debugging** — required for the screen-control tools to drive your own Chrome. LangMesh shows a one-click prompt that opens `chrome://inspect`. Enable the remote-debugging toggle once.

Neither is needed for plain chat or the file, shell, and web tools.

## Option 2 — Build from source

LangMesh is **two artifacts**, built independently, because the app is a *client* of the daemon rather than its container. That bundle carries the harness, the `langmesh` command and `langmeshd`, which hosts every session, in one signed image. The app is a window that finds a daemon and talks to it. Build them in either order; neither build triggers the other.

You need [Nix](https://nixos.org) (the flake devshell pins everything else, `uv` included) and optionally [direnv](https://direnv.net).

### Every step, and what you should see

| # | Run | What it does | You should see | Takes |
|---|---|---|---|---|
| 1 | `git clone https://github.com/ghovax/langmesh.git && cd langmesh` | | | seconds |
| 2 | `direnv allow` — or `nix develop` | Loads uv, bun, rustc, cargo, cargo-tauri, pkg-config | `dev env loaded: uv 0.x, bun 1.x, rustc 1.x` | first time, minutes |
| 3 | `uv sync` | Creates `.venv` with the project **and the dev group**, so PyInstaller arrives here | Resolution and install log | ~1 min |
| 4 | `cd web && bun install && cd ..` | UI dependencies | | ~1 min |
| 5 | `packaging/build-daemon.sh` | Freezes the harness, then smoke-tests it in an isolated set of XDG directories | `freezing the harness…`, then `ok: langmeshd answers on its own socket`, then the install commands it prints | **several minutes** |
| 6 | `packaging/create-signing-cert.sh` — **once per machine** | Makes the persistent identity "LangMesh Local Codesign" | A keychain prompt | seconds |
| 7 | `packaging/sign-app.sh "packaging/dist/LangMesh Computer Use.app"` | Signs the daemon `--deep` with its entitlements | `signed …`, then `Identifier=` and `Authority=` | seconds |
| 8 | `ditto "packaging/dist/LangMesh Computer Use.app" "/Applications/LangMesh Computer Use.app"` | Installs the harness | | seconds |
| 9 | `ln -sf "/Applications/LangMesh Computer Use.app/Contents/MacOS/langmesh" /usr/local/bin/langmesh` | Puts `langmesh` and `langmeshd` on your `PATH` | May need `sudo` | seconds |
| 10 | `cd web && bun run tauri:build` | Rust compile plus a static export. **No Python in this step** | `LangMesh.app` and a `.dmg` under `web/src-tauri/target/release/bundle/` | first time, ~10 min |
| 11 | `packaging/sign-app.sh web/src-tauri/target/release/bundle/macos/LangMesh.app` | Signs the app plainly — same identity, so both fold into one Accessibility row | `signed …` | seconds |
| 12 | `ditto` that `LangMesh.app` to `/Applications` | Installs the window. **Required** before `langmesh app` can find it by identifier | | seconds |
| 13 | `langmesh app` | Starts the daemon, launches the app | `{"opened":"com.ghovax.langmesh","daemon":true}`; first run seeds `~/.config/langmesh/configuration.yaml` | seconds |
| 14 | `langmesh configure providers.anthropic.api_key <key>` | A model to run on | The key echoed back | |

### Things that will catch you

| Symptom | Cause | Fix |
|---|---|---|
| `langmesh: nothing on this system claims com.ghovax.langmesh` | You built `LangMesh.app` but left it in the Tauri target directory. macOS resolves `-b` through LaunchServices, which does not know about it there | Step 12 — `ditto` it to `/Applications` |
| The app opens but only ever shows the connection picker | No daemon is running. The app never starts one | `langmesh serve`, or use `langmesh app` |
| Computer control keeps asking for Accessibility after every rebuild | The daemon serving you is the checkout's (`uv run langmesh`), whose code identity is the Python interpreter, not the signed image | `langmesh daemon status` reports `image.frozen`. If it is `false`, stop that daemon and start the installed one |
| Two `langmesh` on your `PATH` behave differently | The checkout's and the installed one share `~/.config/langmesh/` and the runtime directory, so whichever daemon started first owns it | `which -a langmesh`, and check `image.executable` in `langmesh daemon status` |
| `ln -sf … /usr/local/bin/langmesh` is denied | `/usr/local/bin` is root-owned | `sudo ln -sf …`, or symlink into `~/.local/bin` and put that on `PATH` |
| `packaging/build-daemon.sh` says "daemon up to date" after you changed something | The freshness guard decided nothing that goes into the freeze had changed | `FORCE=1 packaging/build-daemon.sh` |

Signing (steps 6, 7, 11) is optional for a build that only runs. It is necessary for a **stable Accessibility grant**. Without it, every rebuild is a new code identity, and macOS asks again.

Both artifacts carry the same `CFBundleName` and identifier. One certificate over both therefore keeps them as a single **LangMesh** row. See the [Development guide](development.md#building-and-signing).

