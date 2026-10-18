.PHONY: help setup selftest hazard licences claims build test test-py test-scripts test-ui ci lint fmt check spec daemon pixels label stress

PYPATH := spec:daemon:pixels:engines:gates:plugins/max:tools
PY := PYTHONPATH=$(PYPATH) .venv/bin/python
PYTEST := .venv/bin/pytest
RUFF := .venv/bin/ruff

help:
	@echo "make setup     uv sync (Python 3.12) + npm install"
	@echo "make selftest  fidelity_bench selftest (first CI job)"
	@echo "make hazard    Windows-hazard scanner selftest, then the scan (fcntl, os.rename, open() without encoding, ...)"
	@echo "make licences  fail on a non-commercial model weight named anywhere in pixels/"
	@echo "make claims    run the tests behind every public claim (claims.yaml); fail if one lacks a passing test"
	@echo "make test      selftest, hazard scan, then pytest and npm test"
	@echo "make lint      ruff check + format check"
	@echo "make ci        every step of .github/workflows/ci.yml, in its order"
	@echo "make spec      validate the three example packets"
	@echo "make daemon    run the daemon on 127.0.0.1:4610 (fake engine)"
	@echo "make pixels    run the pixels worker in stub mode"
	@echo "make label DIR=<folder of images>   open the labelling tool"
	@echo "make stress    the attackers' regression scripts (opt-in; each starts its own daemon)"

setup:
	uv sync --python 3.12
	npm install

selftest:
	$(PY) -m plate_gates.fidelity_bench selftest

test-py:
	$(PYTEST)

test-ui:
	npm test

hazard:
	$(PY) scripts/hazard_scan.py --selftest
	$(PY) scripts/hazard_scan.py --strict-allow

licences:
	$(PY) -m plate_pixels.licences --check

claims:
	$(PY) scripts/claims_check.py

build:
	npm run build

test-scripts:
	$(PYTEST) scripts/tests

test: selftest hazard test-py test-ui

# The CI job, step for step (scripts/tests/test_ci_parity.py keeps this, the justfile and ci.yml in step).
ci: selftest hazard lint licences test-py test-scripts claims test-ui build

lint:
	$(RUFF) check .
	$(RUFF) format --check .

fmt:
	$(RUFF) format .
	$(RUFF) check --fix .

spec:
	$(PYTEST) spec/tests -q

check: lint test

daemon:
	$(PY) -m plate_daemon

pixels:
	$(PY) -m plate_pixels.server

label:
	$(PY) -m plate_label serve $(DIR)

stress:
	$(PY) scripts/stress/run_all.py
