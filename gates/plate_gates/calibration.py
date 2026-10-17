"""Calibration of the gates against human verdicts: the statistics behind THE BET (BUILD_PLAN P1 exit).

The labelling tool (tools/plate_label) appends one line per Ship / Fix / Render-it verdict to
calib/labels.jsonl. This module turns those labels plus the gate reports that scored the same takes into
the numbers the bet is decided on:

    labels = load_labels("calib/labels.jsonl")
    summary = report(labels, reports_dir="<project>/gates", out_md=None)   # writes calib/report_<date>.md
    summary["bet_passed"]  # n >= 100 and n_rejects >= 60 and AUC >= 0.85 and false_pass <= 5%

Conventions, fixed here so every number in a report means one thing:

- A take is a "reject" when its verdict is fix or render_it, a "ship" otherwise.
- Scores are goodness: higher means the gates like the candidate more. Labels are 1 = ship (the positive
  class), 0 = reject. A candidate is ACCEPTED by the gates when score >= threshold.
- tpr = share of ships the gates accept; fpr = share of rejects the gates accept. A false pass is a reject
  the gates accept, so false_pass = fpr = false passes / rejects (60 rejects with none passing bounds the
  rate under 5% by the rule of three, which is why the bet asks for 60 rejects).
- ensemble_score is a weighted mean of per-gate goodness in [0, 1]: 1 when a metric meets its threshold,
  falling linearly to 0 at one `scale` beyond the bound (scale defaults to |bound|, the range width, or 1
  for an equals-zero bound). Weights come from thresholds.yaml `ensemble:` (metric id -> weight, or
  {weight, scale}); until that block exists DEFAULT_ENSEMBLE is used. Metrics a report lacks are left out
  and the mean re-normalised over the rest.

    python -m plate_gates.calibration report --labels calib/labels.jsonl --reports <dir>
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from . import thresholds

__all__ = [
    "BET",
    "DEFAULT_ENSEMBLE",
    "EnsembleEntry",
    "auc",
    "dedupe_latest",
    "evaluate_threshold",
    "ensemble_score",
    "find_gate_report",
    "fit_threshold",
    "gate_values",
    "goodness",
    "inter_rater",
    "is_reject",
    "load_ensemble",
    "load_labels",
    "report",
]

VERDICTS = ("ship", "fix", "render_it")
REJECT_VERDICTS = ("fix", "render_it")

# The bet's pass conditions (BUILD_PLAN P1 exit): all four must hold.
BET = {"min_n": 100, "min_rejects": 60, "min_auc": 0.85, "max_false_pass": 0.05}

# First-guess weights until labelled data fit them: the gates that catch invention and misregistration count
# most, the advisory tone / agreement triage counts least. Every id must exist in thresholds.yaml.
DEFAULT_ENSEMBLE: dict[str, float] = {
    "registration.residual_px": 2.0,
    "registration.shift_px": 1.0,
    "outside_mask.de00": 1.0,
    "feather_band.de00_p99": 1.0,
    "gbuffer_edge_recall_r2": 3.0,
    "invention_flags": 3.0,
    "material.de00_median": 2.0,
    "material.de00_p90": 1.0,
    "ral.de00": 1.0,
    "texture.lapvar": 2.0,
    "texture.fine_ratio": 1.0,
    "tone.luma": 0.5,
    "tone.contrast": 0.5,
    "tone.sat": 0.5,
    "video.first_frame": 1.0,
    "video.last_frame": 1.0,
    "video.camera_lock_median_flow_px": 2.0,
    "video.phase_corr_px": 0.5,
    "video.reproj_edge_recall": 2.0,
    "image_edge_agreement": 0.5,
}

_THRESHOLDS_YAML = Path(thresholds.__file__).with_name("thresholds.yaml")
_REPO_CALIB = Path(__file__).resolve().parents[2] / "calib"
DEFAULT_LABELS = _REPO_CALIB / "labels.jsonl"
_SAFE_ID = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"


# --- labels ---------------------------------------------------------------------------------


def is_reject(verdict: str) -> bool:
    return verdict in REJECT_VERDICTS


def _valid_label(rec: object) -> str | None:
    """None when `rec` is a usable label, else the reason it is not."""
    if not isinstance(rec, dict):
        return "not an object"
    if rec.get("v") != 1:
        return f"unsupported schema version {rec.get('v')!r}"
    for key in ("rater", "take_id", "candidate"):
        if not isinstance(rec.get(key), str) or not rec[key]:
            return f"missing {key}"
    if rec.get("verdict") not in VERDICTS:
        return f"bad verdict {rec.get('verdict')!r}"
    reasons = rec.get("reasons", [])
    if not isinstance(reasons, list) or not all(isinstance(c, str) for c in reasons):
        return "reasons must be a list of codes"
    return None


def load_labels(path: str | Path) -> list[dict]:
    """Read calib/labels.jsonl (UTF-8, one JSON object per line) in file order.

    Blank lines are ignored. A line that is not JSON or not a v1 label (a crash mid-write, a hand edit)
    is skipped with a warning naming the line, never silently. A missing file raises FileNotFoundError.
    """
    out: list[dict] = []
    with open(path, encoding="utf-8", newline="") as f:
        for n, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                warnings.warn(f"{path}:{n}: skipped a line that is not JSON", stacklevel=2)
                continue
            why = _valid_label(rec)
            if why:
                warnings.warn(f"{path}:{n}: skipped a label ({why})", stacklevel=2)
                continue
            out.append(rec)
    return out


def dedupe_latest(labels: Iterable[dict]) -> list[dict]:
    """One label per (rater, take_id, plate, candidate): the last one in file order wins (a relabel or an
    undo-and-redo), keeping first-seen order."""
    latest: dict[tuple, dict] = {}
    for rec in labels:
        latest[(rec["rater"], rec["take_id"], rec.get("plate"), rec["candidate"])] = rec
    return list(latest.values())


# --- statistics -----------------------------------------------------------------------------


def _as_scores(scores: Iterable[float]) -> np.ndarray:
    s = np.asarray(list(scores), dtype=np.float64)
    if s.ndim != 1:
        raise ValueError("scores must be a flat sequence")
    if np.isnan(s).any():
        raise ValueError("scores contain NaN")
    return s


def _as_labels(labels: Iterable[object], n: int) -> np.ndarray:
    raw = list(labels)
    if len(raw) != n:
        raise ValueError(f"{n} scores but {len(raw)} labels")
    if any(isinstance(x, str) or x not in (0, 1) for x in raw):
        raise ValueError("labels must be 0/1 or booleans (1 = ship)")
    return np.asarray([bool(x) for x in raw], dtype=bool)


def _ranks(x: np.ndarray) -> np.ndarray:
    """1-based ranks with ties given the average of the ranks they span."""
    _, inverse, counts = np.unique(x, return_inverse=True, return_counts=True)
    last = np.cumsum(counts)
    return (last - (counts - 1) / 2.0)[inverse]


def auc(scores: Iterable[float], labels: Iterable[object]) -> float:
    """ROC AUC by the rank formula (sum of positive ranks - n_pos (n_pos + 1) / 2) / (n_pos n_neg), ties
    averaged, no sklearn. labels: 1 / True = positive (ship), 0 / False = negative (reject); higher scores
    should mean more positive. Constant scores give 0.5. NaN when a class is empty (AUC is undefined).
    """
    s = _as_scores(scores)
    y = _as_labels(labels, len(s))
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return math.nan
    ranks = _ranks(s)
    return float((ranks[y].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def wilson_upper(k: int, n: int, z: float = 1.6449) -> float:
    """One-sided 95% Wilson upper bound for k events in n trials (1.0 when n = 0)."""
    if n <= 0:
        return 1.0
    p = k / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + margin) / denom)


def _curve(
    scores: Iterable[float], labels: Iterable[object]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    """Distinct scores ascending, and for each the ships / rejects accepted at threshold = that score."""
    s = _as_scores(scores)
    y = _as_labels(labels, len(s))
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        raise ValueError("an operating point needs at least one ship and one reject")
    uniq, inverse = np.unique(s, return_inverse=True)
    pos = np.bincount(inverse, weights=y.astype(float), minlength=len(uniq))
    neg = np.bincount(inverse, weights=(~y).astype(float), minlength=len(uniq))
    return uniq, np.cumsum(pos[::-1])[::-1], np.cumsum(neg[::-1])[::-1], n_pos, n_neg


def _point(
    threshold: float, n_tp: int, n_fp: int, n_pos: int, n_neg: int, cap: float, min_fpr_any_ship: float
) -> dict[str, float]:
    false_pass = n_fp / n_neg
    return {
        "threshold": float(threshold),
        "tpr": n_tp / n_pos,
        "fpr": false_pass,
        "false_pass": false_pass,
        "n_pos": n_pos,
        "n_neg": n_neg,
        "n_accepted": n_tp + n_fp,
        "n_false_pass": n_fp,
        "false_pass_ub95": wilson_upper(n_fp, n_neg),
        "min_fpr_any_ship": min_fpr_any_ship,
        "max_false_pass": cap,
    }


def fit_threshold(
    scores: Iterable[float], labels: Iterable[object], max_false_pass: float = 0.05
) -> dict[str, float]:
    """The operating point with the highest recall of ships whose false-pass rate stays <= max_false_pass.

    Accept when score >= threshold. Among all thresholds meeting the cap this picks the one accepting the
    most ships (ties go to the higher threshold, which accepts fewer rejects). If no threshold both
    accepts a ship and stays under the cap, the point is "accept nothing": threshold just above the top
    score, tpr 0, fpr 0 (the cap is then met only vacuously, and `min_fpr_any_ship` says how far over it
    the least-bad point that accepts a ship is). Needs at least one ship and one reject (ValueError).

    Returns {threshold, tpr, fpr, false_pass (== fpr), n_pos, n_neg, n_accepted, n_false_pass,
    false_pass_ub95 (Wilson upper bound), min_fpr_any_ship, max_false_pass}.
    """
    if not 0.0 <= max_false_pass <= 1.0:
        raise ValueError("max_false_pass must be between 0 and 1")
    uniq, tp, fp, n_pos, n_neg = _curve(scores, labels)
    max_fp = int(math.floor(max_false_pass * n_neg + 1e-9))
    feasible = fp <= max_fp
    best = -1
    if feasible.any():
        best_tp = tp[feasible].max()
        if best_tp > 0:
            best = int(np.flatnonzero(feasible & (tp == best_tp)).max())
    min_fpr = float(fp[tp > 0].min() / n_neg)
    if best < 0:
        return _point(np.nextafter(uniq[-1], np.inf), 0, 0, n_pos, n_neg, max_false_pass, min_fpr)
    return _point(uniq[best], int(tp[best]), int(fp[best]), n_pos, n_neg, max_false_pass, min_fpr)


def evaluate_threshold(
    scores: Iterable[float], labels: Iterable[object], threshold: float, max_false_pass: float = 0.05
) -> dict[str, float]:
    """The same numbers as fit_threshold for a FROZEN threshold: how a point chosen on one set of takes
    behaves on another (the held-out check the bet asks for). Accept when score >= threshold."""
    s = _as_scores(scores)
    y = _as_labels(labels, len(s))
    _, tp, fp, n_pos, n_neg = _curve(s, y)
    accepted = s >= threshold
    n_tp, n_fp = int((accepted & y).sum()), int((accepted & ~y).sum())
    return _point(threshold, n_tp, n_fp, n_pos, n_neg, max_false_pass, float(fp[tp > 0].min() / n_neg))


def inter_rater(labels_a: Sequence[object], labels_b: Sequence[object]) -> float:
    """Cohen's kappa between two raters' categorical labels for the same items, in the same order.

    1.0 is perfect agreement, 0 is chance level. Two raters who both used a single, identical category
    agree perfectly (kappa 1.0 by convention: chance agreement is 1 so the usual ratio is 0/0).
    """
    a, b = list(labels_a), list(labels_b)
    if len(a) != len(b):
        raise ValueError(f"raters labelled {len(a)} and {len(b)} items: they must be the same items")
    if not a:
        raise ValueError("no items to compare")
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[c] * cb[c] for c in set(ca) | set(cb)) / (n * n)
    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


# --- the gate ensemble ----------------------------------------------------------------------


@dataclass(frozen=True)
class EnsembleEntry:
    weight: float
    scale: float | None = None  # None = derive from the threshold


def _parse_ensemble(raw: object, known: Mapping[str, Any] | None) -> dict[str, EnsembleEntry]:
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("ensemble must be a non-empty mapping of metric id -> weight or {weight, scale}")
    out: dict[str, EnsembleEntry] = {}
    for mid, spec in raw.items():
        if known is not None and mid not in known:
            raise ValueError(f"ensemble names unknown metric id {mid!r}")
        weight, scale = (
            (spec, None) if not isinstance(spec, Mapping) else (spec.get("weight"), spec.get("scale"))
        )
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(weight)
            or weight < 0
        ):
            raise ValueError(f"ensemble weight for {mid!r} must be a number >= 0")
        if scale is not None and (
            isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not scale > 0
            or not math.isfinite(scale)
        ):
            raise ValueError(f"ensemble scale for {mid!r} must be a number > 0")
        out[str(mid)] = EnsembleEntry(float(weight), None if scale is None else float(scale))
    return out


def load_ensemble(path: str | Path | None = None) -> dict[str, EnsembleEntry]:
    """Weights from the `ensemble:` block of thresholds.yaml (or `path`); DEFAULT_ENSEMBLE when the file
    has none. Unknown metric ids in a file block raise ValueError: a typo must not silently drop a gate."""
    yaml_path = Path(path) if path else _THRESHOLDS_YAML
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) if yaml_path.exists() else None
    block = raw.get("ensemble") if isinstance(raw, dict) else None
    if block is None:
        return {mid: EnsembleEntry(w) for mid, w in DEFAULT_ENSEMBLE.items()}
    return _parse_ensemble(block, thresholds.load().metrics)


def goodness(thr: thresholds.Threshold, value: float, scale: float | None = None) -> float | None:
    """One metric's value as goodness in [0, 1]: 1 when it meets `thr`, then linear down to 0 at `scale`
    beyond the bound (distance to the nearest edge for a range). None when the bound is supplied at check
    time (video.duration_s) so there is nothing to normalise against. NaN values score 0."""
    v = float(value)
    if math.isnan(v):
        return 0.0
    if thr.range is not None:
        lo, hi = thr.range
        excess = max(lo - v, v - hi, 0.0)
        default = hi - lo if hi > lo else max(abs(hi), 1.0)
    elif thr.op in ("le", "ge", "eq") and thr.value is not None:
        b = float(thr.value)
        excess = {"le": max(v - b, 0.0), "ge": max(b - v, 0.0), "eq": max(abs(v - b) - thr.tol, 0.0)}[thr.op]
        default = abs(b) if abs(b) > 0 else 1.0
    else:
        return None
    return 1.0 - min(excess / (scale or default), 1.0)


def _is_num(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def gate_values(gate_report: object) -> dict[str, float]:
    """Flat {metric_id: value} from a gate report (GateReport, or its dict / parsed JSON): the scored
    checks first, then any other scalar entries of `metrics`. Non-numeric and missing values are left out."""
    if hasattr(gate_report, "model_dump"):
        gate_report = gate_report.model_dump(mode="json")
    if not isinstance(gate_report, Mapping):
        return {}
    out: dict[str, float] = {}
    for chk in gate_report.get("checks") or []:
        if isinstance(chk, Mapping) and isinstance(chk.get("metric_id"), str) and _is_num(chk.get("value")):
            out[chk["metric_id"]] = float(chk["value"])
    for key, val in (gate_report.get("metrics") or {}).items():
        if key not in out and _is_num(val):
            out[str(key)] = float(val)
    return out


def _gate_goodness(values: Mapping[str, float], ensemble: Mapping[str, EnsembleEntry]) -> dict[str, float]:
    known = thresholds.load().metrics
    out: dict[str, float] = {}
    for mid, entry in ensemble.items():
        if mid in values and mid in known:
            g = goodness(known[mid], values[mid], entry.scale)
            if g is not None:
                out[mid] = g
    return out


def ensemble_score(gate_report: object, ensemble: Mapping[str, EnsembleEntry] | None = None) -> float:
    """Weighted mean of per-gate goodness over the metrics this report has, in [0, 1]; higher is better.
    NaN when the report has none of the ensemble's metrics (nothing to score)."""
    ens = load_ensemble() if ensemble is None else ensemble
    good = _gate_goodness(gate_values(gate_report), ens)
    den = sum(ens[m].weight for m in good)
    if den <= 0:
        return math.nan
    return sum(ens[m].weight * g for m, g in good.items()) / den


def find_gate_report(label: Mapping[str, Any], reports_dir: str | Path | None) -> Path | None:
    """Where the gate report for a label lives, or None.

    In order: the label's own `gate_report` path (as written, then under `reports_dir`, then by file name
    under `reports_dir`: the path in a label is relative to the pairs manifest, not to here); then
    `reports_dir/<run_id>.<n>.json` with the highest n (the newest scoring: gates/report.write never
    overwrites); then the same two forms keyed by take_id.
    """
    base = Path(reports_dir) if reports_dir else None
    ref = label.get("gate_report")
    if isinstance(ref, str) and ref:
        ref_path = Path(ref.replace("\\", "/"))
        tries = [ref_path]
        if base:
            tries += [base / ref_path, base / ref_path.name]
        for p in tries:
            if p.is_file():
                return p
    if base and base.is_dir():
        for key in (label.get("run_id"), label.get("take_id")):
            if not isinstance(key, str) or not key or set(key) - set(_SAFE_ID):
                continue
            numbered = []
            for p in base.glob(f"{key}.*.json"):
                n = p.name[len(key) + 1 : -len(".json")]
                if n.isdigit():
                    numbered.append((int(n), p))
            if numbered:
                return max(numbered)[1]
            if (base / f"{key}.json").is_file():
                return base / f"{key}.json"
    return None


# --- the report -----------------------------------------------------------------------------


@dataclass
class _Row:
    label: dict
    reject: bool
    gate_report: str | None = None
    values: dict[str, float] | None = None
    good: dict[str, float] | None = None
    score: float = math.nan

    @property
    def scored(self) -> bool:
        return not math.isnan(self.score)


def _load_report_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _score_rows(
    rows: list[dict], reports_dir: Path | None, ensemble: Mapping[str, EnsembleEntry]
) -> tuple[list[_Row], int]:
    """Attach a gate report and an ensemble score to each label. Returns (rows, unreadable report count)."""
    out: list[_Row] = []
    unreadable = 0
    for rec in rows:
        row = _Row(label=rec, reject=is_reject(rec["verdict"]))
        embedded = rec.get("gate_report")
        gate_report = embedded if isinstance(embedded, Mapping) else None
        if gate_report is None:
            path = find_gate_report(rec, reports_dir)
            if path is not None:
                gate_report = _load_report_json(path)
                if gate_report is None:
                    unreadable += 1
                row.gate_report = path.as_posix()
        values = gate_values(gate_report)
        if values:
            row.values = values
            row.good = _gate_goodness(values, ensemble)
            row.score = ensemble_score(gate_report, ensemble)
        out.append(row)
    return out, unreadable


def _f(x: float | None, digits: int = 3) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{digits}f}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None or math.isnan(x) else f"{100 * x:.1f}%"


def _table(header: list[str], body: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return lines


def _gate_auc_rows(scored: list[_Row]) -> list[dict]:
    ids = sorted({m for r in scored for m in (r.good or {})})
    out = []
    for mid in ids:
        items = [(r.good[mid], not r.reject) for r in scored if r.good and mid in r.good]
        goods = [g for g, _ in items]
        ships = [ship for _, ship in items]
        out.append(
            {
                "metric_id": mid,
                "n": len(items),
                "n_ship": sum(ships),
                "n_reject": len(items) - sum(ships),
                "auc": auc(goods, ships),
            }
        )
    return out


def _pick_rater(rows: list[dict], rater: str | None) -> str | None:
    if rater:
        return rater
    counts = Counter(r["rater"] for r in rows)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if counts else None


def _agreement(rows: list[dict], primary: str) -> list[dict]:
    """Cohen's kappa between the primary rater and each other rater on the takes both labelled."""
    mine = {
        (r["take_id"], r.get("plate"), r["candidate"]): r["verdict"] for r in rows if r["rater"] == primary
    }
    out = []
    for other in sorted({r["rater"] for r in rows} - {primary}):
        theirs = {
            (r["take_id"], r.get("plate"), r["candidate"]): r["verdict"] for r in rows if r["rater"] == other
        }
        keys = sorted(mine.keys() & theirs.keys(), key=str)
        if not keys:
            continue
        a, b = [mine[k] for k in keys], [theirs[k] for k in keys]
        out.append(
            {
                "rater": other,
                "n": len(keys),
                "kappa_3class": inter_rater(a, b),
                "kappa_ship_vs_reject": inter_rater([is_reject(x) for x in a], [is_reject(x) for x in b]),
            }
        )
    return out


def report(
    labels: Iterable[dict] | str | Path,
    reports_dir: str | Path | None,
    out_md: str | Path | None = None,
    *,
    max_false_pass: float = BET["max_false_pass"],
    threshold: float | None = None,
    rater: str | None = None,
    today: date | None = None,
    ensemble: Mapping[str, EnsembleEntry] | None = None,
    worst: int = 10,
) -> dict[str, Any]:
    """Score the labelled takes with the gate ensemble and write the calibration report.

    labels: the records (or the path of a labels.jsonl). Repeat labels of one take by one rater collapse
    to the latest; the primary rater (`rater`, else whoever labelled most) is scored, others only feed the
    agreement table. reports_dir: where gate reports are looked up (see find_gate_report). Takes with no
    usable gate report are counted but cannot be scored and do not count toward the bet's n.

    threshold: a frozen operating point (chosen on an earlier set of takes) to evaluate on these labels,
    which is how the bet is run on held-out takes. Without it the point is fitted on these same takes
    (in-sample, optimistic) and the report says so.

    The bet's false-pass is the rate at the operating point, except that a point accepting no ship at all
    meets any cap vacuously: then the rate of the least-bad point that does accept a ship is used.

    out_md: the markdown file; None means <repo>/calib/report_<YYYY-MM-DD>.md, an existing folder gets
    report_<date>.md inside it. Returns the numbers (and the written path) as a dict.
    """
    if isinstance(labels, (str, Path)):
        labels = load_labels(labels)
    rows_all = dedupe_latest(labels)
    ens = load_ensemble() if ensemble is None else ensemble
    primary = _pick_rater(rows_all, rater)
    rdir = Path(reports_dir) if reports_dir else None
    rows, unreadable = _score_rows([r for r in rows_all if r["rater"] == primary], rdir, ens)
    scored = [r for r in rows if r.scored]

    n_scored = len(scored)
    n_rejects = sum(r.reject for r in scored)
    ship_flags = [not r.reject for r in scored]
    scores = [r.score for r in scored]
    ens_auc = auc(scores, ship_flags)

    point: dict[str, float] | None = None
    if 0 < n_rejects < n_scored:
        if threshold is None:
            point = fit_threshold(scores, ship_flags, max_false_pass)
        else:
            point = evaluate_threshold(scores, ship_flags, threshold, max_false_pass)
    vacuous = point is not None and point["tpr"] == 0
    false_pass = math.nan if point is None else point["min_fpr_any_ship" if vacuous else "false_pass"]
    bet = {
        "n": {"value": n_scored, "need": f">= {BET['min_n']}", "ok": n_scored >= BET["min_n"]},
        "n_rejects": {
            "value": n_rejects,
            "need": f">= {BET['min_rejects']}",
            "ok": n_rejects >= BET["min_rejects"],
        },
        "auc": {"value": ens_auc, "need": f">= {BET['min_auc']}", "ok": bool(ens_auc >= BET["min_auc"])},
        "false_pass": {
            "value": false_pass,
            "need": f"<= {BET['max_false_pass']}",
            "ok": bool(false_pass <= BET["max_false_pass"]),
        },
    }
    bet_passed = all(c["ok"] for c in bet.values())

    cut = None if point is None else point["threshold"]
    missed = sorted(
        (r for r in scored if r.reject and cut is not None and r.score >= cut),
        key=lambda r: (-r.score, r.label["take_id"]),
    )[:worst]
    gate_rows = _gate_auc_rows(scored)
    agreement = _agreement(rows_all, primary) if primary else []

    stamp = (today or datetime.now(UTC).date()).strftime("%Y-%m-%d")
    out_path = _REPO_CALIB / f"report_{stamp}.md" if out_md is None else Path(out_md)
    if out_path.is_dir():
        out_path = out_path / f"report_{stamp}.md"

    summary: dict[str, Any] = {
        "path": out_path,
        "rater": primary,
        "n_labelled": len(rows),
        "n": n_scored,
        "n_rejects": n_rejects,
        "n_unscored": len(rows) - n_scored,
        "n_unreadable_reports": unreadable,
        "auc": ens_auc,
        "gate_auc": {g["metric_id"]: g["auc"] for g in gate_rows},
        "operating_point": point,
        "operating_point_frozen": threshold is not None,
        "operating_point_vacuous": vacuous,
        "false_pass": false_pass,
        "bet_passed": bet_passed,
        "bet": bet,
        "worst_false_passes": [
            {
                "take_id": r.label["take_id"],
                "candidate": r.label["candidate"],
                "verdict": r.label["verdict"],
                "reasons": list(r.label.get("reasons", [])),
                "score": r.score,
            }
            for r in missed
        ],
        "agreement": agreement,
    }
    lines = _render(
        summary, stamp, rdir, rows, scored, gate_rows, missed, len(rows_all), len(ens), max_false_pass
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return summary


def _yes(ok: bool) -> str:
    return "yes" if ok else "no"


def _render(
    summary: dict[str, Any],
    stamp: str,
    rdir: Path | None,
    rows: list[_Row],
    scored: list[_Row],
    gate_rows: list[dict],
    missed: list[_Row],
    n_labels_in_file: int,
    n_gates: int,
    max_false_pass: float,
) -> list[str]:
    """The markdown for report(): one section per question the bet asks."""
    bet, point = summary["bet"], summary["operating_point"]
    cut = None if point is None else point["threshold"]
    n_scored, n_rejects = summary["n"], summary["n_rejects"]
    verdicts = Counter(r.label["verdict"] for r in rows)
    md = [f"# Calibration report {stamp}", ""]
    md.append(
        f"Rater scored: `{summary['rater']}`. Labels in file: {n_labels_in_file} (latest per rater and take). "
        f"Gate reports from: `{rdir.as_posix() if rdir else 'none'}`."
    )

    md += ["", "## The bet", ""]
    md.append(
        f"**{'PASSED' if summary['bet_passed'] else 'NOT PASSED'}**: all four of n >= {BET['min_n']}, "
        f"rejects >= {BET['min_rejects']}, AUC >= {BET['min_auc']}, false-pass <= {_pct(BET['max_false_pass'])}."
    )
    md.append("")
    md += _table(
        ["check", "value", "need", "ok"],
        [
            ["takes scored (n)", str(n_scored), bet["n"]["need"], _yes(bet["n"]["ok"])],
            ["rejects among them", str(n_rejects), bet["n_rejects"]["need"], _yes(bet["n_rejects"]["ok"])],
            ["ensemble AUC", _f(bet["auc"]["value"]), bet["auc"]["need"], _yes(bet["auc"]["ok"])],
            [
                "false-pass at the operating point",
                _pct(summary["false_pass"]),
                bet["false_pass"]["need"],
                _yes(bet["false_pass"]["ok"]),
            ],
        ],
    )
    if summary["operating_point_vacuous"]:
        md += [
            "",
            "The operating point accepts no ship at all, so its own false-pass of 0% is vacuous. The figure "
            "above is the lowest false-pass at which the gates accept any ship.",
        ]

    md += ["", "## Counts", ""]
    md += _table(
        ["", "takes"],
        [
            ["labelled by this rater", str(len(rows))],
            ["ship", str(verdicts.get("ship", 0))],
            ["fix", str(verdicts.get("fix", 0))],
            ["render_it", str(verdicts.get("render_it", 0))],
            ["with a usable gate report (scored)", str(n_scored)],
            ["without one (not scored, not counted in n)", str(len(rows) - n_scored)],
        ],
    )
    if summary["n_unreadable_reports"]:
        md += [
            "",
            f"{summary['n_unreadable_reports']} gate report file(s) could not be read and count as missing.",
        ]

    md += ["", "## AUC", ""]
    md.append(
        "Positive class = ship; a higher score means the gates like the candidate more. 0.5 is a coin flip."
    )
    md.append("")
    md += _table(
        ["gate", "n", "ships", "rejects", "AUC"],
        [["**ensemble**", str(n_scored), str(n_scored - n_rejects), str(n_rejects), _f(summary["auc"])]]
        + [
            [g["metric_id"], str(g["n"]), str(g["n_ship"]), str(g["n_reject"]), _f(g["auc"])]
            for g in gate_rows
        ],
    )

    md += ["", "## Operating point", ""]
    if point is None:
        md.append("Not available: it needs at least one ship and one reject among the scored takes.")
    else:
        how = (
            f"frozen at {_f(cut, 4)} (chosen elsewhere: a held-out evaluation)"
            if summary["operating_point_frozen"]
            else f"fitted here for the highest recall of ships with false-pass <= {_pct(max_false_pass)}"
        )
        md.append(f"Accept when the ensemble score is >= **{_f(cut, 4)}**; {how}.")
        md.append("")
        md += _table(
            [
                "tpr (ships accepted)",
                "false-pass (rejects accepted)",
                "false-pass 95% upper bound",
                "accepted",
            ],
            [
                [
                    _pct(point["tpr"]),
                    f"{_pct(point['fpr'])} ({point['n_false_pass']} of {point['n_neg']})",
                    _pct(point["false_pass_ub95"]),
                    str(point["n_accepted"]),
                ]
            ],
        )
        if not summary["operating_point_frozen"]:
            md += [
                "",
                "Fitted on the same takes it is scored on, so optimistic: freeze it and confirm on held-out "
                "takes (`--threshold`).",
            ]

    md += ["", "## Confusion (human verdict against gates)", ""]
    if point is None:
        md.append("Not available without an operating point.")
    else:
        body = []
        for v in VERDICTS:
            group = [r for r in rows if r.label["verdict"] == v]
            accepted = sum(1 for r in group if r.scored and r.score >= cut)
            flagged = sum(1 for r in group if r.scored and r.score < cut)
            body.append([v, str(accepted), str(flagged), str(len(group) - accepted - flagged)])
        md += _table(["human verdict", "gates accept", "gates flag", "no gate report"], body)

    md += ["", "## Worst false passes", ""]
    if not missed:
        md.append("None." if point else "Not available without an operating point.")
    else:
        md.append("Rejects the gates accept, most confidently first.")
        md.append("")
        md += _table(
            ["take", "candidate", "verdict", "reasons", "score"],
            [
                [
                    r.label["take_id"],
                    r.label["candidate"],
                    r.label["verdict"],
                    " ".join(r.label.get("reasons", [])) or "-",
                    _f(r.score),
                ]
                for r in missed
            ],
        )

    reasons = Counter(c for r in scored if r.reject for c in r.label.get("reasons", []))
    if point is not None and reasons:
        md += ["", "## Reasons given on rejects", ""]
        body = []
        for code, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])):
            caught = sum(
                1 for r in scored if r.reject and code in r.label.get("reasons", []) and r.score < cut
            )
            body.append([code, str(n), str(caught), str(n - caught)])
        md += _table(["code", "rejects", "caught by gates", "missed"], body)

    md += ["", "## Inter-rater agreement", ""]
    if not summary["agreement"]:
        md.append("No second rater has labelled the same takes yet.")
    else:
        md += _table(
            ["other rater", "shared takes", "kappa (3 verdicts)", "kappa (ship vs reject)"],
            [
                [a["rater"], str(a["n"]), _f(a["kappa_3class"]), _f(a["kappa_ship_vs_reject"])]
                for a in summary["agreement"]
            ],
        )

    md += ["", "## Notes", ""]
    md.append(
        f"- Ensemble of up to {n_gates} gate metrics, weighted as in thresholds.yaml `ensemble:` "
        "(DEFAULT_ENSEMBLE in calibration.py until that block exists). The weights are a first guess, not fitted."
    )
    md.append(
        "- A metric a report does not contain is left out of its score and the mean re-normalised, so "
        "reports from different gate runs are comparable but not identical in coverage."
    )
    md.append("- n and rejects in the bet count scored takes only.")
    return md


# --- command line ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m plate_gates.calibration", description=__doc__.split("\n\n")[0]
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="score labelled takes with the gate ensemble and write the report")
    r.add_argument(
        "--labels", type=Path, default=DEFAULT_LABELS, help="labels file (default calib/labels.jsonl)"
    )
    r.add_argument("--reports", type=Path, default=None, help="folder of gate report JSON files")
    r.add_argument(
        "--out", type=Path, default=None, help="markdown file or folder (default calib/report_<date>.md)"
    )
    r.add_argument("--rater", default=None, help="rater to score (default: whoever labelled most)")
    r.add_argument("--max-false-pass", type=float, default=BET["max_false_pass"])
    r.add_argument(
        "--threshold", type=float, default=None, help="frozen operating point to evaluate (held-out run)"
    )
    r.add_argument("--strict", action="store_true", help="exit 1 when the bet is not passed")
    args = p.parse_args(argv)
    try:
        summary = report(
            args.labels,
            args.reports,
            args.out,
            max_false_pass=args.max_false_pass,
            threshold=args.threshold,
            rater=args.rater,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"wrote {summary['path']}")
    print(
        f"n={summary['n']} rejects={summary['n_rejects']} auc={_f(summary['auc'])} "
        f"false_pass={_pct(summary['false_pass'])} bet_passed={summary['bet_passed']}"
    )
    return 1 if args.strict and not summary["bet_passed"] else 0


if __name__ == "__main__":
    sys.exit(main())
