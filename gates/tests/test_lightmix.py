"""lightmix.py: NNLS gains on light selects (recovery, non-negativity, mask, clip, determinism, presets,
per-channel RGB gains) and the LightMix node's handling of vector gains."""

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
    assert lightmix.mood_presets["dusk"] == {"sun": 0.15, "sky": 0.6, "interior_*": 3.0}
    g = lightmix.preset_gains("dusk", ["Sun", "sky", "interior_kitchen", "interior_hall", "garden_spots"])
    assert g == {"Sun": 0.15, "sky": 0.6, "interior_kitchen": 3.0, "interior_hall": 3.0, "garden_spots": 1.0}
    assert lightmix.preset_gains({"a*": 2.0}, ["abc", "b"], default=0.0) == {"abc": 2.0, "b": 0.0}
    with pytest.raises(KeyError):
        lightmix.preset_gains("midnight", ["sun"])
    # The preset feeds recompose directly (a starting mood) without going through the solver.
    lights = _lights()
    start = lightmix.recompose(lights, lightmix.preset_gains("dusk", list(lights)))
    assert start.shape == (H, W, 3) and np.isfinite(start).all()


# A fixture rig: the kinds of lights a luxury exterior carries, one name that no preset knows ("garden_spots").
RIG = [
    "Sun",
    "sky",
    "interior_kitchen",
    "interior_hall",
    "facade_north",
    "facade_pool",
    "landscape_path",
    "garden_spots",
]

NEW_PRESETS = {
    "moonlight": {"sun": 0.0, "sky": 0.08, "interior_*": 0.6, "facade_*": 0.3},
    "luxury_exterior": {"sun": 0.0, "sky": 0.15, "interior_*": 0.0, "facade_*": 2.5, "landscape_*": 1.5},
    "artificial_light": {"sun": 0.0, "sky": 0.05, "interior_*": 3.0, "facade_*": 1.0, "landscape_*": 1.0},
}

# What each preset resolves to on RIG; "garden_spots" matches no pattern and keeps the default gain of 1.0.
RIG_GAINS = {
    "moonlight": [0.0, 0.08, 0.6, 0.6, 0.3, 0.3, 1.0, 1.0],
    "luxury_exterior": [0.0, 0.15, 0.0, 0.0, 2.5, 2.5, 1.5, 1.0],
    "artificial_light": [0.0, 0.05, 3.0, 3.0, 1.0, 1.0, 1.0, 1.0],
}


@pytest.mark.parametrize("name", sorted(NEW_PRESETS))
def test_new_mood_presets_resolve_on_a_fixture_light_list(name):
    assert lightmix.mood_presets[name] == NEW_PRESETS[name]
    got = lightmix.preset_gains(name, RIG)
    assert list(got) == RIG
    assert list(got.values()) == RIG_GAINS[name]
    assert all(type(g) is float for g in got.values())
    # Patterns that match no light are ignored: a rig with only a sun and a sky still resolves.
    assert lightmix.preset_gains(name, ["sun", "sky"]) == {
        "sun": 0.0,
        "sky": NEW_PRESETS[name]["sky"],
    }
    assert json.loads(json.dumps(got)) == got


def test_presets_keep_the_declared_names_and_dusk_unchanged():
    assert list(lightmix.mood_presets) == ["dusk", "moonlight", "luxury_exterior", "artificial_light"]
    assert lightmix.mood_presets["dusk"] == {"sun": 0.15, "sky": 0.6, "interior_*": 3.0}


@pytest.mark.parametrize("name", sorted(NEW_PRESETS))
def test_new_presets_feed_recompose(name):
    kinds = ["sun", "sky", "interior_lamp"]
    lights = {n: _lights(seed=i)[kinds[i % 3]] for i, n in enumerate(RIG)}
    gains = lightmix.preset_gains(name, list(lights))
    rec = lightmix.recompose(lights, gains)
    want = sum(g * lights[n].astype(np.float64) for n, g in gains.items())
    assert rec.shape == (H, W, 3) and rec.dtype == np.float32
    assert np.allclose(rec, want.astype(np.float32), atol=1e-6)


def test_rgb_gain_doubles_only_the_red_channel_of_that_light():
    """Exact on a synthetic two-light scene: [2, 1, 1] on the lamp changes red by the lamp's own red, nothing else."""
    rng = np.random.default_rng(11)
    lamp = rng.random((H, W, 3))
    moon = rng.random((H, W, 3))
    lights = {"lamp": lamp, "moon": moon}

    rgb = lightmix.recompose(lights, {"lamp": [2, 1, 1], "moon": 1.0})
    scalar = lightmix.recompose(lights, {"lamp": 1.0, "moon": 1.0})
    assert rgb.shape == (H, W, 3) and rgb.dtype == np.float32

    # Green and blue are untouched, bit for bit.
    assert np.array_equal(rgb[..., 1:], scalar[..., 1:])
    # Red is exactly (2 * lamp_r + moon_r), accumulated in float64 and rounded once to float32.
    want_red = (2.0 * lamp[..., 0] + moon[..., 0]).astype(np.float32)
    assert np.array_equal(rgb[..., 0], want_red)
    # The difference to the scalar mix is exactly the lamp's red channel, in red only.
    diff = rgb.astype(np.float64) - scalar.astype(np.float64)
    assert np.allclose(diff[..., 0], lamp[..., 0], atol=1e-6)
    assert np.allclose(diff[..., 1:], 0.0, atol=0.0)
    # And the moon's own pass is not scaled by the lamp's vector.
    only_moon = lightmix.recompose(lights, {"moon": 1.0, "lamp": [0, 0, 0]})
    assert np.array_equal(only_moon, moon.astype(np.float32))


def test_rgb_gain_equal_components_is_the_scalar_gain():
    lights = _lights()
    a = lightmix.recompose(lights, {"sun": 1.5, "sky": [0.25, 0.25, 0.25]})
    b = lightmix.recompose(lights, {"sun": [1.5, 1.5, 1.5], "sky": 0.25})
    assert np.array_equal(a, b)


def test_rgb_gain_accepts_tuples_and_arrays_and_scalar_behaviour_is_unchanged():
    lights = _lights()
    base = lightmix.recompose(lights, {"sun": 2.0, "sky": 0.5})
    for vec in ((2.0, 2.0, 2.0), np.array([2.0, 2.0, 2.0]), [2, 2, 2]):
        assert np.array_equal(lightmix.recompose(lights, {"sun": vec, "sky": 0.5}), base)
    assert np.array_equal(lightmix.recompose(lights, {"sun": np.float32(2.0), "sky": 0.5}), base)


@pytest.mark.parametrize(
    "bad",
    [
        [1.0, 1.0],
        [1.0, 1.0, 1.0, 1.0],
        [],
        [1.0, -0.1, 1.0],
        [1.0, float("nan"), 1.0],
        [float("inf"), 1.0, 1.0],
        [[1.0, 1.0, 1.0]],
        [1.0, [1.0], 1.0],
        [[1, 1, 1], [1, 1, 1], [1, 1, 1]],
        "2",
        None,
        {"r": 1},
    ],
)
def test_recompose_rejects_bad_rgb_gains(bad):
    with pytest.raises(ValueError, match="gain for 'sun'"):
        lightmix.recompose(_lights(), {"sun": bad})


def test_solve_still_returns_scalars_and_feeds_recompose():
    lights = _lights()
    out = lightmix.solve(lights, _mix(lights, {"sun": 0.5, "sky": 2.0, "interior_lamp": 0.25}))
    assert all(type(g) is float for g in out["gains"].values())
    assert {lightmix.gain_channels(g) for g in out["gains"].values()} == {"scalar"}
    assert lightmix.recompose(lights, out["gains"]).shape == (H, W, 3)


def test_gain_channels():
    assert lightmix.gain_channels(2.0) == "scalar"
    assert lightmix.gain_channels(np.float32(2.0)) == "scalar"
    assert lightmix.gain_channels([2.0, 1.0, 1.0]) == "rgb"
    assert lightmix.gain_channels((1, 1, 1)) == "rgb"
    assert lightmix.gain_channels(np.ones(3)) == "rgb"


def test_preset_gains_pass_vectors_through():
    table = {"lamp_*": [2, 1, 0.5], "sun": 0.0, "moon*": (0.2, 0.3, 0.9)}
    got = lightmix.preset_gains(table, ["lamp_a", "sun", "moon_rim", "fill"])
    assert got == {"lamp_a": [2.0, 1.0, 0.5], "sun": 0.0, "moon_rim": [0.2, 0.3, 0.9], "fill": 1.0}
    assert type(got["lamp_a"]) is list and all(type(c) is float for c in got["lamp_a"])
    assert json.loads(json.dumps(got)) == got
    # A vector default reaches every unmatched light; the mixed result goes straight into recompose.
    assert lightmix.preset_gains({"x": 2.0}, ["a"], default=[0.5, 1.0, 1.5]) == {"a": [0.5, 1.0, 1.5]}
    lights = {"lamp_a": _lights()["sun"], "sun": _lights()["sky"]}
    rec = lightmix.recompose(lights, lightmix.preset_gains(table, list(lights)))
    assert np.allclose(rec[..., 0], 2.0 * lights["lamp_a"][..., 0], atol=1e-6)
    assert np.allclose(rec[..., 1], 1.0 * lights["lamp_a"][..., 1], atol=1e-6)
    assert np.allclose(rec[..., 2], 0.5 * lights["lamp_a"][..., 2], atol=1e-6)
    with pytest.raises(ValueError, match="gain for 'a'"):
        lightmix.preset_gains({"a": [1.0, 2.0]}, ["a"])


# --------------------------------------------------------------------------------------- the LightMix node

node = pytest.importorskip("plate_daemon.nodes.lightmix_node")
PipelineError = pytest.importorskip("plate_daemon.pipeline").PipelineError

NODE_LIGHTS = ["interior_lamp", "sky", "sun"]


def test_node_accepts_scalar_and_rgb_gains_in_params():
    got = node._gains_in({"sun": 0.25, "sky": [3, 1, 1], "interior_lamp": (1, 1, 2.5)}, NODE_LIGHTS)
    assert got == {"sun": 0.25, "sky": [3.0, 1.0, 1.0], "interior_lamp": [1.0, 1.0, 2.5]}
    assert json.loads(json.dumps(got)) == got  # the spec is hashed and stored as JSON
    assert node._gains_in(None, NODE_LIGHTS) is None
    assert node._gains_in({}, NODE_LIGHTS) == {}


@pytest.mark.parametrize(
    "gains",
    [
        {"sun": [1, 1]},
        {"sun": [1, 1, 1, 1]},
        {"sun": [1, -1, 1]},
        {"sun": [1, float("nan"), 1]},
        {"sun": [1, float("inf"), 1]},
        {"sun": [1, "x", 1]},
        {"sun": [[1], 1, 1]},
        {"sun": {"r": 1}},
        {"sun": -0.5},
        {"sun": float("nan")},
        {"moon": [1, 1, 1]},
        [1, 1, 1],
    ],
)
def test_node_rejects_bad_gains_as_bad_params(gains):
    with pytest.raises(PipelineError) as e:
        node._gains_in(gains, NODE_LIGHTS)
    assert e.value.code == "bad_params"


def _spec(**kw):
    spec = {"lights": NODE_LIGHTS, "gains": None, "preset": None, "target_hash": None, "max_gain": 8.0}
    spec.update(kw)
    return spec


def test_node_resolves_vector_gains_and_clips_each_channel():
    selects = {n: np.zeros((2, 2, 3), np.float32) for n in NODE_LIGHTS}
    out = node.resolve_gains(_spec(gains={"sky": [2, 1, 1], "sun": [50, 1, 12]}, max_gain=4.0), selects, None)
    assert out["mode"] == "explicit" and out["residual_rms"] is None
    assert out["gains"] == {"interior_lamp": 1.0, "sky": [2.0, 1.0, 1.0], "sun": [4.0, 1.0, 4.0]}
    assert out["clipped"] == ["sun"]  # one entry per light, whichever channel hit the cap
    assert node.gain_channels(out["gains"]) == {"interior_lamp": "scalar", "sky": "rgb", "sun": "rgb"}
    assert json.loads(json.dumps(out)) == out

    # A preset gives the starting positions; an explicit vector overrides one light of it.
    pre = node.resolve_gains(
        _spec(preset="moonlight", gains={"interior_lamp": [0.6, 0.5, 0.4]}), selects, None
    )
    assert pre["gains"] == {"interior_lamp": [0.6, 0.5, 0.4], "sky": 0.08, "sun": 0.0}
    assert pre["mode"] == "explicit" and pre["clipped"] == []
    only_pre = node.resolve_gains(_spec(preset="artificial_light"), selects, None)
    assert only_pre["mode"] == "preset"
    assert only_pre["gains"] == {"interior_lamp": 3.0, "sky": 0.05, "sun": 0.0}

    # Scalar behaviour is unchanged: floats in, floats out, the cap clips and reports.
    scalar = node.resolve_gains(_spec(gains={"sun": 50.0}, max_gain=4.0), selects, None)
    assert scalar["gains"] == {"interior_lamp": 1.0, "sky": 1.0, "sun": 4.0} and scalar["clipped"] == ["sun"]


def test_node_solve_mode_returns_scalars():
    rng = np.random.default_rng(5)
    selects = {n: rng.random((8, 8, 3)).astype(np.float32) for n in NODE_LIGHTS}
    target = sum(g * selects[n] for n, g in {"sun": 0.5, "sky": 2.0, "interior_lamp": 0.0}.items())
    out = node.resolve_gains(_spec(), selects, target.astype(np.float32))
    assert out["mode"] == "solve"
    assert set(node.gain_channels(out["gains"]).values()) == {"scalar"}


def test_node_preview_reports_channels_per_light(monkeypatch):
    from types import SimpleNamespace

    selects = {n: np.full((2, 2, 3), 0.5, np.float32) for n in NODE_LIGHTS}
    monkeypatch.setattr(node, "load_light_selects", lambda pv: selects)
    monkeypatch.setattr(node, "_base", lambda pv, sel, names: (np.zeros((2, 2, 3)), 0.0))
    spec = _spec(gains={"sky": [2, 1, 1], "sun": 0.25}, keep_base=False)
    ctx = SimpleNamespace(packet=lambda: SimpleNamespace(), spec=spec)
    out = node.preview(ctx)
    assert out["lights"] == NODE_LIGHTS
    assert out["gains"] == {"interior_lamp": 1.0, "sky": [2.0, 1.0, 1.0], "sun": 0.25}
    assert out["channels"] == {"interior_lamp": "scalar", "sky": "rgb", "sun": "scalar"}
    assert out["mode"] == "explicit" and out["residual_rms"] is None and out["clipped"] == []
    assert json.loads(json.dumps(out)) == out
