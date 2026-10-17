#!/usr/bin/env bash
# Run the PLATE CI job on macOS, step for step (the Mac twin of scripts/test.ps1, and what `make ci` does):
# fidelity selftest, hazard scan, ruff, licences, pytest, scripts tests, claims, npm test, ui build.
# scripts/tests/test_ci_parity.py keeps this file, test.ps1, the Makefile, the justfile and ci.yml in step.
# Usage: scripts/test.sh [--skip-ui]
set -euo pipefail

cd "$(dirname "$0")/.."
skip_ui=0
for arg in "$@"; do
    case "$arg" in
        --skip-ui) skip_ui=1 ;;
        -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
        *) echo "test.sh: unknown option $arg" >&2; exit 2 ;;
    esac
done

py=".venv/bin/python"
[ -x "$py" ] || { echo "No .venv: run scripts/bootstrap.sh first" >&2; exit 1; }
export PYTHONUTF8=1
export PYTHONPATH="spec:daemon:pixels:engines:gates:plugins/max:tools"

step() { echo "==> $1"; shift; "$@"; }

step "fidelity_bench selftest" "$py" -m plate_gates.fidelity_bench selftest
step "hazard scanner selftest" "$py" scripts/hazard_scan.py --selftest
step "hazard scan" "$py" scripts/hazard_scan.py --strict-allow
step "ruff check" "$py" -m ruff check .
step "ruff format" "$py" -m ruff format --check .
step "licences" "$py" -m plate_pixels.licences --check
step "pytest" "$py" -m pytest
step "scripts tests" "$py" -m pytest scripts/tests
step "claims" "$py" scripts/claims_check.py
if [ "$skip_ui" -eq 0 ]; then
    step "npm test" npm test
    step "npm run build" npm run build
fi
echo "All PLATE checks passed."
