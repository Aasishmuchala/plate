"""lightmix.py: NNLS gains on light selects (recovery, non-negativity, mask, clip, determinism, presets)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from plate_gates import lightmix

H, W = 48, 64


def _lights(h: int = H, w: int = W, seed: int = 7) -> dict[str, np.ndarray]:
    """Three lights with different spatial falloff and colour cast, so the system is well conditioned."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    u, v = xx / (w - 1), yy / (h - 1)
    sun = np.stack([1.0 * u, 0.8 * u * v + 0.05, 0.4 * (1 - v)], -1) + 0.02 * rng.random((h, w, 3))
    sky = np.stack([0.3 * (1 - u), 0.5 * v + 0.1, 0.9 * (1 - u) * v + 0.1], -1) + 0.02 * rng.random((h, w, 3))
    lamp = np.stack(
        [0.9 * np.exp(-((u - 0.2) ** 2 + (v - 0.7) ** 2) * 8), 0.6 * (1 - u) * (1 - v), 0.1 * v], -1
    )
    return {"sun": sun, "sky": sky, "interior_lamp": lamp + 0.01 * rng.random((h, w, 3))}


def _mix(lights: dict[str, np.ndarray], gains: dict[str, float]) -> np.ndarray:
    return sum(g * lights[n] for n, g in gains.items())


def test_recovers_known_gains():
    lights = _lights()
    truth = {"sun": 0.5, "sky": 2.0, "interior_lamp": 0.0}
    out = lightmix.solve(lights, _mix(lights, truth))
    for n, g in truth.items():
        assert out["gains"][n] == pytest.approx(g, abs=1e-3), n
    assert out["residual_rms"] < 1e-3
    assert out["clipped"] == []


def test_recompose_of_solved_gains_matches_target():
    lights = _lights()
    target = _mix(lights, {"sun": 0.5, "sky": 2.0, "interior_lamp": 0.0})
    out = lightmix.solve(lights, target)
    rec = lightmix.recompose(lights, out["gains"])
    assert rec.shape == (H, W, 3) and rec.dtype == np.float32
    assert float(np.sqrt(np.mean((rec - target) ** 2))) < 1e-3


def test_negative_gain_is_impossible():
    lights = _lights()
    # The exact answer needs sky at -1.0: NNLS must refuse it and settle at the boundary.
    target = _mix(lights, {"sun": 1.0, "sky": -1.0, "interior_lamp": 0.5})
    out = lightmix.solve(lights, target)
    assert all(g >= 0.0 for g in out["gains"].values())
    assert out["gains"]["sky"] == 0.0
    assert out["residual_rms"] > 1e-2  # honest: the target is not reachable with real lights
    # A target darker than any non-negative mix: every gain is zero, the residual is the target's own RMS.
    dark = lightmix.solve(lights, -0.5 * np.ones((H, W, 3)))
    assert set(dark["gains"].values()) == {0.0}
    assert dark["residual_rms"] == pytest.approx(0.5)


def test_recompose_rejects_bad_gains():
    lights = _lights()
    with pytest.raises(ValueError, match="gain for 'sun'"):
        lightmix.recompose(lights, {"sun": -0.1})
    with pytest.raises(ValueError, match="gain for 'sky'"):
        lightmix.recompose(lights, {"sky": float("nan")})
    with pytest.raises(ValueError, match="unknown light selects"):
        lightmix.recompose(lights, {"moon": 1.0})


def test_recompose_missing_gain_means_light_off():
    lights = _lights()
    rec = lightmix.recompose(lights, {"sun": 2.0})
    assert np.allclose(rec, 2.0 * lights["sun"], atol=1e-6)
    assert np.array_equal(lightmix.recompose(lights, {}), np.zeros((H, W, 3), np.float32))


def test_mask_ignores_pixels_outside():
    lights = _lights()
    truth = {"sun": 0.5, "sky": 2.0, "interior_lamp": 0.25}
    target = _mix(lights, truth)
    mask = np.zeros((H, W), bool)
    mask[8:40, 10:50] = True
    corrupted = target.copy()
    corrupted[~mask] = 50.0  # garbage everywhere outside the mask
    masked = lightmix.solve(lights, corrupted, mask=mask)
    for n, g in truth.items():
        assert masked["gains"][n] == pytest.approx(g, abs=1e-3), n
    assert masked["residual_rms"] < 1e-3
    unmasked = lightmix.solve(lights, corrupted)
    assert abs(unmasked["gains"]["sky"] - 2.0) > 0.1  # without the mask the garbage drags the fit


@pytest.mark.parametrize(
    "encode", [lambda m: m, lambda m: m.astype(np.uint8) * 255, lambda m: m.astype(np.float32)]
)
def test_mask_encodings(encode):
    lights = _lights()
    target = _mix(lights, {"sun": 1.5, "sky": 0.25, "interior_lamp": 0.0})
    mask = np.zeros((H, W), bool)
    mask[:, : W // 2] = True
    out = lightmix.solve(lights, target, mask=encode(mask))
    assert out["gains"]["sun"] == pytest.approx(1.5, abs=1e-3)


def test_max_gain_clips_and_reports():
    lights = _lights()
    target = _mix(lights, {"sun": 12.0, "sky": 1.0, "interior_lamp": 0.5})
    out = lightmix.solve(lights, target, max_gain=8.0)
    assert out["gains"]["sun"] == 8.0
    assert out["clipped"] == ["sun"]
    assert out["residual_rms"] > 0.05  # the clipped recomposite is measurably off, and the report says so
    free = lightmix.solve(lights, target, max_gain=20.0)
    assert free["gains"]["sun"] == pytest.approx(12.0, abs=1e-3)
    assert free["clipped"] == []


def test_large_image_uses_seeded_sample_and_is_deterministic():
    lights = _lights(128, 96)  # 12288 px > 4096 sample
    truth = {"sun": 0.75, "sky": 1.25, "interior_lamp": 0.4}
    target = _mix(lights, truth)
    a = lightmix.solve(lights, target)
    b = lightmix.solve(lights, target)
    assert a == b
    for n, g in truth.items():
        assert a["gains"][n] == pytest.approx(g, abs=1e-3), n
    # Noisy target: a different seed picks different pixels, the answer stays close.
    noisy = target + np.random.default_rng(3).normal(0, 0.01, target.shape)
    n1 = lightmix.solve(lights, noisy, seed=1)
    n2 = lightmix.solve(lights, noisy, seed=2)
    assert n1["gains"] != n2["gains"]
    for n, g in truth.items():
        assert n1["gains"][n] == pytest.approx(g, abs=0.05)
        assert n2["gains"][n] == pytest.approx(g, abs=0.05)


def test_sample_is_capped_at_4096_pixels(monkeypatch):
    seen = {}
    real = lightmix.nnls

    def spy(a, b, **kw):
        seen["rows"] = a.shape[0]
        return real(a, b, **kw)

    monkeypatch.setattr(lightmix, "nnls", spy)
    lights = _lights(128, 96)
    lightmix.solve(lights, _mix(lights, {"sun": 1.0, "sky": 1.0, "interior_lamp": 1.0}))
    assert seen["rows"] == 3 * 4096
    seen.clear()
    small = _lights(20, 30)
    lightmix.solve(small, _mix(small, {"sun": 1.0, "sky": 1.0, "interior_lamp": 1.0}))
    assert seen["rows"] == 3 * 20 * 30


def test_non_finite_pixels_are_dropped():
    lights = _lights()
    truth = {"sun": 0.5, "sky": 2.0, "interior_lamp": 0.0}
    target = _mix(lights, truth)
    lights["sun"] = lights["sun"].copy()
    lights["sun"][3, 4, 1] = np.inf
    target[10, 10, 0] = np.nan
    out = lightmix.solve(lights, target)
    assert np.isfinite(out["residual_rms"])
    assert out["gains"]["sky"] == pytest.approx(2.0, abs=1e-3)


def test_single_light_and_float32_inputs():
    lights = {"only": _lights()["sun"].astype(np.float32)}
    out = lightmix.solve(lights, 3.0 * lights["only"])
    assert out["gains"]["only"] == pytest.approx(3.0, abs=1e-4)


def test_duplicate_lights_stay_finite():
    sun = _lights()["sun"]
    out = lightmix.solve({"a": sun, "b": sun.copy()}, 2.0 * sun)
    assert sum(out["gains"].values()) == pytest.approx(2.0, abs=1e-3)
    assert out["residual_rms"] < 1e-3


def test_result_is_plain_json():
    lights = _lights()
    out = lightmix.solve(lights, _mix(lights, {"sun": 1.0, "sky": 1.0, "interior_lamp": 1.0}))
    assert list(out) == ["gains", "residual_rms", "clipped"]
    assert list(out["gains"]) == list(lights)  # input order
    assert all(type(g) is float for g in out["gains"].values())
    assert type(out["residual_rms"]) is float
    assert json.loads(json.dumps(out)) == out


def test_input_validation():
    lights = _lights()
    target = _mix(lights, {"sun": 1.0, "sky": 1.0, "interior_lamp": 1.0})
    with pytest.raises(ValueError, match="empty"):
        lightmix.solve({}, target)
    with pytest.raises(ValueError, match="differ in shape"):
        lightmix.solve({**lights, "bad": np.zeros((H, W + 1, 3))}, target)
    with pytest.raises(ValueError, match=r"\(H, W, 3\)"):
        lightmix.solve({"gray": np.zeros((H, W))}, target)
    with pytest.raises(ValueError, match="target"):
        lightmix.solve(lights, target[:, :-1])
    with pytest.raises(ValueError, match="mask shape"):
        lightmix.solve(lights, target, mask=np.ones((H, W + 1), bool))
    with pytest.raises(ValueError, match="no valid pixels"):
        lightmix.solve(lights, target, mask=np.zeros((H, W), bool))
    for bad in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="max_gain"):
            lightmix.solve(lights, target, max_gain=bad)


def test_mood_presets_are_initial_guesses():
    assert lightmix.mood_presets == {"dusk": {"sun": 0.15, "sky": 0.6, "interior_*": 3.0}}
    g = lightmix.preset_gains("dusk", ["Sun", "sky", "interior_kitchen", "interior_hall", "garden_spots"])
    assert g == {"Sun": 0.15, "sky": 0.6, "interior_kitchen": 3.0, "interior_hall": 3.0, "garden_spots": 1.0}
    assert lightmix.preset_gains({"a*": 2.0}, ["abc", "b"], default=0.0) == {"abc": 2.0, "b": 0.0}
    with pytest.raises(KeyError):
        lightmix.preset_gains("midnight", ["sun"])
    # The preset feeds recompose directly (a starting mood) without going through the solver.
    lights = _lights()
    start = lightmix.recompose(lights, lightmix.preset_gains("dusk", list(lights)))
    assert start.shape == (H, W, 3) and np.isfinite(start).all()
