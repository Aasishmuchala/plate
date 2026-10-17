"""reprojection.py + video_gates.py: drift through the time map, the flow camera lock, the assembled report
(router plan T29).

Clips are built from fixtures/packets/dolly_12f's clay frames through ffmpeg (25 -> 24 fps retime via the fps
filter, so the time map's fractional packet frames are exercised) and skipped when ffmpeg or the fixture is
absent. The drifted clip shifts frame j by 6 * j px (a camera sliding away from the packet path).
"""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np
import pytest

from plate_gates import io as pio
from plate_gates import reprojection as rp
from plate_gates import video_gates
from plate_gates.timemap import TimeMap
from plate_spec import project_point

ROOT = Path(__file__).resolve().parents[2]
DOLLY = ROOT / "fixtures" / "packets" / "dolly_12f"

needs_ffmpeg = pytest.mark.skipif(
    pio.ffmpeg() is None or pio.ffprobe() is None, reason="ffmpeg / ffprobe not found"
)
needs_dolly = pytest.mark.skipif(
    not (DOLLY / "manifest.json").is_file() or not (DOLLY / "motion" / "z.0000.exr").is_file(),
    reason="fixtures/packets/dolly_12f not built yet (T2)",
)


def _shift(frame: np.ndarray, dx: float) -> np.ndarray:
    m = np.float32([[1, 0, dx], [0, 1, 0]])
    return cv2.warpAffine(frame, m, (frame.shape[1], frame.shape[0]), borderMode=cv2.BORDER_REPLICATE)


@pytest.fixture(scope="module")
def dolly():
    manifest = json.loads((DOLLY / "manifest.json").read_text(encoding="utf-8"))
    clays = rp.sequence_paths(DOLLY, manifest, "clay")
    frames = [pio.to_uint8(pio.load_rgb(p)) for _, p in sorted(clays.items())]
    return manifest, clays, frames


@pytest.fixture(scope="module")
def clips(tmp_path_factory, dolly):
    """good: the 12 clay frames at 25 fps retimed to 24 by ffmpeg; bad: 6 px/frame drift; static; pan."""
    manifest, _, frames = dolly
    d = tmp_path_factory.mktemp("reproj")
    src = pio.write_clip(d / "clay25.mp4", frames, fps=25)
    good = d / "good24.mp4"
    cmd = [str(pio.ffmpeg()), "-v", "error", "-nostdin", "-y", "-i", str(src), "-vf", "fps=24"]
    cmd += ["-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv444p", str(good)]
    subprocess.run(cmd, check=True, capture_output=True)
    bad = pio.write_clip(d / "bad24.mp4", [_shift(f, 6 * i) for i, f in enumerate(frames)], fps=24)
    static = pio.write_clip(d / "static.mp4", [frames[0]] * 12, fps=24)
    pan = pio.write_clip(d / "pan.mp4", [_shift(frames[0], 3 * i) for i in range(12)], fps=24)
    return {"good": good, "bad": bad, "static": static, "pan": pan, "dir": d}


@pytest.fixture(scope="module")
def tm() -> TimeMap:
    return TimeMap(25, 24, (0, 11))


# --------------------------------------------------------------------------- readers and the warp


@needs_dolly
def test_readers_follow_the_manifest_channel_names(dolly):
    manifest, _, _ = dolly
    z = rp.read_z(DOLLY / "motion" / "z.0000.exr", manifest)
    ids = rp.read_rank0_ids(DOLLY / "motion" / "id.0000.exr", manifest)
    assert (
        z.shape == (180, 320) and z.dtype == np.float32 and ids.shape == (180, 320) and ids.dtype == np.uint32
    )
    assert float(z[np.isfinite(z) & (z < 1e9)].min()) > 1.0
    legend = json.loads((DOLLY / "id_legend.json").read_text(encoding="utf-8"))
    floor = next(e for e in legend["entries"] if e["name"] == "HARD_floor")
    assert int(floor["id"].split(":")[1], 16) in set(np.unique(ids).tolist())
    # without a manifest the suffix search finds the same channels
    assert np.array_equal(rp.read_z(DOLLY / "motion" / "z.0000.exr"), z)
    assert np.array_equal(rp.read_rank0_ids(DOLLY / "motion" / "id.0000.exr"), ids)
    seq = rp.sequence_paths(DOLLY, manifest, "z_seq")
    assert sorted(seq) == list(range(12)) and seq[11].name == "z.0011.exr"
    with pytest.raises(KeyError):
        rp.sequence_paths(DOLLY, manifest, "normals")


def test_warp_pixels_is_the_identity_on_one_camera_and_matches_project_point():
    cam_a = {
        "world_from_camera": [[1, 0, 0, 0.5], [0, 0, -1, -2.0], [0, 1, 0, 1.5], [0, 0, 0, 1]],
        "intrinsics_px": {"fx": 300.0, "fy": 300.0, "principal_point": [160.0, 90.0]},
    }
    c, s = np.cos(np.radians(12)), np.sin(np.radians(12))
    r_b = (
        np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
        @ np.asarray(cam_a["world_from_camera"])[:3, :3]
    )
    m_b = np.eye(4)
    m_b[:3, :3], m_b[:3, 3] = r_b, [0.9, -1.6, 1.4]  # camera A yawed 12 deg and moved a little
    cam_b = {
        "world_from_camera": m_b.tolist(),
        "intrinsics_px": {"fx": 320.0, "fy": 320.0, "principal_point": [160.0, 90.0]},
    }
    us = np.array([10, 160, 300]), np.array([5, 90, 170])
    z = np.array([2.0, 3.5, 5.0])
    u, v, d = rp.warp_pixels(us[0], us[1], z, cam_a, cam_a)
    assert np.allclose(u, us[0] + 0.5, atol=1e-9) and np.allclose(v, us[1] + 0.5, atol=1e-9)
    assert np.allclose(d, z)
    u2, v2, d2 = rp.warp_pixels(us[0], us[1], z, cam_a, cam_b)
    m = np.asarray(cam_a["world_from_camera"], float)
    for i in range(3):
        x = (us[0][i] + 0.5 - 160.0) / 300.0 * z[i]
        y = -(us[1][i] + 0.5 - 90.0) / 300.0 * z[i]
        pw = m @ np.array([x, y, -z[i], 1.0])
        pu, pv, pd = project_point(cam_b, pw[:3])
        assert (u2[i], v2[i], d2[i]) == pytest.approx((pu, pv, pd), abs=1e-6)


@needs_dolly
def test_reference_edges_weight_by_clay_visibility_and_keep_the_edge_map(dolly):
    manifest, clays, _ = dolly
    z = rp.read_z(DOLLY / "motion" / "z.0000.exr", manifest)
    ids = rp.read_rank0_ids(DOLLY / "motion" / "id.0000.exr", manifest)
    emap, w, info = rp.reference_edges(z, ids, manifest["camera"], rp.read_clay(clays[0]))
    assert emap.shape == z.shape and w.shape == z.shape and w.dtype == np.float32
    assert emap.counts()["normal"] == 0 and emap.counts()["depth"] > 0 and emap.counts()["id"] > 0
    assert info["visibility_weighted"] is True and info["coc_weighted"] is False  # dolly has DOF off
    assert 0.5 < info["plate_visibility"] < 1.0
    assert set(np.unique(w).tolist()) <= {0.0, 1.0}
    _, w2, info2 = rp.reference_edges(z, ids, manifest["camera"], None)
    assert info2["visibility_weighted"] is False and float(w2.min()) == 1.0


# --------------------------------------------------------------------------- drift


@needs_ffmpeg
@needs_dolly
def test_drift_on_the_retimed_clay_clip_passes(clips, tm):
    res = rp.drift(clips["good"], DOLLY, None, None, None, tm, samples=8)
    assert res["min_recall"] >= 0.8 and res["verdict"] == "pass" and res["status"] == "pass"
    assert res["threshold"] == 0.8 and res["metric_id"] == "video.reproj_edge_recall"
    assert len(res["per_frame"]) == 8 and res["samples"] == 8
    frames = [f["engine_frame"] for f in res["per_frame"]]
    assert frames[0] == 0 and frames[-1] == 11 and frames == sorted(frames)
    for f in res["per_frame"]:
        assert f["edge_recall_r2"] >= 0.8
        assert (
            f["median_flow_px"] <= 2.0
        )  # 1 px is the aligned baseline (Canny sits beside the G-buffer step)
        assert f["camera_delta_px"] < 1.0  # at most half a packet frame of dolly travel
        assert f["packet_frame"] == pytest.approx(f["engine_frame"] * 25 / 24)
        assert f["nearest_packet_frame"] == min(round(f["packet_frame"]), 11)
        assert f["n_ref_weighted"] > 0.5 * f["n_ref"]
    assert res["per_frame"][-1]["clamped"] is True and res["per_frame"][0]["clamped"] is False
    assert res["visibility_weighted"] is True and res["coc_weighted"] is False
    assert res["edge_kinds"] == ["depth", "id"]
    assert res["width"] == 320 and res["packet_width"] == 320 and res["video_frames"] == 12
    assert res["timemap"] == tm.to_json()
    assert "clay visibility" in res["backend"] and "@320" in res["backend"]
    assert any("working width 320" in n for n in res["notes"])


@needs_ffmpeg
@needs_dolly
def test_drift_with_six_px_per_frame_injected_fails(clips, tm):
    res = rp.drift(clips["bad"], DOLLY, None, None, None, tm, samples=8)
    assert res["min_recall"] < 0.8 and res["verdict"] == "fail" and res["status"] == "fail"
    first, later = res["per_frame"][0], res["per_frame"][1:]
    assert first["edge_recall_r2"] >= 0.8  # frame 0 carries no drift yet
    assert all(f["edge_recall_r2"] < 0.6 for f in later)
    dists = [f["median_flow_px"] for f in later]
    assert (
        dists[0] >= 2.5 and dists[-1] > dists[0]
    )  # the distance to the nearest clip edge grows with the drift
    assert all(f["shift_px"][0] < 0 for f in later)  # best_shift points back toward the packet (dx < 0)
    assert res["mean_recall"] < 0.6


@needs_ffmpeg
@needs_dolly
def test_drift_accepts_explicit_sequences_with_a_manifest_mapping(clips, tm, dolly):
    manifest, clays, _ = dolly
    zs = rp.sequence_paths(DOLLY, manifest, "z")
    ids = rp.sequence_paths(DOLLY, manifest, "id")
    cams = rp.load_camera_frames(DOLLY, manifest)
    by_mapping = rp.drift(clips["good"], manifest, zs, ids, cams, tm, samples=4, clay_seq=clays)
    as_lists = rp.drift(
        clips["good"],
        manifest,
        [zs[n] for n in range(12)],
        [ids[n] for n in range(12)],
        cams["frames"],
        tm,
        samples=4,
        clay_seq=[clays[n] for n in range(12)],
    )
    by_folder = rp.drift(clips["good"], DOLLY, None, None, None, tm, samples=4)
    assert by_mapping["min_recall"] == pytest.approx(by_folder["min_recall"], abs=1e-9)
    assert as_lists["min_recall"] == pytest.approx(by_folder["min_recall"], abs=1e-9)
    assert len(by_folder["per_frame"]) == 4
    with pytest.raises(ValueError):
        rp.drift(clips["good"], manifest, None, None, cams, tm)
    with pytest.raises(ValueError):
        rp.drift(clips["good"], manifest, zs, ids, None, tm)


@needs_ffmpeg
@needs_dolly
def test_drift_unweighted_counts_edges_the_clay_cannot_show(clips, tm):
    weighted = rp.drift(clips["good"], DOLLY, None, None, None, tm, samples=3)
    plain = rp.drift(clips["good"], DOLLY, None, None, None, tm, samples=3, use_clay=False)
    assert plain["visibility_weighted"] is False
    assert any("unweighted" in n for n in plain["notes"])
    assert plain["min_recall"] < weighted["min_recall"]
    assert plain["per_frame"][0]["n_ref_weighted"] == plain["per_frame"][0]["n_ref"]


@needs_ffmpeg
@needs_dolly
def test_drift_samples_only_the_frames_the_time_map_covers(clips, tm):
    short = TimeMap(25, 24, (0, 5))  # the clip has 12 frames, the map covers 6
    res = rp.drift(clips["good"], DOLLY, None, None, None, short, samples=8)
    assert res["engine_frames_used"] == 6 and res["samples"] == 6
    assert [f["engine_frame"] for f in res["per_frame"]] == list(range(6))
    assert res["notes"] == [n for n in res["notes"] if "clip has" not in n]  # the clip is longer, not shorter
    longer = TimeMap(25, 24, (0, 24))  # 25 packet frames -> 24 engine frames; the clip has 12
    res2 = rp.drift(clips["good"], DOLLY, None, None, None, longer, samples=4)
    assert res2["engine_frames_used"] == 12 and any("clip has 12 frames" in n for n in res2["notes"])


# --------------------------------------------------------------------------- camera lock


class _ConstFlow:
    """A FlowBackend stand-in with a fixed horizontal flow and a chosen impl name."""

    def __init__(self, px: float, impl: str):
        self.px, self.impl = px, impl

    def flow(self, a, b):
        u = np.full(a.shape[:2], self.px, np.float32)
        return u, np.zeros_like(u)

    def info(self):
        return {"model": "fake", "impl": self.impl, "stub": True}


@needs_ffmpeg
@needs_dolly
def test_camera_lock_on_a_static_clip_is_zero_with_dis(clips):
    from plate_pixels.flow import DisFlow

    res = rp.camera_lock(clips["static"], DisFlow(), width=960, samples=8)
    assert res["median_flow_px"] == pytest.approx(0.0, abs=0.3) and res["p95"] == pytest.approx(0.0, abs=0.5)
    assert res["backend"] == "dis" and res["status"] == "pass" and res["threshold_status"] == "advisory"
    assert res["threshold"] == 1.0 and res["metric_id"] == "video.camera_lock_median_flow_px"
    assert res["width"] == 320 and res["samples"] == 8 and len(res["per_pair"]) == 7
    assert res["backend_info"]["impl"] == "dis" and res["backend_info"]["stub"] is False
    default = rp.camera_lock(clips["static"], None, samples=3)  # None -> DisFlow
    assert default["backend"] == "dis" and default["median_flow_px"] == pytest.approx(0.0, abs=0.3)


@needs_ffmpeg
@needs_dolly
def test_camera_lock_tracks_a_pan_and_reports_the_worst_sampled_moment(clips):
    from plate_pixels.flow import DisFlow

    res = rp.camera_lock(clips["pan"], DisFlow(), width=960, samples=8)
    last = res["per_pair"][-1]
    assert last["engine_frame"] == 11 and last["median"] == pytest.approx(33.0, abs=0.5)  # 3 px/frame * 11
    medians = [p["median"] for p in res["per_pair"]]
    assert medians == sorted(medians)
    assert (
        res["median_flow_px"] == pytest.approx(max(medians)) and res["status"] == "advisory"
    )  # dis: never hard


@needs_ffmpeg
@needs_dolly
def test_camera_lock_is_hard_on_raft_and_advisory_on_dis(clips):
    hard = rp.camera_lock(clips["static"], _ConstFlow(2.0, "raft"), samples=2)
    assert hard["status"] == "fail" and hard["threshold_status"] == "hard" and hard["backend"] == "raft"
    soft = rp.camera_lock(clips["static"], _ConstFlow(2.0, "dis"), samples=2)
    assert soft["status"] == "advisory" and soft["threshold_status"] == "advisory"
    ok = rp.camera_lock(clips["static"], _ConstFlow(0.5, "raft"), samples=2)
    assert ok["status"] == "pass" and ok["median_flow_px"] == 0.5
    assert rp.backend_name(_ConstFlow(0, "stub")) == "stub" and rp.backend_name(object()) == "object"


# --------------------------------------------------------------------------- the assembled report


@needs_ffmpeg
@needs_dolly
def test_video_gates_run_assembles_a_motion_report(clips, tm, dolly):
    from plate_pixels.flow import DisFlow

    _, clays, _ = dolly
    rep = video_gates.run(
        clips["good"],
        clays[0],
        clays[11],
        DOLLY,
        tm,
        DisFlow(),
        engine={"id": "kling-o3-4k", "version": "fx"},
    )
    ids = {c["metric_id"]: c["status"] for c in rep["checks"]}
    assert set(ids) == {
        "video.first_frame",
        "video.last_frame",
        "video.reproj_edge_recall",
        "video.duration_s",
    }
    assert rep["verdict"] == "pass" and all(s == "pass" for s in ids.values())
    assert rep["engine"] == {"id": "kling-o3-4k", "version": "fx"} and rep["tier"] == "A"
    assert rep["threshold_set_id"] == "ts-2026.10.17-draft" and rep["gate_version"] == "0.1.0"
    m = rep["metrics"]
    assert m["video.first_frame"] < 3 and m["video.last_frame"] < 3
    assert m["video.reproj_edge_recall"] >= 0.8 and m["video.duration_s"] == pytest.approx(0.5, abs=0.01)
    assert m["timemap"] == tm.to_json() and m["drift"]["timemap"] == tm.to_json()
    assert m["camera_lock"]["backend"] == "dis" and "video.camera_lock_median_flow_px" not in m
    assert m["output"] == {
        "fps": "24/1",
        "fps_float": 24.0,
        "width": 320,
        "height": 180,
        "codec": "h264",
        "pix_fmt": "yuv444p",
        "bit_depth": 8,
        "nb_frames": 12,
        "duration_s": pytest.approx(0.5, abs=0.01),
    }
    assert m["duration"]["required_s"] == 0.48 and m["duration"]["ok"] is True
    assert m["flicker"]["frames"] == 12
    notes = rep["backend_notes"]
    assert "flow backend dis @320" in notes
    assert any(n.startswith("drift reference: canny-rgb") for n in notes)
    assert any("camera lock not scored" in n for n in notes)
    assert tm.describe() in notes
    assert rep["created_utc"].endswith("Z") and rep["metric_code_hash"]


@needs_ffmpeg
@needs_dolly
def test_video_gates_run_scores_the_lock_on_a_locked_clip_without_a_packet(clips, dolly):
    from plate_pixels.flow import DisFlow

    _, _, frames = dolly
    rep = video_gates.run(clips["static"], frames[0], None, None, None, DisFlow(), shot_duration_s=0.4)
    ids = {c["metric_id"]: c["status"] for c in rep["checks"]}
    assert set(ids) == {"video.first_frame", "video.camera_lock_median_flow_px", "video.duration_s"}
    assert rep["verdict"] == "pass"
    assert rep["metrics"]["video.camera_lock_median_flow_px"] == pytest.approx(0.0, abs=0.3)
    assert "drift" not in rep["metrics"] and "timemap" not in rep["metrics"]
    notes = rep["backend_notes"]
    assert any("drift skipped" in n for n in notes) and any("no end still" in n for n in notes)
    assert any("advisory on dis" in n for n in notes)
    # the hard-on-raft path fails the report when a fake raft reports 2 px
    bad = video_gates.run(clips["static"], frames[0], None, None, None, _ConstFlow(2.0, "raft"))
    assert bad["verdict"] == "fail"
    assert any("no shot duration" in n for n in bad["backend_notes"])
    assert "video.duration_s" not in bad["metrics"] and bad["metrics"]["duration"]["required_s"] is None


@needs_ffmpeg
@needs_dolly
def test_video_gates_builds_the_time_map_from_the_packet_and_the_clip(clips, dolly):
    _, clays, _ = dolly
    rep = video_gates.run(clips["good"], clays[0], None, DOLLY, None, _ConstFlow(0.0, "dis"), samples=3)
    assert rep["metrics"]["timemap"] == TimeMap(25, 24, (0, 11)).to_json()
    assert any("time map built from the packet fps" in n for n in rep["backend_notes"])
    assert rep["metrics"]["video.reproj_edge_recall"] >= 0.8
    # expect_locked overrides the default on a motion job and scores the lock
    forced = video_gates.run(
        clips["good"], clays[0], None, DOLLY, None, _ConstFlow(0.0, "dis"), samples=3, expect_locked=True
    )
    assert "video.camera_lock_median_flow_px" in forced["metrics"]


def test_bit_depth_from_pix_fmt():
    assert rp._bit_depth("yuv420p") == 8 and rp._bit_depth("yuv444p") == 8 and rp._bit_depth("rgb24") == 8
    assert rp._bit_depth("yuv420p10le") == 10 and rp._bit_depth("gbrp12le") == 12
    assert (
        rp._bit_depth("yuv422p10be") == 10 and rp._bit_depth("rgb48le") == 16 and rp._bit_depth("gray") == 8
    )
    assert rp._bit_depth("gbrapf32le") is None and rp._bit_depth(None) is None


def test_build_timemap_reads_the_manifest_frames():
    manifest = {"camera": {"frames": {"hero": 0, "start": 0, "end": 124, "fps": 25}}}
    tm = video_gates.build_timemap(manifest, Fraction(24))
    assert tm.engine_frames == 120 and tm.packet_frames == (0, 124)
