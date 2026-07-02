#!/usr/bin/env bash
# Freeze the harness server (server.py) into a self-contained binary and place it
# where the Tauri app bundles it from (web/src-tauri/server-bin/daisy-server).
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

target="web/src-tauri/server-bin/daisy-server/daisy-server"

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
./packaging/dist/daisy-server/daisy-server >/tmp/daisy-server-smoke.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT
sleep 8
if curl -fsS -m 5 http://127.0.0.1:8822/home >/dev/null; then
  echo "ok: server responds on :8822"
else
  echo "failed: server did not respond; see /tmp/daisy-server-smoke.log" >&2
  exit 1
fi
kill "$server_pid" 2>/dev/null || true
trap - EXIT

echo "installing into web/src-tauri/server-bin/daisy-server"
rm -rf web/src-tauri/server-bin/daisy-server
cp -R packaging/dist/daisy-server web/src-tauri/server-bin/daisy-server

echo "done; the desktop build will now bundle the local server"
