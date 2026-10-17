"""Versioned gate reports (BUILD_PLAN decision 27): every metric block ships with the code that made it.

A GateReport carries the gate package version, a BLAKE3 hash of the gates source files, the threshold-set id,
the engine, the per-metric checks and a verdict, so a shipped report can always be traced to the exact code and
thresholds that scored it. Re-scoring never rewrites an old report: write() always makes a new numbered file.

    rep = GateReport.score({"texture.lapvar": 412.0, "ral.de00": 0.4}, tier="A", engine={"id": "nano-banana-2.1"})
    path = write(rep, project_dir, run_id)        # <project_dir>/gates/<run_id>.<n>.json, n = next free integer

verdict is 'fail' iff any hard check fails, 'advisory' if only advisory / calibration_needed checks miss (or
nothing was scored), else 'pass'.

Every check scored against a registered threshold id also carries what it was held to (`threshold`, `op`,
`unit`; null for an unregistered id), the report lists those thresholds under `thresholds` with the registered
status (a check can be forced advisory while its threshold is still hard: the feather band, until calibration
sets a bound), and `flagged_points` names the image boxes a human should look at first.
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
    "FlaggedPoint",
    "GateReport",
    "ThresholdRef",
    "scored_check",
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
    """One scored metric. `threshold` / `op` / `unit` say what it was held to (thresholds.Threshold.describe);
    they stay null for an id that is not in the threshold set."""

    metric_id: str
    value: float | None
    status: Status
    threshold: float | list[float] | None = None
    op: str | None = None
    unit: str | None = None


class ThresholdRef(BaseModel):
    """A registered threshold as a report saw it: op, the bound (`value`, or `range` for a [low, high] entry),
    unit, the registered status (hard / advisory / calibration_needed) and the width it is valid at."""

    op: str | None = None
    value: float | None = None
    range: list[float] | None = None
    unit: str | None = None
    status: Literal["hard", "advisory", "calibration_needed"]
    resolution_px: int | None = None


class FlaggedPoint(BaseModel):
    """An image box (pixels, plate coordinates, clamped to the frame) worth a human look, and why."""

    x: int
    y: int
    w: int
    h: int
    metric_id: str
    note: str = ""


def threshold_ref(
    thr: thresholds.Threshold,
    description: dict[str, Any] | None = None,
    *,
    backend: str | None = None,
    variant: str | None = None,
    target: float | None = None,
) -> ThresholdRef:
    """The ThresholdRef of `thr`: `description` is a Threshold.describe() result (computed when absent)."""
    d = description if description is not None else thr.describe(variant=variant, target=target)
    b = d.get("threshold")
    is_range = isinstance(b, (list, tuple))
    return ThresholdRef(
        op=d.get("op"),
        value=None if is_range or b is None else float(b),
        range=[float(b[0]), float(b[1])] if is_range else None,
        unit=d.get("unit"),
        status=thr.effective_status(backend),
        resolution_px=thr.resolution_px,
    )


def scored_check(
    ts: thresholds.ThresholdSet,
    metric_id: str,
    value: float | None,
    *,
    status: Status | None = None,
    variant: str | None = None,
    backend: str | None = None,
    target: float | None = None,
) -> Check:
    """A Check for a registered metric: scored by ThresholdSet.check unless `status` is forced, with the
    threshold, op and unit it was held to filled in. An unregistered id raises KeyError."""
    thr = ts.get(metric_id)
    if status is None:
        if value is None:  # an unmeasured hard gate never passes
            status = "fail" if thr.effective_status(backend) == "hard" else "advisory"
        else:
            status, _ = ts.check(metric_id, value, variant=variant, backend=backend, target=target)
    return Check(
        metric_id=metric_id,
        value=_jsonable(value),
        status=status,
        **thr.describe(variant=variant if variant in thr.variants else None, target=target),
    )


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
    thresholds: dict[str, ThresholdRef] = Field(default_factory=dict)
    verdict: Status = "advisory"
    flagged_points: list[FlaggedPoint] = Field(default_factory=list)
    backend_notes: list[str] = Field(default_factory=list)
    created_utc: str = Field(default_factory=_now_utc)

    @field_validator("metrics", mode="before")
    @classmethod
    def _plain(cls, v: Any) -> Any:
        return _jsonable(v)

    @model_validator(mode="after")
    def _derive_verdict(self) -> GateReport:
        self.verdict = verdict_of(self.checks)
        self._describe_checks()
        return self

    def _describe_checks(self) -> None:
        """Fill what the producers left out, for the committed set only (a report scored against another set
        id keeps what it has): a check on a registered id with a fixed bound gets its threshold / op / unit,
        and every described check gets its entry under `thresholds`. Variant and param entries (the
        gbuffer preview bound, the shot duration) are never guessed: their producers fill them."""
        ts = thresholds.load()
        if self.threshold_set_id != ts.id:
            return
        checks = []
        for c in self.checks:
            thr = ts.metrics.get(c.metric_id)
            if thr is None or thr.op == "param":
                checks.append(c)
                continue
            if c.op is None and c.threshold is None and c.unit is None and not thr.variants and not thr.param:
                c = c.model_copy(update=thr.describe())
            checks.append(c)
            if c.op is not None and c.metric_id not in self.thresholds:
                self.thresholds[c.metric_id] = threshold_ref(
                    thr, {"threshold": c.threshold, "op": c.op, "unit": c.unit}
                )
        self.checks = checks

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
        refs = {}
        for metric_id, value in values.items():
            thr = ts.get(metric_id)
            be = backend if thr.status_by_backend else None
            variant, target = (variants or {}).get(metric_id), (targets or {}).get(metric_id)
            c = scored_check(ts, metric_id, value, variant=variant, backend=be, target=target)
            checks.append(c)
            refs[metric_id] = threshold_ref(
                thr, {"threshold": c.threshold, "op": c.op, "unit": c.unit}, backend=be
            )
        return cls(
            threshold_set_id=ts.id,
            engine=engine,
            tier=tier,
            metrics={**values, **(extra_metrics or {})},
            checks=checks,
            thresholds=refs,
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
