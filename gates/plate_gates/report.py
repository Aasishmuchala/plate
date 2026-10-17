"""Versioned gate reports (BUILD_PLAN decision 27): every metric block ships with the code that made it.

A report carries the gate package version, a BLAKE3 hash of the metric source files, the threshold-set id
and the engine version, so a shipped report can always be traced to the exact code and thresholds that
scored it. Re-scoring never rewrites an old report; it makes a new one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from blake3 import blake3

from . import __version__
from .thresholds import DEFAULT_THRESHOLDS_ID

_HASHED_MODULES = ("color.py", "structure.py", "compare.py", "labmatch.py")


def metric_code_hash() -> str:
    """BLAKE3 hex of the UTF-8 source of color, structure, compare, labmatch (that order, LF line endings)."""
    here = Path(__file__).resolve().parent
    h = blake3()
    for name in _HASHED_MODULES:
        # normalise CRLF so a Windows checkout without eol=lf still hashes to the same value
        h.update((here / name).read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def gate_report(
    metrics: dict,
    thresholds_id: str = DEFAULT_THRESHOLDS_ID,
    engine_version: str | None = None,
) -> dict:
    """Wrap a metrics dict in the versioned report envelope."""
    return {
        "gate_version": __version__,
        "metric_code_hash": metric_code_hash(),
        "thresholds_id": thresholds_id,
        "engine_version": engine_version,
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metrics": metrics,
    }
