"""Budget alerts scanned from a project's ledger: the unattended cap and the daily cap.

`budgets.authorize` refuses an approval that would pass a cap and puts a live alert on the SSE bus, but
nothing durable remembers it, a human approval is never checked against the caps at all, and a bus event is
gone when nobody was watching. This module reads `runs.jsonl` after the fact (nightly, or on demand) and
writes what it finds to `<project>/alerts.jsonl`, then gives the UI status bar one line.

What it reads. A run's spend is the price on its `approved` record, counted as zero when the run was
refunded (it failed, was cancelled or expired before it reached `submitted`, or the record says
`data.refunded`). A human is present from any record of kind `human_minutes` or `override`, or any
`approved`, `done` or `export` record whose `by` names a person (not `auto`, `agent...` or a system actor).
The ledger cannot see someone who only browses, so the scan can be stricter than the live check, never
looser.

Alerts, each written once per key (re-running the scan never repeats one):

    daily_cap_near            warn      a UTC day's approved spend reached 80% of `per_user_daily_cap_usd`
    daily_cap_exceeded        critical  it passed the cap (a human approval can: `authorize` only gates auto/agent)
    unattended_spend          warn      auto or agent approvals were made with no human present
    unattended_cap_exceeded   critical  auto + agent spend since the last human passed `unattended_cap_usd`
                                        while one of those approvals was unattended

The file uses the same line shape as the engine alerts of `plate_engines.health.AlertLog` (`v, at, kind,
severity, engine_id, route_id, message, data`), ASCII with `\\n` line ends and one write per line, so a
single reader handles both; the kinds here are different, and that reader skips lines it does not know.
`data.key` is the dedupe key and `data.project_id` names the project.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..budgets import Policy
from ..ledger import Ledger
from ..projects import Project
from ..timeutil import iso, parse_iso, utcnow

ALERTS_JSONL = "alerts.jsonl"
NEAR_FRACTION = Decimal("0.8")
HUMAN_KINDS = frozenset({"human_minutes", "override"})
HUMAN_BY_KINDS = frozenset({"approved", "done", "export"})
NON_HUMAN_ACTORS = frozenset({"auto", "system", "reconcile", "worker", "scheduler", "api", "budget"})
REFUND_KINDS = frozenset({"failed", "cancelled", "expired"})
KINDS = ("daily_cap_near", "daily_cap_exceeded", "unattended_spend", "unattended_cap_exceeded")
SEVERITIES = {
    "daily_cap_near": "warn",
    "daily_cap_exceeded": "critical",
    "unattended_spend": "warn",
    "unattended_cap_exceeded": "critical",
}

__all__ = [
    "ALERTS_JSONL",
    "BudgetAlert",
    "Scan",
    "Summary",
    "read_alerts",
    "run",
    "scan",
    "status",
    "summary_line",
]


@dataclass(frozen=True)
class BudgetAlert:
    kind: str
    key: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        return SEVERITIES[self.kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "key": self.key,
            "message": self.message,
            "data": dict(self.data),
        }

    def to_line(self, at: str, project_id: str | None) -> dict[str, Any]:
        return {
            "v": 1,
            "at": at,
            "kind": self.kind,
            "severity": self.severity,
            "engine_id": None,
            "route_id": None,
            "message": self.message,
            "data": {"key": self.key, "project_id": project_id, **self.data},
        }


@dataclass(frozen=True)
class Summary:
    """What the UI status bar shows: `line`, coloured by `level` (ok, warn, critical)."""

    level: str
    line: str
    today_usd: float
    daily_cap_usd: float
    unattended_usd: float
    unattended_cap_usd: float
    unattended_now: bool
    active_alerts: int


@dataclass
class Scan:
    alerts: list[BudgetAlert]
    summary: Summary
    written: int = 0
    path: Path | None = None


# ---------------------------------------------------------------------------- reading the ledger


def _money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _at(rec: dict[str, Any]) -> datetime | None:
    v = rec.get("at")
    if not isinstance(v, str):
        return None
    try:
        t = parse_iso(v)
    except ValueError:
        return None
    return t.astimezone(UTC) if t is not None else None


def _data(rec: dict[str, Any]) -> dict[str, Any]:
    d = rec.get("data")
    return d if isinstance(d, dict) else {}


def _is_agent(by: str) -> bool:
    return by == "agent" or by.startswith("agent:")


def _is_human(rec: dict[str, Any]) -> bool:
    kind = rec.get("kind")
    if kind in HUMAN_KINDS:
        return True
    by = rec.get("by")
    if kind in HUMAN_BY_KINDS and isinstance(by, str) and by:
        return by not in NON_HUMAN_ACTORS and not _is_agent(by)
    return False


def _net_prices(events: list[tuple[datetime, dict[str, Any]]]) -> dict[str, Decimal]:
    """Run id -> approved price, zero for a run whose reservation was refunded."""
    price: dict[str, Decimal] = {}
    submitted: set[str] = set()
    refunded: set[str] = set()
    for _t, rec in events:
        rid, kind = rec.get("run_id"), rec.get("kind")
        if not isinstance(rid, str) or not rid:
            continue
        if kind == "approved":
            q = _money(rec.get("cost_usd_quoted"))
            if q is not None:
                price[rid] = q
        elif kind == "submitted":
            submitted.add(rid)
        elif kind in REFUND_KINDS and (_data(rec).get("refunded") is True or rid not in submitted):
            refunded.add(rid)
    return {rid: (Decimal(0) if rid in refunded else p) for rid, p in price.items()}


def _usd(d: Decimal) -> float:
    return float(round(d, 4))


# ---------------------------------------------------------------------------- the scan


def scan(
    records: Ledger | Iterable[dict[str, Any]],
    policy: Policy | None = None,
    *,
    now: datetime | None = None,
    project_id: str | None = None,
) -> Scan:
    """Alerts and the status-bar summary for a ledger (a `Ledger` or its records). Writes nothing."""
    policy = policy or Policy()
    now = (now or utcnow()).astimezone(UTC)
    recs = records.records() if isinstance(records, Ledger) else list(records)
    events = sorted(
        ((t, rec) for rec in recs if (t := _at(rec)) is not None), key=lambda e: e[0]
    )  # stable: file order breaks ties
    prices = _net_prices(events)
    daily_cap = Decimal(str(policy.per_user_daily_cap_usd))
    unattended_cap = Decimal(str(policy.unattended_cap_usd))
    idle = timedelta(minutes=policy.unattended_after_min)

    alerts: list[BudgetAlert] = []
    day_total: dict[str, Decimal] = {}
    day_flags: dict[str, set[str]] = {}
    sessions: dict[str, dict[str, Any]] = {}
    seen_runs: set[str] = set()
    last_human: datetime | None = None

    def session_key() -> str:
        return iso(last_human) if last_human is not None else "never"

    for t, rec in events:
        if _is_human(rec):
            last_human = t if last_human is None or t > last_human else last_human
        rid = rec.get("run_id")
        if rec.get("kind") != "approved" or not isinstance(rid, str) or rid in seen_runs or rid not in prices:
            continue
        seen_runs.add(rid)
        price = prices[rid]
        by = rec.get("by") if isinstance(rec.get("by"), str) else ""

        day = t.date().isoformat()
        total = day_total[day] = day_total.get(day, Decimal(0)) + price
        flags = day_flags.setdefault(day, set())
        if total > daily_cap and "daily_cap_exceeded" not in flags:
            flags.add("daily_cap_exceeded")
            alerts.append(
                BudgetAlert(
                    "daily_cap_exceeded",
                    f"daily_cap_exceeded:{day}",
                    f"{day}: approved spend USD {_usd(total):.2f} passed the daily cap USD {_usd(daily_cap):.2f}",
                    {
                        "day": day,
                        "spent_usd": _usd(total),
                        "cap_usd": _usd(daily_cap),
                        "occurred_utc": iso(t),
                    },
                )
            )
        elif (
            total > 0
            and total >= daily_cap * NEAR_FRACTION
            and not flags & {"daily_cap_near", "daily_cap_exceeded"}
        ):
            flags.add("daily_cap_near")
            alerts.append(
                BudgetAlert(
                    "daily_cap_near",
                    f"daily_cap_near:{day}",
                    f"{day}: approved spend USD {_usd(total):.2f} reached 80% of the daily cap USD {_usd(daily_cap):.2f}",
                    {
                        "day": day,
                        "spent_usd": _usd(total),
                        "cap_usd": _usd(daily_cap),
                        "occurred_utc": iso(t),
                    },
                )
            )

        if by == "auto" or _is_agent(by):
            key = session_key()
            s = sessions.setdefault(
                key,
                {"spend": Decimal(0), "unattended": Decimal(0), "n": 0, "first": None, "exceeded_at": None},
            )
            s["spend"] += price
            if last_human is None or t - last_human >= idle:
                s["unattended"] += price
                s["n"] += 1
                s["first"] = s["first"] or t
                if s["spend"] > unattended_cap and s["exceeded_at"] is None:
                    s["exceeded_at"] = t

    for key, s in sessions.items():
        if not s["n"]:
            continue
        since = "no human has interacted yet" if key == "never" else f"since the human interaction at {key}"
        data = {
            "session": key,
            "approvals": s["n"],
            "unattended_usd": _usd(s["unattended"]),
            "since_human_usd": _usd(s["spend"]),
            "cap_usd": _usd(unattended_cap),
            "occurred_utc": iso(s["first"]),
        }
        alerts.append(
            BudgetAlert(
                "unattended_spend",
                f"unattended_spend:{key}",
                f"{s['n']} auto/agent approval(s) with no human present ({since}): "
                f"USD {_usd(s['spend']):.2f} of the USD {_usd(unattended_cap):.2f} unattended cap",
                data,
            )
        )
        if s["exceeded_at"] is not None:
            alerts.append(
                BudgetAlert(
                    "unattended_cap_exceeded",
                    f"unattended_cap_exceeded:{key}",
                    f"unattended spend USD {_usd(s['spend']):.2f} passed the unattended cap "
                    f"USD {_usd(unattended_cap):.2f} ({since})",
                    {**data, "occurred_utc": iso(s["exceeded_at"])},
                )
            )

    today = now.date().isoformat()
    today_usd = day_total.get(today, Decimal(0))
    current = sessions.get(session_key())
    unattended_usd = current["spend"] if current else Decimal(0)
    unattended_now = last_human is None or now - last_human >= idle
    active = sum(
        1
        for a in alerts
        if a.data.get("day") == today
        or (a.data.get("session") == session_key() and a.kind.startswith("unatt"))
    )
    level = "ok"
    near_daily = today_usd > 0 and today_usd >= daily_cap * NEAR_FRACTION
    near_unattended = unattended_usd > 0 and unattended_usd >= unattended_cap * NEAR_FRACTION
    if near_daily or near_unattended:
        level = "warn"
    if today_usd > daily_cap or unattended_usd > unattended_cap:
        level = "critical"
    line = (
        f"Budget: USD {_usd(today_usd):.2f} of {_usd(daily_cap):.2f} today; "
        f"USD {_usd(unattended_usd):.2f} of {_usd(unattended_cap):.2f} unattended"
        + (" (no human present)" if unattended_now else "")
        + (f"; {active} active alert(s)" if active else "")
    )
    summary = Summary(
        level=level,
        line=line,
        today_usd=_usd(today_usd),
        daily_cap_usd=_usd(daily_cap),
        unattended_usd=_usd(unattended_usd),
        unattended_cap_usd=_usd(unattended_cap),
        unattended_now=unattended_now,
        active_alerts=active,
    )
    return Scan(alerts=alerts, summary=summary)


# ---------------------------------------------------------------------------- alerts.jsonl


def read_alerts(path: Path | str) -> list[dict[str, Any]]:
    """Every alert line of an alerts file; a line that does not parse (a torn last write) is skipped."""
    path = Path(path)
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _existing_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for a in read_alerts(path):
        data = a.get("data")
        if isinstance(data, dict) and isinstance(data.get("key"), str):
            keys.add(data["key"])
    return keys


def _append_lines(path: Path, lines: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        for obj in lines:
            text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            fh.write(text.encode("ascii") + b"\n")
        fh.flush()
        os.fsync(fh.fileno())


def run(
    project_dir: Path | str,
    policy: Policy | None = None,
    *,
    now: datetime | None = None,
    alerts_path: Path | str | None = None,
) -> Scan:
    """Scan the project's ledger and append the alerts not already in `alerts.jsonl` (default: in the
    project folder). Returns the scan with `written` set to the number of new lines."""
    root = Path(project_dir)
    now = (now or utcnow()).astimezone(UTC)
    path = Path(alerts_path) if alerts_path is not None else root / ALERTS_JSONL
    project_id = _project_id(root)
    result = scan(Ledger(root), policy, now=now, project_id=project_id)
    have = _existing_keys(path)
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = [a.to_line(at, project_id) for a in result.alerts if a.key not in have]
    if fresh:
        _append_lines(path, fresh)
    result.written = len(fresh)
    result.path = path
    return result


def status(project_dir: Path | str, policy: Policy | None = None, *, now: datetime | None = None) -> Summary:
    """The status-bar summary for a project right now (reads the ledger, writes nothing)."""
    root = Path(project_dir)
    return scan(Ledger(root), policy, now=now, project_id=_project_id(root)).summary


def summary_line(
    project_dir: Path | str, policy: Policy | None = None, *, now: datetime | None = None
) -> str:
    """Just the line: `Budget: USD 12.40 of 50.00 today; USD 3.00 of 20.00 unattended (no human present)`."""
    return status(project_dir, policy, now=now).line


def _project_id(root: Path) -> str | None:
    try:
        return Project.load(root).project_id
    except (OSError, ValueError):  # not a project folder yet: the alerts simply carry no project id
        return None
