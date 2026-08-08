#!/usr/bin/env bash
# Start the web UI in development through the stable local bridge.
#
#   ./scripts/web-development.sh          # then open http://localhost:3000
#
# The browser uses port 8824 while that bridge follows the daemon's ephemeral authenticated endpoint.
# Run this outside `nix develop`; the handoff below enters the devshell only after finding the daemon.
set -euo pipefail

repository="$(cd "$(dirname "$0")/.." && pwd)"

# Asked of the daemon rather than read off a path this script works out for itself: the CLI
# already owns where the runtime directory is, and one source of truth for that is the point.
if [[ -x "$repository/.venv/bin/python" ]]; then
  langmesh=("$repository/.venv/bin/python" -m langmesh)
else
  langmesh=(uv run --project "$repository" python -m langmesh)
fi

if ! daemon_endpoint="$("${langmesh[@]}" daemon endpoint 2>&1)"; then
  echo "Could not reach a daemon: $daemon_endpoint" >&2
  echo "Start one first, from an ordinary shell:  uv run python -m langmesh langmeshd" >&2
  exit 1
fi

bridge_url="http://127.0.0.1:8824"
bridge_process_id=""

stop_started_bridge() {
  if [[ -z "$bridge_process_id" ]]; then
    return
  fi
  kill "$bridge_process_id" 2>/dev/null || true
  wait "$bridge_process_id" 2>/dev/null || true
}
trap stop_started_bridge EXIT INT TERM

if ! curl --silent --fail "$bridge_url/health" >/dev/null 2>&1; then
  "${langmesh[@]}" serve --host 127.0.0.1 --port 8824 &
  bridge_process_id="$!"
  for startup_attempt in {1..100}; do
    if curl --silent --fail "$bridge_url/health" >/dev/null 2>&1; then
      break
    fi
    if ! kill -0 "$bridge_process_id" 2>/dev/null; then
      wait "$bridge_process_id"
      exit 1
    fi
    sleep 0.1
  done
fi

if ! curl --silent --fail "$bridge_url/health" >/dev/null 2>&1; then
  echo "Could not start the LangMesh bridge at $bridge_url" >&2
  exit 1
fi

echo "LangMesh bridge on $bridge_url — starting the UI at http://localhost:${DEV_PORT:-3000}" >&2

cd "$repository/web"
unset NEXT_PUBLIC_API_BASE NEXT_PUBLIC_TOKEN

if command -v bun >/dev/null 2>&1; then
  bun run dev "$@"
  exit $?
fi
nix develop . -c bun run dev "$@"
