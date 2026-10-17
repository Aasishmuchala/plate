"""Shared fixtures for the plate_max tests. Puts this folder on sys.path (pytest runs with
--import-mode=importlib, which does not) so tests can import fake_pymxs, and resets the injected
runtime and the config environment around every test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_pymxs import fake_scene  # noqa: E402

from plate_max import rt  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_CONFIG", str(tmp_path / "plate-config" / "config.json"))
    monkeypatch.delenv("PLATE_HOME", raising=False)
    yield
    rt.set_runtime(None)


@pytest.fixture
def scene():
    """fake_scene() installed as the plate_max runtime."""
    runtime = fake_scene()
    rt.set_runtime(runtime)
    return runtime
