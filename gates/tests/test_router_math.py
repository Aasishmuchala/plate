"""router_math: detail resolvability, disocclusion budget, orbit angle, threshold crossings (router plan T28).

The motion vectors use a synthetic two-plane scene (a wall at 4 m, a box at 2.5 m) rendered analytically from any
pose, so the disocclusion fraction has a closed form: a lateral dolly of d metres hides a strip of wall
f*d/Zw px wide at the far edge of the frame (outside the start frame) and a strip f*d*(Zw/Zb - 1)/Zw px wide
behind the box (occluded in the start frame), the latter f*(by1 - by0)/Zb px tall.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from plate_gates import router_math as rm

W, H = 320, 180
F, CX, CY = 200.0, 160.0, 90.0
K = [[F, 0.0, CX], [0.0, F, CY], [0.0, 0.0, 1.0]]
BG = 1e10
ZW, ZB = 4.0, 2.5  # wall and box planes (metres in front of the start camera)
BOX = (-0.5, 0.5, -0.4, 0.4)  # x0, x1, y0, y1 of the box at ZB

REPO = Path(__file__).resolve().parents[2]
DOLLY = REPO / "fixtures" / "packets" / "dolly_12f"


def pose(R: np.ndarray | None = None, t=(0.0, 0.0, 0.0)) -> list[list[float]]:
    m = np.eye(4)
    if R is not None:
        m[:3, :3] = R
    m[:3, 3] = t
    return m.tolist()


def rot_y(deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def render_two_planes(cam, wall_z=ZW, box=BOX, box_z=ZB, radial=False) -> np.ndarray:
    """Planar (or radial) depth of the wall (world z = -wall_z) and the box rectangle (world z = -box_z) seen
    from `cam`; BG where the ray misses the wall."""
    m = np.asarray(cam, dtype=np.float64)
    R, t = m[:3, :3], m[:3, 3]
    u = (np.arange(W) + 0.5 - CX) / F
    v = (np.arange(H) + 0.5 - CY) / F
    dirs_cam = np.stack(np.broadcast_arrays(u[None, :], -v[:, None], -np.ones((H, W))), axis=-1)  # (H, W, 3)
    dirs = dirs_cam @ R.T
    z = np.full((H, W), BG, dtype=np.float64)
    planes = [(wall_z, None)] + ([(box_z, box)] if box is not None else [])
    for plane_z, rect in planes:
        dz = dirs[..., 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            s = (-plane_z - t[2]) / dz
        hit = np.isfinite(s) & (s > 0)  # the plane is ahead along the ray (any pose)
        p = t + s[..., None] * dirs
        if rect is not None:
            x0, x1, y0, y1 = rect
            hit &= (p[..., 0] >= x0) & (p[..., 0] <= x1) & (p[..., 1] >= y0) & (p[..., 1] <= y1)
        depth = s * (np.linalg.norm(dirs_cam, axis=-1) if radial else 1.0)
        z = np.where(hit & (depth < z), depth, z)
    return z.astype(np.float32)


def analytic_lateral_fraction(d: float) -> float:
    x0, x1, y0, y1 = BOX
    outside = F * d / ZW * H
    occluded = min(F * d * (ZW / ZB - 1.0) / ZW, F * (x1 - x0) / ZB) * F * (y1 - y0) / ZB
    return (outside + occluded) / (W * H)


# --------------------------------------------------------------------------- detail resolvability


def legend(*entries):
    return {"packet_id": "pkt_TEST", "entries": list(entries)}


def entry(hex_id: str, name: str, cls: str, material: str | None = None, **extra):
    e = {
        "id": f"o:{hex_id}",
        "kind": "object",
        "name": name,
        "class": cls,
        "class_source": "rule",
        "pixel_share": 0.1,
    }
    if material:
        e["material"] = material
    e.update(extra)
    return e


LEAF = entry("0000a001", "VEG_tree_A", "vegetation", "VEG_leaf")
K35 = {"fx": 3733.0, "fy": 3733.0, "principal_point": [1920.0, 1080.0]}  # 35 mm on 3840 px


@pytest.mark.parametrize("z_m,want_d,want_verdict", [(10.0, 22.4, "ai_by_region"), (30.0, 7.5, "vray_only")])
def test_leaf_detail_at_10m_and_30m(z_m, want_d, want_verdict):
    """D = 0.06 * 3733 / Z: 22.4 px at 10 m (AI by region), 7.5 px at 30 m (V-Ray only)."""
    z = np.full((36, 64), z_m, dtype=np.float32)
    ids = np.full((36, 64), 0xA001, dtype=np.uint32)
    out = rm.detail_resolvability(z, ids, legend(LEAF), K35, {"meters_per_unit": 1.0})
    assert out["D_px_median"] == pytest.approx(want_d, abs=0.05)
    assert out["verdict"] == want_verdict
    assert out["per_class"] == {"vegetation": pytest.approx(want_d, abs=0.05)}
    assert out["per_id"] == {"o:0000a001": pytest.approx(want_d, abs=0.05)}
    assert out["n_pixels"] == 36 * 64 and out["fx"] == 3733.0


def test_scene_units_convert_the_detail_size():
    """The same scene in millimetres (Max default) gives the same D: s is converted with meters_per_unit."""
    z = np.full((8, 8), 10000.0, dtype=np.float32)
    ids = np.full((8, 8), 0xA001, dtype=np.uint32)
    out = rm.detail_resolvability(
        z, ids, legend(LEAF), K35, {"system_unit": "millimeters", "meters_per_unit": 0.001}
    )
    assert out["D_px_median"] == pytest.approx(22.4, abs=0.05)
    assert out["meters_per_unit"] == 0.001


def test_whole_frame_ok_at_24px_and_boundaries():
    z = np.full((8, 8), 10.0, dtype=np.float32)
    ids = np.full((8, 8), 0xA001, dtype=np.uint32)
    # 0.0625 m and these focals are exact in binary, so D lands exactly on the 24 / 8 px boundaries
    leaf = {"vegetation": 0.0625}
    out = rm.detail_resolvability(z, ids, legend(LEAF), {"fx": 3840.0, "principal_point": [0, 0]}, 1.0, leaf)
    assert out["D_px_median"] == 24.0 and out["verdict"] == "whole_frame_ok"
    out = rm.detail_resolvability(z, ids, legend(LEAF), {"fx": 1280.0, "principal_point": [0, 0]}, 1.0, leaf)
    assert out["D_px_median"] == 8.0 and out["verdict"] == "ai_by_region"
    out = rm.detail_resolvability(z, ids, legend(LEAF), {"fx": 1279.0, "principal_point": [0, 0]}, 1.0, leaf)
    assert out["D_px_median"] < 8.0 and out["verdict"] == "vray_only"


def test_ground_sett_rule_entry_override_and_furniture():
    brick = entry("0000b001", "HARD_paving", "ground", "HARD_brick")
    sett = entry("0000b002", "HARD_paving_old", "ground", "HARD_granite_setts")
    own = entry("0000b003", "HARD_floor", "ground", "HARD_floor", detail_size_m=0.1)
    sofa = entry("0000c001", "FURN_sofa", "furniture", "FURN_linen")
    assert rm.detail_size_m(brick) == 0.065
    assert rm.detail_size_m(sett) == 0.10
    assert rm.detail_size_m(own) == 0.1
    assert rm.detail_size_m(sofa) == 0.02
    assert rm.detail_size_m(entry("0000d001", "ARCH_wall", "architecture")) is None
    assert rm.detail_size_m(brick, {"ground": 0.05}) == 0.05  # class override, sett rule still wins for setts
    assert rm.detail_size_m(sett, {"ground": 0.05}) == 0.10

    z = np.full((4, 40), 10.0, dtype=np.float32)
    ids = np.zeros((4, 40), dtype=np.uint32)
    ids[:, :10], ids[:, 10:20], ids[:, 20:30], ids[:, 30:] = 0xB001, 0xB002, 0xC001, 0xD001
    out = rm.detail_resolvability(
        z, ids, legend(brick, sett, sofa, entry("0000d001", "ARCH_wall", "architecture")), K, 1.0
    )
    # ground is two equal groups (brick 1.3 px, setts 2.0 px): the median is their mid-value
    assert out["per_class"]["ground"] == pytest.approx((F * 0.065 / 10.0 + F * 0.10 / 10.0) / 2)
    assert out["per_id"]["o:0000b001"] == pytest.approx(F * 0.065 / 10.0)
    assert out["per_id"]["o:0000b002"] == pytest.approx(F * 0.10 / 10.0)
    assert out["per_class"]["furniture"] == pytest.approx(F * 0.02 / 10.0)
    assert "architecture" not in out["per_class"] and out["n_pixels"] == 120


def test_unknown_when_nothing_textured_or_background_only():
    z = np.full((8, 8), 10.0, dtype=np.float32)
    ids = np.full((8, 8), 0xD001, dtype=np.uint32)
    out = rm.detail_resolvability(z, ids, legend(entry("0000d001", "ARCH_wall", "architecture")), K, 1.0)
    assert out["D_px_median"] is None and out["verdict"] == "unknown" and out["per_class"] == {}
    z_bg = np.full((8, 8), BG, dtype=np.float32)
    out = rm.detail_resolvability(z_bg, np.full((8, 8), 0xA001, np.uint32), legend(LEAF), K, 1.0, bg_value=BG)
    assert out["D_px_median"] is None and out["verdict"] == "unknown"


def test_ranks_array_material_entries_and_shape_checks():
    z = np.full((8, 8), 10.0, dtype=np.float32)
    ranks = np.zeros((8, 8, 6), dtype=np.uint32)
    ranks[..., 0] = 0xA001
    mat = {**LEAF, "id": "m:0000a001", "kind": "material"}
    out = rm.detail_resolvability(z, ranks, legend(LEAF, mat), K, 1.0)
    assert out["per_id"] == {
        "o:0000a001": pytest.approx(F * 0.06 / 10.0)
    }  # material entries are not pixel ids
    with pytest.raises(ValueError):
        rm.detail_resolvability(z, np.zeros((4, 4), np.uint32), legend(LEAF), K, 1.0)
    with pytest.raises(ValueError):
        rm.detail_resolvability(z, ranks, legend(LEAF), K, {"meters_per_unit": 0})


# --------------------------------------------------------------------------- disocclusion budget


@pytest.mark.parametrize("d", [1.0, 0.37])
def test_lateral_dolly_matches_the_analytic_strip(d):
    """A lateral dolly of d m: fraction = (outside strip + occluded strip) / valid, within 0.5 points."""
    cam_s, cam_e = pose(), pose(t=(d, 0.0, 0.0))
    out = rm.disocclusion_budget(render_two_planes(cam_s), cam_s, render_two_planes(cam_e), cam_e, K, BG)
    want = analytic_lateral_fraction(d)
    assert out["fraction"] == pytest.approx(want, abs=0.005)
    assert out["orbit_deg"] == pytest.approx(0.0, abs=1e-9) and out["behind"] == 0
    assert out["n_valid"] == W * H and out["n_invisible"] == out["outside"] + out["occluded"]
    assert out["verdict"] == ("render_move" if want > 0.10 else "rendered_endpoints_plus_clay")


def test_lateral_dolly_one_metre_is_exact_on_pixel_boundaries():
    """d = 1 m lands on integer pixel shifts: 50 outside columns x 180 rows + a 30 x 64 occluded strip."""
    cam_s, cam_e = pose(), pose(t=(1.0, 0.0, 0.0))
    out = rm.disocclusion_budget(render_two_planes(cam_s), cam_s, render_two_planes(cam_e), cam_e, K, BG)
    assert out["outside"] == 50 * 180 and out["occluded"] == 30 * 64
    assert out["fraction"] == pytest.approx((9000 + 1920) / (W * H))


def test_push_in_is_below_two_percent():
    cam_s, cam_e = pose(), pose(t=(0.0, 0.0, -0.3))  # forward is -Z
    out = rm.disocclusion_budget(render_two_planes(cam_s), cam_s, render_two_planes(cam_e), cam_e, K, BG)
    assert out["fraction"] < 0.02 and out["verdict"] == "ai_interpolate"
    assert out["outside"] == 0 and out["behind"] == 0


def test_identity_move_is_fully_visible():
    cam = pose()
    z = render_two_planes(cam)
    out = rm.disocclusion_budget(z, cam, z, cam, K, BG)
    assert out["fraction"] == 0.0 and out["n_invisible"] == 0 and out["verdict"] == "ai_interpolate"


def test_orbit_35_degrees_is_render_move_even_with_little_disocclusion():
    pivot = (0.0, 0.0, -3.0)
    R = rot_y(35.0)
    centre = np.asarray(pivot) - 3.0 * (-(R @ np.array([0.0, 0.0, 1.0])))
    cam_s, cam_e = pose(), pose(R, centre)
    assert rm.orbit_angle_deg(cam_s, cam_e)["orbit_deg"] == pytest.approx(35.0, abs=1e-6)
    out = rm.disocclusion_budget(render_two_planes(cam_s), cam_s, render_two_planes(cam_e), cam_e, K, BG)
    assert out["orbit_deg"] == pytest.approx(35.0, abs=1e-6) and out["verdict"] == "render_move"
    # with the look-at pivot the subtended angle (35 deg) is added
    with_pivot = rm.disocclusion_budget(
        render_two_planes(cam_s), cam_s, render_two_planes(cam_e), cam_e, K, BG, pivot=pivot
    )
    assert with_pivot["pivot_deg"] == pytest.approx(35.0, abs=1e-6)
    assert with_pivot["orbit_deg"] == pytest.approx(70.0, abs=1e-6)
    # a 20 degree orbit stays under the limit: the verdict is then down to the fraction alone
    R20 = rot_y(20.0)
    c20 = np.asarray(pivot) - 3.0 * (-(R20 @ np.array([0.0, 0.0, 1.0])))
    small = rm.disocclusion_budget(
        render_two_planes(cam_s), cam_s, render_two_planes(pose(R20, c20)), pose(R20, c20), K, BG
    )
    assert small["orbit_deg"] == pytest.approx(20.0, abs=1e-6) and small["verdict"] != "unknown"


def test_turning_round_puts_everything_behind_the_start_camera():
    cam_s = pose()
    cam_e = pose(rot_y(180.0), (0.0, 0.0, -ZW - 1.0))  # past the wall, facing +Z (back toward the start)
    z_e = render_two_planes(
        cam_e, wall_z=-(ZW + 1.0), box=None
    )  # a plane at world z = +5, ahead of cam_e only
    out = rm.disocclusion_budget(render_two_planes(cam_s), cam_s, z_e, cam_e, K, BG)
    assert out["n_valid"] > 0 and out["behind"] == out["n_valid"] and out["fraction"] == 1.0
    assert out["verdict"] == "render_move"


def test_radial_depth_gives_the_same_fraction_as_planar():
    cam_s, cam_e = pose(), pose(t=(0.37, 0.0, 0.0))
    planar = rm.disocclusion_budget(render_two_planes(cam_s), cam_s, render_two_planes(cam_e), cam_e, K, BG)
    radial = rm.disocclusion_budget(
        render_two_planes(cam_s, radial=True),
        cam_s,
        render_two_planes(cam_e, radial=True),
        cam_e,
        K,
        BG,
        depth_kind="radial",
    )
    assert radial["fraction"] == pytest.approx(planar["fraction"], abs=0.002)
    with pytest.raises(ValueError):
        rm.disocclusion_budget(
            render_two_planes(cam_s), cam_s, render_two_planes(cam_e), cam_e, K, BG, depth_kind="x"
        )


def test_empty_end_frame_is_unknown():
    cam = pose()
    z_bg = np.full((H, W), BG, dtype=np.float32)
    out = rm.disocclusion_budget(render_two_planes(cam), cam, z_bg, cam, K, BG)
    assert out["fraction"] is None and out["verdict"] == "unknown" and out["n_valid"] == 0


def test_occlusion_tolerance_keeps_a_surface_from_hiding_itself():
    """Bilinear resampling on a sloped surface must not flag the surface as occluding itself."""
    cam_s = pose()
    cam_e = pose(t=(0.123, 0.0, 0.0))
    # a wall seen at a grazing pitch: tilt the cameras 20 deg down
    tilt = np.array(
        [
            [1, 0, 0],
            [0, math.cos(math.radians(-20)), -math.sin(math.radians(-20))],
            [0, math.sin(math.radians(-20)), math.cos(math.radians(-20))],
        ]
    )
    cam_s, cam_e = pose(tilt), pose(tilt, (0.123, 0.0, 0.0))
    z_s, z_e = render_two_planes(cam_s, box=None), render_two_planes(cam_e, box=None)
    out = rm.disocclusion_budget(z_s, cam_s, z_e, cam_e, K, BG)
    assert out["occluded"] == 0 and out["fraction"] == pytest.approx(out["outside"] / out["n_valid"])


def test_camera_block_and_bad_matrices():
    cam = {
        "world_from_camera": pose(),
        "intrinsics_px": {"fx": F, "fy": F, "principal_point": [CX, CY], "K": K},
    }
    z = render_two_planes(pose())
    out = rm.disocclusion_budget(z, cam, z, cam, cam["intrinsics_px"], BG)
    assert out["fraction"] == 0.0
    with pytest.raises(ValueError):
        rm.disocclusion_budget(z, np.eye(3), z, cam, K, BG)
    with pytest.raises(ValueError):
        rm.disocclusion_budget(z, cam, z, cam, np.eye(4), BG)
    assert np.allclose(rm.camera_forward(pose()), [0.0, 0.0, -1.0])


# --------------------------------------------------------------------------- route_move


def push_in_motion(**extra):
    cam_s, cam_e = pose(), pose(t=(0.0, 0.0, -0.3))
    pm = {"cam_start": cam_s, "cam_end": cam_e, "K": K, "bg_value": BG, "packet_id": "pkt_TEST", **extra}
    return pm, render_two_planes(cam_s), render_two_planes(cam_e)


def test_route_move_push_in_is_advisory_ai_interpolate(monkeypatch):
    monkeypatch.delenv(rm.ROUTER_MODE_ENV, raising=False)
    pm, z0, z1 = push_in_motion()
    v = rm.route_move(pm, z0, z1)
    assert isinstance(v, rm.RouterVerdict)
    assert v.mode == "advisory" and v.verdict == "ai_interpolate" and v.blocks is False
    assert v.numbers["disocclusion_fraction"] < 0.02 and v.numbers["orbit_deg"] == 0.0
    assert v.numbers["crossings"] == [] and v.numbers["packet_id"] == "pkt_TEST"
    assert "AI may interpolate" in v.explanation and "(advisory)" in v.explanation
    dumped = v.model_dump()
    assert set(dumped) == {"mode", "verdict", "numbers", "explanation", "blocks"}
    assert rm.RouterVerdict.model_validate(json.loads(json.dumps(dumped))) == v


def test_threshold_crossing_forces_render_move_and_blocks_in_blocking_mode():
    pm, z0, z1 = push_in_motion()
    v = rm.route_move(pm, z0, z1, [{"kind": "threshold"}], mode="blocking")
    assert v.verdict == "render_move" and v.mode == "blocking" and v.blocks is True
    assert v.numbers["crossings"] == [{"kind": "threshold"}]
    assert "path crossing" in v.explanation and "render the move" in v.explanation
    advisory = rm.route_move(
        pm, z0, z1, [{"frame": 88, "kind": "threshold", "note": "gate line"}], mode="advisory"
    )
    assert advisory.verdict == "render_move" and advisory.blocks is False
    assert advisory.numbers["crossings"] == [{"frame": 88, "kind": "threshold", "note": "gate line"}]


def test_manifest_crossings_and_argument_crossings_are_both_counted():
    pm, z0, z1 = push_in_motion(crossings=[{"frame": 3, "kind": "room_change"}])
    v = rm.route_move(pm, z0, z1, ["threshold"])
    assert v.verdict == "render_move" and len(v.numbers["crossings"]) == 2
    assert v.numbers["crossings"][1] == {"kind": "threshold"}


def test_route_move_reports_detail_numbers_without_changing_the_verdict():
    pm, z0, z1 = push_in_motion()
    detail = {"D_px_median": 7.5, "verdict": "vray_only", "per_class": {"vegetation": 7.5}}
    v = rm.route_move(pm, z0, z1, detail=detail)
    assert v.verdict == "ai_interpolate"
    assert v.numbers["D_px_median"] == 7.5 and v.numbers["detail_verdict"] == "vray_only"
    assert v.numbers["detail_per_class"] == {"vegetation": 7.5}


def test_route_move_large_lateral_and_orbit_explanations():
    cam_s, cam_e = pose(), pose(t=(1.0, 0.0, 0.0))
    pm = {"cam_start": cam_s, "cam_end": cam_e, "K": K, "bg_value": BG}
    v = rm.route_move(pm, render_two_planes(cam_s), render_two_planes(cam_e))
    assert v.verdict == "render_move" and "disocclusion 19.0 % > 10 %" in v.explanation
    R = rot_y(35.0)
    pm["cam_end"] = pose(
        R, (3.0 * math.sin(math.radians(35.0)), 0.0, -3.0 + 3.0 * math.cos(math.radians(35.0)))
    )
    v = rm.route_move(pm, render_two_planes(cam_s), render_two_planes(pm["cam_end"]))
    assert v.verdict == "render_move" and "orbit 35.0 deg > 30" in v.explanation


def test_router_mode_seam(monkeypatch):
    monkeypatch.delenv(rm.ROUTER_MODE_ENV, raising=False)
    assert rm.router_mode() == "advisory"
    monkeypatch.setenv(rm.ROUTER_MODE_ENV, "blocking")
    assert rm.router_mode() == "blocking"
    assert rm.router_mode("advisory") == "advisory"  # explicit wins over the environment
    pm, z0, z1 = push_in_motion()
    assert rm.route_move(pm, z0, z1).mode == "blocking"
    with pytest.raises(ValueError):
        rm.router_mode("maybe")


def test_route_move_rejects_incomplete_packet_motion():
    pm, z0, z1 = push_in_motion()
    del pm["K"]
    with pytest.raises(ValueError):
        rm.route_move(pm, z0, z1)


# --------------------------------------------------------------------------- the committed fixture


@pytest.mark.skipif(
    not (DOLLY / "manifest.json").exists(), reason="fixtures/packets/dolly_12f not built yet (T2)"
)
def test_dolly_12f_first_last_frames():
    """The number recorded in docs/gates/render_it.md: 0.6 m forward dolly, 1.5 % unseen, ai_interpolate."""
    pm, z0, z1 = rm.load_motion(DOLLY)
    assert pm["frames"] == (0, 11) and pm["depth_kind"] == "planar" and pm["bg_value"] == 1e10
    assert pm["crossings"] == [] and np.asarray(pm["K"]).shape == (3, 3)
    assert z0.shape == z1.shape == (180, 320)
    v = rm.route_move(pm, z0, z1)
    assert v.verdict == "ai_interpolate" and v.numbers["orbit_deg"] == pytest.approx(0.0, abs=1e-3)
    assert v.numbers["disocclusion_fraction"] == pytest.approx(0.0152, abs=0.002)
    assert v.numbers["outside"] == 0 and v.numbers["behind"] == 0
    assert (
        v.numbers["packet_id"]
        == json.loads((DOLLY / "manifest.json").read_text(encoding="utf-8"))["packet_id"]
    )


def test_load_motion_refuses_a_still_packet(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"kind": "still"}), encoding="utf-8", newline="\n")
    with pytest.raises(ValueError):
        rm.load_motion(tmp_path)
