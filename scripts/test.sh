#!/usr/bin/env bash
# Run the PLATE test suite on macOS: fidelity selftest, hazard scan, ruff, pytest, npm test (the Mac twin of
# scripts/test.ps1, and what `make test` does).  Usage: scripts/test.sh [--skip-ui]
set -euo pipefail

cd "$(dirname "$0")/.."
skip_ui=0
for arg in "$@"; do
    case "$arg" in
        --skip-ui) skip_ui=1 ;;
        -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
        *) echo "test.sh: unknown option $arg" >&2; exit 2 ;;
    esac
done

py=".venv/bin/python"
[ -x "$py" ] || { echo "No .venv: run scripts/bootstrap.sh first" >&2; exit 1; }
export PYTHONUTF8=1
export PYTHONPATH="spec:daemon:pixels:engines:gates:plugins/max:tools"

step() { echo "==> $1"; shift; "$@"; }

step "fidelity_bench selftest" "$py" -m plate_gates.fidelity_bench selftest
step "hazard scan" "$py" scripts/hazard_scan.py
step "ruff check" "$py" -m ruff check .
step "pytest" "$py" -m pytest
if [ "$skip_ui" -eq 0 ]; then
    step "npm test" npm test
fi
echo "All PLATE tests passed."
