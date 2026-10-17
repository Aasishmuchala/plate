.PHONY: help setup selftest test test-py test-ui lint fmt check spec daemon pixels label

PYPATH := spec:daemon:pixels:engines:gates:plugins/max:tools
PY := PYTHONPATH=$(PYPATH) .venv/bin/python
PYTEST := .venv/bin/pytest
RUFF := .venv/bin/ruff

help:
	@echo "make setup     uv sync (Python 3.12) + npm install"
	@echo "make selftest  fidelity_bench selftest (first CI job)"
	@echo "make test      selftest, then pytest and npm test"
	@echo "make lint      ruff check + format check"
	@echo "make spec      validate the three example packets"
	@echo "make daemon    run the daemon on 127.0.0.1:4610 (fake engine)"
	@echo "make pixels    run the pixels worker in stub mode"
	@echo "make label DIR=<folder of images>   open the labelling tool"

setup:
	uv sync --python 3.12
	npm install

selftest:
	$(PY) -m plate_gates.fidelity_bench selftest

test-py:
	$(PYTEST)

test-ui:
	npm test

test: selftest test-py test-ui

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
