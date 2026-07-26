#!/usr/bin/env bash
# Freeze the harness (packaging/entry.py) into "Daisy Computer Use.app" — the daemon, the CLI, and every
# session worker as one self-contained image, entered by its first argument.
#
# This used to be a *sidecar*: the desktop app bundled the result as a resource and spawned it,
# which made the window the harness's parent process. It does not any more. The app is a client
# that finds a daemon and talks to it, so this builds an artifact you install, not one that rides
# inside something else. The output stays in packaging/dist/ and is installed from there.
#
# The .app wrapper is not packaging decoration. macOS attributes a permission to the code identity
# of the process that exercises it; the process calling the Accessibility API is a session worker,
# which is a re-exec of the daemon, which is this image. Carrying the same CFBundleName and
# identifier as the desktop app and signed with the same certificate, the whole fleet folds into
# one stable "Daisy" row in Privacy > Accessibility that survives rebuilds. A bare binary would
# show a raw filename, and a differently-signed one would prompt again.
#
# Run it directly. It is no longer wired into `tauri build`, because the app no longer contains
# what it produces. Set FORCE=1 to rebuild unconditionally.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# Where the executable lands, which is not the same shape on every platform. The spec ends in
# `BUNDLE(...)`, and PyInstaller only honours that on macOS — everywhere else it stops after
# `COLLECT` and leaves a plain directory. Assuming the bundle meant a Linux build froze
# correctly for two minutes and then failed its own smoke test on a path that was never going
# to exist, reporting "the frozen daemon exited before it was ready" over an `env: No such file
# or directory`. The freeze was fine; the post-condition was wrong.
case "$(uname -s)" in
  Darwin) target="packaging/dist/Daisy Computer Use.app/Contents/MacOS/daisy" ;;
  *)      target="packaging/dist/daisy/daisy" ;;
esac

# Freshness guard: skip the freeze when the binary already exists and nothing that goes into it
# is newer. `find -newer` prints the first newer file, so empty means fresh.
#
# Two things this used to get wrong. It did not watch `.agents/`, which the spec bundles into the
# freeze — so editing a shipped agent or skill left the guard convinced nothing had changed, and
# the build shipped the old one. And it counted `__pycache__` as a source change, so merely
# *running* the harness rewrote bytecode and forced a needless multi-minute re-freeze; the same
# was true of a stray .DS_Store. Both are pruned here rather than left to the caller's habits.
if [ -z "${FORCE:-}" ] && [ -x "$target" ]; then
  newer_source="$(find packaging/entry.py packaging/daisy-daemon.spec pyproject.toml uv.lock \
    src/daisy .agents/agents .agents/skills .agents/mcp.json web/out \
    -type d -name __pycache__ -prune -o \
    -type f ! -name '.DS_Store' ! -name '*.pyc' -newer "$target" -print -quit 2>/dev/null || true)"
  if [ -z "$newer_source" ]; then
    echo "daemon up to date; skipping freeze (set FORCE=1 to rebuild)"
    exit 0
  fi
  echo "daemon stale (changed: $newer_source); re-freezing"
fi

echo "freezing the harness with pyinstaller"
uv run pyinstaller \
  --clean --noconfirm \
  --distpath packaging/dist \
  --workpath packaging/build \
  packaging/daisy-daemon.spec

echo "smoke-testing the frozen daemon"
# In a sandbox of its own, and this is the whole point rather than tidiness.
#
# A daemon started here with the caller's XDG directories finds the lock already held by
# whatever daemon the developer is running, logs "Another daisyd already holds the runtime
# directory; standing down", and exits **0**. The old probe then found the *existing* daemon's
# socket answering and printed "ok" — a green smoke test for a binary it had never exercised,
# and green precisely in the case that is most common now that the app expects you to have a
# daemon running. Isolating every XDG root means this binary is the only thing that could
# possibly answer, so the probe tests what it claims to. It also stops a build from seeding the
# developer's real configuration or writing to their transcript store.
smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/daisy-smoke.XXXXXX")"
trap 'kill "${daemon_pid:-}" 2>/dev/null || true; rm -rf "$smoke_root"' EXIT
smoke_log="$smoke_root/daemon.log"

# The binary is a single image with three entry points, so the daemon has to be asked for by
# name; launching it bare would land in the CLI and exit immediately.
if [ ! -x "$target" ]; then
  echo "failed: pyinstaller did not produce $target" >&2
  exit 1
fi
env XDG_RUNTIME_DIR="$smoke_root/run" \
    XDG_CONFIG_HOME="$smoke_root/config" \
    XDG_DATA_HOME="$smoke_root/data" \
    XDG_STATE_HOME="$smoke_root/state" \
    XDG_CACHE_HOME="$smoke_root/cache" \
  "$repo_root/$target" daisyd >"$smoke_log" 2>&1 &
daemon_pid=$!

# Readiness is this daemon publishing its handshake and answering on its socket — not a fixed
# port, because the loopback port is chosen at startup. Poll rather than sleeping once: a frozen
# binary's first boot unpacks itself and imports a heavy graph, which can take far longer than
# any fixed wait.
socket="$smoke_root/run/daisy/daisyd.sock"
ready=""
for _ in $(seq 1 60); do
  sleep 1
  # Liveness first. A dead child can never become ready, and checking the socket first is how
  # the old version came to trust one it did not own.
  if ! kill -0 "$daemon_pid" 2>/dev/null; then
    echo "failed: the frozen daemon exited before it was ready" >&2
    sed 's/^/  /' "$smoke_log" >&2 || true
    exit 1
  fi
  if [ -S "$socket" ] \
     && curl -fsS -m 5 --unix-socket "$socket" http://daemon/health >/dev/null 2>&1; then
    ready=1
    break
  fi
done
if [ -z "$ready" ]; then
  echo "failed: daisyd did not become ready" >&2
  sed 's/^/  /' "$smoke_log" >&2 || true
  exit 1
fi
echo "ok: daisyd answers on its own socket"
kill "$daemon_pid" 2>/dev/null || true
wait "$daemon_pid" 2>/dev/null || true
daemon_pid=""

if [ "$(uname -s)" = "Darwin" ]; then
  cat <<'NEXT'

built: packaging/dist/Daisy Computer Use.app

Next, for an Accessibility grant that survives rebuilds:
  packaging/sign-app.sh "packaging/dist/Daisy Computer Use.app"
Then install it and put the command on your PATH:
  ditto "packaging/dist/Daisy Computer Use.app" "/Applications/Daisy Computer Use.app"
  ln -sf "/Applications/Daisy Computer Use.app/Contents/MacOS/daisy" /usr/local/bin/daisy
NEXT
else
  # No bundle, no signing, and no Accessibility to preserve — those are macOS concerns. What a
  # Linux build gets is the same three-entry-point image, which is all the CLI and daemon need.
  cat <<'NEXT'

built: packaging/dist/daisy/daisy

Put it on your PATH:
  ln -sf "$PWD/packaging/dist/daisy/daisy" ~/.local/bin/daisy

The .app wrapper and code signing are macOS-only: they exist for the Accessibility grant,
which has no counterpart here. The desktop app is macOS-only too — `daisy web` is the
interface on this platform.
NEXT
fi
