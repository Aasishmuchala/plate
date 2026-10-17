"""Shared fixtures for the gate tests: the historical project root (optional), the committed goldens, and the
--update-goldens switch.

The tests dir and the golden dir go on sys.path here so test modules can `import clientdata` and
`import procedural` (the suite runs with --import-mode=importlib, which does not add them itself).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE / "golden"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="regenerate the frozen golden JSON (gates/tests/golden/expected_procedural.json); never used in CI",
    )


@pytest.fixture(scope="session")
def update_goldens(request: pytest.FixtureRequest) -> bool:
    """True when pytest was started with --update-goldens."""
    return bool(request.config.getoption("--update-goldens"))


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


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """pyproject sets addopts -q, so `pytest ... -q` is -qq and pytest drops its own "N passed" line. Print it
    (counts only) in that case so a run's last line still says what happened."""
    if config.get_verbosity() > -2:
        return
    parts = []
    for outcome in ("failed", "passed", "skipped", "error", "xfailed", "xpassed"):
        n = len(terminalreporter.stats.get(outcome, []))
        if n:
            parts.append(f"{n} {outcome}")
    if parts:
        terminalreporter.write_line(", ".join(parts))
