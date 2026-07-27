# Installation

Frank targets **macOS on Apple Silicon (`aarch64`)**. The screen-control tools (`control_screen`) and the packaged app are macOS-specific. The harness itself is portable Python, but the desktop experience is built for the Mac.

## Option 1 — Download the app

1. Open the [**Releases**](https://github.com/ghovax/frank/releases) page and download the latest `FRANK_<version>_aarch64.dmg`.
2. Open the `.dmg` and drag **Frank** into **Applications**.
3. Launch it.

### Gatekeeper

The app is **self-signed, not Apple-notarized**. macOS Gatekeeper refuses the first launch with an "unidentified developer" or "damaged" message. This is expected. Clear it once, either way:

- **Right-click** `Frank.app` → **Open** → **Open** in the dialog, or
- from a terminal:

  ```sh
  xattr -dr com.apple.quarantine /Applications/Frank.app
  ```

Notarized builds are planned. Until then this one-time step is required.

### Permissions the app may ask for

- **Accessibility** — required for the screen-control tools (`control_screen`) to read and act on native apps. Frank prompts you and deep-links to the right settings pane. Grant it to Frank.
- **Chrome remote debugging** — required for the screen-control tools to drive your own Chrome. Frank shows a one-click prompt that opens `chrome://inspect`. Enable the remote-debugging toggle once.

Neither is needed for plain chat or the file, shell, and web tools.

## Option 2 — Build from source

Frank is **two artifacts**, built independently, because the app is a *client* of the daemon rather than its container. The daemon is the harness — the `frank` command, `frankd`, and every session worker in one signed image. The app is a window that finds a daemon and talks to it. Build them in either order; neither build triggers the other.

You need [Nix](https://nixos.org) (the flake devshell pins everything else, `uv` included) and optionally [direnv](https://direnv.net).

### Every step, and what you should see

| # | Run | What it does | You should see | Takes |
|---|---|---|---|---|
| 1 | `git clone https://github.com/ghovax/frank.git && cd frank` | | | seconds |
| 2 | `direnv allow` — or `nix develop` | Loads uv, bun, rustc, cargo, cargo-tauri, pkg-config | `dev env loaded: uv 0.x, bun 1.x, rustc 1.x` | first time, minutes |
| 3 | `uv sync` | Creates `.venv` with the project **and the dev group**, so PyInstaller arrives here | Resolution and install log | ~1 min |
| 4 | `cd web && bun install && cd ..` | UI dependencies | | ~1 min |
| 5 | `packaging/build-daemon.sh` | Freezes the harness, then smoke-tests it in an isolated set of XDG directories | `freezing the harness…` → `ok: frankd answers on its own socket` → the install commands it prints | **several minutes** |
| 6 | `packaging/create-signing-cert.sh` — **once per machine** | Makes the persistent identity "Frank Local Codesign" | A keychain prompt | seconds |
| 7 | `packaging/sign-app.sh "packaging/dist/Frank Computer Use.app"` | Signs the daemon `--deep` with its entitlements | `signed …`, then `Identifier=` and `Authority=` | seconds |
| 8 | `ditto "packaging/dist/Frank Computer Use.app" "/Applications/Frank Computer Use.app"` | Installs the harness | | seconds |
| 9 | `ln -sf "/Applications/Frank Computer Use.app/Contents/MacOS/frank" /usr/local/bin/frank` | Puts `frank` and `frankd` on your `PATH` | May need `sudo` | seconds |
| 10 | `cd web && bun run tauri:build` | Rust compile plus a static export. **No Python in this step** | `Frank.app` and a `.dmg` under `web/src-tauri/target/release/bundle/` | first time, ~10 min |
| 11 | `packaging/sign-app.sh web/src-tauri/target/release/bundle/macos/Frank.app` | Signs the app plainly — same identity, so both fold into one Accessibility row | `signed …` | seconds |
| 12 | `ditto` that `Frank.app` to `/Applications` | Installs the window. **Required** before `frank app` can find it by identifier | | seconds |
| 13 | `frank app` | Starts the daemon, launches the app | `{"opened":"com.ghovax.frank","daemon":true}`; first run seeds `~/.config/frank/configuration.yaml` | seconds |
| 14 | `frank configure providers.anthropic.api_key <key>` | A model to run on | The key echoed back | |

### Things that will catch you

| Symptom | Cause | Fix |
|---|---|---|
| `frank: nothing on this system claims com.ghovax.frank` | You built `Frank.app` but left it in the Tauri target directory. macOS resolves `-b` through LaunchServices, which does not know about it there | Step 12 — `ditto` it to `/Applications` |
| The app opens but only ever shows the connection picker | No daemon is running. The app never starts one | `frank serve`, or use `frank app` |
| Computer control keeps asking for Accessibility after every rebuild | The daemon serving you is the checkout's (`uv run frank`), whose code identity is the Python interpreter, not the signed image | `frank daemon status` reports `image.frozen`. If it is `false`, stop that daemon and start the installed one |
| Two `frank` on your `PATH` behave differently | The checkout's and the installed one share `~/.config/frank/` and the runtime directory, so whichever daemon started first owns it | `which -a frank`, and check `frank daemon status` → `image.executable` |
| `ln -sf … /usr/local/bin/frank` is denied | `/usr/local/bin` is root-owned | `sudo ln -sf …`, or symlink into `~/.local/bin` and put that on `PATH` |
| `packaging/build-daemon.sh` says "daemon up to date" after you changed something | The freshness guard decided nothing that goes into the freeze had changed | `FORCE=1 packaging/build-daemon.sh` |

Signing (steps 6, 7, 11) is optional for a build that merely runs, and required for a **stable Accessibility grant**: without it every rebuild is a new code identity and macOS asks again. Both artifacts carry the same `CFBundleName` and identifier, which is why one certificate over both keeps them a single **Frank** row — see [Development guide](development.md#building-and-signing).

## Coming from a build named XEAC

The harness was briefly called XEAC and is now Frank again. Nothing migrates: Frank reads `~/.config/frank/` and `~/.local/share/frank/`, so it starts with a freshly seeded configuration and an empty transcript store. Your old configuration and transcripts are still at `~/.config/xeac/configuration.yaml` and `~/.local/share/xeac/history.db` if you want to move them across by hand. Your agents, skills, memories and MCP servers are unaffected — they live in `~/.agents/`, which was never named after the product. The desktop app's bundle identifier changed too, so macOS will ask for the Accessibility grant once more the first time you use the screen tools.
