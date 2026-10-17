#!/usr/bin/env bash
# Start the PLATE daemon in the foreground on 127.0.0.1:4610 (the Mac twin of scripts/start-daemon.ps1).
# The daemon prints "PLATE daemon http://127.0.0.1:<port>/#token=<token>" itself; stdout is the terminal here.
# When ui/dist/index.html is missing the UI is built first; a failed build is a warning, the daemon still starts.
#   scripts/start-daemon.sh [--port N] [--tailscale]
#   --tailscale   also bind this machine's Tailscale IPv4 (never 0.0.0.0)
set -euo pipefail

cd "$(dirname "$0")/.."
port="${PLATE_PORT:-4610}"
tailscale=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --port) port="${2:?--port needs a number}"; shift 2 ;;
        --tailscale) tailscale=1; shift ;;
        -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
        *) echo "start-daemon.sh: unknown option $1" >&2; exit 2 ;;
    esac
done

py=".venv/bin/python"
[ -x "$py" ] || { echo "No .venv: run scripts/bootstrap.sh first" >&2; exit 1; }
export PYTHONUTF8=1
export PYTHONPATH="spec:daemon:pixels:engines:gates:plugins/max:tools"
if [ ! -f "ui/dist/index.html" ]; then
    echo "building ui/dist"
    if ! npm run build -w ui; then
        echo "warning: the UI build failed; the daemon starts without the UI (scripts/bootstrap.sh installs what the build needs)" >&2
    fi
fi
export PLATE_PORT="$port"
bind="127.0.0.1"
if [ "$tailscale" -eq 1 ]; then
    command -v tailscale >/dev/null 2>&1 || { echo "tailscale CLI not found (is Tailscale installed and up?)" >&2; exit 1; }
    ts="$(tailscale ip -4 | head -n 1)"
    [ -n "$ts" ] || { echo "No Tailscale IPv4 address found (is Tailscale up?)" >&2; exit 1; }
    bind="127.0.0.1,$ts"
fi
export PLATE_BIND="$bind"
echo "PLATE daemon on $bind port $port"
exec "$py" -m plate_daemon
