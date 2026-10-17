"""gbuffer.py + coc.py: G-buffer structural edges, CoC weighting, edge recall (router plan T15).

Synthetic vectors come from fixtures.synthetic.make_depth_scene (analytic edges, a two-plane scene). The
room_a tests read the committed Blender fixture packet and skip when it is not built yet (T2).
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fixtures.synthetic import make_depth_scene, shift_image

from plate_gates import coc, gbuffer, structure, thresholds
from plate_gates import io as gio
from plate_gates.gbuffer import KIND_DEPTH, KIND_ID, KIND_NORMAL, EdgeParams

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
NATIVE = EdgeParams(width=None)

CAMERA = {
    "focal_length_mm": 35.0,
    "film_width_mm": 36.0,
    "f_number": 2.8,
    "focus_distance": 2.5,
    "dof_enabled": True,
}


@pytest.fixture(scope="module")
def scene():
    return make_depth_scene((256, 256))


@pytest.fixture(scope="module")
def scene_edges(scene):
    z, n, ids, K, _ = scene
    return gbuffer.structural_edges(z, n, ids, K, NATIVE)


@pytest.fixture(scope="module")
def room_a():
    if not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file():
        pytest.skip("fixtures/packets/room_a not built yet (T2)")
    emap, ctx = gbuffer.from_packet(ROOM_A)
    display = gio.load_rgb(ROOM_A / "display.tif")
    zw = gbuffer.resample_z(ctx["z"], emap.width, emap.height)
    weights = coc.weights(zw, ctx["manifest"])
    return emap, ctx, display, weights


def _no_2x2_blocks(m: np.ndarray) -> bool:
    return not (m[:-1, :-1] & m[1:, :-1] & m[:-1, 1:] & m[1:, 1:]).any()


# --------------------------------------------------------------------------- structural edges, synthetic


def test_analytic_edges_recovered(scene, scene_edges):
    """Every analytic edge pixel of the two-plane scene is a G-buffer edge (r = 0 >= 0.98, r = 1 all) and
    nothing else is: the tangent-plane test removes the grazing side face's false depth steps (precision 1)."""
    em = scene_edges
    r0 = structure.tol_metrics(scene.edges, em.edges, 0)
    r1 = structure.tol_metrics(scene.edges, em.edges, 1)
    assert r0["recall"] >= 0.98
    assert r1["recall"] >= 0.999
    assert r1["precision"] >= 0.99
    assert em.params["plane_test"] is True
    raw = gbuffer.structural_edges(*scene[:4], EdgeParams(width=None, plane_test=False))
    assert (
        structure.tol_metrics(scene.edges, raw.edges, 0)["precision"] < 0.9
    )  # the false edges the test removes
    assert raw.params["plane_test"] is False


def test_rendered_image_keeps_every_analytic_edge(scene, scene_edges):
    """The render of the same scene scores recall 1.0 against its geometry; so does its best_shift."""
    res = gbuffer.edge_recall(scene_edges, scene.image, r=2)
    assert res["recall_r2"] >= 0.98
    assert res["precision_r2"] >= 0.98  # a clean Lambert render has no texture edges
    assert res["weighted"] is False and res["plate_visibility"] is None
    assert res["n_ref"] == int(scene_edges.edges.sum())
    assert res["metric_id"] == "gbuffer_edge_recall_r2"
    assert res["detector"] == "rgb" and "canny-rgb" in res["backend"]
    assert all(v >= 0.98 for v in res["recall_by_kind"].values())
    assert res["best_shift"]["recall_r2"] >= 0.98
    assert gbuffer.verdict(res) == ("pass", 0.9)


def test_kind_bits_land_where_the_geometry_says(scene, scene_edges):
    em = scene_edges
    lab = scene.labels
    # the box front / box side crease: same object, continuous depth, 90 deg normal turn -> normal only
    crease = np.zeros_like(scene.edges)
    crease[:, :-1] |= ((lab[:, :-1] == 1) & (lab[:, 1:] == 2)) | ((lab[:, :-1] == 2) & (lab[:, 1:] == 1))
    assert crease.sum() > 50
    assert (em.by_kind("normal") & crease).sum() >= 0.95 * crease.sum()
    assert (em.by_kind(KIND_ID) & crease).sum() <= 2
    assert (em.by_kind(KIND_DEPTH) & crease).sum() <= 2
    # the box / wall silhouette: depth step and id change together
    sil = scene.edges & ~crease
    assert (em.by_kind("depth") & sil).sum() >= 0.98 * sil.sum()
    assert (em.by_kind("id") & sil).sum() >= 0.98 * sil.sum()
    assert em.kind.dtype == np.uint8 and em.edges.dtype == bool and em.strength.dtype == np.float32
    assert (em.strength[em.edges] > 0).all() and em.strength.max() <= 1.0
    assert em.counts()["total"] == int(em.edges.sum())


def test_each_kind_is_one_pixel_thin(scene_edges):
    for kind in (KIND_DEPTH, KIND_NORMAL, KIND_ID):
        assert _no_2x2_blocks(scene_edges.by_kind(kind))


def test_thin_reduces_a_fat_line_and_keeps_end_points():
    m = np.zeros((20, 40), bool)
    m[8:11, 5:35] = True  # 3 px thick line
    t = gbuffer.thin(m)
    assert t.sum() < m.sum() and _no_2x2_blocks(t)
    # still spans the length (Zhang-Suen trims the ends of a thick line by about its half-thickness)
    assert t[:, 7:33].any(axis=0).all()
    assert t[8:11].any(axis=1).sum() >= 1 and t.sum() >= 26
    assert gbuffer.thin(np.zeros((5, 5), bool)).sum() == 0
    dot = np.zeros((5, 5), bool)
    dot[2, 2] = True
    assert gbuffer.thin(dot).sum() == 1  # isolated pixels survive


def test_background_is_excluded_from_depth_and_normal_edges(scene):
    z, n, ids, K, _ = scene
    zb = z.copy()
    zb[:40, :] = 1e10  # nothing rendered in the top band
    em = gbuffer.structural_edges(zb, n, ids, K, NATIVE)
    band = slice(38, 41)
    assert em.by_kind("depth")[band].sum() == 0
    assert em.by_kind("normal")[band].sum() == 0
    # ids are untouched, so no id edge appears there either; an id change against 0 would count
    ids0 = ids.copy()
    ids0[:40, :] = 0
    em0 = gbuffer.structural_edges(zb, n, ids0, K, NATIVE)
    assert em0.by_kind("id")[39, :].sum() >= 250


def test_missing_normals_or_ids_drop_that_kind(scene):
    z, n, ids, K, _ = scene
    no_n = gbuffer.structural_edges(z, None, ids, K, NATIVE)
    assert no_n.counts()["normal"] == 0 and no_n.counts()["id"] > 0 and no_n.params["plane_test"] is False
    no_i = gbuffer.structural_edges(z, n, None, K, NATIVE)
    assert no_i.counts()["id"] == 0 and no_i.counts()["normal"] > 0
    full = gbuffer.structural_edges(z, n, ids, K, NATIVE)
    ranks = np.stack([ids, np.zeros_like(ids)], axis=-1)  # a (H, W, levels) ranks array uses rank 0
    from_ranks = gbuffer.structural_edges(z, n, ranks, K, NATIVE)
    assert np.array_equal(from_ranks.edges, full.edges) and np.array_equal(from_ranks.kind, full.kind)
    assert full.counts()["id"] == no_n.counts()["id"]


def test_working_width_never_upsamples_and_records_itself(scene):
    z, n, ids, K, _ = scene
    default = gbuffer.structural_edges(z, n, ids, K)  # width 2048 requested, source is 256
    assert default.width == 256 and default.params["width_native"] == 256 and default.params["scale"] == 1.0
    assert default.params["resample"] == {
        "z": "nearest",
        "ids": "nearest",
        "normals": "area",
        "images": "area",
    }
    assert default.params["kind_bits"] == {"depth": 1, "normal": 2, "id": 4}
    assert default.params["depth_rel"] == 0.02 and default.params["normal_deg"] == 20.0
    down = gbuffer.structural_edges(z, n, ids, K, EdgeParams(width=128))
    assert down.shape == (128, 128) and down.params["scale"] == 0.5
    assert gbuffer.edge_recall(down, scene.image, r=2, shift_rng=0)["recall_r2"] >= 0.98
    up = gbuffer.structural_edges(z, n, ids, K, EdgeParams(width=512, allow_upsample=True))
    assert up.shape == (512, 512)
    rep = default.to_report()
    assert set(rep) == {"params", "width", "height", "counts"}
    json.dumps(rep)  # report-ready
    assert gbuffer.working_width(640) == 640 and gbuffer.working_width(3840) == 2048
    assert gbuffer.working_size((2160, 3840)) == (2048, 1152)


def test_intrinsics_accept_matrix_and_manifest_blocks():
    k = np.array([[600.0, 0, 320.0], [0, 600.0, 180.0], [0, 0, 1]])
    assert gbuffer.intrinsics(k) == (600.0, 600.0, 320.0, 180.0)
    assert gbuffer.intrinsics(k, 0.5) == (300.0, 300.0, 160.0, 90.0)
    block = {"fx": 600.0, "fy": 610.0, "principal_point": [320.0, 180.0]}
    assert gbuffer.intrinsics(block) == (600.0, 610.0, 320.0, 180.0)
    assert gbuffer.intrinsics({"intrinsics_px": {"K": k.tolist()}}) == (600.0, 600.0, 320.0, 180.0)
    assert gbuffer.intrinsics(None) is None
    with pytest.raises(ValueError):
        gbuffer.intrinsics(np.eye(2))


def test_legend_helpers():
    legend = {
        "entries": [
            {"id": "o:0000000a", "kind": "object", "class": "architecture"},
            {"id": "m:0000000a", "kind": "material", "class": "other"},
        ]
    }
    assert (
        gbuffer.parse_id("o:0000000a") == 10 and gbuffer.parse_id("0x0A") == 10 and gbuffer.parse_id(10) == 10
    )
    assert gbuffer.legend_classes(legend) == {10: "architecture"}
    ids = np.array([[10, 11]], np.uint32)
    cmap, names = gbuffer.class_map(ids, legend)
    assert names == ["architecture"] and cmap.tolist() == [[0, -1]]


# --------------------------------------------------------------------------- CoC


def test_coc_worked_number_from_the_plan():
    """f 35 mm, N 2.8, Zf 2.5 m, Z 4 m -> A 12.5 mm, c_mm 0.0666, 3.79 px at 2048 / 36 mm, w 0.37."""
    z = np.array([[4.0, 2.5]], np.float32)
    mm = coc.coc_mm(z, CAMERA)
    assert mm[0, 0] == pytest.approx(0.0666, abs=0.0005) and mm[0, 1] == 0.0
    px = coc.coc_px(z, CAMERA, 2048)
    assert px[0, 0] == pytest.approx(3.79, abs=0.01) and px.dtype == np.float32
    w = coc.weights(z, CAMERA, 2048)
    assert w[0, 0] == pytest.approx(0.37, abs=0.005) and w[0, 1] == 1.0
    assert coc.c_max_px(2048) == 6.0 and coc.c_max_px(1024) == 3.0


def test_coc_weight_is_width_invariant_and_dof_off_is_one():
    z = np.array([[4.0, 2.5, 1e10, 0.0, np.nan]], np.float32)
    w2048 = coc.weights(z, CAMERA, 2048)
    w640 = coc.weights(z, CAMERA, 640)
    np.testing.assert_allclose(w2048, w640, atol=1e-6)
    assert w2048[0, 2] == 0.0  # the far limit disc A f / (Zf - f) = 10 px > c_max
    assert w2048[0, 3] == 1.0 and w2048[0, 4] == 1.0  # nothing rendered: no weighting
    off = coc.weights(z, {**CAMERA, "dof_enabled": False}, 2048)
    assert (off == 1.0).all()
    # scene units: 400 cm at 250 cm focus with meters_per_unit 0.01 is the same 3.79 px
    cm = coc.coc_px(
        np.array([[400.0]], np.float32), {**CAMERA, "focus_distance": 250.0}, 2048, meters_per_unit=0.01
    )
    assert cm[0, 0] == pytest.approx(3.79, abs=0.01)
    whole = {"camera": {**CAMERA, "focus_distance": 250.0}, "units": {"meters_per_unit": 0.01}}
    assert coc.coc_px(np.array([[400.0]], np.float32), whole, 2048)[0, 0] == pytest.approx(3.79, abs=0.01)


def test_coc_rejects_degenerate_cameras():
    with pytest.raises(ValueError):
        coc.camera_params({**CAMERA, "f_number": None})
    with pytest.raises(ValueError):
        coc.camera_params({**CAMERA, "focus_distance": 0.01})  # 10 mm < f
    with pytest.raises(ValueError):
        coc.weights(np.ones((2, 2), np.float32), CAMERA, 2048, c_max=0.0)
    p = coc.camera_params({**CAMERA, "dof_enabled": False, "f_number": None, "focus_distance": None})
    assert p["dof_enabled"] is False and p["N"] is None


# --------------------------------------------------------------------------- recall semantics (synthetic)


def test_weights_and_visibility_change_the_recall(scene, scene_edges):
    em = scene_edges
    # the box spans x ~146..225; zero weight right of x = 185: only the left part of its edges is asked
    split = 185
    assert em.edges[:, :split].sum() > 100 and em.edges[:, split:].sum() > 100
    w = np.ones(em.shape, np.float32)
    w[:, split:] = 0.0
    blank = scene.image.copy()
    blank[:, split:] = 200  # wipe the right part: its edges are gone
    res = gbuffer.edge_recall(em, blank, w, r=2, shift_rng=0)
    assert res["recall_r2"] >= 0.98 and res["coc_weighted"] is True and res["weighted"] is True
    assert res["recall_r2_unweighted"] < 0.8
    assert res["n_ref_weighted"] == pytest.approx(float(w[em.edges].sum()))
    # visibility: a plate that lacks the right part's edges stops asking for them
    vis = gbuffer.edge_recall(em, blank, r=2, plate_rgb=blank, shift_rng=0)
    assert vis["recall_r2"] == pytest.approx(1.0)
    assert vis["visibility_weighted"] is True and vis["coc_weighted"] is False and vis["weighted"] is True
    assert 0.3 < vis["plate_visibility"] < 0.8
    assert vis["recall_r2_all"] == pytest.approx(vis["recall_r2_unweighted"])
    assert vis["n_ref_visible"] < vis["n_ref"]


def test_recall_by_class_counts_both_sides_of_a_boundary(scene, scene_edges):
    legend = {
        "entries": [
            {"id": scene.wall_id, "kind": "object", "class": "architecture", "name": "ARCH_wall_N"},
            {"id": scene.box_id, "kind": "object", "class": "furniture", "name": "box"},
        ]
    }
    res = gbuffer.edge_recall(scene_edges, scene.image, r=2, ids=scene.ids, legend=legend, shift_rng=0)
    assert set(res["recall_by_class"]) == {"architecture", "furniture"}
    assert all(v >= 0.98 for v in res["recall_by_class"].values())
    assert gbuffer.edge_recall(scene_edges, scene.image, r=2, shift_rng=0)["recall_by_class"] == {}


def test_fidelity_bench_detector_is_selectable_and_reported(scene, scene_edges):
    res = gbuffer.edge_recall(scene_edges, scene.image, r=2, detector="fidelity_bench", shift_rng=0)
    assert res["detector"] == "fidelity_bench" and "fidelity_bench" in res["backend"]
    with pytest.raises(ValueError):
        gbuffer.test_edges(scene.image, "sobel")


def test_test_edges_sees_chroma_edges():
    img = np.full((64, 64, 3), (203, 200, 192), np.uint8)
    img[20:44, 20:44] = (165, 198, 226)  # the synthetic window colour: ~9 levels of luma, 38 of red
    assert gbuffer.test_edges(img).sum() > 60
    assert gbuffer.test_edges(gbuffer.gray_u8(img)).sum() == 0  # gray alone misses it
    assert gbuffer.test_edges(img.astype(np.float32) / 255.0).sum() == gbuffer.test_edges(img).sum()


# --------------------------------------------------------------------------- room_a packet


def test_room_a_display_agrees_with_its_own_geometry(room_a):
    """The plan's sanity vector: the render keeps >= 0.90 of its G-buffer edges (CoC-weighted, as the plan
    specifies the gate). Measured 0.907 with the RGB detector (0.890 unweighted; the gap is the frameless glass
    pane against the sky and the shadowed wall corner, which the render cannot show). With the plate as the
    visibility reference the recall is exactly 1.0."""
    emap, ctx, display, weights = room_a
    assert emap.width == 640 and emap.params["plane_test"] is True
    assert emap.counts()["depth"] > 1000 and emap.counts()["normal"] > 1000 and emap.counts()["id"] > 1000
    res = gbuffer.edge_recall(emap, display, weights, r=2, ids=ctx["ids"], legend=ctx["legend"], shift_rng=0)
    assert res["recall_r2"] >= 0.90, (
        f"self-recall {res['recall_r2']:.3f} < 0.90 (measured 0.907 on the committed fixture; a re-rendered "
        "fixture with different noise moves this by about 0.01)"
    )
    assert res["coc_weighted"] is True and res["precision_r2"] > 0.6
    assert set(res["recall_by_kind"]) == {"depth", "normal", "id"}
    assert "architecture" in res["recall_by_class"] and "vegetation" in res["recall_by_class"]
    assert gbuffer.verdict(res) == ("pass", 0.9) and gbuffer.verdict(res, "preview") == ("pass", 0.85)
    full = gbuffer.edge_recall(emap, display, weights, r=2, plate_rgb=display)
    assert full["recall_r2"] == pytest.approx(1.0)
    assert full["plate_visibility"] == pytest.approx(res["recall_r2"], abs=1e-6)
    assert full["best_shift"]["dx"] == 0 and full["best_shift"]["dy"] == 0
    assert all(v == pytest.approx(1.0) for v in full["recall_by_kind"].values())


def test_room_a_three_pixel_shift_fails_and_best_shift_recovers_it(room_a):
    emap, ctx, display, weights = room_a
    shifted = shift_image(gio.to_uint8(display), 3, 0)  # content moved right by 3 px
    res = gbuffer.edge_recall(emap, shifted, weights, r=2, plate_rgb=display)
    assert res["recall_r2"] < 0.90
    assert gbuffer.verdict(res)[0] == "fail"
    assert (res["best_shift"]["dx"], res["best_shift"]["dy"]) == (-3, 0)
    assert res["best_shift"]["recall_r2"] >= 0.98


def test_room_a_blur_fails_and_grain_does_not_pass_for_free(room_a):
    emap, ctx, display, weights = room_a
    u8 = gio.to_uint8(display)
    blurred = cv2.GaussianBlur(u8, (0, 0), 3.0)
    assert (
        gbuffer.edge_recall(emap, blurred, weights, r=2, plate_rgb=display, shift_rng=0)["recall_r2"] < 0.85
    )
    rng = np.random.default_rng(0)
    grainy = np.clip(u8.astype(np.float32) + rng.normal(0, 8, u8.shape), 0, 255).astype(np.uint8)
    res = gbuffer.edge_recall(emap, grainy, weights, r=2, shift_rng=0)
    clean = gbuffer.edge_recall(emap, u8, weights, r=2, shift_rng=0)
    assert res["recall_r2"] <= clean["recall_r2"] + 0.02  # grain adds test edges but not recall
    assert res["precision_r2"] < clean["precision_r2"]  # and the report shows it


def test_room_a_coc_weights_follow_the_focus(room_a):
    """CAM_a: f/2.8 focused at 2.5 m. Pixels at focus weigh ~1, the far wall weighs well under 1."""
    emap, ctx, display, weights = room_a
    z = ctx["z"]
    w = coc.weights(z, ctx["manifest"])  # native size, whole-manifest form
    near = np.abs(z - 2.5) < 0.05
    far = (z > 5.0) & (z < 1e9)
    assert near.sum() > 1000 and far.sum() > 1000
    assert w[near].min() > 0.95
    assert w[far].max() < 0.2
    assert (weights[emap.edges] < 1.0).any()
    assert thresholds.load().get("gbuffer_edge_recall_r2").resolution_px == 2048


def test_room_a_from_packet_rejects_tier_c(room_a):
    emap, ctx, _, _ = room_a
    m = dict(ctx["manifest"])
    m["tier"] = "C"
    with pytest.raises(ValueError):
        gbuffer.from_packet(ROOM_A, manifest=m)
