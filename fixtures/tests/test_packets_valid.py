"""The committed Shot Packets under fixtures/packets are valid, geometrically exact and small (router plan T2).

These packets are Blender/Cycles renders repacked to V-Ray layer names by fixtures/blender/build_packets.py. The
tests read the EXRs with OpenEXR directly, so they do not depend on plate_pixels (which is itself tested against
these files).
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from pathlib import Path

import numpy as np
import OpenEXR
import pytest

import plate_spec as ps

ROOT = Path(__file__).resolve().parents[2]
PACKETS = ROOT / "fixtures" / "packets"
MARKERS = ROOT / "fixtures" / "blender" / "markers.json"
SHOTS = ("room_a", "room_b", "room_a_v2", "room_a_ral3020", "dolly_hero", "dolly_12f")
STILLS = tuple(s for s in SHOTS if s != "dolly_12f")
RAW_SHOTS = ("room_a", "room_a_ral3020")  # carry the opt-in raw_lighting / raw_gi / reflection elements
TOTAL_BUDGET_BYTES = 25 * 1024 * 1024
PACKET_BUDGET_BYTES = 3 * 1024 * 1024  # zipped, packets without the raw elements
RAW_PACKET_BUDGET_BYTES = 4 * 1024 * 1024  # zipped, packets with them (Cycles noise in three more RGB layers)


def _manifest(shot: str) -> dict:
    return json.loads((PACKETS / shot / "manifest.json").read_text(encoding="utf-8"))


def _channels(shot: str) -> dict[str, np.ndarray]:
    f = OpenEXR.File(str(PACKETS / shot / "beauty.exr"), separate_channels=True)
    return {k: np.ascontiguousarray(v.pixels) for part in f.parts for k, v in part.channels.items()}


def _crypto_ranks(ch: dict[str, np.ndarray], layer: str = "CryptoObject"):
    """(ids uint32 (H, W, 6), coverage float64 (H, W, 6)) from the three RGBA crypto layers."""
    ids, cov = [], []
    for i in range(3):
        base = f"{layer}{i:02d}"
        ids += [ch[f"{base}.R"].view(np.uint32), ch[f"{base}.B"].view(np.uint32)]
        cov += [ch[f"{base}.G"], ch[f"{base}.A"]]
    return np.stack(ids, -1), np.stack(cov, -1).astype(np.float64)


def _obj_id(name: str) -> int:
    return int(ps.cryptomatte_id(name, "object").split(":")[1], 16)


def _rgb(ch: dict[str, np.ndarray], layer: str) -> np.ndarray:
    return np.stack([ch[f"{layer}.{c}"] for c in "RGB"], -1).astype(np.float64)


def _packet_files(shot: str) -> list[Path]:
    return sorted(p for p in (PACKETS / shot).rglob("*") if p.is_file())


def _zipped_size(shot: str) -> int:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in _packet_files(shot):
            z.write(p, p.relative_to(PACKETS / shot).as_posix())
    return len(buf.getvalue())


# --------------------------------------------------------------------------- validity and layout


def test_every_packet_directory_is_listed_here():
    on_disk = {p.name for p in PACKETS.iterdir() if p.is_dir()}
    assert on_disk == set(SHOTS)


@pytest.mark.parametrize("shot", SHOTS)
def test_packet_validates_with_file_checks(shot):
    assert ps.validate_packet_dir(PACKETS / shot, check_files=True) == []


@pytest.mark.parametrize("shot", SHOTS)
def test_fixture_manifest_shape(shot):
    m = _manifest(shot)
    assert m["packet_version"] == ps.PACKET_VERSION
    assert m["tier"] == "A"
    assert m["host"]["app"] == "none"
    assert m["renderer"] == {"name": "vray", "version": "fixture-cycles-5.1"}
    assert m["render"]["session"] == "fixture"
    assert m["colour"]["max_colour_mode"] == "legacy_gamma" and m["colour"]["legacy_gamma"] == 2.2
    assert m["colour"]["working_space"] == "lin_rec709" and m["colour"]["master"] == "display_tiff16"
    assert m["colour"]["vfb_corrections_baked"] is False
    assert m["files"]["beauty_exr"]["parts"] == "single"
    assert m["kind"] == ("motion" if shot == "dolly_12f" else "still")


@pytest.mark.parametrize("shot", SHOTS)
def test_beauty_exr_is_one_part_with_vray_layer_names(shot):
    f = OpenEXR.File(str(PACKETS / shot / "beauty.exr"), separate_channels=True)
    assert len(f.parts) == 1
    names = set(f.parts[0].channels)
    need = {"R", "G", "B", "A", "VRayZDepth.Z", "VRayNormals.X", "VRayNormals.Y", "VRayNormals.Z"}
    need |= {f"CryptoObject0{i}.{c}" for i in range(3) for c in "RGBA"}
    need |= {f"CryptoMaterial0{i}.{c}" for i in range(3) for c in "RGBA"}
    need |= {f"VRayWireColor.{c}" for c in "RGB"} | {f"VRayDiffuseFilter.{c}" for c in "RGB"}
    assert need <= names, sorted(need - names)
    raw = {
        f"{layer}.{c}"
        for layer in ("VRayRawLighting", "VRayRawGlobalIllumination", "VRayReflection")
        for c in "RGB"
    }
    assert (raw <= names) == (shot in RAW_SHOTS)
    assert not any(n.startswith("ViewLayer") for n in names), "Blender-native layer names must not leak"


@pytest.mark.parametrize("shot", STILLS)
def test_z_is_planar_unfiltered_with_the_declared_background(shot):
    z = _channels(shot)["VRayZDepth.Z"]
    assert z.dtype == np.float32
    bg = z >= 1e9
    assert bg.any() and np.all(z[bg] == np.float32(1e10))
    fg = z[~bg]
    assert fg.min() > 0.2 and fg.max() < 20.0  # a 5 x 4 x 3 m room


@pytest.mark.parametrize("shot", STILLS)
def test_normals_are_unit_camera_space(shot):
    n = np.stack([_channels(shot)[f"VRayNormals.{c}"] for c in "XYZ"], -1).astype(np.float64)
    length = np.linalg.norm(n, axis=-1)
    hit = length > 0.5
    assert hit.mean() > 0.9
    assert np.abs(length[hit] - 1.0).max() < 0.02
    # camera space: surfaces seen head-on face the viewer, so +Z dominates over most of the frame
    assert (n[..., 2][hit] > 0).mean() > 0.6


# --------------------------------------------------------------------------- geometry is exact


def _markers() -> list[dict]:
    return json.loads(MARKERS.read_text(encoding="utf-8"))["markers"]


def test_markers_json_lists_eight_two_centimetre_spheres():
    doc = json.loads(MARKERS.read_text(encoding="utf-8"))
    assert doc["radius_m"] == 0.02 and len(doc["markers"]) == 8
    for m in doc["markers"]:
        x, y, z = m["position"]
        assert -2.5 < x < 2.5 and -2.0 < y < 2.0 and 0.0 < z < 3.0, "inside the 5 x 4 x 3 m room"


@pytest.mark.parametrize("shot", SHOTS)
def test_marker_reprojection_rms_under_half_a_pixel_at_640_wide(shot):
    cam = _manifest(shot)["camera"]
    ch = _channels(shot)
    ids, cov = _crypto_ranks(ch)
    h, w = ids.shape[:2]
    scale = (
        640.0 / w
    )  # the budget is stated at 640 wide; the 320 wide motion packet is held to the same figure
    xs, ys = (np.arange(w) + 0.5)[None, :], (np.arange(h) + 0.5)[:, None]
    radius = json.loads(MARKERS.read_text(encoding="utf-8"))["radius_m"]
    errs = []
    for mk in _markers():
        try:
            u, v, depth = ps.project_point(cam, mk["position"])
        except ValueError:
            continue
        c = (cov * (ids == _obj_id(mk["name"]))).sum(-1)
        seen = c.sum()
        expected_area = math.pi * (radius * cam["intrinsics_px"]["fx"] / depth) ** 2
        if (
            seen < 0.8 * expected_area
        ):  # off frame or partly hidden: its centroid is biased, not a test of the camera
            continue
        cx, cy = float((c * xs).sum() / seen), float((c * ys).sum() / seen)
        errs.append(math.hypot(cx - u, cy - v) * scale)
    assert len(errs) >= 6, f"only {len(errs)} markers cleanly visible"
    rms = math.sqrt(sum(e * e for e in errs) / len(errs))
    assert rms <= 0.5, f"marker reprojection RMS {rms:.3f} px"


@pytest.mark.parametrize("shot", STILLS)
def test_z_matches_projected_marker_depth(shot):
    """The Z layer is planar depth in metres: at each marker's centroid it equals project_point()'s depth."""
    cam = _manifest(shot)["camera"]
    z = _channels(shot)["VRayZDepth.Z"]
    ids, cov = _crypto_ranks(_channels(shot))
    checked = 0
    for mk in _markers():
        try:
            u, v, depth = ps.project_point(cam, mk["position"])
        except ValueError:
            continue
        c = cov[..., 0] * (ids[..., 0] == _obj_id(mk["name"]))
        if c.sum() < 8:
            continue
        ys, xs = np.nonzero(c > 0.99)
        if len(xs) == 0:
            continue
        # the sphere's visible surface is at most one radius nearer than its centre
        assert np.all(z[ys, xs] <= depth + 1e-2) and np.all(z[ys, xs] >= depth - 0.03)
        checked += 1
    assert checked >= 5


def test_dolly_hero_matches_motion_frame_zero():
    hero, motion = _manifest("dolly_hero"), _manifest("dolly_12f")
    assert motion["motion"]["hero_still_packet_id"] == hero["packet_id"]
    frames = json.loads((PACKETS / "dolly_12f" / "camera_frames.json").read_text(encoding="utf-8"))["frames"]
    assert len(frames) == 12
    m0 = np.array(frames[0]["world_from_camera"])
    assert np.abs(m0 - np.array(hero["camera"]["world_from_camera"])).max() < 1e-6
    assert hero["camera"]["resolution"]["width"] == 640 and motion["camera"]["resolution"]["width"] == 320


def test_dolly_travels_point_six_metres_forward():
    frames = json.loads((PACKETS / "dolly_12f" / "camera_frames.json").read_text(encoding="utf-8"))["frames"]
    m0, m1 = np.array(frames[0]["world_from_camera"]), np.array(frames[-1]["world_from_camera"])
    forward = -m0[:3, 2]  # the camera looks down its local -Z
    delta = m1[:3, 3] - m0[:3, 3]
    assert abs(float(delta @ forward) - 0.6) < 1e-4
    assert np.linalg.norm(delta - (delta @ forward) * forward) < 1e-4, "a pure dolly: no sideways drift"
    assert np.abs(m1[:3, :3] - m0[:3, :3]).max() < 1e-6, "no rotation"


def test_dolly_sequences_are_complete_and_clay_is_sixteen_bit():
    import cv2

    root = PACKETS / "dolly_12f" / "motion"
    for i in range(12):
        for name in (f"clay.{i:04d}.png", f"id.{i:04d}.exr", f"z.{i:04d}.exr"):
            assert (root / name).is_file(), name
    px = cv2.imdecode(np.fromfile(str(root / "clay.0000.png"), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert px.dtype == np.uint16 and px.shape == (180, 320, 3)


@pytest.mark.parametrize("shot", ("room_a", "room_b"))
def test_cam_a_is_overscanned_2_39_inside_16_9_and_cam_b_is_full_frame(shot):
    cam = _manifest(shot)["camera"]
    assert cam["resolution"]["width"] == 640 and cam["resolution"]["height"] == 360
    sf = cam["safe_frame"]
    if shot == "room_a":
        assert (sf["width"], sf["height"]) == (640, 268) and sf["y"] == 46  # 2.39:1, centred
        assert cam["focal_length_mm"] == 35.0 and cam["dof_enabled"] is True and cam["f_number"] == 2.8
        assert cam["focus_distance"] == 2.5
    else:
        assert (sf["width"], sf["height"]) == (640, 360)
        assert cam["focal_length_mm"] == 24.0 and cam["dof_enabled"] is False


# --------------------------------------------------------------------------- the family of packets


def test_room_a_and_room_b_are_one_scene_two_cameras():
    a, b = _manifest("room_a"), _manifest("room_b")
    assert a["scene_hash"] == b["scene_hash"]
    assert a["view_hash"] != b["view_hash"]
    assert a["camera"]["world_from_camera"] != b["camera"]["world_from_camera"]


def test_room_a_v2_supersedes_room_a_with_one_material_changed():
    a, v2 = _manifest("room_a"), _manifest("room_a_v2")
    assert v2["supersedes"] == a["packet_id"]
    assert a["supersedes"] is None
    assert v2["camera"] == a["camera"]
    assert v2["scene_hash"] != a["scene_hash"]
    la = {
        e["name"]: e["digest"]
        for e in json.loads((PACKETS / "room_a" / "id_legend.json").read_text(encoding="utf-8"))["entries"]
    }
    lb = {
        e["name"]: e["digest"]
        for e in json.loads((PACKETS / "room_a_v2" / "id_legend.json").read_text(encoding="utf-8"))["entries"]
    }
    assert la.keys() == lb.keys()
    changed = sorted(n for n in la if la[n] != lb[n])
    assert changed, "the revision must change something"
    assert all(n.startswith("ARCH_wall_E") for n in changed), changed  # the wall and its object, nothing else


def test_ral3020_variant_differs_only_in_the_panel_albedo():
    a, r = _manifest("room_a"), _manifest("room_a_ral3020")
    assert r["camera"] == a["camera"]
    ca, cr = _channels("room_a"), _channels("room_a_ral3020")
    ia, _ = _crypto_ranks(ca)
    ir, _ = _crypto_ranks(cr)
    assert np.array_equal(ia, ir), "identical geometry: every Cryptomatte id is in the same place"
    panel = _obj_id("ARCH_panel_7016")
    in_panel = (ia == panel).any(-1)
    assert in_panel.sum() > 5000
    da, dr = _rgb(ca, "VRayDiffuseFilter"), _rgb(cr, "VRayDiffuseFilter")
    assert np.abs(da - dr)[~in_panel].max() == 0.0, "diffuse albedo differs outside the panel"
    # RAL 7016 #383E42 vs RAL 3020 #CC0605 (linear sRGB): the red channel rises, green and blue fall
    inner = ia[..., 0] == panel
    assert dr[inner][:, 0].mean() > da[inner][:, 0].mean() * 3
    assert dr[inner][:, 1].mean() < da[inner][:, 1].mean() * 0.2


@pytest.mark.parametrize("shot", RAW_SHOTS)
def test_recomposition_identity_holds_on_the_fixture(shot):
    """beauty ~= diffuse_filter x (raw_lighting + raw_gi) + reflection on the panels T31 recolours."""
    ch = _channels(shot)
    beauty = np.stack([ch[c] for c in "RGB"], -1).astype(np.float64)
    rec = _rgb(ch, "VRayDiffuseFilter") * (
        _rgb(ch, "VRayRawLighting") + _rgb(ch, "VRayRawGlobalIllumination")
    )
    rec = rec + _rgb(ch, "VRayReflection")
    ids, _ = _crypto_ranks(ch)
    for name in ("ARCH_panel_7016", "ARCH_panel_3020"):
        sel = ids[..., 0] == _obj_id(name)
        if sel.sum() < 2000:
            continue
        rel = np.abs(rec[sel] - beauty[sel]).max(-1) / np.maximum(beauty[sel].max(-1), 1e-3)
        assert np.median(rel) < 0.02, (name, float(np.median(rel)))
        assert np.percentile(rel, 95) < 0.06, (name, float(np.percentile(rel, 95)))


# --------------------------------------------------------------------------- legend and policy


@pytest.mark.parametrize("shot", STILLS)
def test_legend_names_and_classes(shot):
    entries = json.loads((PACKETS / shot / "id_legend.json").read_text(encoding="utf-8"))["entries"]
    by = {e["name"]: e for e in entries if e["kind"] == "object"}
    assert by["GLZ_pane"]["class"] == "glazing" and by["SKY_dome"]["class"] == "sky"
    assert by["VEG_shrub_A"]["class"] == "vegetation" and by["PEOPLE_dummy"]["class"] == "people"
    assert by["WATER_pool"]["class"] == "water" and by["SIGN_plate01"]["class"] == "signage"
    assert (
        by["ARCH_panel_7016"]["class"] == "architecture" and by["ARCH_panel_3020"]["class"] == "architecture"
    )
    assert all(by[n]["layer"] == n.split("_")[0] + "_" for n in by)
    assert by["ARCH_wall_N"].get("planar") is True and by["HARD_floor"].get("planar") is True
    editable = set(_manifest(shot)["policy"]["editable_ids"])
    assert editable == {e["id"] for e in entries if e["class"] in {"vegetation", "people", "sky", "water"}}
    assert {by[n]["id"] for n in ("SKY_dome", "VEG_shrub_A", "PEOPLE_dummy", "WATER_pool")} <= editable
    for e in entries:
        if e["kind"] == "object" and e["name"].startswith("ARCH_marker"):
            assert e["id"] in set(_manifest(shot)["policy"]["locked_ids"])


@pytest.mark.parametrize("shot", STILLS)
def test_every_visible_marker_is_in_the_legend(shot):
    """Blender's manifest hashes disagree with its pixels for ARCH_marker_04; the repack uses the spec hash."""
    ch = _channels(shot)
    ids, cov = _crypto_ranks(ch)
    legend = {
        e["id"]
        for e in json.loads((PACKETS / shot / "id_legend.json").read_text(encoding="utf-8"))["entries"]
    }
    for mk in _markers():
        present = bool(((ids == _obj_id(mk["name"])) & (cov > 0)).any())
        assert present == (ps.cryptomatte_id(mk["name"], "object") in legend), mk["name"]


# --------------------------------------------------------------------------- budget


def test_fixture_packets_fit_the_size_budget():
    total = sum(p.stat().st_size for shot in SHOTS for p in _packet_files(shot))
    assert total <= TOTAL_BUDGET_BYTES, f"{total / 1e6:.1f} MB of packets"
    for shot in SHOTS:
        limit = RAW_PACKET_BUDGET_BYTES if shot in RAW_SHOTS else PACKET_BUDGET_BYTES
        assert _zipped_size(shot) <= limit, f"{shot}: {_zipped_size(shot) / 1e6:.2f} MB zipped"
