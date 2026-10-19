"""A one-frame clip is unscored, never a pass (BUILD_PLAN decision 90, T156).

`reprojection.camera_lock` and `video.flicker` have nothing to compare on a clip of fewer than two frames. They
used to answer 0.0 ("perfectly locked", "perfectly steady"), which the video gate scored as a pass. Now both
return `status: "unscored"` with None values, and `video_gates` puts no camera-lock number into the report and
says so in `backend_notes`. Clips are built with the ffmpeg helpers the other gate tests use; the module skips
when ffmpeg / ffprobe are missing.
"""

from __future__ import annotations

import numpy as np
import pytest

from plate_gates import io as pio
from plate_gates import reprojection, video, video_gates

pytestmark = pytest.mark.skipif(
    pio.ffmpeg() is None or pio.ffprobe() is None, reason="ffmpeg / ffprobe not found"
)

W, H = 160, 90
LOCK_NOTE = "camera lock not scored: fewer than 2 frames"
FLICKER_NOTE = "flicker not scored: fewer than 2 frames"


def _flat(level: int = 120) -> np.ndarray:
    return np.full((H, W, 3), level, np.uint8)


class _NoFlow:
    """A FlowBackend stand-in that fails the test if the gate asks it for any flow."""

    impl = "dis"

    def flow(self, a, b):  # pragma: no cover - reaching this is the failure
        raise AssertionError("camera_lock decoded and compared frames of a one-frame clip")

    def info(self):
        return {"model": "none", "impl": self.impl, "stub": True}


class _ZeroFlow:
    """A FlowBackend stand-in reporting no motion at all, under a chosen impl name."""

    def __init__(self, impl: str = "dis"):
        self.impl = impl

    def flow(self, a, b):
        u = np.zeros(a.shape[:2], np.float32)
        return u, np.zeros_like(u)

    def info(self):
        return {"model": "zero", "impl": self.impl, "stub": True}


@pytest.fixture()
def one_frame(tmp_path):
    return pio.write_clip(tmp_path / "one.mp4", [_flat()], fps=24)


@pytest.fixture()
def static_12(tmp_path):
    return pio.write_clip(tmp_path / "static12.mp4", [_flat()] * 12, fps=24)


def test_camera_lock_on_a_one_frame_clip_is_unscored_and_decodes_nothing(one_frame, monkeypatch):
    grabs: list[float] = []
    real = pio.frame_at
    monkeypatch.setattr(pio, "frame_at", lambda *a, **k: grabs.append(a[1]) or real(*a, **k))
    res = reprojection.camera_lock(one_frame, _NoFlow(), samples=8)
    assert res["status"] == "unscored"
    assert res["median_flow_px"] is None and res["p95"] is None
    assert res["per_pair"] == [] and res["samples"] == 0 and res["video_frames"] == 1
    assert res["metric_id"] == reprojection.LOCK_METRIC_ID
    assert res["backend"] == "dis" and res["backend_info"]["impl"] == "dis"
    assert res["threshold"] == 1.0 and res["threshold_status"] == "advisory"
    assert (res["width"], res["height"]) == (W, H)
    assert res["note"] == LOCK_NOTE
    assert grabs == []  # not even the first frame was decoded


def test_camera_lock_unscored_threshold_status_follows_the_backend(one_frame):
    res = reprojection.camera_lock(one_frame, _ZeroFlow("raft"))
    assert res["status"] == "unscored" and res["backend"] == "raft" and res["threshold_status"] == "hard"


def test_camera_lock_with_a_start_time_on_the_last_frame_is_unscored(static_12):
    res = reprojection.camera_lock(static_12, _NoFlow(), t0=11 / 24)  # one frame left from t0 on
    assert res["status"] == "unscored" and res["median_flow_px"] is None
    assert res["video_frames"] == 12 and res["samples"] == 0


def test_flicker_on_a_one_frame_clip_is_unscored(one_frame):
    assert video.flicker(one_frame) == {
        "mean_abs_luma_delta": None,
        "p95": None,
        "frames": 1,
        "status": "unscored",
    }


def test_the_assembled_report_has_no_camera_lock_value_and_says_why(one_frame):
    rep = video_gates.run(one_frame, _flat(), None, None, None, _NoFlow())
    metrics = rep["metrics"]
    assert reprojection.LOCK_METRIC_ID not in metrics
    assert metrics["camera_lock"]["status"] == "unscored" and metrics["camera_lock"]["median_flow_px"] is None
    assert metrics["flicker"]["status"] == "unscored" and metrics["flicker"]["mean_abs_luma_delta"] is None
    notes = rep["backend_notes"]
    assert LOCK_NOTE in notes and FLICKER_NOTE in notes
    # the lock is not among the checks, so the verdict can never rest on it
    ids = {c["metric_id"] for c in rep["checks"]}
    assert reprojection.LOCK_METRIC_ID not in ids
    assert ids == {"video.first_frame"}  # no end still, no shot duration: only the landing was measured


def test_a_moves_by_design_clip_keeps_its_own_note_not_the_lock_note(one_frame, tmp_path):
    rep = video_gates.run(one_frame, _flat(), None, None, None, _NoFlow(), expect_locked=False)
    notes = rep["backend_notes"]
    assert "camera lock not scored: the camera moves by design (motion job)" in notes
    assert LOCK_NOTE not in notes and FLICKER_NOTE in notes
    assert reprojection.LOCK_METRIC_ID not in rep["metrics"]


def test_control_a_twelve_frame_static_clip_still_scores_and_passes(static_12):
    lock = reprojection.camera_lock(static_12, _ZeroFlow(), samples=8)
    assert lock["status"] == "pass" and lock["samples"] == 8 and len(lock["per_pair"]) == 7
    assert lock["median_flow_px"] == 0.0 and "note" not in lock
    fl = video.flicker(static_12)
    assert fl["status"] == "scored" and fl["frames"] == 12 and fl["mean_abs_luma_delta"] < 0.1

    rep = video_gates.run(static_12, _flat(), None, None, None, _ZeroFlow())
    assert rep["verdict"] == "pass"
    assert rep["metrics"][reprojection.LOCK_METRIC_ID] == 0.0
    ids = {c["metric_id"]: c["status"] for c in rep["checks"]}
    assert ids == {"video.first_frame": "pass", reprojection.LOCK_METRIC_ID: "pass"}
    assert rep["metrics"]["camera_lock"]["samples"] == 8
    notes = rep["backend_notes"]
    assert LOCK_NOTE not in notes and FLICKER_NOTE not in notes
