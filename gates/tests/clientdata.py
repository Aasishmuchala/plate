"""Access to the client's historical project folders for the optional regression tests.

Client imagery under ~/Desktop/test (or PLATE_TEST_ROOT) is client data: it is never copied into the repo and
tests reach it only through path(), which skips the test when the file is absent (a fresh clone, CI, the
render box). The root is derived from Path.home(), not hard-coded.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

__all__ = ["root", "path"]


def root() -> Path:
    """PLATE_TEST_ROOT if set, else ~/Desktop/test (may not exist)."""
    return Path(os.environ.get("PLATE_TEST_ROOT") or Path.home() / "Desktop" / "test")


def path(rel: str) -> Path:
    """Absolute path of `rel` under the client root; pytest.skip when the root or the file is absent."""
    p = root() / rel
    if not p.exists():
        pytest.skip(f"client data not present: {p}")
    return p
