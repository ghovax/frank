#!/usr/bin/env bash
# Freeze the harness server (server.py) into a self-contained background helper bundle
# ("Daisy Computer Use.app") and place it where the Tauri app bundles it from
# (web/src-tauri/server-bin/). The .app wrapper gives the server a real bundle identity — the
# same CFBundleName/identifier as the desktop app — so it folds into the app's single
# "Daisy" macOS Accessibility entry instead of showing a bare "daisy-server".
#
# This is invoked automatically by the desktop build via `beforeBuildCommand` in
# tauri.conf.json — it is NOT a manual step. It lives in its own script (rather than
# build.rs or the Rust build graph) because the freeze is slow and its toolchain is
# Python/PyInstaller, while `tauri dev` and `cargo build` run constantly; a freshness
# guard makes it a cheap no-op unless the server sources actually changed.
#
# Set FORCE=1 to rebuild unconditionally.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

target="web/src-tauri/server-bin/Daisy Computer Use.app/Contents/MacOS/daisy"

# Freshness guard: skip the freeze when the installed binary already exists and no
# server source (server.py, the harness package, the spec, or the locked deps) is
# newer than it. `find -newer` prints the first newer file, so empty means fresh.
if [ -z "${FORCE:-}" ] && [ -x "$target" ]; then
  newer_source="$(find server.py packaging/daisy-server.spec pyproject.toml uv.lock src/harness \
    -type f -newer "$target" -print -quit 2>/dev/null || true)"
  if [ -z "$newer_source" ]; then
    echo "sidecar up to date; skipping freeze (set FORCE=1 to rebuild)"
    exit 0
  fi
  echo "sidecar stale (changed: $newer_source); re-freezing"
fi

echo "freezing harness server with pyinstaller"
uv run pyinstaller \
  --clean --noconfirm \
  --distpath packaging/dist \
  --workpath packaging/build \
  packaging/daisy-server.spec

echo "smoke-testing the frozen server"
"./packaging/dist/Daisy Computer Use.app/Contents/MacOS/daisy" >/tmp/daisy-server-smoke.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT
# Poll for readiness rather than a single fixed sleep: a frozen binary's first boot
# (PyInstaller unpacking + the heavy import graph) can take well over 8s, so a fixed
# wait produces false failures. Retry for up to ~40s, and bail early if the process dies.
ready=""
for _ in $(seq 1 40); do
  sleep 1
  if curl -fsS -m 5 http://127.0.0.1:8822/home >/dev/null 2>&1; then ready=1; break; fi
  kill -0 "$server_pid" 2>/dev/null || break
done
if [ -n "$ready" ]; then
  echo "ok: server responds on :8822"
else
  echo "failed: server did not respond; see /tmp/daisy-server-smoke.log" >&2
  exit 1
fi
kill "$server_pid" 2>/dev/null || true
trap - EXIT

echo "installing into web/src-tauri/server-bin/Daisy Computer Use.app"
rm -rf web/src-tauri/server-bin/daisy-server "web/src-tauri/server-bin/Daisy Computer Use.app"
cp -R "packaging/dist/Daisy Computer Use.app" "web/src-tauri/server-bin/Daisy Computer Use.app"

echo "done; the desktop build will now bundle the local server"
