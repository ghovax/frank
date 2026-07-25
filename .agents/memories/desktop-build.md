---
name: desktop-build
title: Desktop app build & signing pipeline (macOS)
description: How the XEAC macOS desktop app is built end to end — freezing the harness into a signed helper bundle, the Accessibility/TCC identity trick, the self-signed codesign identity, the icon pipeline, and the two packaging gotchas that bloated the bundle.
importance: high
tags: packaging, tauri, pyinstaller, codesign, accessibility, macos
---

The XEAC desktop app is a Tauri v2 shell (Rust + a Next.js static export) that bundles the harness as a **frozen sidecar** and spawns it as `xeacd` for local mode. Everything below is macOS-only and reproducible from a clean checkout. Never touch git history; work in-branch.

## One command

`cd web && bun run tauri:build` runs the whole chain (the script is `tauri build`, run from `web/` — the directory `beforeBuildCommand`'s `../packaging/...` paths assume). `tauri.conf.json` `beforeBuildCommand` invokes `packaging/build-sidecar.sh`, then Tauri bundles `web/src-tauri/server-bin/` as a resource and compiles the Rust shell. `packaging/sign-app.sh <XEAC.app>` signs the result; install by `ditto`-ing it to `/Applications`.

## Freezing the server → a signed *helper .app* (the Accessibility identity trick)

`packaging/xeac-server.spec` (PyInstaller) freezes `server.py` into a binary named `xeac`. It is **one image with three entry points** — `xeac`, `xeacd`, `worker`, chosen by the first argument — which is not just packaging convenience: the daemon re-execs *this same binary* to make each session worker, so every worker carries the signed bundle's code identity and one Accessibility grant covers the fleet. Heavy deps use dynamic imports (litellm, uvicorn[standard], langchain, a2a), so the spec `collect_all`s them explicitly and `copy_metadata`s the ones read via `importlib.metadata`. `.agents/{agents,skills}` + `mcp.json` are bundled at the frozen root (the app's shipped base layer; `.agents/memories` is NOT shipped).

`build-sidecar.sh` smoke-tests the frozen daemon before the build proceeds: it launches it **with the `xeacd` argument** (a bare launch lands in the CLI and exits) and waits for it to answer `/health` on its unix socket. The loopback port is ephemeral, so the probe is the socket, never a fixed port.

The spec ends in a `BUNDLE(...)` step that wraps the frozen output as **`XEAC Computer Use.app`** with `CFBundleName="XEAC"`, `bundle_identifier="com.ghovax.xeac"`, `LSUIElement=True`.

Why a bundle, not a bare binary: the *session worker* — not the Tauri shell — is the process that calls the macOS Accessibility API for the computer-use tool, and TCC lists whichever process exercises a permission. A bare binary shows its raw filename in System Settings ▸ Privacy ▸ Accessibility. Wrapped as a bundle carrying the **same** `CFBundleName` + identifier as the desktop app and signed with the same cert, it folds into the app's single **"XEAC"** entry instead of a second row, and — because the identity is stable — the grant survives rebuilds. This is also why a worker must be a re-exec of the same image rather than a separate helper: a different path is a different identity, and would prompt once per session.

macOS caches `AXIsProcessTrusted` per process, so after granting, the **daemon must restart** to see the grant (its workers are re-execs of it). The app exposes a `restart_app` Tauri command; the Settings dialog prompts a restart and auto-enables computer-control on return (localStorage `xeac:pendingComputerControlEnable`).

## The self-signed codesign identity (stable across rebuilds)

Ad-hoc signing changes the cdhash every build, which invalidates the TCC grant. Instead `packaging/create-signing-cert.sh` makes a persistent self-signed identity **"XEAC Local Codesign"** in the login keychain. Gotcha: use **`/usr/bin/openssl`** (LibreSSL) for the p12 — openssl 3.x writes a MAC that `security import` rejects ("MAC verification failed"). Import with `security import -A -T /usr/bin/codesign`.

`packaging/sign-app.sh` runs `codesign --force --deep --sign "XEAC Local Codesign" <XEAC.app>`. Each bundle takes its identifier from its own Info.plist (both `com.ghovax.xeac`); no hardened runtime (so PyInstaller dylibs still load).

## Icon — Apple's own no-Xcode pipeline

`packaging/export-icon.py` drives Icon Composer's own `ictool` over `web/src-tauri/XEAC.icon`, exporting a 1024² macOS rendition; that export becomes `app-icon.png` for the non-macOS platform assets, `icon.icns` is written directly through Pillow at every size in `MACOS_ICON_SIZES`, and `tauri icon` generates the rest. No squircle or glass is hand-drawn — modern macOS applies the rounded-rect mask itself. Do NOT hand-roll the icon; the `.icon` document is the source of truth and this script is the sanctioned way to render it.

## Two packaging gotchas that bloated the app 1.8 GB → ~228 MB

Both are packaging bugs, not the freezer — swapping PyInstaller for Nuitka/etc. would not have helped (Nuitka's macOS onedir/.app output is typically ~2× PyInstaller's and much slower to build).

1. **Committed skill `.venv` rode into the freeze.** `.agents/skills/literature-search/scripts/.venv` is a committed ~145 MB uv devshell (pymupdf/libmupdf 33 MB, lxml, numpy, PIL). The spec now walks `.agents/{agents,skills}` file-by-file (`_bundle_tree`) and prunes regenerable runtime artifacts (`.venv`, `__pycache__`, `.git`, `node_modules`, tool caches) instead of a wholesale `datas.append((dir, dest))`. Skills recreate their venv on demand where they run.
2. **Tauri dereferences PyInstaller's symlinks.** PyInstaller's codesignable macOS layout puts real Mach-Os in `Contents/Frameworks`, real Python in `Contents/Resources`, and cross-symlinks each side so the import root sees one merged tree (~380 MB via `du`). Tauri's resource copier follows those symlinks when it bundles the helper, turning every one into a full second copy (~5×). `sign-app.sh` fixes it: before signing, it `rm`s the flattened helper in the built app and `ditto`s the pristine one back from `web/src-tauri/server-bin/` (untouched by Tauri; `ditto` preserves symlinks).

## Freshness / gitignore

`build-sidecar.sh` skips the freeze when `web/src-tauri/server-bin/XEAC Computer Use.app/Contents/MacOS/xeac` is newer than all of `server.py packaging/xeac-server.spec pyproject.toml uv.lock src/xeac`. `FORCE=1` rebuilds. A stray `src/xeac/.DS_Store` can trip the guard into a needless re-freeze. `web/src-tauri/server-bin/*` is gitignored (regenerated every build) with a kept `.gitkeep`.

The Python steps in this chain (`export-icon.py`, and `web/`'s `check:events`) go through `uv run --project ..` rather than a hardcoded `.venv/bin/python`, so the build works whether or not the virtualenv has been created yet.
