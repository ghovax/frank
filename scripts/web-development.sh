#!/usr/bin/env bash
# Start the web UI in development, pointed at the daemon already running on this machine.
#
#   ./scripts/web-development.sh          # then open http://localhost:3000
#
# The daemon takes an ephemeral loopback port and mints a capability token. The desktop shell
# reads both out of the runtime directory and hands them to the page; a browser tab can read
# neither, so `bun run dev` on its own guesses a port the daemon never binds and presents no
# token — every list comes back empty and nothing says why. This asks the daemon for its own
# endpoint and passes it in as `NEXT_PUBLIC_*`, which is the only way a value reaches a client
# bundle.
#
# Development only, and enforced rather than trusted: the reader in `web/src/lib/api.ts` ignores
# the token unless `NODE_ENV` is not production, and Next eliminates that branch from a
# production build — so a token cannot end up in a shipped export even if this is exported into
# the environment of one.
#
# Run this from an ordinary shell, NOT from inside `nix develop`. The devshell rewrites
# `TMPDIR`, and the runtime directory hangs off it, so a daemon started outside the devshell is
# invisible to anything started inside it — `frank ps` included. The handoff to bun below enters
# the devshell itself, for exactly that reason.
set -euo pipefail

repository="$(cd "$(dirname "$0")/.." && pwd)"

# Asked of the daemon rather than read off a path this script works out for itself: the CLI
# already owns where the runtime directory is, and one source of truth for that is the point.
if [[ -x "$repository/.venv/bin/python" ]]; then
  frank=("$repository/.venv/bin/python" -m frank)
else
  frank=(uv run --project "$repository" python -m frank)
fi

if ! endpoint="$("${frank[@]}" daemon endpoint 2>&1)"; then
  echo "Could not reach a daemon: $endpoint" >&2
  echo "Start one first, from an ordinary shell:  uv run python -m frank frankd" >&2
  exit 1
fi

read -r port token <<<"$(
  printf '%s' "$endpoint" | python3 -c \
    'import json, sys; endpoint = json.load(sys.stdin); print(endpoint["port"], endpoint["token"])'
)"

echo "Daemon on 127.0.0.1:$port — starting the UI at http://localhost:${DEV_PORT:-3000}" >&2

cd "$repository/web"
export NEXT_PUBLIC_API_BASE="http://127.0.0.1:$port"
export NEXT_PUBLIC_TOKEN="$token"

if command -v bun >/dev/null 2>&1; then
  exec bun run dev "$@"
fi
exec nix develop . -c bun run dev "$@"
