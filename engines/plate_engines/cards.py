"""Engine cards: pass rate per engine, task and shot class, with Wilson 95% lower bounds.

New for the PLATE monorepo (router plan T27; BUILD_PLAN section 4 "Routing"). A card is one cell of the table
(engine_id, task, shot_class). It accumulates from the run ledger and the gate reports, and it starts steering
routing only when the cell holds `n >= CARD_MIN_N` (20) scored runs: below that the router ignores it, because a
pass rate from five runs is an anecdote. The router ranks steering cells by `wilson_lower_95`, the pessimistic
end of the pass rate, so an engine that went 3 for 3 does not beat one that went 180 for 200.

    wilson_lower(passes, n, z=1.96) = (p + z^2/2n - z*sqrt(p(1-p)/n + z^2/4n^2)) / (1 + z^2/n),  p = passes/n

(n = 20, passes = 18 gives 0.6989.) n = 0 gives 0.0.

Where a card's numbers come from, per run_id in the ledger (records are plain dicts, as `runs.jsonl` holds them;
nothing here imports the daemon):

- engine_id: the last record that names one. task and shot_class: `data.task` and `data.shot_class` on any record
  of the run (last wins). A run with no engine_id or no task belongs to no cell and is skipped.
- verdict: the run's last `gate_report`. A dict is read directly; a string is looked up in `gate_reports`
  (a mapping of ledger gate_report ref or run_id to a report: a dict with a `verdict`, or any object with a
  `verdict` attribute such as plate_gates' GateReport). A run with no verdict (cancelled, failed before output,
  still running) is not counted. Verdict "pass" is a pass and "fail" a fail. "advisory" (only advisory or
  calibration_needed checks missed, or nothing was scored) is excluded from n by default; `advisory=` can count
  it as "pass" or "fail" instead.
- cost: the vendor-billed figure when the ledger has one (sum of `billed` records, else the last
  `cost_usd_billed`), else the approved quote, else the quote. mean_cost is the mean over counted runs that have
  a cost, None when none do.
- custody: the worst effective grade among the counted runs (D is worse than C2 is worse than C1 than C0).
- determinism_var: runs may carry `data.repeat_key`, the same value for identical inputs, params and seed. For
  each key with two or more counted runs take the population variance of the pass indicator (0 = every repeat
  agreed, 0.25 = a coin flip), then average over the keys. None when no key repeats. A seeded engine should sit
  at 0; a high value means the verdict depends on luck rather than on the setting.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .router import CARD_MIN_N, GRADE_ORDER

Z95 = 1.96
AdvisoryRule = Literal["exclude", "pass", "fail"]
CardKey = tuple[str, str, str | None]

__all__ = ["CARD_MIN_N", "Card", "Z95", "from_ledger", "index_cards", "wilson_lower"]


def wilson_lower(passes: int, n: int, z: float = Z95) -> float:
    """Lower end of the Wilson score interval for `passes` successes in `n` trials (0.0 when n is 0)."""
    if n <= 0:
        return 0.0
    if not 0 <= passes <= n:
        raise ValueError(f"passes must be between 0 and n, got passes={passes}, n={n}")
    p = passes / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - margin) / (1 + z2 / n))


@dataclass(frozen=True)
class Card:
    engine_id: str
    task: str
    shot_class: str | None
    n: int
    passes: int
    pass_rate: float
    wilson_lower_95: float
    determinism_var: float | None
    mean_cost: float | None
    custody: str

    @property
    def key(self) -> CardKey:
        return (self.engine_id, self.task, self.shot_class)

    def steering(self, min_n: int = CARD_MIN_N) -> bool:
        """True once the cell holds enough runs for the router to rank on it."""
        return self.n >= min_n

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Card:
        return cls(**{k: raw[k] for k in cls.__dataclass_fields__})

    @classmethod
    def make(
        cls,
        engine_id: str,
        task: str,
        shot_class: str | None,
        passes: int,
        n: int,
        *,
        determinism_var: float | None = None,
        mean_cost: float | None = None,
        custody: str = "D",
    ) -> Card:
        return cls(
            engine_id=engine_id,
            task=task,
            shot_class=shot_class,
            n=n,
            passes=passes,
            pass_rate=(passes / n) if n else 0.0,
            wilson_lower_95=wilson_lower(passes, n),
            determinism_var=determinism_var,
            mean_cost=mean_cost,
            custody=custody,
        )


def index_cards(cards: Iterable[Card] | Mapping[Any, Card] | None) -> dict[CardKey, Card]:
    """{(engine_id, task, shot_class): Card}; a later card for the same cell replaces an earlier one."""
    if cards is None:
        return {}
    values = cards.values() if isinstance(cards, Mapping) else cards
    return {c.key: c for c in values}


# ---------------------------------------------------------------------------- building cards from the ledger


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


@dataclass
class _Run:
    engine_id: str | None = None
    task: str | None = None
    shot_class: str | None = None
    custody: str | None = None
    repeat_key: str | None = None
    gate_ref: Any = None
    quoted: dict[str, float] | None = None
    billed_sum: float | None = None
    billed_other: float | None = None

    def cost(self) -> float | None:
        if self.billed_sum is not None:
            return self.billed_sum
        if self.billed_other is not None:
            return self.billed_other
        for kind in ("approved", "quoted"):
            if self.quoted and kind in self.quoted:
                return self.quoted[kind]
        return next(reversed(self.quoted.values()), None) if self.quoted else None


def _fold(records: Iterable[Mapping[str, Any]]) -> dict[str, _Run]:
    runs: dict[str, _Run] = {}
    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        run_id = _str(rec.get("run_id"))
        if run_id is None:
            continue
        run = runs.setdefault(run_id, _Run())
        kind = _str(rec.get("kind")) or ""
        run.engine_id = _str(rec.get("engine_id")) or run.engine_id
        run.custody = _str(rec.get("custody_grade")) or run.custody
        data = rec.get("data") if isinstance(rec.get("data"), Mapping) else {}
        run.task = _str(data.get("task")) or _str(rec.get("task")) or run.task
        run.shot_class = _str(data.get("shot_class")) or _str(rec.get("shot_class")) or run.shot_class
        run.repeat_key = _str(data.get("repeat_key")) or run.repeat_key
        if rec.get("gate_report") is not None:
            run.gate_ref = rec["gate_report"]
        quoted = _num(rec.get("cost_usd_quoted"))
        if quoted is not None:
            run.quoted = run.quoted or {}
            run.quoted.pop(kind, None)
            run.quoted[kind] = quoted
        billed = _num(rec.get("cost_usd_billed"))
        if billed is not None:
            if kind == "billed":
                run.billed_sum = (run.billed_sum or 0.0) + billed
            else:
                run.billed_other = billed
    return runs


def _verdict(report: Any) -> str | None:
    if report is None:
        return None
    if isinstance(report, Mapping):
        return _str(report.get("verdict"))
    return _str(getattr(report, "verdict", None))


def _run_verdict(run: _Run, run_id: str, gate_reports: Mapping[str, Any]) -> str | None:
    ref = run.gate_ref
    if isinstance(ref, Mapping):
        return _verdict(ref)
    if isinstance(ref, str):
        found = gate_reports.get(ref)
        if found is not None:
            return _verdict(found)
    return _verdict(gate_reports.get(run_id))


def from_ledger(
    ledger_records: Iterable[Mapping[str, Any]],
    gate_reports: Mapping[str, Any] | None = None,
    *,
    advisory: AdvisoryRule = "exclude",
) -> list[Card]:
    """Fold ledger records and gate reports into cards, sorted by (engine_id, task, shot_class)."""
    reports = gate_reports or {}
    cells: dict[CardKey, list[tuple[bool, float | None, str | None, str | None]]] = {}
    for run_id, run in _fold(ledger_records).items():
        if run.engine_id is None or run.task is None:
            continue
        verdict = _run_verdict(run, run_id, reports)
        if verdict == "advisory":
            if advisory == "exclude":
                continue
            verdict = advisory
        if verdict not in ("pass", "fail"):
            continue
        cells.setdefault((run.engine_id, run.task, run.shot_class), []).append(
            (verdict == "pass", run.cost(), run.custody, run.repeat_key)
        )

    out: list[Card] = []
    for (engine_id, task, shot_class), rows in sorted(cells.items(), key=lambda kv: tuple(map(str, kv[0]))):
        n = len(rows)
        passes = sum(1 for ok, *_ in rows if ok)
        costs = [c for _, c, _, _ in rows if c is not None]
        out.append(
            Card.make(
                engine_id,
                task,
                shot_class,
                passes,
                n,
                determinism_var=_determinism_var(rows),
                mean_cost=round(sum(costs) / len(costs), 6) if costs else None,
                custody=_worst_grade(g for _, _, g, _ in rows),
            )
        )
    return out


def _determinism_var(rows: list[tuple[bool, float | None, str | None, str | None]]) -> float | None:
    groups: dict[str, list[float]] = {}
    for ok, _, _, key in rows:
        if key is not None:
            groups.setdefault(key, []).append(1.0 if ok else 0.0)
    variances = []
    for outcomes in groups.values():
        if len(outcomes) >= 2:
            mean = sum(outcomes) / len(outcomes)
            variances.append(sum((x - mean) ** 2 for x in outcomes) / len(outcomes))
    return round(sum(variances) / len(variances), 6) if variances else None


def _worst_grade(grades: Iterable[str | None]) -> str:
    """Highest index in GRADE_ORDER seen; a run with no recorded grade counts as D (unknown is the worst case)."""
    worst = 0
    for g in grades:
        worst = max(worst, GRADE_ORDER.index(g) if g in GRADE_ORDER else GRADE_ORDER.index("D"))
    return GRADE_ORDER[worst]
