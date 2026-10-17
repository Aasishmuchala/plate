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
test:
    {{py}} -m plate_gates.fidelity_bench selftest
    {{py}} scripts/hazard_scan.py
    uv run --no-sync pytest
    npm test

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

# fail on Windows-hazard constructs (fcntl, os.rename, text open() without encoding, ...)
hazard:
    {{py}} scripts/hazard_scan.py
