"""Spend routes (router plan T88): what a project, or every project, has cost and what it bought.

- `GET /projects/{id}/spend[?since=<ISO>&by=engine|day|week]` answers `Ledger.summarize` (`spent_usd_quoted`,
  `spent_usd_billed`, `generations`, `accepted`, `rerolled`, `by_engine`, `by_custody`) plus `series`.
- `GET /spend?all_projects=1[&since=&by=]` is the same over every project `Pipeline.project_list` knows; the
  flag is required, so a client cannot ask for the whole studio's money by accident.

`series` is folded from the ledger records with the same rules `summarize` uses (a run counts once it was
submitted, billed or decided; its quoted price counts only when it was submitted): one row per (bucket,
engine), sorted by bucket then engine. `by=engine` buckets by the engine id; `by=day` by the UTC day
(`2026-10-18`) and `by=week` by the ISO week (`2026-W42`) of the run's first submission (the time of its
first record when it never reached `submitted`; `unknown` when no record carries a time). A row is
`{bucket, engine_id, runs, quoted_usd, billed_usd, accepted, cost_per_accepted}`, where `cost_per_accepted`
is the billed figure (the quoted one while nothing is billed yet) divided by the accepted count, null when
nothing was accepted. A vendor bill that names no run is still money: it lands in its day or week under its
own engine (`unattributed` when the line names none), so the rows add up to the totals.

Everything is a read of `runs.jsonl`; nothing is written.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..ledger import DECISIONS, Ledger, RunView, fold_runs, summarize_records, usd_out
from ..timeutil import parse_iso
from . import API, ApiError, Ctx, ctx, require_token
from .tierc import project_or_404

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

BY = ("engine", "day", "week")
UNKNOWN = "unknown"
TRUE = frozenset({"1", "true", "yes", "on"})


def _by(raw: str | None) -> str:
    by = (raw or "engine").strip().lower()
    if by not in BY:
        raise ApiError(422, {"errors": [f"by must be one of {list(BY)}, got {raw!r}"]})
    return by


def _since(raw: str | None) -> datetime | None:
    if raw is None or not raw.strip():
        return None
    try:
        parsed = parse_iso(raw.strip())
    except ValueError as e:
        raise ApiError(422, {"errors": [f"since must be an ISO date or time, got {raw!r}"]}) from e
    return parsed


def _at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


def _money(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return Decimal(0)
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)
    return d if d.is_finite() else Decimal(0)


def _bucket(by: str, engine_id: str, at: datetime | None) -> str:
    if by == "engine":
        return engine_id
    if at is None:
        return UNKNOWN
    day = at.astimezone(UTC).date()
    if by == "day":
        return day.isoformat()
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _run_time(rv: RunView) -> datetime | None:
    return rv.first_at.get("submitted") or min(rv.first_at.values(), default=None)


def _spent(rv: RunView) -> bool:
    """Did this run cost or decide anything? (`summarize_records` skips the rest.)"""
    return bool(rv.submitted or rv.billed_usd or rv.decision in DECISIONS)


def build_series(records: list[dict[str, Any]], by: str) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}

    def row(bucket: str, engine_id: str) -> dict[str, Any]:
        return rows.setdefault(
            (bucket, engine_id),
            {"runs": 0, "quoted": Decimal(0), "billed": Decimal(0), "accepted": 0},
        )

    for rv in fold_runs(records).values():
        if not _spent(rv):
            continue
        engine_id = rv.engine_id or UNKNOWN
        r = row(_bucket(by, engine_id, _run_time(rv)), engine_id)
        r["runs"] += 1
        r["billed"] += rv.billed_usd or Decimal(0)
        if rv.submitted:
            r["quoted"] += rv.quoted_usd or Decimal(0)
        if rv.decision == "accepted":
            r["accepted"] += 1
    for rec in records:  # bills that name no run
        if isinstance(rec.get("run_id"), str) and rec.get("run_id"):
            continue
        if rec.get("kind") != "billed":
            continue
        eng = rec.get("engine_id")
        engine_id = eng if isinstance(eng, str) and eng else "unattributed"
        row(_bucket(by, engine_id, _at(rec.get("at"))), engine_id)["billed"] += _money(
            rec.get("cost_usd_billed")
        )
    series = []
    for (bucket, engine_id), r in sorted(rows.items()):
        spend = r["billed"] if r["billed"] > 0 else r["quoted"]
        series.append(
            {
                "bucket": bucket,
                "engine_id": engine_id,
                "runs": r["runs"],
                "quoted_usd": usd_out(r["quoted"]),
                "billed_usd": usd_out(r["billed"]),
                "accepted": r["accepted"],
                "cost_per_accepted": usd_out(spend / r["accepted"]) if r["accepted"] else None,
            }
        )
    return series


def _filter_since(records: Iterable[dict[str, Any]], since: datetime | None) -> list[dict[str, Any]]:
    """The records with `at >= since`, as `summarize_records` filters them."""
    if since is None:
        return list(records)
    return [r for r in records if (t := _at(r.get("at"))) is not None and t >= since]


def spend_report(records: list[dict[str, Any]], by: str, since: datetime | None) -> dict[str, Any]:
    """The summary totals plus the series, over `records` (already read from one or more ledgers)."""
    records = _filter_since(records, since)
    return {
        "by": by,
        "since": since.isoformat() if since else None,
        **summarize_records(records),
        "series": build_series(records, by),
    }


def _records(c: Ctx, root: Path) -> list[dict[str, Any]]:
    return Ledger(root, clock=c.now).records()


@router.get("/projects/{project_id}/spend")
def project_spend(
    request: Request, project_id: str, since: str | None = None, by: str | None = None
) -> dict[str, Any]:
    c = ctx(request)
    window, bucket_by = _since(since), _by(by)
    project = project_or_404(c, project_id)
    return {"project_id": project_id, **spend_report(_records(c, project.root), bucket_by, window)}


@router.get("/spend")
def all_spend(
    request: Request, all_projects: str | None = None, since: str | None = None, by: str | None = None
) -> dict[str, Any]:
    if (all_projects or "").strip().lower() not in TRUE:
        raise ApiError(
            422,
            {
                "errors": ["pass all_projects=1 for the studio-wide figures, or use /projects/{id}/spend"],
            },
        )
    c = ctx(request)
    window, bucket_by = _since(since), _by(by)
    records: list[dict[str, Any]] = []
    ids: list[str] = []
    for head in c.pipeline.project_list():
        pid = head["project_id"]
        ids.append(pid)
        for rec in _records(c, Path(head["root"])):
            rid = rec.get("run_id")
            # A run id is unique within a project; namespace it so two projects can never fold into one run.
            records.append({**rec, "run_id": f"{pid}/{rid}"} if isinstance(rid, str) and rid else rec)
    return {"scope": "all_projects", "projects": ids, **spend_report(records, bucket_by, window)}
