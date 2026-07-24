#!/usr/bin/env bash
# Throwaway end-to-end exercise of a real session. Removed once it has served its purpose.
set -u

root="/home/user/daisy"
sandbox="/tmp/xeac-flow"
rm -rf "$sandbox"
export XDG_RUNTIME_DIR="$sandbox/run" XDG_DATA_HOME="$sandbox/data" \
       XDG_CONFIG_HOME="$sandbox/cfg" XDG_STATE_HOME="$sandbox/state" \
       PYTHONPATH="$root/src"
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" "$XDG_STATE_HOME"
cd "$root" || exit 1

log="$sandbox/daemon.log"
setsid python3 -m xeac xeacd > "$log" 2>&1 < /dev/null &
sleep 12

pidfile="$XDG_RUNTIME_DIR/xeac/xeacd.pid"
if [ ! -f "$pidfile" ]; then
  echo "FAIL: the daemon never published a pid"
  tail -5 "$log"
  exit 1
fi
daemon_pid=$(cat "$pidfile")
echo "ok    daemon running as pid $daemon_pid"

session=$(timeout 90 python3 -m xeac create --agent general-assistant -C /tmp | tail -1)
case "$session" in
  session-*) echo "ok    created $session" ;;
  *) echo "FAIL: create returned '$session'"; tail -5 "$log"; exit 1 ;;
esac

child=$(timeout 90 python3 -m xeac create --parent "$session" --mode read_only | tail -1)
echo "ok    child $child"

timeout 120 python3 -m xeac send "$session" "hello" > "$sandbox/send.out" 2>&1
if grep -q "failed\|error" "$sandbox/send.out"; then
  echo "FAIL: send -> $(tail -1 "$sandbox/send.out")"
else
  echo "ok    send accepted"
fi
sleep 8

unauthorized=$(grep -c "unauthorized" "$log" 2>/dev/null | head -1 || echo 0); unauthorized=${unauthorized:-0}
if [ "$unauthorized" -gt 0 ]; then
  echo "FAIL: $unauthorized persistence call(s) were refused"
else
  echo "ok    the worker persists through the daemon"
fi

turns=$(timeout 30 python3 -m xeac history "$session" --json 2>/dev/null | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
if [ "$turns" -gt 0 ]; then
  echo "ok    $turns turn(s) persisted and readable"
else
  echo "FAIL: no turns were persisted"
fi

# A worker that dies without being reaped must be noticed, not left claiming to run.
worker_pid=$(timeout 30 python3 -m xeac get "$child" --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['pid'])" 2>/dev/null)
if [ -n "${worker_pid:-}" ] && [ "$worker_pid" != "0" ]; then
  kill -9 "$worker_pid" 2>/dev/null
  sleep 4
  state=$(timeout 30 python3 -m xeac get "$child" --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])" 2>/dev/null)
  if [ "$state" = "failed" ] || [ "$state" = "exited" ]; then
    echo "ok    a killed worker is noticed (status $state)"
  else
    echo "FAIL: a killed worker still reads as '$state'"
  fi
fi

timeout 30 python3 -m xeac daemon stop > /dev/null 2>&1
sleep 6
if kill -0 "$daemon_pid" 2>/dev/null; then
  echo "FAIL: the daemon survived stop"
else
  echo "ok    daemon stopped"
fi
if [ -f "$XDG_RUNTIME_DIR/xeac/token" ]; then
  echo "FAIL: the handshake outlived the daemon"
else
  echo "ok    handshake cleaned up"
fi
leftover=$(ls "$XDG_RUNTIME_DIR/xeac/sessions" 2>/dev/null | wc -l)
echo "ok    $leftover leftover session socket(s)"
