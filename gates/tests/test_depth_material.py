"""Depth agreement (Tier C), per-material dE00 by Cryptomatte, and the outside-mask check (router plan T16)."""

from __future__ import annotations

import numpy as np
import pytest
from fixtures.synthetic import make_depth_scene, make_pair

from plate_gates import depth_agreement, iddiff, material_de
from plate_gates.color import ciede2000, srgb_to_lab
from plate_gates.composite import composite_masked
from plate_gates.masks import rect

# --------------------------------------------------------------------------- depth agreement


@pytest.fixture(scope="module")
def scene():
    return make_depth_scene((256, 256))


def test_depth_recovers_known_scale_and_shift(scene):
    a, b = 0.62, 1.35  # Tier C depth is relative: the estimate lives in its own units
    z_est = a * scene.z + b
    r = depth_agreement.compare(scene.z, z_est)
    assert r["scale"] == pytest.approx(a, rel=0.01)
    assert r["shift"] == pytest.approx(b, rel=0.01)
    assert r["abs_rel"] < 1e-6
    assert r["delta_1"] == 1.0
    assert r["edge_f1"] == pytest.approx(1.0)
    assert r["n_valid"] == scene.z.size


def test_depth_recovers_scale_and_shift_through_noise_and_a_mask(scene):
    rng = np.random.default_rng(3)
    a, b = 2.4, -1.1
    z_est = a * scene.z + b + rng.normal(0.0, 0.01, scene.z.shape)
    bad = rect(scene.z.shape, (10, 10, 70, 70))  # an edited region whose depth was destroyed
    z_est[bad] = 9.0
    r_all = depth_agreement.compare(scene.z, z_est)
    r_masked = depth_agreement.compare(scene.z, z_est, mask=~bad)
    assert r_masked["scale"] == pytest.approx(a, rel=0.01)
    assert r_masked["shift"] == pytest.approx(b, abs=0.02)
    assert r_masked["abs_rel"] < 0.01
    assert abs(r_masked["scale"] - a) < abs(
        r_all["scale"] - a
    )  # the destroyed region pulls the unmasked fit off
    assert r_masked["n_valid"] == int((~bad).sum()) < r_all["n_valid"]


def test_depth_ignores_invalid_and_background_pixels(scene):
    z_ref = scene.z.copy().astype(np.float64)
    z_est = 1.5 * z_ref + 0.2
    z_ref[:8] = 1e10  # renderer background
    z_est[:8, :] = np.nan
    z_est[8:16, :] = -1.0
    r = depth_agreement.compare(z_ref, z_est)
    assert r["scale"] == pytest.approx(1.5, rel=0.01)
    assert r["shift"] == pytest.approx(0.2, abs=0.01)
    assert r["n_valid"] == z_ref.size - 16 * z_ref.shape[1]


def test_depth_flags_a_collapsed_estimate(scene):
    """The box smeared into the wall: a flat z_est is fitted by a = 0 (abs_rel looks perfect), corr and edge_f1 are not fooled."""
    z_flat = scene.z.copy()
    x0, y0, x1, y1 = scene.box_rect
    z_flat[y0:y1, x0:x1] = 4.0  # the box front pushed back to the wall: its outline disappears
    r = depth_agreement.compare(scene.z, z_flat)
    assert r["scale"] == pytest.approx(0.0, abs=1e-9)
    assert r["corr"] < depth_agreement.MIN_CORR
    assert r["edge_recall"] == 0.0 and r["edge_f1"] == 0.0


def test_depth_partly_flattened_box_keeps_its_outline(scene):
    z_est = scene.z.copy()
    x0, y0, x1, y1 = scene.box_rect
    z_est[y0:y1, x0:x1] = 3.2  # nearer than the wall by far more than 2%: still a discontinuity
    r = depth_agreement.compare(scene.z, z_est)
    assert r["corr"] > 0.9 and r["edge_f1"] > 0.9


def test_depth_flags_an_invented_edge(scene):
    z_est = scene.z.copy()
    z_est[20:60, 20:60] *= 1.3  # a bump on the wall that is not in the plate
    r = depth_agreement.compare(scene.z, z_est)
    assert r["edge_precision"] < 0.95
    assert r["edge_f1"] < 1.0


def test_depth_flat_scene_agrees_there_are_no_edges():
    z = np.full((64, 64), 3.0)
    r = depth_agreement.compare(z, z * 2.0)
    assert r["edge_f1"] == 1.0
    assert r["abs_rel"] == pytest.approx(0.0, abs=1e-9)


def test_depth_edges_follow_the_two_percent_rule():
    z = np.full((8, 16), 4.0)
    z[:, 8:] = 4.0 * 1.019  # under 2 percent: no edge
    assert not depth_agreement.depth_edges(z).any()
    z[:, 8:] = 4.0 * 1.03
    e = depth_agreement.depth_edges(z)
    assert e[:, 7].all() and e.sum() == 8  # one pixel thick, on the pixel left of the jump


def test_depth_rejects_bad_input(scene):
    with pytest.raises(ValueError, match="shape"):
        depth_agreement.compare(scene.z, scene.z[:-1])
    with pytest.raises(ValueError, match="valid pixels"):
        depth_agreement.compare(scene.z, scene.z, mask=np.zeros(scene.z.shape, bool))
    with pytest.raises(ValueError, match="mask shape"):
        depth_agreement.compare(scene.z, scene.z, mask=np.ones((3, 3), bool))


# --------------------------------------------------------------------------- per-material dE00


def _crypto(expected, ids_key="ids"):
    ids = expected[ids_key]
    return ids[..., None], np.ones(ids.shape + (1,), dtype=np.float32)


def _legend(expected, kind="material"):
    return [{"id": cid, "kind": kind, "name": name} for name, cid in expected["legend"].items()]


@pytest.fixture(scope="module")
def shift_pair():
    return make_pair("material_shift_de5", (256, 256), seed=0)


def test_material_shift_de5_p90_near_5_and_others_untouched(shift_pair):
    ref, test, mask, exp = shift_pair
    ranks, cov = _crypto(exp)
    res = material_de.per_material(ref, test, ranks, cov, _legend(exp))
    assert set(res) == set(exp["legend"].values())
    shifted = res[exp["shifted_id"]]
    assert abs(shifted["de00_p90"] - 5.0) <= 0.5
    assert abs(shifted["de00_median"] - 5.0) <= 0.5
    assert shifted["n_px"] <= int(mask.sum())
    for cid, row in res.items():
        if cid != exp["shifted_id"]:
            assert row["de00_p90"] <= 0.1 and row["de00_median"] <= 0.1
            assert row["lab_mean_plate"] == row["lab_mean_test"]


def test_material_global_blur_bleeds_into_neighbours(shift_pair):
    """The literal fidelity_bench blur stays available; it still sees the shift but smears it over the border."""
    ref, test, _, exp = shift_pair
    ranks, cov = _crypto(exp)
    leaky = material_de.per_material(ref, test, ranks, cov, _legend(exp), isolate=False)
    clean = material_de.per_material(ref, test, ranks, cov, _legend(exp), isolate=True)
    assert abs(leaky[exp["shifted_id"]]["de00_p90"] - 5.0) <= 0.5
    wall = exp["legend"]["ARCH_wall_N"]
    assert leaky[wall]["de00_p90"] > clean[wall]["de00_p90"]


def test_material_target_de_against_a_hero_lab(shift_pair):
    ref, test, _, exp = shift_pair
    ranks, cov = _crypto(exp)
    probe = material_de.per_material(ref, test, ranks, cov, _legend(exp))
    sid = exp["shifted_id"]
    res = material_de.per_material(
        ref,
        test,
        ranks,
        cov,
        _legend(exp),
        targets={sid: probe[sid]["lab_mean_plate"], exp["legend"]["HARD_floor"]: [0.0, 0.0, 0.0]},
    )
    assert res[sid]["target_de"] == pytest.approx(5.0, abs=0.6)  # the plate's colour was the hero target
    assert res[exp["legend"]["HARD_floor"]]["target_de"] > 20.0
    assert "target_de" not in res[exp["legend"]["ARCH_wall_N"]]
    on_target = material_de.per_material(
        ref, test, ranks, cov, _legend(exp), targets={sid: probe[sid]["lab_mean_test"]}
    )
    assert on_target[sid]["target_de"] == pytest.approx(0.0, abs=1e-9)


def test_material_skips_small_ids_and_other_kinds(shift_pair):
    ref, test, _, exp = shift_pair
    ids = exp["ids"].copy()
    tiny = 0xDEADBEEF
    ids[:9, :9] = tiny  # 81 px < 100
    ranks, cov = ids[..., None], np.ones(ids.shape + (1,), dtype=np.float32)
    legend = _legend(exp) + [{"id": "m:deadbeef", "kind": "material", "name": "MAT_tiny"}]
    res = material_de.per_material(ref, test, ranks, cov, legend)
    assert "m:deadbeef" not in res
    only_objects = material_de.per_material(ref, test, ranks, cov, _legend(exp, kind="object"))
    assert only_objects == {}
    assert len(material_de.per_material(ref, test, ranks, cov, _legend(exp, kind="object"), kind=None)) == 3


def test_material_ignores_pixels_below_half_coverage_and_sums_ranks(shift_pair):
    ref, test, _, exp = shift_pair
    sid = exp["shifted_id"]
    u = material_de.id_to_u32(sid)
    ids = exp["ids"]
    ranks = np.stack([ids, np.zeros_like(ids)], axis=-1)
    cov = np.stack([np.ones(ids.shape, np.float32), np.zeros(ids.shape, np.float32)], axis=-1)
    full = material_de.per_material(ref, test, ranks, cov, _legend(exp))[sid]["n_px"]
    # the id's pixels split across two ranks (0.3 + 0.3 = 0.6 >= 0.5) still count; a lone 0.3 does not
    cov2, ranks2 = cov.copy(), ranks.copy()
    panel = ids == u
    cov2[panel, 0], cov2[panel, 1] = 0.3, 0.3
    ranks2[panel, 1] = u
    split = material_de.per_material(ref, test, ranks2, cov2, _legend(exp))[sid]["n_px"]
    assert split == full
    cov2[panel, 1] = 0.0
    assert sid not in material_de.per_material(ref, test, ranks2, cov2, _legend(exp))


def test_material_legend_forms_and_shape_checks(shift_pair):
    ref, test, _, exp = shift_pair
    ranks, cov = _crypto(exp)
    as_file = {"packet_id": "pkt_x", "entries": _legend(exp)}
    as_name_to_id = dict(exp["legend"])  # the fixture's {name: id} form
    a = material_de.per_material(ref, test, ranks, cov, as_file)
    b = material_de.per_material(ref, test, ranks, cov, as_name_to_id, kind=None)
    assert a.keys() == b.keys()
    with pytest.raises(ValueError, match="shape mismatch"):
        material_de.per_material(ref, test[:-1], ranks, cov, as_file)
    with pytest.raises(ValueError):
        material_de.legend_entries({"nonsense": "value"})


def test_material_accepts_16_bit_and_float_images(shift_pair):
    ref, test, _, exp = shift_pair
    ranks, cov = _crypto(exp)
    base = material_de.per_material(ref, test, ranks, cov, _legend(exp))
    as16 = material_de.per_material(
        ref.astype(np.uint16) * 257, test.astype(np.uint16) * 257, ranks, cov, _legend(exp)
    )
    asf = material_de.per_material(ref / 255.0, test / 255.0, ranks, cov, _legend(exp))
    assert as16 == base and asf == base


# --------------------------------------------------------------------------- outside-mask check


@pytest.fixture(scope="module")
def composited():
    ref, _, mask, exp = make_pair("feather_leak", (256, 256), seed=1)
    plate16 = ref.astype(np.uint16) * 257
    ai16 = np.clip(plate16.astype(np.int32) + np.array([9000, -2000, -7000]), 0, 65535).astype(np.uint16)
    out16 = composite_masked(plate16, ai16, mask, feather_px=8, tone_match_ring=False)
    return plate16, out16, mask


def test_outside_mask_identical_for_a_composite(composited):
    plate16, out16, mask = composited
    r = iddiff.outside_mask_check(plate16, out16, mask)
    assert r["identical_outside"] is True
    assert r["de00_max_outside"] == 0.0
    assert r["n_changed_outside"] == 0
    assert r["feather_band_p99"] > 0.0  # the edit does reach into the band
    assert r["n_band_px"] > 0


def test_outside_mask_catches_a_one_pixel_leak(composited):
    plate16, out16, mask = composited
    leaky = out16.copy()
    ys, xs = np.nonzero(mask)
    y, x = ys.min() - 1, xs.min()  # a single outside pixel, right against the mask edge
    assert not mask[y, x]
    leaky[y, x, 0] += 1
    r = iddiff.outside_mask_check(plate16, leaky, mask)
    assert r["identical_outside"] is False
    assert r["n_changed_outside"] == 1
    assert 0.0 < r["de00_max_outside"] < 0.1  # one 16-bit level is invisible, but bit equality still fails
    leaky[y, x] = plate16[y, x]
    assert iddiff.outside_mask_check(plate16, leaky, mask)["identical_outside"] is True


def test_outside_mask_catches_the_feather_leak_fixture():
    ref, test, mask, exp = make_pair("feather_leak", (256, 256), seed=0)
    clean = exp["test_clean"]
    ok = iddiff.outside_mask_check(ref.astype(np.uint16) * 257, clean.astype(np.uint16) * 257, mask)
    bad = iddiff.outside_mask_check(ref.astype(np.uint16) * 257, test.astype(np.uint16) * 257, mask)
    assert ok["identical_outside"] is True and ok["de00_max_outside"] == 0.0
    assert bad["identical_outside"] is False
    assert bad["n_changed_outside"] == exp["outside_changed_px"]
    assert bad["de00_max_outside"] > 0.5


def test_feather_band_is_the_12px_ring_inside_the_mask():
    h = w = 96
    mask = rect((h, w), (20, 20, 76, 76))
    plate = np.full((h, w, 3), 20000, np.uint16)
    out = plate.copy()
    band = iddiff.feather_band(mask)
    assert band[20, 40] and band[31, 40] and not band[32, 40]  # pixels 1..12 deep
    assert not band[19, 40]  # outside the mask
    out[32:64, 32:64] += 6000  # the deep interior only: nothing in the band moved
    assert iddiff.outside_mask_check(plate, out, mask)["feather_band_p99"] == 0.0
    out[31, 32:64] += 6000  # the 12th ring pixel moved
    assert iddiff.outside_mask_check(plate, out, mask)["feather_band_p99"] > 0.0


def test_feather_band_does_not_treat_the_frame_border_as_outside():
    mask = rect((64, 64), (0, 0, 40, 64))
    band = iddiff.feather_band(mask)
    assert not band[:, 0].any() and band[30, 39] and not band[30, 20]


def test_outside_mask_accepts_8_bit_and_rejects_mismatches(composited):
    plate16, out16, mask = composited
    plate8 = (plate16 // 257).astype(np.uint8)
    r8 = iddiff.outside_mask_check(plate8, plate8.copy(), mask)
    assert r8["identical_outside"] is True and r8["feather_band_p99"] == 0.0
    with pytest.raises(ValueError, match="shape mismatch"):
        iddiff.outside_mask_check(plate16, out16[:-1], mask)
    with pytest.raises(ValueError, match="uint16"):
        iddiff.outside_mask_check(plate16.astype(np.float32), out16, mask)


def test_outside_mask_de00_uses_ciede2000_on_the_leaked_pixels():
    h = w = 32
    plate = np.full((h, w, 3), 30000, np.uint16)
    out = plate.copy()
    out[0, 0] = (30000, 30000, 40000)
    mask = rect((h, w), (10, 10, 20, 20))
    got = iddiff.outside_mask_check(plate, out, mask)["de00_max_outside"]
    want = float(
        ciede2000(
            srgb_to_lab(np.array([30000, 30000, 30000]) * 255.0 / 65535.0),
            srgb_to_lab(np.array([30000, 30000, 40000]) * 255.0 / 65535.0),
        )
    )
    assert got == pytest.approx(want)
