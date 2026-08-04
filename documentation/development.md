# Development

Frank has three parts:

- The **Python image**: one executable, entered as `frank`, `frankd`, `prototype`, or `session`. It carries the harness.
- The **Next.js web UI**.
- The **Tauri desktop shell**. In development you run the daemon and the UI directly. The packaged app is built only for releases.

## Toolchain

The repo ships a **Nix flake devshell** that pins bun, Rust, `cargo-tauri`, and `pkg-config`. With [direnv](https://direnv.net):

| Command | What it does |
|---|---|
| `direnv allow` | Loads the devshell on entry; or run `nix develop` |

The Python harness runs from a local virtualenv managed with [uv](https://docs.astral.sh/uv/):

| Command | What it does |
|---|---|
| `uv sync` | Create .venv and install the project + dependencies |

## Running it

The CLI starts the daemon on its first command, so usually there is nothing to launch:

```shell
uv run frank create --agent general-assistant --directory ~/code/project
uv run frank send "$id" "What does this project do?" --wait
```

The [`frank` command](cli.md) is the full surface. To run the daemon in the foreground instead — the fastest way to watch a traceback — start it by name:

```shell
uv run python -m frank frankd
```

One image, three entry points, chosen by the first argument: `frank` (the CLI), `frankd` (the daemon), `prototype` (the process sessions are forked out of). A bare launch lands in the CLI, which is why the daemon has to be asked for. `frank daemon stop` takes down a foreground daemon and its sessions with it.

A session is `fork()` **and then** `exec()` back into the same image, through the `session` entry point. The fork is what makes it cheap; the exec is what makes it safe. On macOS, a forked child that has not exec'd cannot use Network.framework or the Objective-C runtime. A session without the exec cannot reach a model at all.

It listens on a unix socket in your runtime directory. For GUI clients it also listens on an ephemeral loopback port. `frank daemon endpoint` reports the port and the capability token.

State follows the XDG convention, and all of it is created on first run:

- Configuration in `~/.config/frank/`
- Durable state in `~/.local/share/frank/`
- Logs in `~/.local/state/frank/`

Add provider keys with `frank configure`, in the configuration file, or through environment variables. See the [Configuration guide](configuration.md).

## Running the web UI

| Command | What it does |
|---|---|
| `cd web && bun install` | Once |
| `./scripts/web-development.sh` | Http://localhost:3000, wired to the daemon already running |

Start the daemon first; the script asks it for its endpoint and passes that to the development server. It has to, and this is worth knowing before the first time it appears to be broken: the daemon takes an **ephemeral** loopback port and requires a **capability token** on every call. The desktop shell reads both out of the runtime directory; a browser tab can read neither, so a bare `bun run dev` addresses a port nothing is listening on and presents no token. The page loads, every list is empty, and nothing says why.

The token reaches the page as `NEXT_PUBLIC_FRANK_TOKEN`, which the client ignores unless `NODE_ENV` is not production — Next eliminates that branch from a production build, so a token cannot end up inside a shipped export even if the variable is set on the machine that builds it.

Run the script from an **ordinary shell, not from inside `nix develop`**. The devshell rewrites `TMPDIR`, the runtime directory hangs off it, and a daemon started outside the devshell is therefore invisible to anything started inside it — `frank ps` and `frank daemon endpoint` included. The script enters the devshell itself for the bun half, after it has already resolved the endpoint.

Useful scripts (in `web/`):

- `bun run lint` — lint the UI.
- `bun run tauri:dev` / `bun run tauri:build` — the desktop shell (see below).
- `bun run build` — production static export (to `web/out`).
- `bun run build:events` — regenerate the TypeScript event schema from the Python models (`scripts/generate_event_schema.py`). Run this whenever the event contract changes.

Outside `web/` the package layering runs `base`, then `protocol`, then `computer`/`locations`, then `runtime`, then `worker`, with the daemon never importing the runtime. Three invariants ride on it. All three are really one invariant about the prototype, which is the process every session is forked out of. None of them is visible in a diff. Check each one by hand when you touch its area:

- **`computer/` is never imported at module level.** It pulls in PyObjC, which initialises CoreFoundation, which genuinely cannot survive a `fork()` on macOS.
- **Nothing reaches the network at import.** This is the half that actually bit. A catalogue fetch at module scope left two *native* threads in the process, and a multi-threaded process cannot legally fork. The failure then surfaces far from its cause, which is why this is checked rather than assumed.

`threading.enumerate()` cannot see those threads; only the kernel's count can. The prototype therefore measures with mach `task_threads`, and refuses to fork when the answer is not 1.
- **The runtime keeps no process-wide state.** Nothing under `runtime/` parks a caller's argument in a module global. Nothing installs a signal handler or registers an exit hook. The runtime is a library now, and one process may host more than one session.

A new setting needs nothing beyond its `Field(description=...)` — no reference file to update, no listing to add it to. `frank configure --all` walks the schema, so a setting is discoverable from the moment it exists. Write the description as the sentence you would want printed at a terminal, because that is exactly where it goes.

## Running the desktop app in dev

| Command | What it does |
|---|---|
| `frank serve` | The app connects to a daemon; it does not start one |
| `cd web` |  |
| `bun run tauri:dev` | Launches the Tauri window against the dev UI |

Start the daemon first, in either order but before you expect the window to work. The app is a client. When nothing is listening, it shows the connection picker and says what to run. It does not launch a harness of its own.

## Logs and copy

Two vocabularies, and they are not the same thing.

**A log message is an event name.** Lowercase, no terminal punctuation, and the facts go in fields rather than into the sentence — `logger.info("session %s takes warm worker pid %d", …)`. This is what makes a line groupable: the message is a label you filter on, not prose you read. Acronyms and proper nouns keep their capitals wherever they fall, including first (`MCP server %r failed to start`), because those are spellings rather than casing.

**Human copy is prose.** The interface catalog, an `HTTPException` `detail`, an `RpcError`, CLI output: sentence case with terminal punctuation, because a person reads it as a sentence. A fragment used as a label or a chip — `high risk`, `waiting`, `write` — stays lowercase; it is not a sentence.

**Never interpolate an exception into a log message.** An exception's message is human copy, so `logger.error("could not start session %s: %s", identifier, error)` staples a sentence — often one wrapping a JSON document — onto the end of an event. Pass the traceback with `exc_info=True`, or the fields with `frank.base.errors.describe`, and leave the message an event.

The interface follows the same split: `swallowed({ component, operation }, error)` carries the place and the attempt as fields, and `serialize-error` parses whatever was thrown — JavaScript lets you throw a string, so a caught value may have no `message` at all.

## Building and signing

There are **two artifacts**, built independently, because the app is a client of the daemon rather than its container. Building one never rebuilds the other.

Build the daemon first. It is one image with three entry points: the daemon, the CLI, and the prototype. Set `FORCE=1` to rebuild when the freshness guard says the build is current.

```shell
packaging/build-daemon.sh
```

Then the desktop app, which is a Tauri shell with no Python in it at all:

```shell
cd web && bun run tauri:build
```

The first freezes the harness with PyInstaller into `packaging/dist/Frank Computer Use.app`, smoke-tests it, and is a no-op when nothing that goes into it has changed. The second produces `web/src-tauri/target/release/bundle/macos/Frank.app`.

It does **not** build a disk image. Installing locally is a `ditto` of the `.app`, and creating, mounting and converting a `.dmg` took about a quarter of every build to produce a file nothing here reads. Use `bun run tauri:dmg` when you actually want one to hand out.

The rest of the time is Rust, and it is not incremental: cargo disables incremental compilation for the `release` profile, and the shell is invalidated on every run anyway because `next build` rewrites `web/out`, which Tauri's build script watches. So a rebuild costs roughly a minute whether or not the frontend changed — which is the reason to rebuild only when it did. A Python-only change needs `packaging/build-daemon.sh` and a daemon restart, nothing more.

The smoke test runs the frozen daemon under a **throwaway set of XDG directories**, which is load-bearing rather than tidy. With your own directories it would find the lock held by the daemon you already run. It would stand down and exit `0`. The probe would then find *that* daemon's socket answering. That is a green result for a binary the probe never exercised, in the most common case of all.

Isolation means the binary under test is the only thing that can answer. It also keeps a build from seeding your configuration or writing to your transcript store.

For the full step-by-step with expected output, see [Installation](installation.md#every-step-and-what-you-should-see).

### Stable code-signing (recommended)

The screen-control tools (`control_screen`) need the macOS **Accessibility** grant, which is tied to code identity. Every session worker is a re-exec of the daemon binary for exactly this reason — one grant covers the fleet. Both artifacts carry the same `CFBundleName` and identifier, so signing both with one persistent identity keeps them a single **Frank** row that survives rebuilds:

Create the self-signed identity in your login keychain once:

```shell
packaging/create-signing-cert.sh
```

Then sign after each build, either artifact or both:

```shell
packaging/sign-app.sh "packaging/dist/Frank Computer Use.app"
packaging/sign-app.sh web/src-tauri/target/release/bundle/macos/Frank.app
```

The daemon is signed `--deep` with `packaging/Entitlements.plist`. It sends Apple Events for its login-items and running-apps probes. It also loads PyInstaller's dylibs without library validation. The app needs neither entitlement, so it signs plain. The identity is self-signed, so Gatekeeper still warns on other machines until a build is Apple-notarized.

### Installing the daemon

```shell
ditto "packaging/dist/Frank Computer Use.app" "/Applications/Frank Computer Use.app"
ln -sf "/Applications/Frank Computer Use.app/Contents/MacOS/frank" /usr/local/bin/frank
```

The symlink is what puts `frank` and `frankd` on your `PATH`, both entering the same signed image. Running from a checkout (`uv run frank …`) works for everything except a stable Accessibility grant, since the interpreter is then the code identity.

## Tests

The repository ships **no unit-test suite**. It ships a **verification battery** instead. The battery holds the specific, falsifiable claims that the architecture rests on, and it checks each one by doing it:

| Command | What it does |
|---|---|
| `uv run ruff check src/ scripts/` |  |
| `cd web && bun run build` | Regenerates and diffs the event schema, then type-checks |

Each stage gets its own temporary XDG roots and its own daemon and cleans up after itself, so a run touches nothing of yours. Exit status is the number of failures.

Two stages need a real machine and are skipped elsewhere, which is the reason to run this locally at least once. `macos-fork` answers the three questions that only macOS can answer:

- May a forked child initialise CoreFoundation?
- Does the Accessibility grant follow a fork?
- Does `sandbox-exec` still work from a child?

It also counts threads with mach. That is the only way to see the threads that make a fork illegal.

Confinement is only genuinely exercised where the kernel can enforce it. Without Landlock, or a working `sandbox-exec`, the battery runs with `sandbox.enforce: preferred`. The sandbox is then never applied.

Beyond the battery: lint with `uv run ruff check`, and drive the affected path through the CLI directly. `pyproject.toml` is already set up for `pytest` (`testpaths = ["tests"]`, `asyncio_mode = "auto"`), so if you add a `tests/` directory `uv run pytest` will pick it up.

## Project layout

See the [documentation index](README.md#the-shape-of-the-project) for the directory map. The harness is in `src/frank/` (with `packaging/entry.py` as the frozen build's entry point), the UI in `web/src/`, and the Tauri shell in `web/src-tauri/`.
