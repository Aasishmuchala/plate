"""Wave 8 report shape (router plan T60): every check says what it was held to, the report lists the thresholds
it scored with their registered status, `flagged_points` is a typed list, and the invention gate gives its
skip reasons per tier."""

from __future__ import annotations

import json

import pytest
from fixtures.synthetic import make_pair

from plate_gates import gbuffer, invention, thresholds
from plate_gates.gbuffer import EdgeParams
from plate_gates.report import Check, FlaggedPoint, GateReport, ThresholdRef, read, scored_check, write


def _score(values, **kw):
    return GateReport.score(values, tier="A", engine={"id": "nano-banana-2.1"}, **kw)


# ---------------------------------------------------------------------------- describe


def test_describe_gives_the_bound_op_and_unit():
    ts = thresholds.load()
    assert ts.describe("registration.residual_px") == {"threshold": 1.0, "op": "le", "unit": "px"}
    assert ts.describe("texture.lapvar") == {"threshold": [300.0, 600.0], "op": "range", "unit": "variance"}
    assert ts.describe("outside_mask.de00")["op"] == "eq"
    assert ts.describe("gbuffer_edge_recall_r2", variant="preview")["threshold"] == 0.85
    assert ts.describe("gbuffer_edge_recall_r2")["threshold"] == 0.90
    assert ts.describe("video.duration_s", target=5.0) == {"threshold": 5.0, "op": "ge", "unit": "s"}
    assert ts.describe("video.duration_s")["threshold"] is None  # the bound is supplied at check time
    assert thresholds.describe("registration.shift_px") == ts.describe("registration.shift_px")
    with pytest.raises(KeyError):
        ts.describe("made.up")


def test_every_registered_metric_names_a_unit():
    ts = thresholds.load()
    for mid, thr in ts.metrics.items():
        if thr.op == "param":
            continue
        assert thr.describe()["unit"], mid  # yaml unit, else thresholds.DEFAULT_UNITS
        assert thr.describe()["op"], mid


# ---------------------------------------------------------------------------- checks carry their bound


def test_checks_carry_threshold_op_and_unit():
    rep = _score({"registration.residual_px": 0.4, "texture.lapvar": 412.0, "outside_mask.de00": 0.0})
    by = {c.metric_id: c for c in rep.checks}
    assert (by["registration.residual_px"].threshold, by["registration.residual_px"].unit) == (1.0, "px")
    assert by["registration.residual_px"].op == "le"
    assert by["texture.lapvar"].threshold == [300.0, 600.0] and by["texture.lapvar"].op == "range"
    assert by["outside_mask.de00"].threshold == 0.0 and by["outside_mask.de00"].unit == "dE00"
    dumped = rep.model_dump(mode="json")["checks"][0]
    assert {"threshold", "op", "unit"} <= set(dumped)


def test_variants_and_targets_change_the_reported_bound():
    r = _score(
        {"gbuffer_edge_recall_r2": 0.87, "video.duration_s": 6.0},
        variants={"gbuffer_edge_recall_r2": "preview"},
        targets={"video.duration_s": 5.0},
    )
    by = {c.metric_id: c for c in r.checks}
    assert by["gbuffer_edge_recall_r2"].threshold == 0.85 and by["video.duration_s"].threshold == 5.0
    assert r.thresholds["gbuffer_edge_recall_r2"].value == 0.85  # the report lists the bound it used
    assert r.thresholds["video.duration_s"].value == 5.0 and r.thresholds["video.duration_s"].unit == "s"


def test_an_unregistered_id_leaves_the_fields_null():
    rep = GateReport(checks=[Check(metric_id="light.same_camera", value=1.0, status="pass")])
    c = rep.checks[0]
    assert (c.threshold, c.op, c.unit) == (None, None, None) and rep.thresholds == {}
    assert scored_check(thresholds.load(), "ral.de00", 0.5).threshold == 1.0


def test_a_check_built_without_a_bound_gets_it_from_the_committed_set():
    rep = GateReport(checks=[Check(metric_id="ral.de00", value=4.0, status="fail")])
    c = rep.checks[0]
    assert (c.threshold, c.op, c.unit) == (1.0, "le", "dE00") and rep.verdict == "fail"
    assert rep.thresholds["ral.de00"].value == 1.0
    # a variant or param entry is never guessed
    g = GateReport(checks=[Check(metric_id="gbuffer_edge_recall_r2", value=0.87, status="pass")])
    assert g.checks[0].threshold is None and "gbuffer_edge_recall_r2" not in g.thresholds
    # nor is a report scored against some other set
    other = GateReport(
        threshold_set_id="ts-1999-other", checks=[Check(metric_id="ral.de00", value=4.0, status="fail")]
    )
    assert other.checks[0].threshold is None and other.thresholds == {}


def test_a_forced_status_is_not_the_thresholds_status(monkeypatch):
    """The feather band is forced advisory by the gate wiring while its threshold keeps the registered status."""
    ts = thresholds.load()
    hard = ts.model_copy(deep=True)
    hard.metrics["feather_band.de00_p99"] = hard.metrics["feather_band.de00_p99"].model_copy(
        update={"status": "hard"}
    )
    monkeypatch.setattr(thresholds, "load", lambda: hard)
    c = scored_check(hard, "feather_band.de00_p99", 3.2, status="advisory")
    assert c.status == "advisory" and c.threshold == 1.0
    rep = GateReport(checks=[c], threshold_set_id=hard.id)
    assert rep.thresholds["feather_band.de00_p99"].status == "hard"
    assert rep.checks[0].status == "advisory" and rep.verdict == "advisory"
    # unforced, the same value fails a hard threshold
    assert scored_check(hard, "feather_band.de00_p99", 3.2).status == "fail"


# ---------------------------------------------------------------------------- thresholds block


def test_thresholds_block_lists_what_the_report_scored():
    rep = _score({"registration.residual_px": 0.4, "texture.lapvar": 412.0, "texture.fine_ratio": 0.5})
    ts = thresholds.load()
    assert set(rep.thresholds) == {"registration.residual_px", "texture.lapvar", "texture.fine_ratio"}
    r = rep.thresholds["registration.residual_px"]
    assert isinstance(r, ThresholdRef)
    assert (r.op, r.value, r.range, r.unit, r.status) == ("le", 1.0, None, "px", "hard")
    t = rep.thresholds["texture.lapvar"]
    assert (t.op, t.value, t.range, t.resolution_px, t.status) == (
        "range",
        None,
        [300.0, 600.0],
        3840,
        "hard",
    )
    assert rep.thresholds["texture.fine_ratio"].status == ts.get("texture.fine_ratio").status
    dumped = rep.model_dump(mode="json")["thresholds"]["registration.residual_px"]
    assert set(dumped) == {"op", "value", "range", "unit", "status", "resolution_px"}


def test_backend_decides_the_listed_status():
    dis = _score({"video.camera_lock_median_flow_px": 0.8}, backend="dis")
    raft = _score({"video.camera_lock_median_flow_px": 0.8}, backend="raft")
    assert dis.thresholds["video.camera_lock_median_flow_px"].status == "advisory"
    assert raft.thresholds["video.camera_lock_median_flow_px"].status == "hard"


def test_report_round_trips_through_its_file(tmp_path):
    rep = _score({"registration.residual_px": 0.4, "texture.lapvar": 412.0})
    rep.flagged_points.append(FlaggedPoint(x=10, y=20, w=128, h=64, metric_id="material.de00_p90", note="n"))
    back = read(write(rep, tmp_path, "r1"))
    assert back.thresholds == rep.thresholds and back.flagged_points == rep.flagged_points
    assert back.checks == rep.checks
    data = json.loads(rep.to_json())
    assert data["flagged_points"] == [
        {"x": 10, "y": 20, "w": 128, "h": 64, "metric_id": "material.de00_p90", "note": "n"}
    ]


def test_an_old_report_without_the_new_fields_still_loads():
    old = _score({"registration.residual_px": 0.4}).model_dump(mode="json")
    for k in ("thresholds", "flagged_points"):
        old.pop(k)
    for c in old["checks"]:
        for k in ("threshold", "op", "unit"):
            c.pop(k)
    rep = GateReport.model_validate(old)
    assert rep.flagged_points == [] and rep.checks[0].status == "pass"


# ---------------------------------------------------------------------------- invention skip reasons


@pytest.fixture(scope="module")
def window():
    ref, test, mask, exp = make_pair("window_added", (256, 256))
    scene = exp["scene"]
    edges = gbuffer.structural_edges(*scene[:4], EdgeParams(width=None))
    return test, edges, scene.ids, exp


def _legend(exp, *, planar=True):
    return {
        "entries": [
            {
                "id": exp["wall_id"],
                "kind": "object",
                "name": "wall",
                "class": "architecture",
                "planar": planar,
            },
            {"id": exp["box_id"], "kind": "object", "name": "box", "class": "architecture", "planar": planar},
        ]
    }


def test_tier_a_legend_without_planar_ids_says_so(window):
    test, edges, ids, exp = window
    rep = invention.report(test, edges, ids, _legend(exp, planar=False), tier="A")
    assert rep["skipped"] is True and rep["count"] is None and rep["status"] == "advisory"
    assert rep["reason"] == invention.NO_PLANAR_REASON == "tier A: legend marks no planar ids"
    assert invention.report(test, edges, ids, None, tier="A")["reason"] == invention.NO_PLANAR_REASON
    assert (
        invention.report(test, edges, ids, {"entries": []}, tier="A")["reason"] == invention.NO_PLANAR_REASON
    )


def test_tier_c_keeps_its_reason_and_a_scanned_report_has_none(window):
    test, edges, ids, exp = window
    c = invention.report(test, edges, ids, _legend(exp, planar=False), tier="C")
    assert c["reason"] == invention.TIER_C_REASON == "tier C: no planar ids"
    scanned = invention.report(test, edges, ids, _legend(exp), tier="A")
    assert scanned["skipped"] is False and scanned["reason"] is None and scanned["count"] == 1


def test_tier_a_without_edges_is_not_blamed_on_tier_c(window):
    test, edges, ids, exp = window
    rep = invention.report(test, None, None, _legend(exp), tier="A")
    assert rep["skipped"] is True and rep["reason"] == invention.NO_GBUFFER_REASON
