"""Make `plate_spec` importable when the daemon runs outside pytest.

pytest puts `spec/` on the path (pyproject pythonpath); `python -m plate_daemon` started from a bare
shell may not. This module adds `<repo>/spec` to `sys.path` only if `import plate_spec` fails, where
repo = two levels above this package (daemon/plate_daemon -> repo). Every daemon module that needs the
packet validator imports it from here so the contract has exactly one implementation (decision 5).
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import plate_spec
except ImportError:  # pragma: no cover - exercised only outside pytest
    _spec_dir = Path(__file__).resolve().parents[2] / "spec"
    if str(_spec_dir) not in sys.path:
        sys.path.insert(0, str(_spec_dir))
    import plate_spec

__all__ = ["plate_spec"]
