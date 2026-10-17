#!/usr/bin/env bash
# Set up PLATE on macOS (the dev machine): uv, Node, the Python 3.12 venv, npm ci, the UI build, config.json with the token.
# Mac twin of scripts/bootstrap.ps1. Run from anywhere:  scripts/bootstrap.sh [--no-install]
#   --no-install   only check for uv and Node, never install them
# Never installs the pixels CUDA extra.
set -euo pipefail

cd "$(dirname "$0")/.."
no_install=0
for arg in "$@"; do
    case "$arg" in
        --no-install) no_install=1 ;;
        -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
        *) echo "bootstrap.sh: unknown option $arg" >&2; exit 2 ;;
    esac
done

have() { command -v "$1" >/dev/null 2>&1; }

if ! have uv; then
    if [ "$no_install" -eq 1 ]; then
        echo "uv is missing. Install it: brew install uv" >&2
        exit 1
    fi
    if have brew; then
        echo "uv not found: installing with Homebrew"
        brew install uv
    else
        echo "uv not found: using the official installer from astral.sh"
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi
    have uv || { echo "uv still not on PATH; open a new terminal and re-run bootstrap.sh" >&2; exit 1; }
fi

# package.json engines (from jsdom): ^22.22.2 || ^24.15.0 || >=26.0.0
node_ok() {
    have node && node -e 'const [a, b] = process.versions.node.split(".").map(Number); process.exit((a === 22 && b >= 22) || (a === 24 && b >= 15) || a >= 26 ? 0 : 1)'
}
if ! node_ok; then
    if [ "$no_install" -eq 1 ] || ! have brew; then
        echo "Node 22.22+, 24.15+ or 26+ is missing. Install it: brew install node (or nodejs.org)" >&2
        exit 1
    fi
    echo "Node not found or too old: installing with Homebrew"
    brew install node
    node_ok || { echo "node on PATH is still too old; open a new terminal and re-run bootstrap.sh" >&2; exit 1; }
fi

echo "==> uv sync (Python 3.12 venv)"
uv sync --python 3.12
echo "==> npm ci (root workspace, includes ui)"
npm ci
echo "==> building ui/dist (the daemon serves it at /)"
npm run build -w ui

have ffmpeg || echo "warning: ffmpeg not on PATH: video-gate tests will be skipped (brew install ffmpeg, or set PLATE_FFMPEG)" >&2

export PYTHONUTF8=1
py=".venv/bin/python"
echo "PLATE config: $("$py" scripts/plate_config.py path)"
echo "Bootstrap done. Next: scripts/test.sh"
echo "Then start the daemon (scripts/start-daemon.sh) and open this URL; the UI keeps the token and sends it as a Bearer header:"
echo "  $("$py" scripts/plate_config.py url)"
