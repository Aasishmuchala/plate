"""EXR IO: manifest-driven reads, multilayer writes and Cryptomatte decoding."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import OpenEXR
import pytest

from plate_pixels import exr
from plate_spec import cryptomatte_float, cryptomatte_id

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"

H, W = 12, 20


def _element(name, layer, channels, dtype, **extra):
    return {
        "element": name,
        "layer": layer,
        "channels": channels,
        "dtype": dtype,
        "filtered": False,
        "denoised": False,
        **extra,
    }


def _crypto_channels(layer, n_layers=3):
    return [f"{layer}{i:02d}.{c}" for i in range(n_layers) for c in "RGBA"]


def _manifest(extra=()):
    elements = [
        _element("beauty", "RGBA", list("RGBA"), "half"),
        _element("zdepth", "VRayZDepth", ["Z"], "float", depth_kind="planar"),
        _element("normals", "VRayNormals", list("XYZ"), "half"),
        _element("wire_colour", "VRayWireColor", list("RGB"), "half"),
        _element(
            "cryptomatte_object", "CryptoObject", _crypto_channels("CryptoObject"), "float", rank_levels=6
        ),
        *extra,
    ]
    return {"files": {"beauty_exr": {"elements": elements}}}


def _random_layers(seed=3):
    rng = np.random.default_rng(seed)

    def plane():
        return rng.random((H, W), dtype=np.float32)

    return {
        "RGBA": {c: plane() for c in "RGBA"},
        "VRayZDepth": {"Z": plane() * 40.0},
        "VRayNormals": {c: plane() * 2 - 1 for c in "XYZ"},
        "VRayWireColor": {c: plane() for c in "RGB"},
        "CryptoObject00": {c: plane() for c in "RGBA"},
        "CryptoObject01": {c: plane() for c in "RGBA"},
        "CryptoObject02": {c: plane() for c in "RGBA"},
    }


DTYPES = {
    "RGBA": "half",
    "VRayZDepth": "float",
    "VRayNormals": "half",
    "VRayWireColor": "half",
    "CryptoObject00": "float",
    "CryptoObject01": "float",
    "CryptoObject02": "float",
}


def test_round_trip_float_exact_and_half_close(tmp_path):
    layers = _random_layers()
    path = tmp_path / "beauty.exr"
    exr.write_multilayer(path, layers, DTYPES, {"plate/test": "hello"})
    got = exr.read_elements(path, _manifest())

    assert set(got) == {"beauty", "zdepth", "normals", "wire_colour", "cryptomatte_object"}
    for arr in got.values():
        assert arr.dtype == np.float32 and arr.ndim == 3 and arr.shape[:2] == (H, W)

    # float layers: bit-exact
    assert np.array_equal(got["zdepth"][..., 0], layers["VRayZDepth"]["Z"])
    crypto = np.stack([layers[f"CryptoObject{i:02d}"][c] for i in range(3) for c in "RGBA"], axis=-1)
    assert got["cryptomatte_object"].shape[2] == 12
    assert np.array_equal(got["cryptomatte_object"], crypto)
    # half layers: within half precision for values in [-1, 1]
    for element, layer in (("beauty", "RGBA"), ("normals", "VRayNormals"), ("wire_colour", "VRayWireColor")):
        want = np.stack([layers[layer][c] for c in layers[layer]], axis=-1)
        assert np.abs(got[element] - want).max() <= 1e-3, element
    # the header attribute survives and the file is a single part
    f = OpenEXR.File(str(path))
    assert len(f.parts) == 1 and f.parts[0].header["plate/test"] == "hello"
    assert not Path(str(path) + ".part").exists()


def test_channel_order_follows_the_manifest(tmp_path):
    layers = _random_layers()
    path = tmp_path / "beauty.exr"
    exr.write_multilayer(path, layers, DTYPES)
    manifest = _manifest()
    manifest["files"]["beauty_exr"]["elements"][2]["channels"] = ["Z", "X", "Y"]
    got = exr.read_elements(path, manifest, ["normals"])["normals"]
    want = np.stack([layers["VRayNormals"][c] for c in "ZXY"], axis=-1)
    assert np.abs(got - want).max() <= 1e-3


def test_subset_only_returns_requested_elements(tmp_path):
    path = tmp_path / "beauty.exr"
    exr.write_multilayer(path, _random_layers(), DTYPES)
    got = exr.read_elements(path, _manifest(), ["zdepth", "wire_colour"])
    assert set(got) == {"zdepth", "wire_colour"}


def test_missing_channel_names_element_and_channel(tmp_path):
    layers = _random_layers()
    del layers["VRayWireColor"]["B"]
    path = tmp_path / "beauty.exr"
    exr.write_multilayer(path, layers, DTYPES)
    with pytest.raises(exr.MissingChannel) as err:
        exr.read_elements(path, _manifest())
    assert err.value.element == "wire_colour" and err.value.channel == "VRayWireColor.B"
    # elements that are intact still read when only they are requested
    assert "zdepth" in exr.read_elements(path, _manifest(), ["zdepth"])


def test_bare_beauty_channels_are_never_mistaken_for_a_layer(tmp_path):
    """wire_colour's 'R' must resolve to VRayWireColor.R, not the beauty R."""
    layers = _random_layers()
    path = tmp_path / "beauty.exr"
    exr.write_multilayer(path, layers, DTYPES)
    got = exr.read_elements(path, _manifest(), ["wire_colour", "beauty"])
    assert not np.allclose(got["wire_colour"][..., 0], got["beauty"][..., 0])


def test_unknown_element_is_an_error(tmp_path):
    path = tmp_path / "beauty.exr"
    exr.write_multilayer(path, _random_layers(), DTYPES)
    with pytest.raises(exr.MissingChannel):
        exr.read_elements(path, _manifest(), ["world_position"])


def test_light_select_elements_get_distinct_keys(tmp_path):
    layers = _random_layers()
    rng = np.random.default_rng(9)
    layers["LightSel_key"] = {c: rng.random((H, W), dtype=np.float32) for c in "RGB"}
    layers["LightSel_fill"] = {c: rng.random((H, W), dtype=np.float32) for c in "RGB"}
    dtypes = {**DTYPES, "LightSel_key": "half", "LightSel_fill": "half"}
    path = tmp_path / "beauty.exr"
    exr.write_multilayer(path, layers, dtypes)
    extra = [
        _element("light_select", "LightSel_key", list("RGB"), "half", light_name="key"),
        _element("light_select", "LightSel_fill", list("RGB"), "half", light_name="fill"),
    ]
    got = exr.read_elements(path, _manifest(extra))
    assert "light_select[key]" in got and "light_select[fill]" in got
    assert not np.allclose(got["light_select[key]"], got["light_select[fill]"])
    assert set(exr.read_elements(path, _manifest(extra), ["light_select"])) == {
        "light_select[key]",
        "light_select[fill]",
    }


def test_multipart_file_is_read_by_part_name_and_by_full_name(tmp_path):
    """Blender's layout: one part per layer, bare channel names. Readers still find them."""
    rng = np.random.default_rng(5)
    z = rng.random((H, W), dtype=np.float32) * 10
    rgb = {c: rng.random((H, W)).astype(np.float16) for c in "RGB"}
    green = rgb["G"].astype(np.float32)  # OpenEXR.Part turns the dict values into Channel objects
    parts = [
        OpenEXR.Part({"type": OpenEXR.scanlineimage, "name": "VRayZDepth"}, {"Z": z}),
        OpenEXR.Part({"type": OpenEXR.scanlineimage, "name": "VRayWireColor"}, rgb),
    ]
    path = tmp_path / "multi.exr"
    OpenEXR.File(parts).write(str(path))
    assert len(OpenEXR.File(str(path)).parts) == 2
    elements = [
        _element("zdepth", "VRayZDepth", ["Z"], "float"),
        _element("wire_colour", "VRayWireColor", list("RGB"), "half"),
    ]
    got = exr.read_elements(path, {"files": {"beauty_exr": {"elements": elements}}})
    assert np.array_equal(got["zdepth"][..., 0], z)
    assert np.array_equal(got["wire_colour"][..., 1], green)


def test_write_multilayer_validates_input(tmp_path):
    with pytest.raises(ValueError):
        exr.write_multilayer(tmp_path / "a.exr", {})
    with pytest.raises(ValueError):
        exr.write_multilayer(
            tmp_path / "b.exr", {"L": {"a": np.zeros((2, 2)), "b": np.zeros((3, 3))}}, {"L": "float"}
        )
    with pytest.raises(ValueError):
        exr.write_multilayer(tmp_path / "c.exr", {"L": {"a": np.zeros((2, 2))}}, {"L": "double"})
    assert not list(tmp_path.glob("*.part"))


# --------------------------------------------------------------------------- Cryptomatte


def _synthetic_crypto(tmp_path):
    """Left third = wall, right third = floor, middle third = 60 % wall / 40 % floor, plus a lamp that only
    shows up at rank 3 in one column."""
    names = {"wall": "ARCH_wall", "floor": "ARCH_floor", "lamp": "ARCH_lamp"}
    bits = {
        k: np.array([cryptomatte_float(v)], dtype=np.float32).view(np.uint32)[0] for k, v in names.items()
    }
    ids = np.zeros((H, W, 6), dtype=np.uint32)
    cov = np.zeros((H, W, 6), dtype=np.float32)
    a, b = W // 3, 2 * W // 3
    ids[:, :a, 0], cov[:, :a, 0] = bits["wall"], 1.0
    ids[:, b:, 0], cov[:, b:, 0] = bits["floor"], 1.0
    ids[:, a:b, 0], cov[:, a:b, 0] = bits["wall"], 0.6
    ids[:, a:b, 1], cov[:, a:b, 1] = bits["floor"], 0.4
    ids[0, 0, 3], cov[0, 0, 3] = bits["lamp"], 0.05  # fourth rank, tiny
    cov[0, 0, 0] = 0.95
    layers = {}
    for i in range(3):
        sl = slice(2 * i, 2 * i + 2)
        pair = ids[..., sl].view(np.float32)
        layers[f"CryptoObject{i:02d}"] = {
            "R": pair[..., 0],
            "G": cov[..., 2 * i],
            "B": pair[..., 1],
            "A": cov[..., 2 * i + 1],
        }
    path = tmp_path / "crypto.exr"
    exr.write_multilayer(path, layers, {k: "float" for k in layers})
    return path, names, bits, (a, b)


def test_cryptomatte_decode_and_id_mask(tmp_path):
    path, names, bits, (a, b) = _synthetic_crypto(tmp_path)
    ranks, coverage = exr.read_cryptomatte(path, _manifest(), "object")
    assert ranks.dtype == np.uint32 and coverage.dtype == np.float32
    assert ranks.shape == (H, W, 6) and coverage.shape == (H, W, 6)
    assert coverage.sum(axis=2).max() <= 1.0 + 1e-6
    assert np.array_equal(ranks[5, 0, :2], [bits["wall"], 0])
    assert exr.present_ids(ranks, coverage) == {int(v) for v in bits.values()}

    wall = exr.id_mask(ranks, coverage, cryptomatte_id(names["wall"]))
    floor = exr.id_mask(ranks, coverage, cryptomatte_id(names["floor"]))
    assert wall[:, :b].all() and not wall[:, b:].any()  # the 60 % column band counts as wall
    assert floor[:, b:].all() and not floor[:, :b].any()
    assert not exr.id_mask(ranks, coverage, cryptomatte_id(names["lamp"])).any()  # 0.05 < threshold
    assert exr.id_mask(ranks, coverage, cryptomatte_id(names["lamp"]), threshold=0.01)[0, 0]
    # id accepted as bare hex, prefixed hex and int
    bare = cryptomatte_id(names["wall"]).split(":")[1]
    assert np.array_equal(exr.id_mask(ranks, coverage, bare), wall)
    assert np.array_equal(exr.id_mask(ranks, coverage, int(bare, 16)), wall)
    # the threshold is on summed coverage over ranks
    assert exr.id_mask(ranks, coverage, cryptomatte_id(names["floor"]), threshold=0.4)[:, a:b].all()
    assert not exr.id_mask(ranks, coverage, cryptomatte_id(names["floor"]), threshold=0.41)[:, a:b].any()


def test_cryptomatte_rejects_bad_which(tmp_path):
    path, *_ = _synthetic_crypto(tmp_path)
    with pytest.raises(ValueError):
        exr.read_cryptomatte(path, _manifest(), "layer")


def test_room_a_cryptomatte_matches_its_legend():
    """Real fixture packet (router task T2). Skipped until fixtures/packets/room_a is committed."""
    manifest_path = ROOM_A / "manifest.json"
    if not manifest_path.is_file():
        pytest.skip("fixtures/packets/room_a not present yet")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    legend = json.loads((ROOM_A / "id_legend.json").read_text(encoding="utf-8"))
    beauty = ROOM_A / manifest["files"]["beauty_exr"]["path"]
    ranks, coverage = exr.read_cryptomatte(beauty, manifest, "object")
    assert ranks.dtype == np.uint32
    assert coverage.min() >= 0.0
    assert coverage.sum(axis=2).max() <= 1.0 + 1e-3
    seen = exr.present_ids(ranks, coverage)
    object_ids = [e["id"] for e in legend["entries"] if e["kind"] == "object"]
    assert object_ids
    missing = [i for i in object_ids if int(i.split(":")[1], 16) not in seen]
    assert not missing, f"legend ids absent from the Cryptomatte layer: {missing}"
    mats, mcov = exr.read_cryptomatte(beauty, manifest, "material")
    assert mats.shape[:2] == ranks.shape[:2] and mcov.sum(axis=2).max() <= 1.0 + 1e-3
