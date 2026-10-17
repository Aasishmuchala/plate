"""Shared fixtures for the gate tests: the historical project root (optional) and the committed goldens."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def test_root() -> Path:
    """Folder holding the historical projects (PLATE_TEST_ROOT, default ~/Desktop/test); skips when absent."""
    root = Path(os.environ.get("PLATE_TEST_ROOT") or Path.home() / "Desktop" / "test")
    if not root.is_dir():
        pytest.skip(f"test root not found: {root} (set PLATE_TEST_ROOT)")
    return root


@pytest.fixture(scope="session")
def golden_dir() -> Path:
    """gates/tests/golden: small committed derivatives that always run."""
    return Path(__file__).resolve().parent / "golden"
