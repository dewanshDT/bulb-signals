#!/usr/bin/env bash
# Install/uninstall the bulbsig LaunchAgents for the current user.
set -euo pipefail

usage() {
  cat <<USAGE
usage: $0 [--sensor-only | --daemon-only | --all] [--uninstall]
          [--python PATH] [--sensor-python PATH]

  --sensor-only     sensor only (daemon runs elsewhere, e.g. a Linux VM; set [daemon] url/token
                    in ~/.config/bulbsig/config.toml)
  --daemon-only     daemon only
  --all             both (default)
  --uninstall       stop and remove the selected agents
  --python          interpreter for the daemon (default: <repo>/.venv/bin/python)
  --sensor-python   interpreter for the sensor (default: <repo>/.venv/bin/python if present,
                    else /usr/bin/python3; any Python >= 3.9, stdlib only)
USAGE
}

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/bulbsig"
UID_="$(id -u)"
which_="all"
uninstall=0
py=""
sensor_py=""

while [ $# -gt 0 ]; do
  case "$1" in
    --sensor-only) which_="sensor" ;;
    --daemon-only) which_="daemon" ;;
    --all) which_="all" ;;
    --uninstall) uninstall=1 ;;
    --python) py="${2:?--python needs a path}"; shift ;;
    --sensor-python) sensor_py="${2:?--sensor-python needs a path}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "this installer is for macOS; on Linux see deploy/linux/bulbsig-daemon.service" >&2
  exit 1
fi

venv_py="$REPO/.venv/bin/python"
[ -n "$py" ] || py="$venv_py"
if [ -z "$sensor_py" ]; then
  if [ -x "$venv_py" ]; then sensor_py="$venv_py"; else sensor_py="/usr/bin/python3"; fi
fi

components=()
case "$which_" in
  sensor) components=(sensor) ;;
  daemon) components=(daemon) ;;
  all) components=(daemon sensor) ;;
esac

sed_escape() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }

stop_agent() {
  launchctl bootout "gui/$UID_/local.bulbsig.$1" 2>/dev/null || true
}

install_agent() {
  local name="$1" python="$2"
  local label="local.bulbsig.$name"
  local src="$REPO/deploy/macos/$label.plist"
  local dst="$AGENTS/$label.plist"
  if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    echo "error: $python is not a working Python >= 3.9" >&2
    exit 1
  fi
  if [ "$name" = "daemon" ] && ! "$python" -c 'import aiohttp' 2>/dev/null; then
    echo "error: $python cannot import aiohttp; create the venv first:" >&2
    echo "  python3 -m venv '$REPO/.venv' && '$REPO/.venv/bin/pip' install -e '$REPO'" >&2
    exit 1
  fi
  mkdir -p "$AGENTS" "$LOGS"
  sed -e "s|__PYTHON__|$(sed_escape "$python")|g" \
      -e "s|__REPO__|$(sed_escape "$REPO")|g" \
      -e "s|__HOME__|$(sed_escape "$HOME")|g" "$src" > "$dst.tmp"
  plutil -lint "$dst.tmp" >/dev/null
  mv "$dst.tmp" "$dst"
  stop_agent "$name"
  launchctl enable "gui/$UID_/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_" "$dst"
  echo "installed $label -> $dst"
}

if [ "$uninstall" = 1 ]; then
  for c in "${components[@]}"; do
    stop_agent "$c"
    rm -f "$AGENTS/local.bulbsig.$c.plist"
    echo "removed local.bulbsig.$c"
  done
  exit 0
fi

for c in "${components[@]}"; do
  if [ "$c" = "daemon" ]; then install_agent daemon "$py"; else install_agent sensor "$sensor_py"; fi
done

if [[ " ${components[*]} " == *" sensor "* ]]; then
  real="$("$sensor_py" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
  cat <<MSG

The sensor reads the Notification Center database and needs Full Disk Access for
the exact binary launchd runs:

    $real

System Settings -> Privacy & Security -> Full Disk Access -> '+', press Cmd+Shift+G,
paste the path above, enable it, then restart the sensor:

    launchctl kickstart -k gui/$UID_/local.bulbsig.sensor

If access is still denied (framework Python builds), also add:
    $("$sensor_py" -c 'import os, sys; p = os.path.join(sys.base_prefix, "Resources/Python.app/Contents/MacOS/Python"); print(p if os.path.exists(p) else "(not applicable)")')
Re-grant after upgrading that Python (the path changes).
MSG
fi

cat <<MSG

Logs:    tail -f "$LOGS"/*.log
Status:  launchctl print gui/$UID_/local.bulbsig.sensor | grep -E 'state|last exit'
Remove:  $0 --uninstall
MSG
