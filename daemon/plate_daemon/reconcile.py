"""Daily reconciliation of the run ledger against vendor billing exports.

The P1 exit is "zero un-ledgered spend, ledger equals billing within 1%". `load_billing` reads one CSV
per provider (`openai.csv`, `google.csv`, `atlas.csv`) and `reconcile(ledger, rows, day)` answers two
questions about one UTC day of billing rows:

* Is there money the ledger never heard of? A row that matches no run is *unledgered spend*.
* Does the price we quoted match the price we were billed? Per engine, summed over matched runs, the
  billed total may differ from the quoted total by at most `tolerance_pct` (1.0).

Billing CSV columns (the same four for every provider; real vendor exports are mapped onto them later by
a `columns.yaml` that fills the `columns=` argument of `load_billing`):

    at,provider_job_id,model,usd
    2026-10-16T11:00:02Z,pred_a1b2c3,bytedance/seedance-2.5-i2v,1.20

`at` is ISO-8601 (`Z`, an offset, or naive meaning UTC); `provider_job_id` may be empty (OpenAI and
Google usage exports carry none); `model` is the vendor's model string; `usd` may carry a `$` and
thousands commas, and is negative for a credit. UTF-8 with or without a BOM.

Matching, one billing row to one run (a run may collect several rows):

1. By `provider_job_id`: any run whose records name that id. The amount is not compared; a price that
   drifted is exactly what the per-engine delta is for.
2. Otherwise by (model, minute bucket, amount within one cent), on runs that reached `submitted` and
   are not matched yet. The model is any of the run's `data.model`, `data.provider_model`, `route_id`
   or `engine_id` (compared with punctuation and case ignored and any `vendor/` prefix dropped), or
   what `model_map` says the billing model means. The minute bucket may be `bucket_slack_min` minutes
   off (default 1) so a boundary second does not split a pair. Candidates are paired nearest in time
   first, so two identical calls pair up one-to-one.

A run that was submitted on the day and has no billing row is listed under `unbilled_runs`. It does not
fail the report: vendors do not bill failed or refused calls, and exports lag.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .ledger import Ledger, RunView, fold_runs, usd_out
from .timeutil import iso, parse_iso

PROVIDER_FILES = ("openai.csv", "google.csv", "atlas.csv")
CANONICAL_COLUMNS = ("at", "provider_job_id", "model", "usd")
RUN_TIME_KINDS = ("submitted", "fetched", "gated", "done", "failed")
CENT = Decimal("0.01")


class BillingFormatError(ValueError):
    """A billing CSV that cannot be read (missing column, bad time or amount)."""


@dataclass(frozen=True)
class BillingRow:
    provider: str
    at: datetime
    provider_job_id: str | None
    model: str
    usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "at": iso(self.at),
            "provider_job_id": self.provider_job_id,
            "model": self.model,
            "usd": self.usd,
        }


def _parse_usd(text: str) -> float:
    cleaned = re.sub(r"[\s$,]", "", text)
    try:
        d = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{text!r} is not an amount") from None
    if not d.is_finite():
        raise ValueError(f"{text!r} is not an amount")
    return float(d)


def _load_file(path: Path, provider: str | None, columns: Mapping[str, str] | None) -> list[BillingRow]:
    provider = (provider or path.stem).lower()
    want = {**{c: c for c in CANONICAL_COLUMNS}, **(columns or {})}
    rows: list[BillingRow] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = {(h or "").strip().lower(): h for h in reader.fieldnames or []}
        missing = [c for c in ("at", "model", "usd") if want[c].strip().lower() not in headers]
        if missing:
            raise BillingFormatError(f"{path.name}: no column for {missing} (have {sorted(headers)})")

        def cell(rec: Mapping[str, str | None], canonical: str) -> str:
            header = headers.get(want[canonical].strip().lower())
            return (rec.get(header) or "").strip() if header is not None else ""

        for rec in reader:
            if not any((v or "").strip() for v in rec.values() if isinstance(v, str)):
                continue
            where = f"{path.name}:{reader.line_num}"
            try:
                at = parse_iso(cell(rec, "at"))
                amount = _parse_usd(cell(rec, "usd"))
            except (ValueError, TypeError) as e:
                raise BillingFormatError(f"{where}: {e}") from e
            if at is None or not cell(rec, "model"):
                raise BillingFormatError(f"{where}: needs at and model")
            rows.append(
                BillingRow(provider, at, cell(rec, "provider_job_id") or None, cell(rec, "model"), amount)
            )
    return rows


def load_billing(
    path: Path | str, *, provider: str | None = None, columns: Mapping[str, str] | None = None
) -> list[BillingRow]:
    """Rows of one billing CSV, or of every `openai.csv` / `google.csv` / `atlas.csv` in a folder.

    `provider` defaults to the file stem. `columns` maps a canonical column name to the header the vendor
    export uses, e.g. {"at": "usage_start_time", "usd": "cost"}."""
    path = Path(path)
    if path.is_dir():
        found = [path / name for name in PROVIDER_FILES if (path / name).is_file()]
        if not found:
            raise FileNotFoundError(f"{path}: none of {', '.join(PROVIDER_FILES)}")
        return [row for f in found for row in _load_file(f, None, columns)]
    return _load_file(path, provider, columns)


# ---------------------------------------------------------------------------- matching


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _billing_keys(model: str, model_map: Mapping[str, str]) -> set[str]:
    keys = {_norm(model), _norm(model.rsplit("/", 1)[-1])}
    for name, engine in model_map.items():
        if name.strip().lower() == model.strip().lower():
            keys.add(_norm(engine))
    keys.discard("")
    return keys


def _run_keys(rv: RunView) -> set[str]:
    keys = {_norm(m) for m in rv.models}
    keys |= {_norm(rv.engine_id or ""), _norm(rv.route_id or "")}
    keys.discard("")
    return keys


def _bucket(t: datetime) -> int:
    return int(t.timestamp() // 60)


def _as_day(day: date | datetime | str) -> date:
    if isinstance(day, datetime):
        return day.astimezone(UTC).date() if day.tzinfo else day.date()
    if isinstance(day, str):
        return date.fromisoformat(day)
    return day


@dataclass
class Report:
    day: str
    by_engine: dict[str, dict[str, Any]]
    matched: list[dict[str, Any]]
    unmatched_billing: list[BillingRow]
    unledgered_spend_usd: float
    credits_usd: float
    unbilled_runs: list[dict[str, Any]]
    tolerance_pct: float
    failures: list[str]
    ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "ok": self.ok,
            "failures": self.failures,
            "tolerance_pct": self.tolerance_pct,
            "by_engine": self.by_engine,
            "matched": self.matched,
            "unmatched_billing": [r.to_dict() for r in self.unmatched_billing],
            "unledgered_spend_usd": self.unledgered_spend_usd,
            "credits_usd": self.credits_usd,
            "unbilled_runs": self.unbilled_runs,
        }


def reconcile(
    ledger: Ledger | list[dict[str, Any]],
    rows: Iterable[BillingRow],
    day: date | datetime | str,
    *,
    model_map: Mapping[str, str] | None = None,
    tolerance_pct: float = 1.0,
    bucket_slack_min: int = 1,
) -> Report:
    """Reconcile the billing `rows` dated `day` (UTC) against the ledger; read-only."""
    the_day = _as_day(day)
    model_map = model_map or {}
    all_rows = list(rows)
    records = ledger.records() if isinstance(ledger, Ledger) else list(ledger)
    runs = fold_runs(records)
    day_rows = [r for r in all_rows if r.at.astimezone(UTC).date() == the_day]

    by_job: dict[str, RunView] = {}
    for rv in runs.values():
        for job_id in rv.provider_job_ids:
            by_job.setdefault(job_id, rv)

    owner: dict[int, tuple[RunView, str]] = {}  # row index -> (run, how)
    for i, row in enumerate(day_rows):
        if row.provider_job_id and row.provider_job_id in by_job:
            owner[i] = (by_job[row.provider_job_id], "provider_job_id")

    claimed = {rv.run_id for rv, _ in owner.values()}
    candidates: list[tuple[float, Decimal, int, str]] = []
    for i, row in enumerate(day_rows):
        if i in owner:
            continue
        row_keys = _billing_keys(row.model, model_map)
        amount = Decimal(str(row.usd))
        for rv in runs.values():
            if not rv.submitted or rv.run_id in claimed or not (row_keys & _run_keys(rv)):
                continue
            if row.provider_job_id and rv.provider_job_ids:
                continue  # both name a job and the ids differ (an equal id was matched above)
            prices = [p for p in (rv.billed_usd, rv.quoted_usd) if p is not None]
            gap = min((abs(amount - p) for p in prices), default=None)
            if gap is None or gap > CENT:
                continue
            gaps_s = [
                abs((row.at - t).total_seconds())
                for k in RUN_TIME_KINDS
                if (t := rv.first_at.get(k)) is not None
                and abs(_bucket(row.at) - _bucket(t)) <= bucket_slack_min
            ]
            if gaps_s:
                candidates.append((min(gaps_s), gap, i, rv.run_id))
    for _, _, i, run_id in sorted(candidates):
        if i not in owner and run_id not in claimed:
            owner[i] = (runs[run_id], "model+minute+amount")
            claimed.add(run_id)

    quoted: dict[str, Decimal] = {}
    billed: dict[str, Decimal] = {}
    rows_n: dict[str, int] = {}
    per_run: dict[str, dict[str, Any]] = {}
    for i, (rv, how) in sorted(owner.items()):
        row = day_rows[i]
        engine = rv.engine_id or "unknown"
        entry = per_run.get(rv.run_id)
        if entry is None:
            q = rv.quoted_usd or Decimal(0)
            quoted[engine] = quoted.get(engine, Decimal(0)) + q
            entry = per_run[rv.run_id] = {
                "run_id": rv.run_id,
                "engine_id": engine,
                "provider": row.provider,
                "provider_job_id": row.provider_job_id or rv.provider_job_id,
                "matched_by": how,
                "quoted_usd": usd_out(q),
                "billed_usd": Decimal(0),
                "rows": 0,
            }
        entry["billed_usd"] += Decimal(str(row.usd))
        entry["rows"] += 1
        billed[engine] = billed.get(engine, Decimal(0)) + Decimal(str(row.usd))
        rows_n[engine] = rows_n.get(engine, 0) + 1
    matched = [{**e, "billed_usd": usd_out(e["billed_usd"])} for e in per_run.values()]

    tol = Decimal(str(tolerance_pct))
    failures: list[str] = []
    by_engine: dict[str, dict[str, Any]] = {}
    for engine in sorted(quoted):
        q, b = quoted[engine], billed[engine]
        delta = (b - q) / q * 100 if q else (Decimal(0) if not b else None)
        by_engine[engine] = {
            "quoted": usd_out(q),
            "billed": usd_out(b),
            "delta_pct": None if delta is None else float(round(delta, 4)),
            "rows": rows_n[engine],
        }
        if delta is None or abs(delta) > tol:
            shown = "undefined" if delta is None else f"{delta:+.2f}%"
            failures.append(
                f"{engine}: billed {usd_out(b):.4f} vs quoted {usd_out(q):.4f} ({shown}, limit {tolerance_pct}%)"
            )

    unmatched = [row for i, row in enumerate(day_rows) if i not in owner]
    unledgered = sum((Decimal(str(r.usd)) for r in unmatched if r.usd > 0), Decimal(0))
    credits = sum((Decimal(str(r.usd)) for r in unmatched if r.usd < 0), Decimal(0))
    if unledgered:
        n = sum(1 for r in unmatched if r.usd > 0)
        failures.append(
            f"unledgered spend USD {usd_out(unledgered):.4f} in {n} billing row(s) with no ledger run"
        )

    every_job_id = {r.provider_job_id for r in all_rows if r.provider_job_id}
    matched_ids = set(per_run)
    unbilled = [
        {
            "run_id": rv.run_id,
            "engine_id": rv.engine_id,
            "provider_job_id": rv.provider_job_id,
            "quoted_usd": usd_out(rv.quoted_usd),
            "submitted_at": iso(rv.first_at["submitted"]),
        }
        for rv in runs.values()
        if rv.submitted
        and "submitted" in rv.first_at
        and rv.first_at["submitted"].astimezone(UTC).date() == the_day
        and rv.run_id not in matched_ids
        and not (set(rv.provider_job_ids) & every_job_id)
    ]
    return Report(
        day=the_day.isoformat(),
        by_engine=by_engine,
        matched=matched,
        unmatched_billing=unmatched,
        unledgered_spend_usd=usd_out(unledgered),
        credits_usd=usd_out(credits),
        unbilled_runs=unbilled,
        tolerance_pct=tolerance_pct,
        failures=failures,
        ok=not failures,
    )


def record_billed(ledger: Ledger, report: Report, *, by: str = "reconcile") -> int:
    """Write the day's bills into the ledger as `billed` records, once: one per matched run (so
    `summarize` can total real money) and one per unmatched billing row, flagged `data.unmatched`.
    Running it again for the same report appends nothing. Returns the number of records appended."""
    have: set[tuple[str, str]] = set()
    for rec in ledger.records():
        data = rec.get("data")
        if rec.get("kind") == "billed" and isinstance(data, dict) and data.get("day") == report.day:
            have.add((str(rec.get("run_id") or ""), str(data.get("row_key") or "")))
    n = 0
    for m in report.matched:
        if (m["run_id"], "") in have:
            continue
        ledger.append(
            kind="billed",
            run_id=m["run_id"],
            engine_id=m["engine_id"],
            provider_job_id=m["provider_job_id"],
            cost_usd_quoted=m["quoted_usd"],
            cost_usd_billed=m["billed_usd"],
            by=by,
            data={
                "day": report.day,
                "provider": m["provider"],
                "rows": m["rows"],
                "matched_by": m["matched_by"],
            },
        )
        n += 1
    for row in report.unmatched_billing:
        key = f"{row.provider}|{iso(row.at)}|{row.provider_job_id or ''}|{row.model}|{row.usd!r}"
        if ("", key) in have:
            continue
        ledger.append(
            kind="billed",
            provider_job_id=row.provider_job_id,
            cost_usd_billed=row.usd,
            by=by,
            reason="billing row matches no ledger run",
            data={"day": report.day, "unmatched": True, "row_key": key, **row.to_dict()},
        )
        n += 1
    return n
