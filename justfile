# PLATE task runner (https://just.systems). The Makefile is the Mac entry; this file is the same set of
# recipes for the Windows render box and for CI parity. `uv run --no-sync` uses the venv `uv sync` built;
# the editable install puts every package root on sys.path, so no PYTHONPATH is needed.
set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

py := "uv run --no-sync python"

# list the recipes
default:
    @just --list

# uv sync (Python 3.12) + npm ci (the ui is a workspace of the root package.json)
setup:
    uv sync --python 3.12
    npm ci

# fidelity selftest, hazard scan, pytest, npm test (the same order as CI)
test: selftest hazard test-py test-ui

# the CI job, step for step (scripts/tests/test_ci_parity.py keeps this, the Makefile and ci.yml in step)
ci: selftest hazard lint licences test-py test-scripts claims test-ui build

# the fidelity harness proves itself first
selftest:
    {{py}} -m plate_gates.fidelity_bench selftest

# pytest over the packages
test-py:
    uv run --no-sync pytest

# pytest over scripts/ (the hazard scanner, claims check and CI parity tests)
test-scripts:
    uv run --no-sync pytest scripts/tests

# the ui's vitest suite
test-ui:
    npm test

# production build of the ui
build:
    npm run build

# fail on a non-commercial model weight named anywhere in pixels/
licences:
    {{py}} -m plate_pixels.licences --check

# run the tests behind every public claim (claims.yaml); fail if one lacks a passing test
claims:
    {{py}} scripts/claims_check.py

# ruff check + format check
lint:
    uv run --no-sync ruff check .
    uv run --no-sync ruff format --check .

# run the daemon on 127.0.0.1:4610 (prints the launch URL with the token)
daemon:
    {{py}} -m plate_daemon

# run the pixels worker in stub mode on 127.0.0.1:4611
pixels:
    {{py}} -m plate_pixels.server

# open the labelling tool on a folder of images:  just label D:\renders\batch1
label DIR:
    {{py}} -m plate_label serve "{{DIR}}"

# fail on Windows-hazard constructs (fcntl, os.rename, text open() without encoding, ...); the scanner proves
# itself first, and a stale scripts/hazard_allow.txt entry fails too
hazard:
    {{py}} scripts/hazard_scan.py --selftest
    {{py}} scripts/hazard_scan.py --strict-allow

# the attackers' regression scripts (opt-in; each starts its own daemon)
stress:
    {{py}} scripts/stress/run_all.py
