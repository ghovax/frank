#!/usr/bin/env bash
# Freeze the harness server (server.py) into a self-contained background helper bundle
# ("XEAC Computer Use.app") and place it where the Tauri app bundles it from
# (web/src-tauri/server-bin/). The .app wrapper gives the server a real bundle identity — the
# same CFBundleName/identifier as the desktop app — so it folds into the app's single
# "XEAC" macOS Accessibility entry instead of showing a bare helper name.
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

target="web/src-tauri/server-bin/XEAC Computer Use.app/Contents/MacOS/xeac"

# Freshness guard: skip the freeze when the installed binary already exists and no
# server source (server.py, the xeac package, the spec, or the locked deps) is
# newer than it. `find -newer` prints the first newer file, so empty means fresh.
if [ -z "${FORCE:-}" ] && [ -x "$target" ]; then
  newer_source="$(find server.py packaging/xeac-server.spec pyproject.toml uv.lock src/xeac \
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
  packaging/xeac-server.spec

echo "smoke-testing the frozen daemon"
# The binary is a single image with three entry points, so the daemon has to be asked for by
# name; launching it bare would land in the CLI and exit immediately.
"./packaging/dist/XEAC Computer Use.app/Contents/MacOS/xeac" daemon >/tmp/xeac-daemon-smoke.log 2>&1 &
daemon_pid=$!
trap 'kill "$daemon_pid" 2>/dev/null || true' EXIT
# Readiness is the daemon publishing its handshake in the runtime directory and answering on
# its socket — not a fixed port, because the loopback port is chosen at startup. Poll rather
# than sleeping once: a frozen binary's first boot unpacks itself and imports a heavy graph,
# which can take far longer than any fixed wait.
runtime_directory="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/xeac"
[ -d "$runtime_directory" ] || runtime_directory="${TMPDIR:-/tmp}/xeac-$(id -u)"
ready=""
for _ in $(seq 1 40); do
  sleep 1
  if [ -S "$runtime_directory/xeacd.sock" ] \
     && curl -fsS -m 5 --unix-socket "$runtime_directory/xeacd.sock" http://daemon/health >/dev/null 2>&1; then
    ready=1
    break
  fi
  kill -0 "$daemon_pid" 2>/dev/null || break
done
if [ -n "$ready" ]; then
  echo "ok: xeacd answers on its socket"
else
  echo "failed: xeacd did not become ready; see /tmp/xeac-daemon-smoke.log" >&2
  exit 1
fi
kill "$daemon_pid" 2>/dev/null || true
trap - EXIT

echo "installing into web/src-tauri/server-bin/XEAC Computer Use.app"
rm -rf web/src-tauri/server-bin/xeac "web/src-tauri/server-bin/XEAC Computer Use.app"
cp -R "packaging/dist/XEAC Computer Use.app" "web/src-tauri/server-bin/XEAC Computer Use.app"

echo "done; the desktop build will now bundle the local server"
