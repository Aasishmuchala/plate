"""Gate-report versioning service for a project folder (BUILD_PLAN decision 27).

Reports live at `<project>/gates/<run_id>.<n>.json`, `n` counting up from 1, in the format
`plate_gates.report.write` produces (the daemon and the CLI read each other's files). A report file is
written once, with an exclusive create, and never modified afterwards:

    write(run_id, report)      the next free n; an existing file is never overwritten
    rescore(run_id, report)    a new n+1 with `rescored_from: <n>` in the body, after a metric-code or
                               threshold change; the old report stays as it was
    shipped(run_id)            marks a report (default: the latest) as delivered by writing a sidecar
                               `<run_id>.<n>.shipped.json` {shipped_utc, report_blake3}; the report file
                               is not touched, so its bytes are the bytes that shipped. A later rescore of
                               that run is written as a new file and carries `rescored_from_shipped: true`.
    verify_shipped(run_id)     recomputes the BLAKE3 and compares it with the sidecar (tamper check)

A report must say what scored it: `gate_version`, `metric_code_hash` and `threshold_set_id` are required
(`GateReport` always carries them; the engine version rides in `engine`). Reports may be a
`plate_gates.report.GateReport`, any object with `model_dump(mode="json")`, or a plain dict. Writers hold
the project's `Lock`; this class does not take it.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from blake3 import blake3

from .fsutil import write_json_atomic
from .projects import GATES_DIR
from .timeutil import iso, utcnow

__all__ = ["NoReport", "ReportError", "Service", "StoredReport"]

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
REQUIRED_FIELDS = ("gate_version", "metric_code_hash", "threshold_set_id")
SHIPPED_SUFFIX = ".shipped.json"


class ReportError(ValueError):
    pass


class NoReport(LookupError):
    pass


@dataclass(frozen=True)
class StoredReport:
    run_id: str
    n: int
    path: Path
    report: dict[str, Any]
    shipped_utc: str | None = None

    @property
    def shipped(self) -> bool:
        return self.shipped_utc is not None


def _plain(report: Any) -> dict[str, Any]:
    if hasattr(report, "model_dump"):
        data = report.model_dump(mode="json")
    elif isinstance(report, Mapping):
        data = copy.deepcopy(dict(report))
    else:
        raise ReportError(f"a gate report is a dict or a GateReport, not {type(report).__name__}")
    missing = [k for k in REQUIRED_FIELDS if not data.get(k)]
    if missing:
        raise ReportError(
            f"gate report is missing {missing}: a report must say which code and thresholds scored it"
        )
    return data


def _dump(data: dict[str, Any]) -> str:
    try:
        return json.dumps(data, indent=1, allow_nan=False) + "\n"
    except (TypeError, ValueError) as e:  # NaN/inf or a non-JSON value: a report must be strict JSON
        raise ReportError(f"gate report is not strict JSON: {e}") from e


def _check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise ReportError(f"run_id {run_id!r} must match [A-Za-z0-9_-]+ (it ends up in a Windows file name)")
    return run_id


class Service:
    def __init__(self, project_dir: str | Path, *, clock: Callable[[], datetime] = utcnow):
        self.project_dir = Path(project_dir)
        self.gates_dir = self.project_dir / GATES_DIR
        self._clock = clock

    # ------------------------------------------------------------------ names

    def _report_path(self, run_id: str, n: int) -> Path:
        return self.gates_dir / f"{run_id}.{n}.json"

    def _shipped_path(self, run_id: str, n: int) -> Path:
        return self.gates_dir / f"{run_id}.{n}{SHIPPED_SUFFIX}"

    def versions(self, run_id: str) -> list[int]:
        """The n of every report written for this run, ascending."""
        _check_run_id(run_id)
        if not self.gates_dir.is_dir():
            return []
        pat = re.compile(rf"^{re.escape(run_id)}\.(\d+)\.json$")
        return sorted(int(m.group(1)) for p in self.gates_dir.iterdir() if (m := pat.match(p.name)))

    # ------------------------------------------------------------------ write

    def _create(self, run_id: str, data: dict[str, Any]) -> Path:
        text = _dump(data)
        self.gates_dir.mkdir(parents=True, exist_ok=True)
        n = max(self.versions(run_id), default=0) + 1
        while True:
            path = self._report_path(run_id, n)
            try:
                fh = open(path, "x", encoding="utf-8", newline="\n")
            except FileExistsError:  # a concurrent writer took n: take the next
                n += 1
                continue
            with fh:
                fh.write(text)
                fh.flush()
            return path

    def write(self, run_id: str, report: Any) -> Path:
        """Store `report` as the next version of the run's gate report and return its path."""
        _check_run_id(run_id)
        return self._create(run_id, _plain(report))

    # ------------------------------------------------------------------ read

    def get(self, run_id: str, n: int) -> StoredReport:
        _check_run_id(run_id)
        path = self._report_path(run_id, n)
        try:
            with open(path, encoding="utf-8") as fh:
                report = json.load(fh)
        except FileNotFoundError:
            raise NoReport(f"no gate report {run_id}.{n}") from None
        except ValueError as e:
            raise ReportError(f"{path.name} is not valid JSON: {e}") from e
        mark = self._read_mark(run_id, n)
        return StoredReport(run_id, n, path, report, mark.get("shipped_utc") if mark else None)

    def latest(self, run_id: str) -> StoredReport | None:
        versions = self.versions(run_id)
        return self.get(run_id, versions[-1]) if versions else None

    def history(self, run_id: str) -> list[StoredReport]:
        return [self.get(run_id, n) for n in self.versions(run_id)]

    # ------------------------------------------------------------------ rescore and ship

    def rescore(self, run_id: str, new_report: Any, *, from_n: int | None = None) -> Path:
        """Write a re-scored report as the next version, recording which one it was scored again from
        (`from_n`, default the latest). NoReport when the run has no report to re-score."""
        _check_run_id(run_id)
        data = _plain(new_report)
        prior = self.get(run_id, from_n) if from_n is not None else self.latest(run_id)
        if prior is None:
            raise NoReport(f"run {run_id} has no gate report to re-score")
        data["rescored_from"] = prior.n
        data.pop("rescored_from_shipped", None)
        if prior.shipped:
            data["rescored_from_shipped"] = True
        return self._create(run_id, data)

    def _read_mark(self, run_id: str, n: int) -> dict[str, Any] | None:
        path = self._shipped_path(run_id, n)
        try:
            with open(path, encoding="utf-8") as fh:
                mark = json.load(fh)
        except FileNotFoundError:
            return None
        except ValueError as e:
            raise ReportError(f"{path.name} is not valid JSON: {e}") from e
        return mark if isinstance(mark, dict) else None

    def shipped(self, run_id: str, n: int | None = None) -> StoredReport:
        """Mark report `n` (default the latest) as shipped and return it. Idempotent: marking again keeps
        the first `shipped_utc`. The report file itself is left exactly as written."""
        _check_run_id(run_id)
        target = self.get(run_id, n) if n is not None else self.latest(run_id)
        if target is None:
            raise NoReport(f"run {run_id} has no gate report to ship")
        if not target.shipped:
            digest = blake3(target.path.read_bytes()).hexdigest()
            mark = {
                "run_id": run_id,
                "n": target.n,
                "report_file": target.path.name,
                "report_blake3": digest,
                "shipped_utc": iso(self._clock()),
            }
            path = self._shipped_path(run_id, target.n)
            if not path.exists():
                write_json_atomic(path, mark)
        return self.get(run_id, target.n)

    def shipped_versions(self, run_id: str) -> list[StoredReport]:
        return [r for r in self.history(run_id) if r.shipped]

    def verify_shipped(self, run_id: str, n: int | None = None) -> bool:
        """True when the shipped report's bytes still hash to what was recorded at ship time."""
        _check_run_id(run_id)
        if n is None:
            marked = self.shipped_versions(run_id)
            if not marked:
                raise NoReport(f"run {run_id} has no shipped report")
            n = marked[-1].n
        mark = self._read_mark(run_id, n)
        if mark is None:
            raise NoReport(f"report {run_id}.{n} was never marked shipped")
        path = self._report_path(run_id, n)
        return path.is_file() and blake3(path.read_bytes()).hexdigest() == mark.get("report_blake3")
