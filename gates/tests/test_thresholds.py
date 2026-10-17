"""thresholds.yaml: one entry per metric, the Decision-14 numbers with the scout corrections, and check()."""

from __future__ import annotations

import math

import pytest
import yaml

from plate_gates import thresholds
from plate_gates.thresholds import Threshold

SPEC_IDS = {
    "registration.residual_px",
    "registration.shift_px",
    "outside_mask.de00",
    "feather_band.de00_p99",
    "gbuffer_edge_recall_r2",
    "invention_flags",
    "material.de00_median",
    "material.de00_p90",
    "ral.de00",
    "texture.lapvar",
    "texture.fine_ratio",
    "tone.luma",
    "tone.contrast",
    "tone.sat",
    "video.first_frame",
    "video.last_frame",
    "video.camera_lock_median_flow_px",
    "video.phase_corr_px",
    "video.reproj_edge_recall",
    "video.duration_s",
    "image_edge_agreement",
    "long_edge_min_px",
}


def test_set_id_and_every_spec_metric_is_present_exactly_once():
    ts = thresholds.load()
    assert ts.id == "ts-2026.10.17-draft" == thresholds.DEFAULT_THRESHOLDS_ID
    assert set(ts.metrics) == SPEC_IDS
    raw = yaml.safe_load(thresholds._YAML.read_text(encoding="utf-8"))
    assert [e["id"] for e in raw["metrics"]] == list(ts.metrics)  # no duplicates swallowed by the dict


def test_every_entry_has_provenance_and_a_known_status():
    for thr in thresholds.load().metrics.values():
        assert thr.provenance.strip(), thr.id
        assert thr.status in {"hard", "advisory", "calibration_needed"}


@pytest.mark.parametrize(
    "metric_id,status",
    [
        ("registration.residual_px", "hard"),
        ("registration.shift_px", "hard"),
        ("outside_mask.de00", "hard"),
        ("feather_band.de00_p99", "hard"),
        ("gbuffer_edge_recall_r2", "hard"),
        ("invention_flags", "hard"),
        ("material.de00_median", "hard"),
        ("material.de00_p90", "hard"),
        ("ral.de00", "hard"),
        ("texture.lapvar", "hard"),
        ("texture.fine_ratio", "calibration_needed"),
        ("tone.luma", "advisory"),
        ("tone.contrast", "advisory"),
        ("tone.sat", "advisory"),
        ("video.first_frame", "calibration_needed"),
        ("video.last_frame", "calibration_needed"),
        ("video.phase_corr_px", "advisory"),
        ("video.reproj_edge_recall", "hard"),
        ("video.duration_s", "hard"),
        ("image_edge_agreement", "advisory"),
    ],
)
def test_statuses_follow_the_plan(metric_id, status):
    assert thresholds.load().get(metric_id).status == status


def test_numbers_follow_the_plan_with_scout_corrections():
    g = thresholds.load().get
    assert g("registration.residual_px").value == 1.0 and g("registration.shift_px").value == 8
    assert g("gbuffer_edge_recall_r2").variants == {"delivery": 0.90, "preview": 0.85}
    assert g("gbuffer_edge_recall_r2").resolution_px == 2048 and g("gbuffer_edge_recall_r2").tier == "A"
    assert g("texture.lapvar").range == (300, 600) and g("texture.lapvar").resolution_px == 3840
    assert g("texture.fine_ratio").range == (0.40, 0.60)  # the plan's 0.35-0.45 contradicts the formula
    assert (g("tone.luma").range, g("tone.contrast").range, g("tone.sat").range) == (
        (100, 120),
        (52, 60),
        (55, 65),
    )
    assert g("video.first_frame").value == 12 and g("video.last_frame").value == 16
    assert g("video.camera_lock_median_flow_px").resolution_px == 960
    assert g("video.reproj_edge_recall").value == 0.8 and g("image_edge_agreement").value == 0.88
    assert g("material.de00_median").value == 2 and g("material.de00_p90").value == 5
    assert g("ral.de00").value == 1 and g("feather_band.de00_p99").value == 1.0


def test_hard_miss_fails_and_pass_passes():
    assert thresholds.check("registration.residual_px", 0.4)[0] == "pass"
    assert thresholds.check("registration.residual_px", 1.4)[0] == "fail"
    assert thresholds.check("texture.lapvar", 412.0)[0] == "pass"
    assert thresholds.check("texture.lapvar", 3500.0)[0] == "fail"
    assert thresholds.check("texture.lapvar", 299.9)[0] == "fail"
    assert thresholds.check("texture.lapvar", 300.0)[0] == "pass"  # inclusive bounds


def test_advisory_and_calibration_misses_never_fail():
    assert thresholds.check("texture.fine_ratio", 0.9)[0] == "advisory"
    assert thresholds.check("texture.fine_ratio", 0.5)[0] == "pass"
    assert thresholds.check("video.first_frame", 20)[0] == "advisory"
    assert thresholds.check("video.first_frame", 10.0)[0] == "pass"  # an accepted clip (car S1_take1)
    assert thresholds.check("video.last_frame", 14.2)[0] == "pass"  # an accepted clip (couple AER_A)
    assert thresholds.check("image_edge_agreement", 0.76)[0] == "advisory"
    assert thresholds.check("tone.luma", 90)[0] == "advisory"


def test_eq_metrics_are_exact_by_default():
    assert thresholds.check("outside_mask.de00", 0.0)[0] == "pass"
    assert thresholds.check("outside_mask.de00", 1e-9)[0] == "fail"
    assert thresholds.check("invention_flags", 0)[0] == "pass"
    assert thresholds.check("invention_flags", 1)[0] == "fail"


def test_gbuffer_variants():
    assert thresholds.check("gbuffer_edge_recall_r2", 0.87)[0] == "fail"  # delivery bound 0.90
    assert thresholds.check("gbuffer_edge_recall_r2", 0.87, variant="preview")[0] == "pass"
    assert thresholds.check("gbuffer_edge_recall_r2", 0.84, variant="preview")[0] == "fail"
    with pytest.raises(KeyError):
        thresholds.check("gbuffer_edge_recall_r2", 0.9, variant="nonsense")


def test_camera_lock_status_depends_on_the_flow_backend():
    assert thresholds.check("video.camera_lock_median_flow_px", 2.0, backend="raft")[0] == "fail"
    assert thresholds.check("video.camera_lock_median_flow_px", 2.0, backend="dis")[0] == "advisory"
    assert (
        thresholds.check("video.camera_lock_median_flow_px", 2.0)[0] == "advisory"
    )  # unknown backend is never hard
    assert thresholds.check("video.camera_lock_median_flow_px", 0.7, backend="raft")[0] == "pass"


def test_duration_bound_is_supplied_at_check_time():
    assert thresholds.check("video.duration_s", 5.04, target=5.0)[0] == "pass"
    assert thresholds.check("video.duration_s", 4.5, target=5.0)[0] == "fail"
    with pytest.raises(ValueError):
        thresholds.check("video.duration_s", 5.0)


def test_nan_never_passes_and_unknown_ids_and_params_raise():
    assert thresholds.check("registration.shift_px", math.nan)[0] == "fail"
    with pytest.raises(KeyError):
        thresholds.check("no.such.metric", 1.0)
    with pytest.raises(ValueError):
        thresholds.check("long_edge_min_px", 30)  # a parameter, not a metric


def test_long_edge_min_px_scales_with_width():
    assert thresholds.long_edge_min_px(1500) == 30
    assert thresholds.long_edge_min_px(640) == 13
    assert thresholds.long_edge_min_px(2048) == 41  # 'about 41 at 2048' in the scout
    assert thresholds.long_edge_min_px(3840) == 77
    assert thresholds.long_edge_min_px(1) == 1
    assert thresholds.load().long_edge_min_px(960) == round(30 * 960 / 1500)


def test_threshold_model_rejects_malformed_entries():
    base = {"id": "x", "status": "hard", "provenance": "test"}
    with pytest.raises(ValueError):
        Threshold(**base)  # no bound at all
    with pytest.raises(ValueError):
        Threshold(**base, op="le")  # op without value or param
    with pytest.raises(ValueError):
        Threshold(**base, range=(5, 1))
    with pytest.raises(ValueError):
        Threshold(**base, range=(1, 5), value=3)
    with pytest.raises(ValueError):
        Threshold(**base, op="le", value=1, bogus_field=2)


def test_load_is_cached_and_immutable_data():
    assert thresholds.load() is thresholds.load()
