"""Cross-view world-position correspondences and the Set-consistency gate (router plan T30).

The fixture pair room_a / room_b is one Blender render of the conformance room from two cameras (T2); every
test that needs it skips when the packets are not on disk yet.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np
import pytest

from plate_gates import crossview, setgate
from plate_gates.color import ciede2000, lab_to_srgb, srgb_to_lab
from plate_gates.crossview import PacketView
from plate_gates.masks import erode
from plate_spec import project_point

REPO = Path(__file__).resolve().parents[2]
PACKETS = REPO / "fixtures" / "packets"
WALL = "m:57b3d007"  # ARCH_wall_N: matte, large in both views
GLAZING = "m:b42836e4"  # GLZ_pane
WATER = "m:c8a9bf52"  # WATER_pool
LEAF = "m:b844e457"  # VEG_leaf: editable class
CHANNEL = {"L": 0, "a": 1, "b": 2}


def _packet(name: str) -> Path:
    d = PACKETS / name
    if not (d / "manifest.json").is_file() or not (d / "beauty.exr").is_file():
        pytest.skip(f"fixture packet {name} not rendered yet (T2)")
    return d


@pytest.fixture(scope="module")
def views() -> tuple[PacketView, PacketView]:
    return PacketView.load(_packet("room_a")), PacketView.load(_packet("room_b"))


@pytest.fixture(scope="module")
def images() -> tuple[np.ndarray, np.ndarray]:
    from plate_pixels.imageio import read

    a = crossview.as_rgb8(read(_packet("room_a") / "display.tif").rgb)
    b = crossview.as_rgb8(read(_packet("room_b") / "display.tif").rgb)
    return a, b


@pytest.fixture(scope="module")
def corr(views) -> list[dict]:
    return crossview.correspondences(*views)


def _shift(rgb8: np.ndarray, mask: np.ndarray, channel: str, amount: float) -> tuple[np.ndarray, float]:
    """Shift one Lab channel of the masked pixels; returns the image and the dE00 that shift makes on the mean."""
    lab = srgb_to_lab(rgb8)
    mean = lab[mask].mean(axis=0)
    moved = lab.copy()
    moved[..., CHANNEL[channel]][mask] += amount
    out = rgb8.copy()
    out[mask] = np.clip(np.rint(lab_to_srgb(moved[mask])), 0, 255).astype(np.uint8)
    target = mean.copy()
    target[CHANNEL[channel]] += amount
    return out, float(ciede2000(mean[None, :], target[None, :])[0])


# --------------------------------------------------------------------------- geometry


def test_backproject_inverts_project_point():
    camera = json.loads((_packet("room_a") / "manifest.json").read_text(encoding="utf-8"))["camera"]
    rng = np.random.default_rng(1)
    uv = np.stack([rng.uniform(0, 640, 200), rng.uniform(0, 360, 200)], axis=1)
    depth = rng.uniform(0.5, 9.0, 200)
    world = crossview.backproject(camera, uv, depth)
    for (u, v), d, p in zip(uv, depth, world, strict=True):
        u2, v2, d2 = project_point(camera, p)
        assert u2 == pytest.approx(u, abs=1e-9)
        assert v2 == pytest.approx(v, abs=1e-9)
        assert d2 == pytest.approx(d, rel=1e-12)


def test_argument_validation(views):
    va, vb = views
    with pytest.raises(ValueError):
        crossview.correspondences(va, vb, n=0)
    with pytest.raises(ValueError):
        crossview.correspondences(va, vb, window=4)
    with pytest.raises(ValueError):
        crossview.delta_e([], np.zeros((4, 4, 3), np.uint8), np.zeros((4, 4, 3), np.uint8), window=2)
    with pytest.raises(TypeError):
        crossview.as_view(42)
    empty = crossview.delta_e([], np.zeros((4, 4, 3), np.uint8), np.zeros((4, 4, 3), np.uint8))
    assert empty["n"] == 0 and empty["overall_p90"] is None and empty["per_material"] == {}


# --------------------------------------------------------------------------- correspondences on the fixture pair


def test_correspondences_on_the_fixture_pair(views, corr):
    va, vb = views
    assert len(corr) >= 500
    shared = set(va.material_entries()) & set(vb.material_entries())
    classes = {mid: e.get("class") for mid, e in va.material_entries().items()}
    h, w = va.shape
    hb, wb = vb.shape
    for c in corr:
        assert set(c) == {"id_m", "p_world", "uv_a", "uv_b", "n_dot_v_a", "n_dot_v_b", "source"}
        assert c["source"] == "z_backproject"
        assert c["id_m"] in shared and classes[c["id_m"]] not in ("glazing", "water")
        assert 0 <= c["uv_a"][0] < w and 0 <= c["uv_a"][1] < h
        assert 0 <= c["uv_b"][0] < wb and 0 <= c["uv_b"][1] < hb
        assert c["n_dot_v_a"] >= 0.3 and c["n_dot_v_b"] >= 0.3
        # the record is self-consistent with the normative projection in both cameras
        ua, vva, da = project_point(va.camera, c["p_world"])
        assert (ua, vva) == pytest.approx(tuple(c["uv_a"]), abs=1e-3)
        xa, ya = int(c["uv_a"][0]), int(c["uv_a"][1])
        assert da == pytest.approx(float(va.z[ya, xa]), rel=1e-4)
        ub, vvb, db = project_point(vb.camera, c["p_world"])
        assert (ub, vvb) == pytest.approx(tuple(c["uv_b"]), abs=1e-6)
        xb, yb = int(ub), int(vvb)
        assert abs(float(vb.z[yb, xb]) - db) <= 0.01 * db  # visible in B: its Z agrees within 1 %
    assert GLAZING not in {c["id_m"] for c in corr}
    assert WATER not in {c["id_m"] for c in corr}
    assert sum(c["id_m"] == WALL for c in corr) >= 100


def test_correspondences_are_deterministic_per_seed(views, corr):
    va, vb = views
    again = crossview.correspondences(va, vb)
    assert again == corr
    other = crossview.correspondences(va, vb, seed=7)
    assert len(other) >= 500
    assert {tuple(c["uv_a"]) for c in other} != {tuple(c["uv_a"]) for c in corr}


def test_world_position_pass_is_used_when_present(views, corr):
    va, vb = views
    h, w = va.shape
    uv = crossview._pixel_centres(h, w)
    wp = crossview.backproject(va.camera, uv, va.z.astype(np.float64).ravel()).reshape(h, w, 3)
    with_pass = crossview.correspondences(dataclasses.replace(va, world_position=wp.astype(np.float32)), vb)
    assert with_pass and all(c["source"] == "world_position" for c in with_pass)
    assert [c["uv_a"] for c in with_pass] == [c["uv_a"] for c in corr]
    # the explicit override wins over the packet, and B's pass is honoured as an extra visibility check
    hb, wb = vb.shape
    wp_b = crossview.backproject(vb.camera, crossview._pixel_centres(hb, wb), vb.z.astype(np.float64).ravel())
    both = crossview.correspondences(va, vb, world_pos_a=wp, world_pos_b=wp_b.reshape(hb, wb, 3))
    assert both and all(c["source"] == "world_position" for c in both)
    assert len(both) >= 0.9 * len(corr)
    with pytest.raises(ValueError):
        crossview.correspondences(va, vb, world_pos_a=wp[:10])


def test_glazing_is_excluded_by_class_not_by_accident(views):
    va, vb = views
    including = crossview.correspondences(va, vb, exclude_classes=())
    assert any(c["id_m"] == GLAZING for c in including)


def test_no_shared_material_gives_no_correspondences(views):
    va, vb = views
    only_a = [e for e in vb.legend if e.get("id") not in va.material_entries()]
    assert crossview.correspondences(va, dataclasses.replace(vb, legend=only_a)) == []


# --------------------------------------------------------------------------- colour at correspondences


def test_same_render_measures_near_zero_on_locked_materials(views, images, corr):
    va, vb = views
    a, b = images
    de = crossview.delta_e(corr, a, b, view_a=va, view_b=vb)
    assert de["isolated"] and de["source"] == "z_backproject" and de["n"] == len(corr)
    assert de["overall_p90"] <= 3.0
    wall = de["per_material"][WALL]
    assert wall["n"] >= 100 and wall["de00_median"] <= 1.0 and wall["de00_p90"] <= 3.0
    assert np.abs(wall["dlab_mean"]).max() < 0.5  # no systematic offset on an identical render
    for mid, row in de["per_material"].items():
        if row["n"] >= setgate.MIN_N and mid not in va.editable_ids:
            assert row["de00_p90"] <= 3.0, (mid, row)
    # the global-blur variant is the same statistic without material isolation
    loose = crossview.delta_e(corr, a, b)
    assert not loose["isolated"] and loose["n"] == len(corr)


@pytest.mark.parametrize("channel,amount", [("b", 6.0), ("L", 6.0)])
def test_shifted_material_is_detected_and_corrected(views, images, corr, channel, amount):
    va, vb = views
    a, b = images
    base = crossview.delta_e(corr, a, b, view_a=va, view_b=vb)["per_material"][WALL]
    mask_b = vb.material_mask(WALL)
    shifted, expected = _shift(b, mask_b, channel, amount)
    de = crossview.delta_e(corr, a, shifted, view_a=va, view_b=vb)
    wall = de["per_material"][WALL]
    # the applied shift is recovered: dE00 of a +6 step depends on where in Lab the material sits (a +6 b* on a
    # low-chroma wall is about 4.3, a +6 L* about 5.6), so the expectation is the analytic value of the step
    assert expected > 3.0
    assert wall["de00_median"] == pytest.approx(expected, abs=1.0)
    assert wall["de00_p90"] == pytest.approx(expected, abs=1.0)
    if channel == "L":
        assert abs(wall["de00_p90"] - 6.0) <= 1.0
    assert wall["dlab_mean"][CHANNEL[channel]] == pytest.approx(-amount, abs=0.5)
    for mid, row in de["per_material"].items():  # the other locked materials are untouched
        if mid != WALL and row["n"] >= setgate.MIN_N and mid not in va.editable_ids:
            assert row["de00_p90"] <= 3.0

    result = setgate.run(va, a, [(vb, shifted)])
    assert result["verdict"] == "fail" and result["cross_view_p90"] > 3.0
    assert result["per_material"][WALL]["hard"] and result["per_material"][WALL]["scored"]
    assert [c["status"] for c in result["checks"] if c["metric_id"] == setgate.METRIC_ID] == ["fail"]
    offsets = result["per_view"][0]["offsets"]  # hero - view: what to add to the view to meet the hero
    assert offsets[WALL][CHANNEL[channel]] == pytest.approx(-amount, abs=0.5)

    fixed = setgate.numeric_correction(
        shifted, (vb.ranks, vb.coverage), {WALL: result["targets"][WALL]}, offsets={WALL: offsets[WALL]}
    )
    assert fixed.dtype == shifted.dtype and fixed.shape == shifted.shape
    after = crossview.delta_e(corr, a, fixed, view_a=va, view_b=vb)["per_material"][WALL]
    assert after["de00_median"] <= 1.0
    assert after["de00_p90"] <= max(1.0, base["de00_p90"] + 0.15)  # back to the render's own noise floor
    assert setgate.run(va, a, [(vb, fixed)])["verdict"] == "pass"
    # untouched ids are bit-identical
    outside = vb.material_share(WALL) == 0
    assert np.array_equal(fixed[outside], shifted[outside])


def test_numeric_correction_absolute_mode_hits_the_hero_target(views, images):
    va, vb = views
    a, b = images
    shifted, _ = _shift(b, vb.material_mask(WALL), "b", 6.0)
    targets = setgate.hero_targets(va, a)
    fixed = setgate.numeric_correction(shifted, (vb.ranks, vb.coverage), {WALL: targets[WALL]})
    region = erode(vb.material_mask(WALL), 3)
    got = srgb_to_lab(fixed)[region].mean(axis=0)
    assert float(ciede2000(got[None, :], np.asarray(targets[WALL]["lab"])[None, :])[0]) <= 0.3


# --------------------------------------------------------------------------- numeric_correction on synthetics


def _two_ids() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    rgb = np.clip(rng.normal(120, 12, (64, 64, 3)), 0, 255).astype(np.uint8)
    ids = np.zeros((64, 64), np.uint32)
    ids[:, :28] = 1
    ids[:, 36:] = 2
    return rgb, ids, ids == 1


def test_numeric_correction_clips_and_leaves_other_ids_untouched():
    rgb, ids, m1 = _two_ids()
    mean1 = srgb_to_lab(rgb)[m1].mean(axis=0)
    target = mean1 + np.array([20.0, -15.0, 0.0])  # far beyond the +-8 / +-6 clips
    out = setgate.numeric_correction(rgb, ids, {"m:00000001": target})
    assert out.dtype == np.uint8 and out.shape == rgb.shape
    assert np.array_equal(out[ids != 1], rgb[ids != 1])
    moved = srgb_to_lab(out)[m1].mean(axis=0) - mean1
    assert moved[0] == pytest.approx(8.0, abs=0.3)
    assert moved[1] == pytest.approx(-6.0, abs=0.3)
    assert moved[2] == pytest.approx(0.0, abs=0.3)
    # chroma-preserving: the per-pixel spread inside the material is kept, only the mean moved
    spread_before = srgb_to_lab(rgb)[m1].std(axis=0)
    spread_after = srgb_to_lab(out)[m1].std(axis=0)
    assert np.allclose(spread_before, spread_after, atol=0.5)
    # an id with too few pixels for a mean is skipped; an unknown target shape is refused
    assert np.array_equal(setgate.numeric_correction(rgb, ids, {"m:00000007": target}), rgb)
    with pytest.raises(ValueError):
        setgate.numeric_correction(rgb, ids, {"m:00000001": [1.0, 2.0]})


def test_numeric_correction_weights_by_coverage_and_keeps_dtype():
    rgb, ids, m1 = _two_ids()
    ranks = ids[..., None]
    coverage = (ids != 0).astype(np.float32)[..., None]
    coverage[:, 20:28, 0] = 0.5  # an anti-aliased edge band of id 1
    out = setgate.numeric_correction(rgb, (ranks, coverage), {}, offsets={"m:00000001": [4.0, 0.0, 0.0]})
    lab_in, lab_out = srgb_to_lab(rgb), srgb_to_lab(out)
    full = (lab_out - lab_in)[:, :20, 0].mean()
    band = (lab_out - lab_in)[:, 20:28, 0].mean()
    assert full == pytest.approx(4.0, abs=0.3)
    assert band == pytest.approx(2.0, abs=0.3)
    assert np.array_equal(out[:, 28:], rgb[:, 28:])
    as_float = rgb.astype(np.float32) / 255.0
    out_f = setgate.numeric_correction(as_float, ids, {}, offsets={"m:00000001": [4.0, 0.0, 0.0]})
    assert out_f.dtype == np.float32 and 0.0 <= out_f.min() and out_f.max() <= 1.0
    as_u16 = (rgb.astype(np.uint16) * 257).astype(np.uint16)
    out_16 = setgate.numeric_correction(as_u16, ids, {}, offsets={"m:00000001": [4.0, 0.0, 0.0]})
    assert out_16.dtype == np.uint16 and np.array_equal(out_16[ids != 1], as_u16[ids != 1])
    with pytest.raises(TypeError):
        setgate.numeric_correction(rgb, "nonsense", {})


# --------------------------------------------------------------------------- the gate


def test_setgate_passes_on_the_same_render(views, images):
    va, vb = views
    a, b = images
    result = setgate.run(va, a, [(vb, b)])
    assert result["verdict"] == "pass"
    assert result["cross_view_p90"] is not None and result["cross_view_p90"] <= 3.0
    assert result["threshold"] == {
        "metric_id": setgate.METRIC_ID,
        "op": "le",
        "value": 3.0,
        "status": "hard",
        "set_id": result["threshold"]["set_id"],
    }
    assert result["hero"]["packet_id"] == va.packet_id
    targets = result["targets"]
    assert len(targets[WALL]["lab"]) == 3 and targets[WALL]["name"] == "ARCH_wall_N"
    assert targets[WALL]["editable"] is False and targets[LEAF]["editable"] is True
    view = result["per_view"][0]
    assert view["packet_id"] == vb.packet_id and view["status"] == "ok" and view["n_corr"] >= 500
    assert view["source"] == "z_backproject"
    assert set(view["offsets"]) == set(view["per_material"])
    wall = result["per_material"][WALL]
    assert wall["hard"] and wall["scored"] and wall["de00_p90"] <= 3.0 and wall["n_dot_v_median"] >= 0.3
    assert all(not r["hard"] for mid, r in result["per_material"].items() if mid in va.editable_ids)
    checks = {c["metric_id"]: c["status"] for c in result["checks"]}
    assert checks[setgate.METRIC_ID] == "pass"

    report = setgate.gate_report(result)
    assert report.verdict == "pass" and report.tier == "A"
    assert [c.metric_id for c in report.checks][0] == setgate.METRIC_ID
    text = report.to_json()
    assert json.loads(text)["metrics"]["cross_view_p90"] == pytest.approx(result["cross_view_p90"])
    assert "offsets" not in text


def test_setgate_with_no_overlap_is_advisory(views, images):
    va, vb = views
    a, b = images
    only_b = [e for e in vb.legend if e.get("id") not in va.material_entries()]
    result = setgate.run(va, a, [(dataclasses.replace(vb, legend=only_b), b)])
    assert result["verdict"] == "advisory"
    assert result["cross_view_p90"] is None and result["per_material"] == {}
    assert result["per_view"][0]["status"] == "insufficient" and result["per_view"][0]["n_corr"] == 0
    assert result["notes"]
    assert setgate.gate_report(result).verdict == "advisory"


def test_setgate_scores_every_view_and_pools_per_material(views, images):
    va, vb = views
    a, b = images
    shifted, _ = _shift(b, vb.material_mask(WALL), "L", 6.0)
    result = setgate.run(va, a, [(vb, b), (vb, shifted)], hard_ids=[WALL])
    assert [v["index"] for v in result["per_view"]] == [0, 1]
    pooled = result["per_material"][WALL]
    assert pooled["n"] == sum(v["per_material"][WALL]["n"] for v in result["per_view"])
    assert result["verdict"] == "fail"  # one bad camera fails the set
    assert all(not r["hard"] for mid, r in result["per_material"].items() if mid != WALL)
    relaxed = setgate.run(va, a, [(vb, b), (vb, shifted)], hard_ids=[WALL], p90_max=10.0)
    assert relaxed["verdict"] == "pass" and relaxed["threshold"]["value"] == 10.0
