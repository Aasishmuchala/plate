"""OCIO colour core: config selection, sRGB round trips, invertibility, DeltaE00 p99."""

from __future__ import annotations

import copy

import numpy as np
import pytest

from plate_gates.color import srgb_to_lab  # noqa: F401  (gates/ must be importable: delta_e00_p99 depends on it)
from plate_pixels import ocio
from plate_pixels.ocio import ColourError, Pipeline

RAMP = np.tile(np.arange(256, dtype=np.float32).reshape(1, 256, 1), (4, 1, 3))  # 256 display levels


def _manifest(
    mode="ocio",
    working="lin_rec709",
    config_name="studio-config-latest",
    display="sRGB - Display",
    view="Un-tone-mapped",
):
    colour = {
        "max_colour_mode": mode,
        "working_space": working,
        "master": "display_tiff16",
        "vfb_corrections_baked": True,
        "display_encoding": {"primaries": "rec709", "transfer": "srgb", "icc": "embedded"},
    }
    if mode == "ocio":
        colour["ocio"] = {
            "config_name": config_name,
            "config_hash": {"algo": "blake3", "hex": "0" * 64},
            "display": display,
            "view": view,
        }
    elif mode == "legacy_gamma":
        colour["legacy_gamma"] = 2.2
    else:
        colour["working_space"] = "display_only"
        colour["master"] = "client_plate"
    return {"colour": colour}


@pytest.fixture(autouse=True)
def _no_env_config(monkeypatch):
    monkeypatch.delenv(ocio.OCIO_CONFIG_ENV, raising=False)


def test_builtin_latest_config_loads_when_the_manifest_names_nothing_known():
    p = Pipeline.from_manifest(_manifest(config_name="vendor-private-config"))
    assert p.mode == "ocio" and p.config_source == "builtin:studio-config-latest"
    assert p.display == "sRGB - Display" and p.view == "Un-tone-mapped" and not p.substituted
    assert p.describe()["config_source"] == p.config_source


def test_manifest_named_builtin_config_wins_over_latest():
    m = _manifest(
        config_name="studio-config-v2.1.0_aces-v1.3_ocio-v2.3", working="acescg", view="ACES 1.0 - SDR Video"
    )
    p = Pipeline.from_manifest(m)
    assert p.config_source == "builtin:studio-config-v2.1.0_aces-v1.3_ocio-v2.3"
    assert p.view == "ACES 1.0 - SDR Video" and not p.substituted


def test_env_config_file_wins_and_missing_file_is_an_error(tmp_path, monkeypatch):
    import PyOpenColorIO as OCIO

    cfg_path = tmp_path / "studio.ocio"
    cfg_path.write_text(
        OCIO.Config.CreateFromBuiltinConfig("studio-config-latest").serialize(), encoding="utf-8"
    )
    monkeypatch.setenv(ocio.OCIO_CONFIG_ENV, str(cfg_path))
    p = Pipeline.from_manifest(_manifest(config_name="studio-config-v2.1.0_aces-v1.3_ocio-v2.3"))
    assert p.config_source == "file:studio.ocio"
    monkeypatch.setenv(ocio.OCIO_CONFIG_ENV, str(tmp_path / "nope.ocio"))
    with pytest.raises(ColourError):
        Pipeline.from_manifest(_manifest())


def test_unknown_view_falls_back_and_says_so():
    p = Pipeline.from_manifest(_manifest(view="ACES 1.0 - SDR Video"))  # not in the v5 config
    assert p.substituted and p.view != "ACES 1.0 - SDR Video"
    assert any("not in config" in n for n in p.notes)


def test_untonemapped_srgb_matches_the_analytic_oetf_and_is_invertible():
    p = Pipeline.from_manifest(_manifest())
    assert p.invertible
    lin = np.linspace(0.0, 1.0, 1001, dtype=np.float32).reshape(-1, 1, 1).repeat(3, axis=2)
    got = p.rederive_display(lin)
    want = ocio.srgb_oetf(lin) * 255.0
    assert got.dtype == np.float32 and got.shape == lin.shape
    assert np.abs(got - want).max() < 0.05


@pytest.mark.parametrize(
    "manifest",
    [_manifest(), _manifest(mode="legacy_gamma")],
    ids=["ocio-untonemapped", "legacy-gamma"],
)
def test_srgb_oetf_round_trip_on_a_256_level_ramp(manifest):
    p = Pipeline.from_manifest(manifest)
    assert p.invertible
    linear = p.display_to_linear(RAMP)
    assert linear.dtype == np.float32 and linear.min() >= 0.0 and linear.max() <= 1.0 + 1e-3
    back = p.rederive_display(linear)
    assert p.delta_e00_p99(RAMP, back) <= 0.05
    assert ocio.delta_e00_p99(RAMP, back) <= 0.05
    assert np.abs(back - RAMP).max() < 0.05  # 0.05 of an 8-bit level


def test_aces_view_is_tone_mapped_and_reports_its_invertibility():
    p = Pipeline.from_manifest(_manifest(working="acescg", view="ACES 2.0 - SDR 100 nits (Rec.709)"))
    grey = np.full((2, 2, 3), 0.18, dtype=np.float32)
    out = p.rederive_display(grey)
    assert out.min() > 0 and out.max() < 255 and np.allclose(out[..., 0], out[..., 1], atol=0.5)
    assert not np.allclose(out, ocio.srgb_oetf(grey) * 255.0, atol=1.0)  # not a bare OETF
    assert isinstance(p.invertible, bool)
    if p.invertible:  # a real round trip inside the display gamut
        mid = np.full((2, 2, 3), 128.0, dtype=np.float32)
        assert np.abs(p.rederive_display(p.display_to_linear(mid)) - mid).max() < 0.5


def test_noop_view_is_not_invertible():
    p = Pipeline.from_manifest(_manifest(view="Raw"))
    assert not p.invertible and any("no-op" in n for n in p.notes)
    with pytest.raises(ColourError):
        p.display_to_linear(RAMP)


def test_tier_c_has_no_linear_source():
    p = Pipeline.from_manifest(_manifest(mode="not_applicable"))
    assert p.mode == "not_applicable" and not p.invertible
    with pytest.raises(ColourError):
        p.rederive_display(np.zeros((2, 2, 3), np.float32))
    with pytest.raises(ColourError):
        p.display_to_linear(RAMP)


def test_unknown_mode_is_rejected():
    bad = copy.deepcopy(_manifest())
    bad["colour"]["max_colour_mode"] = "aces_magic"
    with pytest.raises(ColourError):
        Pipeline.from_manifest(bad)


def test_delta_e00_p99_respects_the_mask():
    a = np.full((20, 20, 3), 100.0, dtype=np.float32)
    b = a.copy()
    b[:5, :5] = (130.0, 100.0, 100.0)  # 25 of 400 pixels differ (6.25 %), well above the 1 % tail
    inside = np.zeros((20, 20), dtype=bool)
    inside[:5, :5] = True
    assert ocio.delta_e00_p99(a, a) == 0.0
    assert ocio.delta_e00_p99(a, b) > 1.0
    assert ocio.delta_e00_p99(a, b, mask=inside) > 1.0
    assert ocio.delta_e00_p99(a, b, mask=~inside) == 0.0  # outside the mask nothing changed
    assert ocio.delta_e00_p99(a, b, mask=np.zeros((20, 20), dtype=bool)) == 0.0
    with pytest.raises(ValueError):
        ocio.delta_e00_p99(a, b[:10])
    with pytest.raises(ValueError):
        ocio.delta_e00_p99(a, b, mask=np.ones((3, 3), dtype=bool))
