"""timemap.py: the packet <-> engine frame-rate map and the camera at a fractional packet frame (router plan T29).

Pure arithmetic and scipy slerp; no clips, no fixtures.
"""

from __future__ import annotations

import math
from fractions import Fraction

import numpy as np
import pytest

from plate_gates import timemap
from plate_gates.timemap import TimeMap, as_fps, camera_at, interpolate_pose, intrinsics_for
from plate_spec import project_point


def _rz(deg: float, t=(0.0, 1.0, 2.0)) -> list[list[float]]:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [[c, -s, 0.0, t[0]], [s, c, 0.0, t[1]], [0.0, 0.0, 1.0, t[2]], [0.0, 0.0, 0.0, 1.0]]


def _yaw_deg(m) -> float:
    m = np.asarray(m)
    return math.degrees(math.atan2(m[1, 0], m[0, 0]))


# --------------------------------------------------------------------------- the map


def test_25_to_24_over_125_frames_gives_120_engine_frames_and_the_plan_number():
    tm = TimeMap(25, 24, (0, 124))
    assert tm.engine_frames == 120
    assert tm.duration_s == Fraction(5)
    assert tm.packet_frame_for(119) == pytest.approx(123.96, abs=0.01)
    assert tm.packet_frame_exact(119) == Fraction(2975, 24)
    assert tm.packet_frame_for(0) == 0.0
    assert tm.engine_frame_for(123.9583333) == pytest.approx(119.0, abs=1e-5)
    assert tm.engine_frame_for(0) == 0.0
    assert tm.method == "nearest"


def test_fractions_are_exact_and_fps_text_parses_back():
    tm = TimeMap("24000/1001", 24, (10, 129))
    assert tm.packet_fps == Fraction(24000, 1001) and tm.engine_fps == Fraction(24)
    assert tm.n_packet_frames == 120
    assert tm.duration_s == Fraction(120 * 1001, 24000)
    assert tm.engine_frames == 120  # 5.005 s * 24 = 120.12 -> 120
    assert as_fps(23.976) == Fraction(2997, 125)
    assert as_fps(Fraction(30)) == Fraction(30)
    assert timemap.fps_text(Fraction(24000, 1001)) == "24000/1001"
    with pytest.raises(ValueError):
        as_fps(0)
    with pytest.raises(TypeError):
        as_fps(None)


def test_nearest_packet_frame_rounds_and_clamps():
    tm = TimeMap(25, 24, (0, 11))
    assert tm.engine_frames == 12  # 0.48 s * 24 = 11.52 -> 12
    assert [tm.nearest_packet_frame(j) for j in range(12)] == list(range(12))
    assert tm.packet_frame_for(11) == pytest.approx(11.4583, abs=1e-4)
    assert tm.clamp(tm.packet_frame_for(11)) == (11.0, True)
    assert tm.clamp(5.2) == (5.2, False)
    assert tm.covers(10) and not tm.covers(11)
    assert tm.nearest_packet_frame(40) == 11  # far past the packet: holds the last frame
    assert tm.engine_time_s(12) == 0.5
    assert tm.packet_time_s(5) == 0.2


def test_half_way_rounds_up():
    tm = TimeMap(2, 1, (0, 3))  # packet frame k = 2 j: j = 0.25 -> k 0.5
    assert tm.nearest_packet_frame(0) == 0
    tm2 = TimeMap(3, 2, (0, 5))  # k = 1.5 j: j = 1 -> 1.5 -> 2
    assert tm2.nearest_packet_frame(1) == 2


def test_start_offset_and_validation():
    tm = TimeMap(30, 24, (100, 159))
    assert tm.packet_frame_for(0) == 100.0
    assert tm.packet_frame_for(8) == 110.0
    assert tm.engine_frame_for(110.0) == 8.0
    assert tm.nearest_packet_frame(47) == 159
    with pytest.raises(ValueError):
        TimeMap(25, 24, (5, 4))
    with pytest.raises(ValueError):
        TimeMap(25, 24, (0, 10), method="linear")
    with pytest.raises(ValueError):
        TimeMap(25, 24, (0, 10), engine_frames=0)


def test_to_json_round_trips_and_is_plain_data():
    tm = TimeMap(25, 24, (0, 124))
    data = tm.to_json()
    assert data == {
        "version": "timemap-v0",
        "packet_fps": "25/1",
        "engine_fps": "24/1",
        "packet_frames": [0, 124],
        "engine_frames": 120,
        "duration_s": 5.0,
        "duration": "5/1",
        "method": "nearest",
    }
    back = TimeMap.from_json(data)
    assert back == tm
    assert "25/1 -> 24/1 (nearest)" in tm.describe() and "120 engine frames" in tm.describe()
    assert (
        TimeMap.from_json({"packet_fps": "25", "engine_fps": "24", "packet_frames": [0, 11]}).engine_frames
        == 12
    )


# --------------------------------------------------------------------------- camera interpolation


def test_slerp_midpoint_between_0_and_30_degree_yaw_is_15_degrees():
    frames = [{"frame": 0, "world_from_camera": _rz(0)}, {"frame": 1, "world_from_camera": _rz(30)}]
    pose = interpolate_pose(frames, 0.5)
    assert _yaw_deg(pose["world_from_camera"]) == pytest.approx(15.0, abs=1e-6)
    assert pose["lo"] == 0 and pose["hi"] == 1 and pose["t"] == 0.5 and pose["clamped"] is False
    r = np.asarray(pose["world_from_camera"])[:3, :3]
    assert np.allclose(r @ r.T, np.eye(3), atol=1e-9) and np.linalg.det(r) == pytest.approx(1.0, abs=1e-9)
    assert _yaw_deg(interpolate_pose(frames, 0.25)["world_from_camera"]) == pytest.approx(7.5, abs=1e-6)


def test_translation_focal_and_focus_lerp_and_end_clamping():
    frames = [
        {
            "frame": 2,
            "world_from_camera": _rz(0, (0.0, 0.0, 0.0)),
            "focal_length_mm": 28.0,
            "focus_distance": 2.0,
        },
        {
            "frame": 4,
            "world_from_camera": _rz(0, (2.0, 4.0, -1.0)),
            "focal_length_mm": 32.0,
            "focus_distance": 4.0,
        },
    ]
    pose = interpolate_pose({"frames": frames}, 3.0)
    m = np.asarray(pose["world_from_camera"])
    assert m[:3, 3].tolist() == pytest.approx([1.0, 2.0, -0.5])
    assert pose["focal_length_mm"] == 30.0 and pose["focus_distance"] == 3.0
    lo = interpolate_pose(frames, -5)
    assert (
        lo["clamped"] is True
        and lo["lo"] == lo["hi"] == 2
        and lo["world_from_camera"] == frames[0]["world_from_camera"]
    )
    hi = interpolate_pose(frames, 9)
    assert hi["clamped"] is True and hi["focal_length_mm"] == 32.0
    exact = interpolate_pose(frames, 4)
    assert exact["t"] == 0.0 and exact["world_from_camera"] == frames[1]["world_from_camera"]
    assert "focal_length_mm" not in interpolate_pose([{"frame": 0, "world_from_camera": _rz(0)}], 0.0)


def test_camera_at_rebuilds_intrinsics_from_the_keyed_focal_and_projects():
    base = {
        "world_from_camera": _rz(0),
        "focal_length_mm": 28.0,
        "film_width_mm": 36.0,
        "zoom_factor": 1.0,
        "resolution": {"width": 320, "height": 180, "pixel_aspect": 1.0},
        "intrinsics_px": {"fx": 248.8889, "fy": 248.8889, "principal_point": [160.0, 90.0]},
        "dof_enabled": False,
    }
    frames = [
        {"frame": 0, "world_from_camera": _rz(0, (0.0, 0.0, 0.0)), "focal_length_mm": 28.0},
        {"frame": 1, "world_from_camera": _rz(0, (0.0, 0.0, 0.0)), "focal_length_mm": 56.0},
    ]
    cam = camera_at(frames, 0.5, base)
    assert cam["focal_length_mm"] == 42.0
    assert cam["intrinsics_px"]["fx"] == pytest.approx(42.0 * 320 / 36.0)
    assert cam["intrinsics_px"]["principal_point"] == [160.0, 90.0]
    assert cam["_pose"] == {"frame": 0.5, "lo": 0, "hi": 1, "t": 0.5, "clamped": False}
    assert "fov_deg" not in cam
    # a point 1 m left of the axis, 4 m ahead (camera looks down -Z): u = cx - fx / 4
    u, v, depth = project_point(cam, (-1.0, 0.0, -4.0))
    assert depth == pytest.approx(4.0) and v == pytest.approx(90.0)
    assert u == pytest.approx(160.0 - cam["intrinsics_px"]["fx"] / 4.0)
    # without a keyed focal the manifest intrinsics are kept untouched
    plain = camera_at([{"frame": 0, "world_from_camera": _rz(10)}], 0.0, base)
    assert plain["intrinsics_px"] is base["intrinsics_px"] and plain["focal_length_mm"] == 28.0
    assert intrinsics_for(base)["fx"] == 248.8889
    assert intrinsics_for(base, 28.0)["K"][0][2] == 160.0
