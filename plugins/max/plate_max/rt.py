"""The only door to pymxs. runtime() returns the injected fake in tests and lazily imports
pymxs inside Max, so every other module imports cleanly off-Max (pattern from MaxDirector's
maxbridge _rt() and MaxGaffer's lazy getters).
"""

from __future__ import annotations

from typing import Any

_runtime: Any = None


def set_runtime(obj: Any) -> None:
    """Inject a runtime (tests). Pass None to go back to the real pymxs."""
    global _runtime
    _runtime = obj


def runtime() -> Any:
    if _runtime is not None:
        return _runtime
    import pymxs  # only here, only when Max is the host

    return pymxs.runtime
