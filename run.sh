#!/usr/bin/env bash
# Start daemon + sensor in the background, open the TUI; quitting the TUI stops all three.
set -euo pipefail
cd "$(dirname "$0")"

LOGS="${BULBSIG_LOGS:-$HOME/Library/Logs/bulbsig}"
mkdir -p "$LOGS"
URL="$(.venv/bin/python -c 'from bulbsig import config; print(config.load().url)')"
SENSOR_PY="${SENSOR_PY:-/usr/bin/python3}"
pids=()

cleanup() {
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if curl -fsS "$URL/v1/health" >/dev/null 2>&1; then
  echo "daemon already running at $URL"
else
  .venv/bin/bulbsig daemon >>"$LOGS/daemon.log" 2>&1 &
  daemon_pid=$!
  pids+=("$daemon_pid")
  for _ in $(seq 1 50); do
    curl -fsS "$URL/v1/health" >/dev/null 2>&1 && break
    if ! kill -0 "$daemon_pid" 2>/dev/null; then
      echo "daemon failed to start; last lines of $LOGS/daemon.log:" >&2
      tail -n 20 "$LOGS/daemon.log" >&2
      exit 1
    fi
    sleep 0.2
  done
fi

if pgrep -f "sensor_macos.py sensor" >/dev/null 2>&1 || pgrep -f "bulbsig sensor" >/dev/null 2>&1; then
  echo "sensor already running"
else
  "$SENSOR_PY" bulbsig/sensor_macos.py sensor >>"$LOGS/sensor.log" 2>&1 &
  pids+=("$!")
fi

.venv/bin/bulbsig tui
