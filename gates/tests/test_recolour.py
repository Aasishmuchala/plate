"""Recolour by element recomposition (router plan T31): the synthetic model exactly, then the Blender fixture
room_a (panel at RAL 7016) recoloured to RAL 3020 against room_a_ral3020, the same camera re-rendered with that
albedo. Scores are in display space (the packet's OCIO pipeline), sigma-blurred inside the mask as material_de
does (two 32-spp Cycles renders carry independent Monte-Carlo noise; the raw per-pixel median is reported too).

Measured on this Mac (docs/gates/recolour.md): recomposition 0.11, ratio 0.10, degraded 5.9 dE00 median; the
reverse direction (red to grey) 1.0 because the fixture truncates the raw lighting layers' low mantissa bits and a
15x brighter diffuse term amplifies that bias.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from plate_gates import recolour
from plate_gates.color import ciede2000, hex_to_rgb8, srgb_to_lab
from plate_gates.masks import erode, rect
from plate_gates.recolour import RecolourError, apply, hex_lab, lab_to_linear, linear_to_lab, ral_lab

PACKETS = Path(__file__).resolve().parents[2] / "fixtures" / "packets"
PANEL = "o:735a8655"  # ARCH_panel_7016 in room_a / room_a_ral3020 (same object, albedo changed)


def _packet(name: str) -> Path:
    d = PACKETS / name
    if not (d / "manifest.json").is_file():
        pytest.skip(f"fixture packet missing: {d} (T2 renders it)")
    return d


# --------------------------------------------------------------------------- colour helpers


def test_ral_table_matches_the_fixture_hexes_and_round_trips():
    assert np.allclose(ral_lab("3020"), srgb_to_lab(hex_to_rgb8("#CC0605"))[0])
    assert np.allclose(ral_lab("RAL 7016"), hex_lab("#383E42"))
    assert np.allclose(ral_lab(7016), ral_lab("7016"))
    with pytest.raises(KeyError):
        ral_lab("0000")
    for code in recolour.RAL_SRGB:
        lab = ral_lab(code)
        lin = lab_to_linear(lab)
        assert lin.shape == (3,) and np.all(lin >= 0) and np.all(lin <= 1)
        assert np.allclose(linear_to_lab(lin), lab, atol=1e-6)


# --------------------------------------------------------------------------- synthetic: the model, exactly


def _synthetic(rho, rho_new, size=(48, 64), seed=0, f=0.05):
    """Elements built from the model with a per-pixel lighting field: (elements_src, beauty_rerender, mask)."""
    rng = np.random.default_rng(seed)
    h, w = size
    light = rng.uniform(0.2, 1.2, (h, w, 1)).astype(np.float32) * np.array([1.0, 0.97, 0.9], np.float32)
    gi = rng.uniform(0.05, 0.3, (h, w, 1)).astype(np.float32) * np.array([0.9, 1.0, 1.1], np.float32)
    refl = rng.uniform(0.0, 0.08, (h, w, 3)).astype(np.float32)
    mask = rect((h, w), (10, 8, 50, 40))
    bg = np.array([0.3, 0.25, 0.2], np.float32)
    df = np.where(mask[..., None], (np.asarray(rho, np.float32) * (1 - f))[None, None], bg[None, None])
    df_new = np.where(
        mask[..., None], (np.asarray(rho_new, np.float32) * (1 - f))[None, None], bg[None, None]
    )
    beauty = df * (light + gi) + refl
    beauty_new = df_new * (light + gi) + refl
    els = {
        "beauty": np.concatenate([beauty, np.ones((h, w, 1), np.float32)], axis=2),
        "diffuse_filter": df,
        "raw_lighting": light.repeat(3, axis=2) if light.shape[2] == 1 else light,
        "raw_gi": gi.repeat(3, axis=2) if gi.shape[2] == 1 else gi,
        "reflection": refl,
    }
    return els, beauty_new, mask


def test_recomposition_reproduces_a_synthetic_rerender_exactly():
    rho, rho_new = (0.04, 0.05, 0.06), (0.6, 0.02, 0.015)
    els, truth, mask = _synthetic(rho, rho_new)
    res = apply(els, mask, linear_to_lab(rho_new), source_lab=linear_to_lab(rho))
    assert res["method"] == "recomposition" and res["expected_de00"] == 1.0 and not res["degraded"]
    assert res["source_albedo_origin"] == "given"
    assert np.allclose(res["rgb_linear"][mask], truth[mask], atol=2e-6)
    # outside the mask the beauty is copied bit for bit
    assert np.array_equal(res["rgb_linear"][~mask], els["beauty"][~mask, :3])
    assert res["elements_used"] == ["beauty", "diffuse_filter", "raw_lighting", "raw_gi"]


def test_source_estimate_from_the_diffuse_filter_recovers_the_albedo():
    rho, rho_new = (0.04, 0.05, 0.06), (0.6, 0.02, 0.015)
    els, truth, mask = _synthetic(rho, rho_new, f=0.04)  # the synthetic dims by (1 - f0) like a renderer
    res = apply(els, mask, linear_to_lab(rho_new))  # no source given, no normals: constant Fresnel
    assert res["source_albedo_origin"] == "estimated"
    assert res["fresnel"]["fresnel"] == "constant"
    assert np.allclose(res["source_albedo_linear"], rho, rtol=1e-4)
    assert np.allclose(res["rgb_linear"][mask], truth[mask], atol=1e-5)
    # a legend albedo wins over the estimate
    res2 = apply(els, mask, linear_to_lab(rho_new), {"id": "o:1", "albedo_lab": linear_to_lab(rho)})
    assert res2["source_albedo_origin"] == "legend:albedo_lab"
    assert np.allclose(res2["rgb_linear"][mask], truth[mask], atol=2e-6)


def test_ratio_and_degraded_fallbacks_and_their_errors():
    rho, rho_new = (0.04, 0.05, 0.06), (0.6, 0.02, 0.015)
    els, truth, mask = _synthetic(rho, rho_new)
    no_raw = {k: v for k, v in els.items() if k not in ("raw_lighting", "raw_gi")}
    res = apply(no_raw, mask, linear_to_lab(rho_new), source_lab=linear_to_lab(rho))
    assert res["method"] == "ratio" and res["expected_de00"] == 2.5 and not res["degraded"]
    assert np.allclose(
        res["rgb_linear"][mask], truth[mask], atol=2e-6
    )  # beauty - reflection is the diffuse share
    bare = {"beauty": els["beauty"], "diffuse_filter": els["diffuse_filter"]}
    res = apply(bare, mask, linear_to_lab(rho_new), source_lab=linear_to_lab(rho))
    assert res["method"] == "degraded" and res["degraded"] and res["expected_de00"] == 4.0
    assert res["specular_share"] is None and not res["metal"]
    # tinting the reflections is the degraded error: the output is brighter than the truth in the red channel
    assert np.all(res["rgb_linear"][mask][:, 0] >= truth[mask][:, 0] - 1e-6)
    # forcing a method the elements cannot support is an error, not a silent fallback
    with pytest.raises(RecolourError):
        apply(no_raw, mask, linear_to_lab(rho_new), method="recomposition")
    with pytest.raises(RecolourError):
        apply(bare, mask, linear_to_lab(rho_new), method="ratio")
    with pytest.raises(RecolourError):
        apply({"beauty": els["beauty"]}, mask, linear_to_lab(rho_new))  # no df, no source albedo
    with pytest.raises(ValueError):
        apply(els, mask[:-1], linear_to_lab(rho_new))
    with pytest.raises(ValueError):
        apply(els, mask, linear_to_lab(rho_new), method="magic")


def test_soft_coverage_blends_and_zero_weight_pixels_are_untouched():
    rho, rho_new = (0.04, 0.05, 0.06), (0.6, 0.02, 0.015)
    els, truth, mask = _synthetic(rho, rho_new)
    full = apply(els, mask, linear_to_lab(rho_new), source_lab=linear_to_lab(rho))["rgb_linear"]
    cov = mask.astype(np.float32) * 0.5
    half = apply(els, cov, linear_to_lab(rho_new), source_lab=linear_to_lab(rho))["rgb_linear"]
    beauty = els["beauty"][..., :3]
    assert np.allclose(half[mask], 0.5 * (beauty[mask] + full[mask]), atol=2e-6)
    assert np.array_equal(half[~mask], beauty[~mask])
    empty = apply(els, np.zeros_like(mask), linear_to_lab(rho_new), source_lab=linear_to_lab(rho))
    assert empty["n_px"] == 0 and np.array_equal(empty["rgb_linear"], beauty)


def test_metal_flag_from_the_specular_energy():
    rho, rho_new = (0.5, 0.5, 0.5), (0.6, 0.02, 0.015)
    els, _, mask = _synthetic(rho, rho_new)
    low = apply(els, mask, linear_to_lab(rho_new), source_lab=linear_to_lab(rho))
    assert not low["metal"] and low["specular_share"] < 0.3
    shiny = dict(els)
    shiny["specular"] = np.full_like(els["reflection"], 0.6)  # a specular element outranks reflection
    hi = apply(shiny, mask, linear_to_lab(rho_new), source_lab=linear_to_lab(rho))
    assert hi["metal"] and hi["specular_share"] > 0.3
    assert any(note.startswith("metal:") for note in hi["notes"])


# --------------------------------------------------------------------------- the fixture re-render harness


@pytest.fixture(scope="module")
def report():
    """room_a -> RAL 3020 scored against room_a_ral3020 with every method, source albedo estimated."""
    return recolour.validate_against_rerender(
        _packet("room_a"), _packet("room_a_ral3020"), PANEL, ral_lab("3020")
    )


def test_fixture_identity_holds(report):
    """beauty ~= diffuse_filter x (raw_lighting + raw_gi) + reflection on the panel (within the fixture's ~2%)."""
    pk = recolour.load_packet(_packet("room_a"))
    els = pk["elements"]
    mask, _, _ = recolour._select(pk, PANEL)
    m = erode(mask, 3)
    recon = els["diffuse_filter"] * (els["raw_lighting"] + els["raw_gi"]) + els["reflection"]
    beauty = els["beauty"][..., :3]
    rel = np.abs(beauty[m] - recon[m]).sum() / beauty[m].sum()
    assert rel < 0.02
    assert report["before"]["de00_median"] > 20  # grey to saturated red: the move is large


def test_recomposition_matches_the_rerender_within_1_de00(report):
    r = report["methods"]["recomposition"]
    assert r["source_albedo_origin"] == "estimated" and r["fresnel"]["fresnel"] == "schlick_per_pixel"
    assert r["de00_median"] <= 1.0 and r["de00_p90"] <= 1.0
    assert r["de00_of_means"] <= 0.5
    assert r["passes"] and r["outside_identical"]
    assert not r["metal"] and 0.1 < r["specular_share"] < 0.3
    # the estimate lands on the albedo the scene was built from (RAL 7016, sRGB #383E42) within 2%
    assert np.allclose(r["source_albedo_linear"], lab_to_linear(ral_lab("7016")), rtol=0.02)
    # the raw per-pixel median sits at the two renders' noise floor, well above the blurred number
    assert 0.5 < r["de00_median_px"] < 2.0


def test_recomposition_with_the_known_source_swatch():
    rep = recolour.validate_against_rerender(
        _packet("room_a"),
        _packet("room_a_ral3020"),
        PANEL,
        ral_lab("3020"),
        source_lab=ral_lab("7016"),
        methods=("recomposition",),
    )
    r = rep["methods"]["recomposition"]
    assert r["source_albedo_origin"] == "given"
    assert r["de00_median"] <= 1.0 and r["de00_of_means"] <= 0.5 and r["passes"]


def test_fresnel_aware_estimate_beats_the_plain_median():
    """Substituting the bare albedo (the plain median of df, i.e. the literal rho' form) overshoots by 1/(1-F)."""
    plain = recolour.validate_against_rerender(
        _packet("room_a"),
        _packet("room_a_ral3020"),
        PANEL,
        ral_lab("3020"),
        methods=("recomposition",),
        f0=None,
    )["methods"]["recomposition"]
    fres = recolour.validate_against_rerender(
        _packet("room_a"), _packet("room_a_ral3020"), PANEL, ral_lab("3020"), methods=("recomposition",)
    )["methods"]["recomposition"]
    assert plain["de00_of_means"] > 2 * fres["de00_of_means"]
    assert plain["de00_median"] <= 1.0  # still inside the gate, just worse


def test_ratio_fallback_within_2_5(report):
    r = report["methods"]["ratio"]
    assert r["method"] == "ratio" and r["elements_used"] == ["beauty", "reflection"]
    assert r["de00_median"] <= 2.5 and r["passes"] and r["outside_identical"]


def test_degraded_path_is_flagged_and_fails_its_expectation_on_a_dark_glossy_panel(report):
    """Whole-beauty scaling tints the panel's reflections (24% of its energy) by the 15x red ratio: measured
    5.9 dE00 median, above the 4.0 the plan expected for this path. The gate reports it as a failure, which is
    the point of the number."""
    r = report["methods"]["degraded"]
    assert r["degraded"] and r["expected_de00"] == 4.0 and r["outside_identical"]
    assert 4.0 < r["de00_median"] < 7.5
    assert not r["passes"]
    assert any(note.startswith("degraded:") for note in r["notes"])


def test_reverse_direction_red_to_grey():
    """room_a_ral3020 -> RAL 7016 against room_a: the fixture's truncated raw layers bias this direction (a 15x
    brighter diffuse term), so recomposition is held to 1.5 and the ratio path, which uses the beauty minus the
    reflection, stays under 1.0."""
    rep = recolour.validate_against_rerender(
        _packet("room_a_ral3020"),
        _packet("room_a"),
        PANEL,
        ral_lab("7016"),
        methods=("recomposition", "ratio"),
    )
    assert rep["methods"]["recomposition"]["de00_median"] <= 1.5
    assert rep["methods"]["ratio"]["de00_median"] <= 1.0


def test_material_id_selects_the_same_panel(report):
    rep = recolour.validate_against_rerender(
        _packet("room_a"),
        _packet("room_a_ral3020"),
        "m:735a8655",
        ral_lab("3020"),
        methods=("recomposition",),
    )
    assert rep["n_px"] == report["n_px"]
    assert rep["methods"]["recomposition"]["de00_median"] == pytest.approx(
        report["methods"]["recomposition"]["de00_median"], abs=1e-6
    )


def test_harness_refuses_a_different_camera():
    with pytest.raises(RecolourError, match="same view"):
        recolour.validate_against_rerender(_packet("room_a"), _packet("room_b"), PANEL, ral_lab("3020"))
    with pytest.raises(RecolourError, match="covers no pixel"):
        recolour.validate_against_rerender(
            _packet("room_a"), _packet("room_a_ral3020"), "o:00000001", ral_lab("3020")
        )


def test_report_is_json_safe(report):
    import json

    text = json.dumps(report)
    assert "rgb_linear" not in text
    assert report["pipeline"]["mode"] == "legacy_gamma"
    assert report["legend"]["name"] == "ARCH_panel_7016"


def test_compare_display_scores_a_shifted_patch():
    """A smooth image with a flat +12 red shift inside the mask: the blurred and per-pixel numbers agree (the
    normalised blur never mixes in the untouched outside), and noise on top only moves the per-pixel median."""
    ramp = np.linspace(100.0, 140.0, 64, dtype=np.float32)[:, None]
    a = np.repeat(np.repeat(ramp[..., None], 64, axis=1), 3, axis=2)
    b = a.copy()
    mask = rect((64, 64), (8, 8, 56, 56))
    b[mask] += np.array([12.0, 0.0, 0.0], np.float32)
    r = recolour.compare_display(a, b, mask, sigma=2.0)
    de = ciede2000(srgb_to_lab(a[mask]), srgb_to_lab(b[mask]))
    assert r["n_px"] == int(erode(mask, 3).sum())
    assert r["de00_median_px"] == pytest.approx(float(np.median(de[erode(mask, 3)[mask]])), abs=1e-6)
    assert r["de00_median"] == pytest.approx(r["de00_median_px"], rel=0.02)
    assert r["de00_of_means"] == pytest.approx(r["de00_median_px"], rel=0.05)
    noisy = b + np.random.default_rng(1).normal(0.0, 6.0, b.shape).astype(np.float32)
    rn = recolour.compare_display(a, noisy, mask, sigma=2.0)
    assert rn["de00_median"] == pytest.approx(r["de00_median"], rel=0.15)  # the blur removes the noise
    assert rn["de00_median_px"] > r["de00_median_px"]  # the per-pixel number counts it
    assert recolour.compare_display(a, a, mask)["de00_median"] == 0.0
    assert recolour.compare_display(a, b, np.zeros((64, 64), bool))["n_px"] == 0
