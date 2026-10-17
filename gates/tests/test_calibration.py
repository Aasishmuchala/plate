"""Calibration statistics and report (T23): AUC by the rank formula, the false-pass-capped operating point,
Cohen's kappa, the gate ensemble, label / gate-report loading and the markdown report. Hermetic: gate
reports are plain dicts shaped like GateReport.model_dump(), labels are written to tmp_path."""

from __future__ import annotations

import itertools
import json
import math
import warnings
from datetime import date

import numpy as np
import pytest

from plate_gates import calibration as cal
from plate_gates import thresholds

# --- AUC ------------------------------------------------------------------------------------


def brute_auc(scores, labels):
    """Mann-Whitney by pairwise comparison, ties counted half."""
    pos = [s for s, y in zip(scores, labels, strict=True) if y]
    neg = [s for s, y in zip(scores, labels, strict=True) if not y]
    total = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p, n in itertools.product(pos, neg))
    return total / (len(pos) * len(neg))


def test_auc_perfectly_separable_is_one_and_inverted_is_zero():
    scores = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
    labels = [0, 0, 0, 1, 1, 1]
    assert cal.auc(scores, labels) == 1.0
    assert cal.auc([-s for s in scores], labels) == 0.0


def test_auc_random_scores_are_a_coin_flip():
    rng = np.random.default_rng(7)
    scores = rng.random(2000)
    labels = rng.integers(0, 2, 2000)
    assert abs(cal.auc(scores, labels) - 0.5) < 0.05


def test_auc_ties_are_averaged():
    assert cal.auc([0.5] * 10, [1, 0] * 5) == 0.5  # constant scores
    assert cal.auc([1, 1, 2, 2], [0, 1, 0, 1]) == 0.5  # pairs: tie, loss, win, tie
    # the textbook example (sklearn's docs): 3 of 4 pairs ranked right
    assert cal.auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75


def test_auc_matches_pairwise_counting_with_many_ties():
    rng = np.random.default_rng(3)
    scores = rng.integers(0, 6, 300).astype(float)
    labels = (rng.random(300) < 0.4 + 0.08 * scores / 5).astype(int)
    assert cal.auc(scores, labels) == pytest.approx(brute_auc(scores, labels), abs=1e-12)


def test_auc_accepts_booleans_and_rejects_bad_input():
    assert cal.auc([1.0, 2.0], [False, True]) == 1.0
    assert math.isnan(cal.auc([1.0, 2.0], [1, 1]))  # undefined with one class
    with pytest.raises(ValueError, match="labels"):
        cal.auc([1.0, 2.0], [1, 2])
    with pytest.raises(ValueError, match="labels"):
        cal.auc([1.0, 2.0], ["ship", "fix"])
    with pytest.raises(ValueError, match="NaN"):
        cal.auc([1.0, math.nan], [0, 1])
    with pytest.raises(ValueError, match="labels"):
        cal.auc([1.0, 2.0, 3.0], [0, 1])


# --- operating point ------------------------------------------------------------------------


def brute_best_tpr(scores, labels, cap):
    s, y = np.asarray(scores), np.asarray(labels, bool)
    best = 0.0
    for t in np.unique(s):
        acc = s >= t
        if (acc & ~y).sum() / (~y).sum() <= cap + 1e-12:
            best = max(best, (acc & y).sum() / y.sum())
    return best


def test_fit_threshold_respects_the_cap_on_a_synthetic_distribution():
    rng = np.random.default_rng(11)
    ships = rng.normal(0.72, 0.12, 1000)
    rejects = rng.normal(0.45, 0.15, 1000)
    scores = np.concatenate([ships, rejects])
    labels = np.concatenate([np.ones(1000, int), np.zeros(1000, int)])
    pt = cal.fit_threshold(scores, labels, max_false_pass=0.05)
    assert pt["false_pass"] <= 0.05 and pt["fpr"] == pt["false_pass"]
    assert pt["n_false_pass"] == int(((scores >= pt["threshold"]) & (labels == 0)).sum())
    assert pt["tpr"] == pytest.approx(((scores >= pt["threshold"]) & (labels == 1)).sum() / 1000)
    assert pt["tpr"] == pytest.approx(brute_best_tpr(scores, labels, 0.05))  # nothing better is feasible
    assert 0.3 < pt["tpr"] < 0.9  # a real trade-off, not a degenerate corner
    looser = cal.fit_threshold(scores, labels, max_false_pass=0.20)
    assert looser["tpr"] > pt["tpr"] and looser["false_pass"] <= 0.20
    strict = cal.fit_threshold(scores, labels, max_false_pass=0.0)
    assert strict["false_pass"] == 0.0


def test_fit_threshold_separable_scores_accept_every_ship():
    pt = cal.fit_threshold([0.9, 0.8, 0.7, 0.3, 0.2, 0.1], [1, 1, 1, 0, 0, 0])
    assert pt["threshold"] == 0.7 and pt["tpr"] == 1.0 and pt["fpr"] == 0.0
    assert pt["n_accepted"] == 3 and pt["n_pos"] == 3 and pt["n_neg"] == 3


def test_fit_threshold_cap_is_inclusive_and_exact():
    # 60 rejects, the top 3 scoring above the lowest ship: 3/60 = 5% is allowed, 4/60 is not
    ships = [0.5] * 10
    rejects = [0.9, 0.8, 0.7] + [0.1] * 57
    pt = cal.fit_threshold(ships + rejects, [1] * 10 + [0] * 60)
    assert pt["threshold"] == 0.5 and pt["n_false_pass"] == 3 and pt["false_pass"] == 0.05
    rejects4 = [0.9, 0.8, 0.7, 0.6] + [0.1] * 56
    pt = cal.fit_threshold(ships + rejects4, [1] * 10 + [0] * 60)
    assert pt["n_false_pass"] == 0 and pt["tpr"] == 0.0 and pt["threshold"] > 0.9  # accept nothing


def test_fit_threshold_ties_between_classes_move_together():
    pt = cal.fit_threshold([1.0, 1.0, 1.0, 0.2], [1, 1, 0, 0], max_false_pass=0.5)
    assert pt["threshold"] == 1.0 and pt["n_accepted"] == 3 and pt["fpr"] == 0.5
    pt = cal.fit_threshold([1.0, 1.0, 1.0, 0.2], [1, 1, 0, 0], max_false_pass=0.25)
    assert pt["tpr"] == 0.0  # the tied reject cannot be left behind


def test_fit_threshold_accept_nothing_reports_the_least_bad_real_point():
    # every ship ties with 6 of 70 rejects: any point that accepts a ship has false-pass 6/70
    scores = [1.0] * 50 + [1.0] * 6 + [0.2] * 64
    labels = [1] * 50 + [0] * 70
    pt = cal.fit_threshold(scores, labels)
    assert pt["tpr"] == 0.0 and pt["false_pass"] == 0.0 and pt["threshold"] > 1.0
    assert pt["min_fpr_any_ship"] == pytest.approx(6 / 70)


def test_fit_threshold_needs_both_classes():
    with pytest.raises(ValueError, match="at least one ship and one reject"):
        cal.fit_threshold([0.1, 0.2], [1, 1])
    with pytest.raises(ValueError, match="at least one ship and one reject"):
        cal.fit_threshold([0.1, 0.2], [0, 0])
    with pytest.raises(ValueError, match="max_false_pass"):
        cal.fit_threshold([0.1, 0.2], [0, 1], max_false_pass=1.5)


def test_evaluate_threshold_scores_a_frozen_point_on_new_takes():
    pt = cal.evaluate_threshold([0.9, 0.8, 0.6, 0.4, 0.3], [1, 1, 0, 0, 1], threshold=0.5)
    assert pt["threshold"] == 0.5 and pt["n_accepted"] == 3 and pt["n_false_pass"] == 1
    assert pt["tpr"] == pytest.approx(2 / 3) and pt["false_pass"] == pytest.approx(1 / 2)


def test_wilson_upper_bound_brackets_the_rule_of_three():
    assert cal.wilson_upper(0, 60) < 0.05  # zero misses in 60 rejects bounds the rate under 5%
    assert cal.wilson_upper(3, 60) > 0.05
    assert cal.wilson_upper(0, 0) == 1.0


# --- inter-rater ----------------------------------------------------------------------------


def test_kappa_identical_labels_is_one():
    labels = ["ship", "fix", "render_it", "fix", "ship", "ship"]
    assert cal.inter_rater(labels, list(labels)) == 1.0


def test_kappa_textbook_value_and_chance_and_disagreement():
    # 50 items: 20 yes/yes, 15 no/no, 10 yes/no, 5 no/yes -> po 0.7, pe 0.5, kappa 0.4
    a = ["y"] * 20 + ["n"] * 15 + ["y"] * 10 + ["n"] * 5
    b = ["y"] * 20 + ["n"] * 15 + ["n"] * 10 + ["y"] * 5
    assert cal.inter_rater(a, b) == pytest.approx(0.4)
    assert cal.inter_rater(["a", "b", "a", "b"], ["b", "a", "b", "a"]) == pytest.approx(-1.0)
    rng = np.random.default_rng(5)
    x = rng.choice(["ship", "fix", "render_it"], 3000).tolist()
    y = rng.choice(["ship", "fix", "render_it"], 3000).tolist()
    assert abs(cal.inter_rater(x, y)) < 0.05  # independent raters sit near zero


def test_kappa_edge_cases():
    assert cal.inter_rater(["ship"] * 4, ["ship"] * 4) == 1.0  # one shared category: 0/0 by convention
    with pytest.raises(ValueError, match="same items"):
        cal.inter_rater(["a"], ["a", "b"])
    with pytest.raises(ValueError, match="no items"):
        cal.inter_rater([], [])


# --- goodness and the ensemble --------------------------------------------------------------


def thr(metric_id):
    return thresholds.load().get(metric_id)


def test_goodness_is_one_inside_the_bound_and_linear_beyond_it():
    le = thr("registration.residual_px")  # <= 1.0, scale 1.0
    assert cal.goodness(le, 0.4) == 1.0 and cal.goodness(le, 1.0) == 1.0
    assert cal.goodness(le, 1.5) == pytest.approx(0.5) and cal.goodness(le, 9.0) == 0.0
    ge = thr("gbuffer_edge_recall_r2")  # >= 0.90, scale 0.90
    assert cal.goodness(ge, 0.95) == 1.0 and cal.goodness(ge, 0.45) == pytest.approx(0.5)
    rng = thr("texture.lapvar")  # [300, 600], scale = width 300
    assert cal.goodness(rng, 450) == 1.0 and cal.goodness(rng, 150) == pytest.approx(0.5)
    assert cal.goodness(rng, 750) == pytest.approx(0.5) and cal.goodness(rng, 5000) == 0.0
    eq = thr("invention_flags")  # == 0, scale 1
    assert cal.goodness(eq, 0) == 1.0 and cal.goodness(eq, 1) == 0.0
    assert cal.goodness(le, math.nan) == 0.0
    assert cal.goodness(le, 3.0, scale=4.0) == pytest.approx(0.5)  # explicit scale wins
    assert cal.goodness(thr("video.duration_s"), 5.0) is None  # bound only known at check time


def gate_report(**values):
    """A GateReport.model_dump()-shaped dict for the given metric values."""
    return {
        "gate_version": "0.1.0",
        "checks": [
            {"metric_id": k.replace("__", "."), "value": v, "status": "pass"} for k, v in values.items()
        ],
        "metrics": {},
        "verdict": "pass",
    }


PERFECT = {
    "registration.residual_px": 0.3,
    "invention_flags": 0,
    "texture.lapvar": 420,
    "gbuffer_edge_recall_r2": 0.95,
}


def report_of(values):
    return {
        "checks": [{"metric_id": k, "value": v, "status": "pass"} for k, v in values.items()],
        "metrics": {},
    }


def test_ensemble_score_is_a_weighted_mean_in_unit_range():
    ens = {
        m: cal.EnsembleEntry(w) for m, w in {"registration.residual_px": 1.0, "invention_flags": 3.0}.items()
    }
    assert cal.ensemble_score(report_of(PERFECT), ens) == 1.0
    bad_reg = {**PERFECT, "registration.residual_px": 2.0}  # goodness 0 on the weight-1 gate
    assert cal.ensemble_score(report_of(bad_reg), ens) == pytest.approx(3 / 4)
    bad_inv = {**PERFECT, "invention_flags": 4}  # goodness 0 on the weight-3 gate
    assert cal.ensemble_score(report_of(bad_inv), ens) == pytest.approx(1 / 4)
    assert cal.ensemble_score(report_of({"registration.residual_px": 2.0, "invention_flags": 1}), ens) == 0.0


def test_ensemble_score_renormalises_over_the_metrics_present():
    ens = {
        m: cal.EnsembleEntry(w) for m, w in {"registration.residual_px": 1.0, "invention_flags": 3.0}.items()
    }
    assert cal.ensemble_score(report_of({"invention_flags": 0}), ens) == 1.0
    assert cal.ensemble_score(report_of({"invention_flags": 1}), ens) == 0.0
    assert math.isnan(cal.ensemble_score(report_of({"texture.lapvar": 400}), ens))  # nothing it weighs
    assert math.isnan(cal.ensemble_score({}, ens)) and math.isnan(cal.ensemble_score("junk", ens))


def test_ensemble_score_reads_checks_then_scalar_metrics_and_skips_junk():
    rep = {
        "checks": [
            {"metric_id": "registration.residual_px", "value": 0.5, "status": "pass"},
            {"metric_id": "invention_flags", "value": None, "status": "advisory"},  # not computed
        ],
        "metrics": {"invention_flags": 0, "texture.lapvar": {"a": 1}, "ral.de00": True, "x": "str"},
    }
    assert cal.gate_values(rep) == {"registration.residual_px": 0.5, "invention_flags": 0.0}
    assert cal.gate_values(None) == {}


def test_ensemble_score_accepts_a_pydantic_report():
    class Dumpable:
        def model_dump(self, mode="python"):
            return report_of(PERFECT)

    ens = {"registration.residual_px": cal.EnsembleEntry(1.0)}
    assert cal.ensemble_score(Dumpable(), ens) == 1.0


def test_default_ensemble_names_only_known_metrics():
    known = thresholds.load().metrics
    assert set(cal.DEFAULT_ENSEMBLE) <= set(known), set(cal.DEFAULT_ENSEMBLE) - set(known)
    assert all(w > 0 for w in cal.DEFAULT_ENSEMBLE.values())
    assert cal.ensemble_score(report_of(PERFECT)) == 1.0  # the default weights, a clean report


def test_load_ensemble_defaults_and_yaml_blocks(tmp_path):
    plain = tmp_path / "plain.yaml"
    plain.write_text("set_id: x\nmetrics: []\n", encoding="utf-8", newline="\n")
    assert {m: e.weight for m, e in cal.load_ensemble(plain).items()} == cal.DEFAULT_ENSEMBLE
    assert cal.load_ensemble()  # the committed thresholds.yaml, with or without an ensemble block

    block = tmp_path / "block.yaml"
    block.write_text(
        "ensemble:\n  invention_flags: 3\n  texture.lapvar: {weight: 1.5, scale: 100}\n",
        encoding="utf-8",
        newline="\n",
    )
    ens = cal.load_ensemble(block)
    assert ens["invention_flags"] == cal.EnsembleEntry(3.0, None)
    assert ens["texture.lapvar"] == cal.EnsembleEntry(1.5, 100.0)
    g = cal.ensemble_score(report_of({"texture.lapvar": 700}), ens)  # 100 beyond the range, scale 100
    assert g == 0.0

    for body, needle in [
        ("ensemble:\n  not.a.metric: 1\n", "unknown metric"),
        ("ensemble:\n  invention_flags: -1\n", "weight"),
        ("ensemble:\n  invention_flags: {weight: 1, scale: 0}\n", "scale"),
        ("ensemble:\n  invention_flags: abc\n", "weight"),
        ("ensemble: []\n", "mapping"),
    ]:
        bad = tmp_path / "bad.yaml"
        bad.write_text(body, encoding="utf-8", newline="\n")
        with pytest.raises(ValueError, match=needle):
            cal.load_ensemble(bad)


# --- labels and gate-report lookup ----------------------------------------------------------


def label(take, verdict="ship", rater="aasish", candidate=None, reasons=None, **extra):
    rec = {
        "v": 1,
        "at": "2026-10-17T09:41:07Z",
        "rater": rater,
        "take_id": take,
        "plate": f"in/{take}.png",
        "candidate": candidate or f"out/{take}.png",
        "verdict": verdict,
        "reasons": reasons if reasons is not None else ([] if verdict == "ship" else ["P1"]),
        "seconds": 5.0,
    }
    rec.update(extra)
    return rec


def write_labels(path, recs):
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in recs),
        encoding="utf-8",
        newline="\n",
    )


def test_load_labels_reads_utf8_and_skips_bad_lines_loudly(tmp_path):
    path = tmp_path / "labels.jsonl"
    good = [label("S3"), label("Zimmer-ünïcode", "fix", reasons=["A2", "P4"])]
    lines = [json.dumps(r, ensure_ascii=False) for r in good]
    lines.insert(1, "")
    lines.insert(2, '{"v":1,"at"')  # crash mid-write
    lines.append(json.dumps({**label("X"), "verdict": "maybe"}))
    lines.append(json.dumps({**label("Y"), "v": 2}))
    lines.append(json.dumps({k: v for k, v in label("Z").items() if k != "take_id"}))
    lines.append(json.dumps({**label("W"), "reasons": "P1"}))
    lines.append("[1, 2]")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        got = cal.load_labels(path)
    assert [r["take_id"] for r in got] == ["S3", "Zimmer-ünïcode"]
    assert len(caught) == 6 and all("skipped" in str(w.message) for w in caught)
    assert any(":3:" in str(w.message) for w in caught)  # names the line
    with pytest.raises(FileNotFoundError):
        cal.load_labels(tmp_path / "missing.jsonl")


def test_dedupe_latest_keeps_the_last_label_per_rater_and_candidate():
    recs = [
        label("S3", "ship"),
        label("S4", "ship"),
        label("S3", "fix", reasons=["P1"]),  # relabelled
        label("S3", "ship", rater="second"),  # another rater is a different key
        label("S3", "ship", candidate="out/S3_b.png"),  # another candidate of the same take
    ]
    got = cal.dedupe_latest(recs)
    assert [(r["rater"], r["take_id"], r["candidate"], r["verdict"]) for r in got] == [
        ("aasish", "S3", "out/S3.png", "fix"),
        ("aasish", "S4", "out/S4.png", "ship"),
        ("second", "S3", "out/S3.png", "ship"),
        ("aasish", "S3", "out/S3_b.png", "ship"),
    ]


def write_gate(folder, name, values):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(report_of(values)), encoding="utf-8", newline="\n")
    return path


def test_find_gate_report_lookup_order(tmp_path):
    reports = tmp_path / "gates"
    explicit = write_gate(tmp_path / "elsewhere", "mine.json", PERFECT)
    by_name = write_gate(reports, "r12.1.json", PERFECT)
    r2 = write_gate(reports, "r12.2.json", PERFECT)
    write_gate(reports, "r12.10.json", PERFECT)
    by_take = write_gate(reports, "S5.3.json", PERFECT)
    plain_take = write_gate(reports, "S6.json", PERFECT)
    find = cal.find_gate_report
    assert find({"gate_report": str(explicit)}, reports) == explicit  # as written
    assert find({"gate_report": "gates/r12.1.json"}, reports) == by_name  # manifest-relative: by file name
    assert find({"gate_report": "r12.2.json"}, reports) == r2  # under reports_dir
    assert find({"run_id": "r12"}, reports).name == "r12.10.json"  # newest numbered scoring, 10 > 2
    assert find({"take_id": "S5"}, reports) == by_take
    assert find({"take_id": "S6"}, reports) == plain_take
    assert find({"run_id": "r99", "take_id": "S5"}, reports) == by_take  # falls through to the take id
    assert find({"take_id": "../x"}, reports) is None  # unsafe ids are never globbed
    assert find({"take_id": "nope"}, reports) is None
    assert find({"take_id": "S5"}, None) is None
    assert find({"gate_report": "gates\\r12.1.json"}, reports) == by_name  # Windows separators


# --- the report -----------------------------------------------------------------------------

CLEAN = {"registration.residual_px": 0.4, "invention_flags": 0, "texture.lapvar": 420}
BROKEN = {"registration.residual_px": 3.0, "invention_flags": 2, "texture.lapvar": 1400}
MILD = {"registration.residual_px": 1.4, "invention_flags": 0, "texture.lapvar": 420}


def build_takes(tmp_path, n_ship, n_clear_reject, n_false_pass, n_mild=0, rater="aasish"):
    """Labels plus one gate report per take: ships and false passes look clean, clear rejects look broken."""
    reports = tmp_path / "gates"
    recs = []
    plan = (
        [("ship", CLEAN)] * n_ship
        + [("fix", BROKEN)] * n_clear_reject
        + [("render_it", CLEAN)] * n_false_pass
        + [("fix", MILD)] * n_mild
    )
    for i, (verdict, values) in enumerate(plan):
        take = f"T{i:03d}"
        write_gate(reports, f"{take}.1.json", values)
        recs.append(
            label(
                take,
                verdict,
                rater=rater,
                reasons=[] if verdict == "ship" else ["P1", "A2"] if i % 2 else ["P3"],
            )
        )
    return recs, reports


TODAY = date(2026, 10, 17)


def test_report_passes_the_bet_and_writes_markdown(tmp_path):
    recs, reports = build_takes(tmp_path, n_ship=50, n_clear_reject=67, n_false_pass=3)
    out = tmp_path / "calib" / "r.md"
    s = cal.report(recs, reports, out, today=TODAY)
    assert s["n"] == 120 and s["n_rejects"] == 70 and s["n_labelled"] == 120 and s["n_unscored"] == 0
    assert s["auc"] == pytest.approx((67 + 1.5) / 70)
    pt = s["operating_point"]
    assert pt["n_false_pass"] == 3 and pt["tpr"] == 1.0 and not s["operating_point_vacuous"]
    assert s["false_pass"] == pytest.approx(3 / 70) and s["bet_passed"] is True
    assert all(c["ok"] for c in s["bet"].values())
    assert s["path"] == out and out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "\r" not in text and text.startswith("# Calibration report 2026-10-17")
    for needle in ["PASSED", "ensemble AUC", "## AUC", "**ensemble**", "registration.residual_px", "## Operating point",
                   "## Confusion", "## Worst false passes", "T117", "## Reasons given on rejects", "## Inter-rater"]:  # fmt: skip
        assert needle in text, needle
    assert "NOT PASSED" not in text
    assert [w["take_id"] for w in s["worst_false_passes"]] == [
        "T117",
        "T118",
        "T119",
    ]  # the 3 look-clean rejects
    assert {g for g in s["gate_auc"]} == {"registration.residual_px", "invention_flags", "texture.lapvar"}
    assert s["gate_auc"]["invention_flags"] == pytest.approx((67 + 1.5) / 70)


def test_report_fails_the_bet_on_small_n_and_says_why(tmp_path):
    recs, reports = build_takes(tmp_path, n_ship=10, n_clear_reject=15, n_false_pass=0)
    s = cal.report(recs, reports, tmp_path / "r.md", today=TODAY)
    assert s["bet_passed"] is False
    assert s["bet"]["n"]["ok"] is False and s["bet"]["n_rejects"]["ok"] is False
    assert s["bet"]["auc"]["ok"] is True and s["bet"]["false_pass"]["ok"] is True
    text = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "NOT PASSED" in text and "| takes scored (n) | 25 | >= 100 | no |" in text


def test_report_fails_when_ships_can_only_be_accepted_with_too_many_false_passes(tmp_path):
    # 6 of 70 rejects look as clean as every ship: accepting any ship means 8.6% false pass
    recs, reports = build_takes(tmp_path, n_ship=50, n_clear_reject=64, n_false_pass=6)
    s = cal.report(recs, reports, tmp_path / "r.md", today=TODAY)
    assert s["operating_point"]["tpr"] == 0.0 and s["operating_point_vacuous"] is True
    assert s["auc"] >= 0.85 and s["false_pass"] == pytest.approx(6 / 70)
    assert s["bet"]["false_pass"]["ok"] is False and s["bet_passed"] is False
    assert "vacuous" in (tmp_path / "r.md").read_text(encoding="utf-8")


def test_report_can_evaluate_a_frozen_threshold_on_held_out_takes(tmp_path):
    recs, reports = build_takes(tmp_path, n_ship=50, n_clear_reject=64, n_false_pass=6)
    s = cal.report(recs, reports, tmp_path / "r.md", threshold=1.0, today=TODAY)
    assert s["operating_point_frozen"] is True
    assert s["operating_point"]["tpr"] == 1.0 and s["false_pass"] == pytest.approx(6 / 70)
    assert s["bet_passed"] is False
    assert "frozen at 1.0000" in (tmp_path / "r.md").read_text(encoding="utf-8")
    s2 = cal.report(
        recs, reports, tmp_path / "r2.md", threshold=0.0, today=TODAY
    )  # accepts the broken ones too
    assert s2["operating_point"]["n_false_pass"] == 70 and s2["false_pass"] == 1.0


def test_report_counts_unscored_takes_and_unreadable_reports(tmp_path):
    recs, reports = build_takes(tmp_path, n_ship=6, n_clear_reject=4, n_false_pass=0)
    recs.append(label("NOREPORT", "fix"))  # no gate report anywhere
    recs.append(label("BADJSON", "ship"))
    (reports / "BADJSON.1.json").write_text("{not json", encoding="utf-8", newline="\n")
    recs.append(label("EMPTY", "ship"))
    write_gate(reports, "EMPTY.1.json", {})  # a report with no metrics
    s = cal.report(recs, reports, tmp_path / "r.md", today=TODAY)
    assert s["n_labelled"] == 13 and s["n"] == 10 and s["n_unscored"] == 3 and s["n_unreadable_reports"] == 1
    assert "could not be read" in (tmp_path / "r.md").read_text(encoding="utf-8")


def test_report_with_no_gate_reports_still_writes_a_report(tmp_path):
    s = cal.report([label("A"), label("B", "fix")], tmp_path / "nowhere", tmp_path / "r.md", today=TODAY)
    assert s["n"] == 0 and math.isnan(s["auc"]) and s["operating_point"] is None and s["bet_passed"] is False
    text = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "Not available" in text and "n/a" in text
    empty = cal.report([], None, tmp_path / "e.md", today=TODAY)
    assert empty["n_labelled"] == 0 and empty["rater"] is None and (tmp_path / "e.md").is_file()


def test_report_default_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "_REPO_CALIB", tmp_path / "calib")
    recs, reports = build_takes(tmp_path, n_ship=3, n_clear_reject=3, n_false_pass=0)
    s = cal.report(recs, reports, today=TODAY)
    assert s["path"] == tmp_path / "calib" / "report_2026-10-17.md" and s["path"].is_file()
    folder = tmp_path / "out"
    folder.mkdir()
    s = cal.report(recs, reports, folder, today=date(2026, 11, 2))
    assert s["path"] == folder / "report_2026-11-02.md"
    assert ":" not in s["path"].name


def test_report_reads_a_labels_file_and_picks_the_busiest_rater(tmp_path):
    recs, reports = build_takes(tmp_path, n_ship=6, n_clear_reject=4, n_false_pass=0)
    others = [label(r["take_id"], r["verdict"], rater="zed", reasons=r["reasons"]) for r in recs[:3]]
    path = tmp_path / "labels.jsonl"
    write_labels(path, recs + others)
    s = cal.report(path, reports, tmp_path / "r.md", today=TODAY)
    assert s["rater"] == "aasish" and s["n_labelled"] == 10
    z = cal.report(path, reports, tmp_path / "z.md", rater="zed", today=TODAY)
    assert z["rater"] == "zed" and z["n_labelled"] == 3


def test_report_inter_rater_agreement_uses_shared_takes(tmp_path):
    recs, reports = build_takes(tmp_path, n_ship=6, n_clear_reject=4, n_false_pass=0)
    second = [
        label(
            r["take_id"],
            r["verdict"] if i != 0 else "fix",
            rater="zed",
            reasons=["P1"] if i == 0 else r["reasons"],
        )
        for i, r in enumerate(recs[:8])
    ]
    s = cal.report(recs + second, reports, tmp_path / "r.md", today=TODAY)
    (agree,) = s["agreement"]
    assert agree["rater"] == "zed" and agree["n"] == 8
    mine = [r["verdict"] for r in recs[:8]]
    theirs = [r["verdict"] for r in second]
    assert agree["kappa_3class"] == pytest.approx(cal.inter_rater(mine, theirs))
    assert 0 < agree["kappa_3class"] < 1
    text = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "| zed | 8 |" in text


def test_report_uses_embedded_gate_reports_and_label_paths(tmp_path):
    reports = tmp_path / "gates"
    path = write_gate(reports, "r1.1.json", CLEAN)
    recs = [
        label("E1", "ship", gate_report=report_of(CLEAN)),  # embedded dict
        label("E2", "fix", gate_report=report_of(BROKEN)),
        label("E3", "ship", gate_report="gates/r1.1.json"),  # path as written in the manifest
    ]
    s = cal.report(recs, reports, tmp_path / "r.md", today=TODAY)
    assert s["n"] == 3 and path.is_file() and s["auc"] == 1.0


def test_main_report_command(tmp_path, capsys, monkeypatch):
    recs, reports = build_takes(tmp_path, n_ship=50, n_clear_reject=67, n_false_pass=3)
    labels_path = tmp_path / "labels.jsonl"
    write_labels(labels_path, recs)
    out = tmp_path / "r.md"
    argv = ["report", "--labels", str(labels_path), "--reports", str(reports), "--out", str(out)]
    assert cal.main(argv) == 0
    stdout = capsys.readouterr().out
    assert "bet_passed=True" in stdout and "n=120" in stdout and out.is_file()
    assert cal.main([*argv, "--threshold", "0.0", "--strict"]) == 1  # everything accepted: the bet fails
    assert cal.main(["report", "--labels", str(tmp_path / "missing.jsonl"), "--out", str(out)]) == 2
    assert "error" in capsys.readouterr().err


def test_labels_written_by_the_labeller_load_and_score(tmp_path):
    """The contract between the two tools: what tools/plate_label writes, calibration reads unchanged."""
    pairs_mod = pytest.importorskip("plate_label.pairs")
    server = pytest.importorskip("plate_label.server")
    from PIL import Image

    root = tmp_path / "run"
    reports = root / "gates"
    manifest = []
    for i, (_verdict, values) in enumerate(
        [("ship", CLEAN), ("fix", BROKEN), ("ship", CLEAN), ("render_it", BROKEN)]
    ):
        for name in (f"in{i}.png", f"out{i}.png"):
            (root / "img").mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (4, 4), (i * 40, 10, 10)).save(root / "img" / name)
        write_gate(reports, f"r{i}.1.json", values)
        manifest.append({"take_id": f"S{i}", "plate": f"img/in{i}.png", "candidate": f"img/out{i}.png",
                         "run_id": f"r{i}", "gate_report": f"gates/r{i}.1.json"})  # fmt: skip
    (root / "pairs.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    out = tmp_path / "labels.jsonl"
    app = server.LabelApp(pairs_mod.load_manifest(root / "pairs.json"), out, "aasish")
    for i, verdict in enumerate(["ship", "fix", "ship", "render_it"]):
        app.label({"index": i, "take_id": f"S{i}", "verdict": verdict,
                   "reasons": [] if verdict == "ship" else ["P1", "A2"], "seconds": 4})  # fmt: skip
    labels = cal.load_labels(out)  # no warnings: every line is a valid v1 label
    assert [r["verdict"] for r in labels] == ["ship", "fix", "ship", "render_it"]
    s = cal.report(labels, reports, tmp_path / "r.md", today=TODAY)
    assert s["n"] == 4 and s["n_rejects"] == 2 and s["auc"] == 1.0
