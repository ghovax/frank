---
name: desktop-build
title: Desktop app build & signing pipeline (macOS)
description: How the Daisy macOS desktop app is built end to end — freezing the harness into a signed helper bundle, the Accessibility/TCC identity trick, the self-signed codesign identity, the icon pipeline, and the two packaging gotchas that bloated the bundle.
importance: high
tags: packaging, tauri, pyinstaller, codesign, accessibility, macos
---

Daisy ships as **two independent macOS artifacts**: the harness (`Daisy Computer Use.app` — the CLI, `daisyd`, and every session worker in one image) and the desktop app (a Tauri v2 shell, Rust + a Next.js static export). The app is a **client**. It does not contain the harness, does not spawn it, and is powerless when no daemon is running — it reads the port and token `daisyd` publishes into the runtime directory, exactly as it would reach a daemon on another host. Everything below is macOS-only and reproducible from a clean checkout. Never touch git history; work in-branch.

## Two builds, neither triggering the other

```sh
packaging/build-daemon.sh                       # the harness; FORCE=1 to override the freshness guard
cd web && bun run tauri:build                   # the app (run from web/ — beforeBuildCommand's ../packaging paths assume it)
packaging/sign-app.sh "packaging/dist/Daisy Computer Use.app"
packaging/sign-app.sh web/src-tauri/target/release/bundle/macos/Daisy.app
```

`tauri.conf.json`'s `beforeBuildCommand` no longer invokes the freeze and `bundle.resources` no longer exists, so the app build is a Rust compile and a static export — nothing Python. Install the harness by `ditto`-ing it to `/Applications` and symlinking `Contents/MacOS/daisy` onto `PATH`; install the app the same way. `daisy app` then starts the daemon if needed and launches the window.

## Freezing the harness → a signed .app (the Accessibility identity trick)

`packaging/daisy-daemon.spec` (PyInstaller) freezes `packaging/entry.py` into a binary named `daisy`. It is **one image with three entry points** — `daisy`, `daisyd`, `prototype`, chosen by the first argument — which is not packaging convenience: the daemon re-execs *this same binary* to start the prototype, and every session is a `fork()` of the prototype. A fork inherits its parent's code signature, so the whole fleet carries the signed bundle's identity and one Accessibility grant covers it, without any session ever being exec'd. Heavy deps use dynamic imports (litellm, uvicorn[standard], langchain, a2a), so the spec `collect_all`s them explicitly and `copy_metadata`s the ones read via `importlib.metadata`. `.agents/{agents,skills}` + `mcp.json` are bundled at the frozen root (the shipped base layer; `.agents/memories` is NOT shipped).

`build-daemon.sh` smoke-tests the frozen daemon before finishing: it launches it **with the `daisyd` argument** (a bare launch lands in the CLI and exits) under a **throwaway set of XDG directories**, and waits for it to answer `/health` on its own unix socket. The loopback port is ephemeral, so the probe is the socket, never a fixed port.

The isolation is the point, not hygiene. Started with the developer's own XDG roots, the frozen daemon finds `daisyd.lock` held by whatever daemon they are running, logs "Another daisyd already holds the runtime directory; standing down", and **exits 0** — and a probe that checked the socket first then found *that* daemon answering and reported success for a binary it had never run. Liveness of the child is now checked before the socket, for the same reason.

The spec ends in a `BUNDLE(...)` step that wraps the frozen output as **`Daisy Computer Use.app`** with `CFBundleName="Daisy"`, `bundle_identifier="com.ghovax.daisy"`, `LSUIElement=True`. Both artifacts carry that same name and identifier, which is why signing both with one certificate keeps them a single **Daisy** row in Accessibility — and why the wrapper survived the app no longer bundling anything.

`packaging/Entitlements.plist` moved from the app to the daemon, where it always belonged: `apple-events` for the login-items and running-apps probes, `disable-library-validation` for PyInstaller's dylibs. Both were the daemon's requirements, sitting on the app only because the app was its parent process.

Why a bundle, not a bare binary: the *session worker* — not the Tauri shell — is the process that calls the macOS Accessibility API for the computer-use tool, and TCC lists whichever process exercises a permission. A bare binary shows its raw filename in System Settings ▸ Privacy ▸ Accessibility. Wrapped as a bundle carrying the **same** `CFBundleName` + identifier as the desktop app and signed with the same cert, it folds into the app's single **"Daisy"** entry instead of a second row, and — because the identity is stable — the grant survives rebuilds. This is also why a worker must be a re-exec of the same image rather than a separate helper: a different path is a different identity, and would prompt once per session.

macOS caches `AXIsProcessTrusted` per process, so after granting, the **daemon must restart** to see the grant (the prototype it forks sessions from is a re-exec of it). Restarting the *app* no longer does that — the daemon is not its child. The Settings dialog asks the daemon to restart itself over the control plane (`daemon.restart`, also `daisy daemon restart`), which **keeps live sessions** — the registry is durable, so each one loses only its process and comes back asleep — then calls the `restart_app` Tauri command to reload the webview against the fresh daemon and auto-enables computer-control on return (localStorage `daisy:pendingComputerControlEnable`).

## The self-signed codesign identity (stable across rebuilds)

Ad-hoc signing changes the cdhash every build, which invalidates the TCC grant. Instead `packaging/create-signing-cert.sh` makes a persistent self-signed identity **"Daisy Local Codesign"** in the login keychain. Gotcha: use **`/usr/bin/openssl`** (LibreSSL) for the p12 — openssl 3.x writes a MAC that `security import` rejects ("MAC verification failed"). Import with `security import -A -T /usr/bin/codesign`.

`packaging/sign-app.sh` takes one or both artifacts. The daemon gets `--deep` plus `packaging/Entitlements.plist` (its dylibs, its Apple Events); the app signs plain, having neither nested code nor those needs. Each bundle takes its identifier from its own Info.plist (both `com.ghovax.daisy`); no hardened runtime, so PyInstaller's dylibs still load.

## Icon — Apple's own no-Xcode pipeline

`packaging/export-icon.py` drives Icon Composer's own `ictool` over `web/src-tauri/Daisy.icon`, exporting a 1024² macOS rendition; that export becomes `app-icon.png` for the non-macOS platform assets, `icon.icns` is written directly through Pillow at every size in `MACOS_ICON_SIZES`, and `tauri icon` generates the rest. No squircle or glass is hand-drawn — modern macOS applies the rounded-rect mask itself. Do NOT hand-roll the icon; the `.icon` document is the source of truth and this script is the sanctioned way to render it.

## Size, and the two gotchas behind it

The frozen harness is ~228 MB; it was 1.8 GB. Neither cause was the freezer — swapping PyInstaller for Nuitka would not have helped (Nuitka's macOS onedir/.app output is typically ~2× PyInstaller's and much slower to build).

1. **Committed skill `.venv` rode into the freeze.** `.agents/skills/literature-search/scripts/.venv` is a committed ~145 MB uv devshell (pymupdf/libmupdf 33 MB, lxml, numpy, PIL). The spec walks `.agents/{agents,skills}` file-by-file (`_bundle_tree`) and prunes regenerable runtime artifacts (`.venv`, `__pycache__`, `.git`, `node_modules`, tool caches) instead of a wholesale `datas.append((dir, dest))`. Skills recreate their venv on demand where they run.
2. **Tauri dereferenced PyInstaller's symlinks.** PyInstaller's codesignable layout puts real Mach-Os in `Contents/Frameworks`, real Python in `Contents/Resources`, and cross-symlinks each side so the import root sees one merged tree. Tauri's resource copier followed every one of those when it bundled the helper, turning each into a full second copy (~5×), and `sign-app.sh` had to `ditto` a pristine copy back before signing. **This is gone**: nothing bundles the daemon, so nothing dereferences it, and the repair step went with the coupling that required it.

## Freshness guard

`build-daemon.sh` skips the freeze when `packaging/dist/Daisy Computer Use.app/Contents/MacOS/daisy` is newer than everything that goes into it: `packaging/entry.py`, the spec, `pyproject.toml`, `uv.lock`, `src/daisy`, and `.agents/{agents,skills,mcp.json}`. `FORCE=1` rebuilds. Two bugs it used to have, both fixed: it did not watch `.agents/` at all despite bundling it, so a skill edit shipped stale; and it counted `__pycache__` and `.DS_Store` as source changes, so merely running the harness forced a needless multi-minute re-freeze. The `find` expression now prunes both.

The Python steps in this chain (`export-icon.py`, and `web/`'s `check:events`) go through `uv run --project ..` rather than a hardcoded `.venv/bin/python`, so the build works whether or not the virtualenv has been created yet.
