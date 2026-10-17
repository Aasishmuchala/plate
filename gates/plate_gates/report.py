"""Versioned gate reports (BUILD_PLAN decision 27): every metric block ships with the code that made it.

A GateReport carries the gate package version, a BLAKE3 hash of the gates source files, the threshold-set id,
the engine, the per-metric checks and a verdict, so a shipped report can always be traced to the exact code and
thresholds that scored it. Re-scoring never rewrites an old report: write() always makes a new numbered file.

    rep = GateReport.score({"texture.lapvar": 412.0, "ral.de00": 0.4}, tier="A", engine={"id": "nano-banana-2.1"})
    path = write(rep, project_dir, run_id)        # <project_dir>/gates/<run_id>.<n>.json, n = next free integer

verdict is 'fail' iff any hard check fails, 'advisory' if only advisory / calibration_needed checks miss (or
nothing was scored), else 'pass'.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
from blake3 import blake3
from pydantic import BaseModel, Field, field_validator, model_validator

from . import __version__, thresholds
from .thresholds import DEFAULT_THRESHOLDS_ID

__all__ = [
    "EngineRef",
    "Check",
    "GateReport",
    "metric_code_hash",
    "verdict_of",
    "write",
    "read",
    "gate_report",
]

Status = Literal["pass", "fail", "advisory"]


@lru_cache(maxsize=1)
def metric_code_hash() -> str:
    """BLAKE3 hex of every gates/plate_gates/*.py, in sorted file-name order, LF line endings.

    Computed once per process (the package does not change while it runs). CRLF is normalised so a Windows
    checkout without eol=lf hashes to the same value.
    """
    here = Path(__file__).resolve().parent
    h = blake3()
    for path in sorted(here.glob("*.py"), key=lambda q: q.name):
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jsonable(x: Any) -> Any:
    """numpy scalars / arrays, tuples, Fractions and Paths -> plain JSON types; NaN / inf -> None."""
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return _jsonable(x.tolist())
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (np.floating, float)):
        f = float(x)
        return f if math.isfinite(f) else None
    if isinstance(x, Fraction):
        return f"{x.numerator}/{x.denominator}"
    if isinstance(x, Path):
        return x.as_posix()
    return x


class EngineRef(BaseModel):
    id: str
    version: str | None = None


class Check(BaseModel):
    metric_id: str
    value: float | None
    status: Status


def verdict_of(checks: list[Check]) -> Status:
    """fail if any check failed (only hard thresholds fail), advisory if any advisory miss or nothing scored."""
    if any(c.status == "fail" for c in checks):
        return "fail"
    if not checks or any(c.status == "advisory" for c in checks):
        return "advisory"
    return "pass"


class GateReport(BaseModel):
    gate_version: str = __version__
    metric_code_hash: str = Field(default_factory=metric_code_hash)
    threshold_set_id: str = DEFAULT_THRESHOLDS_ID
    engine: EngineRef | None = None
    tier: str | None = None
    metrics: dict[str, bool | int | float | dict | list | str | None] = Field(default_factory=dict)
    checks: list[Check] = Field(default_factory=list)
    verdict: Status = "advisory"
    backend_notes: list[str] = Field(default_factory=list)
    created_utc: str = Field(default_factory=_now_utc)

    @field_validator("metrics", mode="before")
    @classmethod
    def _plain(cls, v: Any) -> Any:
        return _jsonable(v)

    @model_validator(mode="after")
    def _derive_verdict(self) -> GateReport:
        self.verdict = verdict_of(self.checks)
        return self

    @classmethod
    def score(
        cls,
        values: dict[str, float],
        *,
        tier: str | None = None,
        engine: EngineRef | dict | None = None,
        backend: str | None = None,
        variants: dict[str, str] | None = None,
        targets: dict[str, float] | None = None,
        extra_metrics: dict[str, Any] | None = None,
        backend_notes: list[str] | None = None,
    ) -> GateReport:
        """Score flat {metric_id: value} against the committed threshold set and wrap the result.

        variants / targets are per-metric overrides for thresholds.check (e.g. {"gbuffer_edge_recall_r2":
        "preview"}, {"video.duration_s": 5.0}); backend names the flow backend (raft / dis) and applies to the
        metrics whose status depends on it. Unknown metric ids raise KeyError: nothing is skipped silently.
        """
        ts = thresholds.load()
        checks = []
        for metric_id, value in values.items():
            thr = ts.get(metric_id)
            status, _ = ts.check(
                metric_id,
                value,
                variant=(variants or {}).get(metric_id),
                backend=backend if thr.status_by_backend else None,
                target=(targets or {}).get(metric_id),
            )
            v = _jsonable(value)
            checks.append(Check(metric_id=metric_id, value=v, status=status))
        return cls(
            threshold_set_id=ts.id,
            engine=engine,
            tier=tier,
            metrics={**values, **(extra_metrics or {})},
            checks=checks,
            backend_notes=list(backend_notes or []),
        )

    def to_json(self) -> str:
        """Pretty JSON text, strict (no NaN), LF newline at the end."""
        return json.dumps(self.model_dump(mode="json"), indent=1, allow_nan=False) + "\n"


_RUN_ID = re.compile(r"^[A-Za-z0-9_\-]+$")


def write(report: GateReport, dir: str | Path, run_id: str, *, subdir: str = "gates") -> Path:
    """Write `report` as <dir>/<subdir>/<run_id>.<n>.json with n the next free integer (1, 2, ...).

    `dir` is the project folder (pass subdir="" to write straight into `dir`). The file is created exclusively,
    so an existing report is never overwritten even with concurrent writers; a lost race just takes the next n.
    run_id may use letters, digits, '_' and '-' only (it ends up in a Windows file name).
    """
    if not _RUN_ID.match(run_id):
        raise ValueError(f"run_id {run_id!r} must match [A-Za-z0-9_-]+")
    folder = Path(dir) / subdir if subdir else Path(dir)
    folder.mkdir(parents=True, exist_ok=True)
    pat = re.compile(rf"^{re.escape(run_id)}\.(\d+)\.json$")
    taken = [int(m.group(1)) for q in folder.iterdir() if (m := pat.match(q.name))]
    n = max(taken, default=0) + 1
    text = report.to_json()
    while True:
        path = folder / f"{run_id}.{n}.json"
        try:
            fh = open(path, "x", encoding="utf-8", newline="\n")
        except FileExistsError:
            n += 1
            continue
        with fh:
            fh.write(text)
        return path


def read(path: str | Path) -> GateReport:
    """Load a report written by write()."""
    return GateReport.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def gate_report(
    metrics: dict,
    thresholds_id: str = DEFAULT_THRESHOLDS_ID,
    engine_version: str | None = None,
    engine: str | None = None,
    tier: str | None = None,
) -> dict:
    """Wrap a free-form metrics dict (e.g. compare() output) in the report envelope; returns plain JSON data.

    Nothing is scored (checks is empty, verdict 'advisory'); use GateReport.score for thresholded values.
    """
    ref = {"id": engine or "unknown", "version": engine_version} if (engine or engine_version) else None
    rep = GateReport(threshold_set_id=thresholds_id, engine=ref, tier=tier, metrics=metrics)
    return rep.model_dump(mode="json")
