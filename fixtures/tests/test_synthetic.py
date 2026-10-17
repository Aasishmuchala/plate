"""The numpy synthetics: each kind carries the ground truth its docstring promises (router plan T2)."""

from __future__ import annotations

import math

import numpy as np
import pytest
from fixtures.synthetic import KINDS, laplacian_variance, make_depth_scene, make_pair, shift_image
from fixtures.synthetic._color import ciede2000, lab_to_srgb8, srgb8_to_lab

import plate_spec as ps

# Sharma, Wu, Dalal (2005) CIEDE2000 test data: (Lab1, Lab2, expected)
SHARMA = [
    ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
    ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
    ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
    ((50.0, -1.3802, -84.2814), (50.0, 0.0, -82.7485), 1.0000),
    ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
    ((50.0, 2.49, -0.001), (50.0, -2.49, 0.0009), 7.1792),
    ((50.0, 2.5, 0.0), (50.0, 0.0, -2.5), 4.3065),
    ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


def _de(a, b) -> np.ndarray:
    return ciede2000(srgb8_to_lab(a), srgb8_to_lab(b))


def _phase_shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int]:
    """Integer (dx, dy) such that b ~ a translated by (dx, dy), by phase correlation of the luma (numpy only)."""
    ga, gb = a.astype(float).mean(-1), b.astype(float).mean(-1)
    cross = np.fft.fft2(gb) * np.conj(np.fft.fft2(ga))
    r = np.fft.ifft2(cross / (np.abs(cross) + 1e-9)).real
    iy, ix = np.unravel_index(int(np.argmax(r)), r.shape)
    h, w = r.shape
    return (ix if ix <= w // 2 else ix - w), (iy if iy <= h // 2 else iy - h)


# --------------------------------------------------------------------------- the colour helpers


@pytest.mark.parametrize(("lab1", "lab2", "expected"), SHARMA)
def test_ciede2000_matches_the_published_vectors(lab1, lab2, expected):
    assert float(ciede2000(np.array(lab1), np.array(lab2))) == pytest.approx(expected, abs=2e-4)
    assert float(ciede2000(np.array(lab2), np.array(lab1))) == pytest.approx(expected, abs=2e-4)


def test_lab_round_trip_is_exact_on_8_bit_values():
    rng = np.random.default_rng(3)
    c = rng.integers(0, 256, (4000, 3), dtype=np.uint8)
    assert np.array_equal(lab_to_srgb8(srgb8_to_lab(c)), c)
    white = srgb8_to_lab(np.array([255, 255, 255], dtype=np.uint8))
    assert white[0] == pytest.approx(100.0, abs=1e-3) and abs(white[1]) < 1e-3 and abs(white[2]) < 1e-3


def test_ciede2000_agrees_with_plate_gates_color_when_available():
    color = pytest.importorskip("plate_gates.color")
    rng = np.random.default_rng(5)
    a = rng.integers(0, 256, (500, 3), dtype=np.uint8)
    b = rng.integers(0, 256, (500, 3), dtype=np.uint8)
    ours = _de(a, b)
    try:
        theirs = np.asarray(color.ciede2000(color.srgb_to_lab(a), color.srgb_to_lab(b)))
    except Exception as e:  # the gates package is mid-edit in some checkouts: not a fixture failure
        pytest.skip(f"plate_gates.color not usable here: {e}")
    assert np.abs(ours - theirs).max() < 5e-3


# --------------------------------------------------------------------------- make_pair contract


@pytest.mark.parametrize("kind", KINDS)
def test_pair_shapes_dtypes_and_determinism(kind):
    ref, test, mask, expected = make_pair(kind)
    assert ref.shape == test.shape == (256, 256, 3) and ref.dtype == test.dtype == np.uint8
    assert mask.shape == (256, 256) and mask.dtype == bool and mask.any()
    assert expected["kind"] == kind
    ref2, test2, mask2, _ = make_pair(kind)
    assert np.array_equal(ref, ref2) and np.array_equal(test, test2) and np.array_equal(mask, mask2)
    if kind not in ("window_added",):  # the depth scene is geometry, not random
        ref3, _, _, _ = make_pair(kind, seed=1)
        assert not np.array_equal(ref, ref3)


@pytest.mark.parametrize("kind", KINDS)
def test_pair_honours_width_height_order(kind):
    ref, test, mask, _ = make_pair(kind, size=(320, 180))
    assert ref.shape == test.shape == (180, 320, 3) and mask.shape == (180, 320)


def test_pair_rejects_unknown_kind_tiny_size_and_misplaced_flag():
    with pytest.raises(ValueError):
        make_pair("nope")
    with pytest.raises(ValueError):
        make_pair("identity", size=(16, 16))
    with pytest.raises(ValueError):
        make_pair("identity", along_edge=True)


def test_identity_is_bit_exact():
    ref, test, mask, e = make_pair("identity")
    assert np.array_equal(ref, test) and e["max_abs_diff"] == 0 and e["de00_max"] == 0.0
    x0, y0, x1, y1 = e["bbox"]
    assert mask[y0:y1, x0:x1].all() and mask.sum() == (x1 - x0) * (y1 - y0)


def test_shift3_is_a_known_translation():
    ref, test, mask, e = make_pair("shift3")
    assert e["shift_px"] == (3.0, 0.0) and e["registration_dx"] == -3.0
    assert _phase_shift(ref, test) == (3, 0)
    assert np.array_equal(test[:, 3:], ref[:, :-3])
    assert np.array_equal(shift_image(test, -3, 0)[:, :-3], ref[:, :-3])
    assert not mask[:, :3].any() and mask[:, 3:].all()


def test_shift_image_moves_content_by_dx_dy():
    img = np.zeros((20, 30, 3), dtype=np.uint8)
    img[5, 7] = (9, 9, 9)
    out = shift_image(img, 4, -2)
    assert tuple(out[3, 11]) == (9, 9, 9)


# --------------------------------------------------------------------------- window_added


def test_window_added_changes_only_the_rectangle_and_touches_no_geometric_edge():
    ref, test, mask, e = make_pair("window_added")
    scene = e["scene"]
    x0, y0, x1, y1 = e["rect"]
    assert e["n_invention_flags"] == 1 and e["wall_id"] == scene.wall_id
    assert np.array_equal(test[~mask], ref[~mask])
    assert (test[mask] != ref[mask]).any(-1).all()
    assert mask.sum() == (x1 - x0) * (y1 - y0)
    grown = np.zeros_like(mask)
    grown[max(y0 - 3, 0) : y1 + 3, max(x0 - 3, 0) : x1 + 3] = True
    assert scene.wall_mask[grown].all(), "the window sits entirely on the wall"
    assert not scene.edges[grown].any(), "and nowhere near an existing edge"
    assert np.array_equal(ref, scene.image)


def test_window_added_along_edge_repaints_exactly_the_box_front():
    ref, test, mask, e = make_pair("window_added", along_edge=True)
    scene = e["scene"]
    assert e["n_invention_flags"] == 0 and e["along_edge"] is True
    assert np.array_equal(mask, scene.labels == 1)
    changed = (test != ref).any(-1)
    # the change is the front face plus its anti-aliased rim: nothing beyond one pixel of the face
    near = mask.copy()
    for _ in range(1):
        p = np.pad(near, 1)
        near = p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
    assert changed[mask].mean() > 0.99 and not changed[~near].any()


# --------------------------------------------------------------------------- make_depth_scene


def test_depth_scene_unpacks_to_five_and_has_the_documented_shapes():
    scene = make_depth_scene()
    z, n, ids, k, wfc = scene
    assert len(scene) == 5
    assert z.shape == (256, 256) and z.dtype == np.float32
    assert n.shape == (256, 256, 3) and n.dtype == np.float32
    assert ids.shape == (256, 256) and ids.dtype == np.uint32
    assert k.shape == (3, 3) and wfc.shape == (4, 4)
    assert scene.z is z and scene.world_from_camera is wfc and scene.K is k
    assert scene.image.shape == (256, 256, 3) and scene.edges.dtype == bool
    assert np.allclose(np.linalg.norm(n, axis=-1), 1.0, atol=1e-6)
    assert set(np.unique(ids)) == {int(scene.wall_id.split(":")[1], 16), int(scene.box_id.split(":")[1], 16)}


def test_depth_scene_depths_are_the_two_planes_and_the_side_face():
    scene = make_depth_scene()
    z, lab = scene.z, scene.labels
    assert np.all(z[lab == 0] == np.float32(4.0))
    assert np.all(z[lab == 1] == np.float32(2.5))
    side = z[lab == 2]
    assert side.min() >= 2.5 - 1e-6 and side.max() <= 3.2 + 1e-6
    assert (lab == 1).sum() > 4000 and (lab == 2).sum() > 100


def test_depth_scene_agrees_with_project_point():
    scene = make_depth_scene((320, 180))
    z = scene.z
    for p, depth in (((-0.6, 4.0, 1.9), 4.0), ((0.8, 2.5, 1.5), 2.5), ((-1.0, 4.0, 0.9), 4.0)):
        u, v, d = ps.project_point(scene.camera, p)
        assert d == pytest.approx(depth, abs=1e-9)
        assert z[int(v), int(u)] == pytest.approx(depth, abs=1e-5)
    # the camera is where the matrix says: a ray through the principal point hits the wall at planar depth 4
    u, v, d = ps.project_point(scene.camera, (0.0, 4.0, 1.5))
    assert (u, v) == pytest.approx((160.0, 90.0)) and d == pytest.approx(4.0)


def test_depth_scene_camera_matrix_is_plate_cam_v0():
    wfc = make_depth_scene().world_from_camera
    r = wfc[:3, :3]
    assert np.allclose(r.T @ r, np.eye(3)) and np.linalg.det(r) == pytest.approx(1.0)
    assert np.allclose(-r[:, 2], (0, 1, 0)), "looks down local -Z = world +Y"
    assert np.allclose(r[:, 1], (0, 0, 1)), "local +Y is world up"
    assert np.allclose(wfc[:3, 3], (0.0, 0.0, 1.5))


def test_depth_scene_edges_are_exactly_the_surface_boundaries():
    scene = make_depth_scene()
    z, edges, lab = scene.z, scene.edges, scene.labels
    # every depth jump and every crease between neighbours is marked on one of its two pixels
    for dz, sl_a, sl_b in (
        (np.abs(z[:, 1:] - z[:, :-1]), (slice(None), slice(None, -1)), (slice(None), slice(1, None))),
        (np.abs(z[1:, :] - z[:-1, :]), (slice(None, -1), slice(None)), (slice(1, None), slice(None))),
    ):
        jump = dz > 0.2
        assert jump.any() and (edges[sl_a][jump]).all()
        del sl_b
    flat = np.zeros_like(edges)
    flat[:, :-1] |= lab[:, :-1] != lab[:, 1:]
    flat[:-1, :] |= lab[:-1, :] != lab[1:, :]
    assert np.array_equal(edges, flat)
    assert 200 < edges.sum() < 900, "thin lines, not regions"


def test_depth_scene_image_agrees_with_its_edges():
    scene = make_depth_scene()
    gray = scene.image.astype(float).mean(-1)
    gx = np.abs(np.diff(gray, axis=1))
    gy = np.abs(np.diff(gray, axis=0))
    strong = np.zeros_like(scene.edges)
    strong[:, :-1] |= gx > 6
    strong[:-1, :] |= gy > 6
    near = scene.edges.copy()
    for _ in range(2):  # dilate by two pixels
        p = np.pad(near, 1)
        near = p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
    assert strong.sum() > 100
    assert (strong & near).sum() / strong.sum() > 0.98, "image edges sit on the analytic edges"
    # and every analytic edge shows up in the image
    dil = strong.copy()
    for _ in range(2):
        p = np.pad(dil, 1)
        dil = p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
    assert (scene.edges & dil).sum() / scene.edges.sum() >= 0.98


def test_depth_scene_box_occludes_a_person_behind_it():
    """The T45 scenario: a point at 6 m behind the 2.5 m box is hidden in the box region and visible elsewhere."""
    scene = make_depth_scene()
    x0, y0, x1, y1 = scene.box_rect
    assert (scene.z[y0:y1, x0:x1] < 4.0).mean() > 0.95
    assert scene.z[5, 5] == pytest.approx(4.0)


# --------------------------------------------------------------------------- material_shift_de5


def test_material_shift_is_five_delta_e_on_the_panel_only():
    ref, test, mask, e = make_pair("material_shift_de5")
    de = _de(ref, test)
    assert np.allclose(de, e["de00_map"], atol=1e-4)
    assert np.percentile(de[mask], 90) == pytest.approx(5.0, abs=0.15)
    assert de[mask].min() > 4.7 and de[mask].max() < 5.3
    assert de[~mask].max() == 0.0 and np.array_equal(ref[~mask], test[~mask])
    assert e["target_de00"] == 5.0 and e["de00_max_unshifted"] == 0.0


def test_material_shift_ids_cover_three_materials_and_the_mask_is_the_shifted_one():
    _, _, mask, e = make_pair("material_shift_de5")
    ids = e["ids"]
    assert ids.dtype == np.uint32 and len(np.unique(ids)) == 3
    shifted = int(e["shifted_id"].split(":")[1], 16)
    assert np.array_equal(ids == shifted, mask)
    assert e["legend"][e["shifted"]] == e["shifted_id"]
    assert set(e["legend"]) == {"ARCH_wall_N", "HARD_floor", "ARCH_panel_7016"}


# --------------------------------------------------------------------------- texture_crunch


def test_texture_crunch_multiplies_laplacian_variance_and_soft_collapses_it():
    ref, test, mask, e = make_pair("texture_crunch")
    assert e["lapvar_ratio"] > 4.0 and e["lapvar_soft_ratio"] < 0.1
    assert laplacian_variance(test, mask) == pytest.approx(e["lapvar_test"])
    assert laplacian_variance(e["test_soft"], mask) == pytest.approx(e["lapvar_soft"])
    assert np.array_equal(test[~mask], ref[~mask]) and np.array_equal(e["test_soft"][~mask], ref[~mask])


# --------------------------------------------------------------------------- feather_leak


def test_feather_leak_changes_a_ring_outside_the_mask_and_the_clean_composite_does_not():
    ref, test, mask, e = make_pair("feather_leak")
    clean = e["test_clean"]
    outside_changed = (test != ref).any(-1) & ~mask
    assert e["outside_identical"] is False and e["leak_px"] == 1
    assert (
        np.array_equal(outside_changed, e["ring_mask"]) and outside_changed.sum() == e["outside_changed_px"]
    )
    assert outside_changed.sum() > 100
    assert np.array_equal(clean[~mask], ref[~mask]) and e["clean_outside_identical"] is True
    # the leak is a visible edit, not rounding noise
    assert _de(ref, test)[outside_changed].min() > 2.0
    # the correct composite feathers inside: pixels at the mask edge are barely changed, the middle fully edited
    edge = _de(ref, clean)[mask]
    assert edge.min() < 1.5 and edge.max() > 8.0
    assert math.isfinite(float(edge.max()))
