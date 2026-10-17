"""Egress / ingress: engine-legal sizes, overscan and edge-extend padding, region crops, the identity round trip,
registration in restore, and the doctrine checks (PNG only, never reflect, never upsample the plate)."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import cv2
import numpy as np
import pytest

from plate_gates.color import ciede2000, srgb_to_lab
from plate_pixels import egress, icc, ingress, sizes
from plate_pixels.egress import EgressError, SizeMap
from plate_pixels.sizes import EngineSize, NoLegalSize

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"

# Test engines (dicts in the plate_engines Sizes shape). 16:9 is the nearest legal aspect to 2.39:1 here.
FIXED_169 = {"sizes": {"legal": [{"w": 1920, "h": 1080}, {"w": 1024, "h": 1024}]}}
TIERED = {
    "sizes": {
        "legal": [{"w": 1024, "h": 1024, "label": "1K"}, {"w": 2048, "h": 2048, "label": "2K"}],
        "auto": True,
        "aspects": ["1:1", "16:9", "21:9", "4:3"],
    }
}
SMALL_PRESETS = {"sizes": {"legal": [{"w": 256, "h": 144}, {"w": 512, "h": 288}, {"w": 1920, "h": 1080}]}}
ARBITRARY = {"sizes": {"arbitrary": {"min_side": 64, "max_side": 4096, "aspect_min": 0.4, "aspect_max": 2.5}}}


# --------------------------------------------------------------------------- helpers


def archviz_plate(h: int, w: int, seed: int = 0) -> np.ndarray:
    """A 16-bit display plate with archviz-like content: low-chroma warm/cool surfaces, fine texture and
    hard-edged frames (uint16, H x W x 3)."""
    rng = np.random.default_rng(seed)
    lum = cv2.GaussianBlur(rng.random((h, w)).astype(np.float32), (0, 0), 1.5)
    lum += 3 * cv2.GaussianBlur(rng.random((h, w)).astype(np.float32), (0, 0), 12.0)
    lum = (lum - lum.min()) / (lum.max() - lum.min())
    mix = cv2.GaussianBlur(rng.random((h, w)).astype(np.float32), (0, 0), 30.0)
    mix = ((mix - mix.min()) / (mix.max() - mix.min()))[..., None]
    warm = np.array([1.0, 0.93, 0.82], np.float32)
    cool = np.array([0.86, 0.93, 1.0], np.float32)
    rgb = (0.12 + 0.76 * lum[..., None]) * (mix * warm + (1 - mix) * cool)
    for _ in range(10):
        x, y = int(rng.integers(0, w - 60)), int(rng.integers(0, h - 60))
        bw, bh = int(rng.integers(20, 60)), int(rng.integers(20, 60))
        rgb[y : y + bh, x : x + 3] = 0.08
        rgb[y : y + bh, x + bw - 3 : x + bw] = 0.08
        rgb[y : y + 3, x : x + bw] = 0.08
        rgb[y + bh - 3 : y + bh, x : x + bw] = 0.08
    return np.round(np.clip(rgb, 0, 1) * 65535).astype(np.uint16)


def as_float(a: np.ndarray) -> np.ndarray:
    if a.dtype == np.uint16:
        return a.astype(np.float64) / 65535.0
    if a.dtype == np.uint8:
        return a.astype(np.float64) / 255.0
    return a.astype(np.float64)


def de00_p99(a: np.ndarray, b: np.ndarray, mask: np.ndarray | None = None) -> float:
    d = ciede2000(srgb_to_lab(as_float(a) * 255.0), srgb_to_lab(as_float(b) * 255.0))
    return float(np.percentile(d[mask] if mask is not None else d, 99))


def decode_png(data: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    return img[..., ::-1]


def png_header(data: bytes) -> tuple[int, int, int, int]:
    """(width, height, bit_depth, colour_type) from IHDR."""
    w, h, depth, ctype = struct.unpack(">IIBB", data[16:26])
    return w, h, depth, ctype


def tier_c(w: int, h: int) -> dict:
    return {"tier": "C", "camera": {"resolution": {"width": w, "height": h, "pixel_aspect": 1.0}}}


def tier_a_overscan(w: int, h: int, safe_h: int) -> dict:
    return {
        "tier": "A",
        "camera": {
            "resolution": {"width": w, "height": h, "pixel_aspect": 1.0},
            "safe_frame": {"x": 0, "y": (h - safe_h) // 2, "width": w, "height": safe_h},
            "overscan": {"engine_aspect": "16:9"},
        },
    }


def shifted(plate16: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """The plate's content moved by (+dx, +dy) (edges replicated), quantised to 8-bit like an engine result."""
    m = np.float32([[1, 0, -dx], [0, 1, -dy]])
    f = as_float(plate16).astype(np.float32)
    out = cv2.warpAffine(
        f, m, f.shape[1::-1], flags=cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE
    )
    return np.round(np.clip(out, 0, 1) * 255).astype(np.uint8)


# --------------------------------------------------------------------------- sizes


def test_legal_sizes_fixed_list():
    legal = sizes.legal_sizes(FIXED_169)
    assert {(s.width, s.height) for s in legal} == {(1920, 1080), (1024, 1024)}
    assert legal.arbitrary is None and not legal.auto
    s = legal[0]
    assert s.aspect == pytest.approx(s.width / s.height)


def test_legal_sizes_tiered_aspects_fit_each_box():
    legal = sizes.legal_sizes(TIERED)
    by = {(s.label, s.ratio): (s.width, s.height) for s in legal}
    assert by[("2K", "16:9")] == (2048, 1152)
    assert by[("1K", "21:9")] == (1024, 439)
    assert by[("1K", "1:1")] == (1024, 1024)
    assert legal.auto


def test_pick_size_exact_fixed_takes_largest_preset_inside_the_frame():
    size, mode = sizes.pick_size(3840, 2160, sizes.legal_sizes(SMALL_PRESETS))
    assert mode == "exact" and (size.width, size.height) == (1920, 1080)
    size, mode = sizes.pick_size(600, 338, sizes.legal_sizes(SMALL_PRESETS))
    assert mode == "exact" and (size.width, size.height) == (512, 288)


def test_pick_size_exact_auto_takes_smallest_tier_covering_the_frame():
    size, mode = sizes.pick_size(1600, 900, sizes.legal_sizes(TIERED))
    assert mode == "exact" and (size.label, size.ratio) == ("2K", "16:9")
    size, mode = sizes.pick_size(4000, 2250, sizes.legal_sizes(TIERED))
    assert mode == "exact" and (size.width, size.height) == (2048, 1152)  # nothing covers: the biggest


def test_pick_size_pad_goes_to_nearest_aspect_never_crop():
    size, mode = sizes.pick_size(956, 400, sizes.legal_sizes(FIXED_169))  # 2.39:1
    assert mode == "pad" and size.aspect == pytest.approx(16 / 9)
    size, mode = sizes.pick_size(956, 400, sizes.legal_sizes(TIERED))  # 21:9 is nearer than 16:9
    assert mode == "pad" and size.ratio == "21:9"
    assert sizes.pad_dims(956, 400, 16 / 9) == (956, 538)
    assert sizes.pad_dims(400, 400, 16 / 9) == (711, 400)


def test_pick_size_pad_tie_goes_to_the_wider_aspect():
    # 4:3 sits exactly between 1:1 and 16:9 (both pad 96 x 72 to 9216 px): the wider wins, deterministically.
    size, mode = sizes.pick_size(96, 72, sizes.legal_sizes(FIXED_169))
    assert mode == "pad" and size.aspect == pytest.approx(16 / 9)
    size, mode = sizes.pick_size(72, 96, sizes.legal_sizes(FIXED_169))  # 3:4: 1:1 is nearer than 16:9
    assert mode == "pad" and size.aspect == pytest.approx(1.0)


def test_pick_size_arbitrary_clamps_to_sides():
    legal = sizes.legal_sizes(ARBITRARY)
    assert sizes.pick_size(800, 600, legal) == (EngineSize(800, 600), "arbitrary")
    size, mode = sizes.pick_size(8192, 4096, legal)
    assert mode == "arbitrary" and (size.width, size.height) == (4096, 2048)
    size, mode = sizes.pick_size(40, 30, legal)
    assert mode == "arbitrary" and min(size.width, size.height) == 64
    size, mode = sizes.pick_size(1000, 100, legal)  # 10:1 is outside [0.4, 2.5]: pad to 2.5
    assert mode == "pad" and size.aspect == pytest.approx(2.5, rel=0.01)


def test_unverified_arbitrary_envelope_never_overrides_presets():
    eng = {
        "flags": ["unverified_p0"],
        "sizes": {
            "legal": [{"w": 1024, "h": 1024}, {"w": 1536, "h": 1024}],
            "arbitrary": {"min_side": 1024, "max_side": 1536, "aspect_min": 0.6667, "aspect_max": 1.5},
        },
    }
    assert sizes.legal_sizes(eng).arbitrary is None
    assert sizes.legal_sizes(eng, allow_unverified=True).arbitrary is not None
    only_arb = {"flags": ["unverified_p0"], "sizes": ARBITRARY["sizes"]}
    assert sizes.legal_sizes(only_arb).arbitrary is not None


def test_no_sizes_is_an_error_not_a_guess():
    with pytest.raises(NoLegalSize):
        sizes.pick_size(640, 360, sizes.legal_sizes({"id": "x"}))
    with pytest.raises(NoLegalSize):
        egress.prepare(archviz_plate(64, 96), None, {"id": "x"})


def test_real_manifests_are_readable():
    """Against the committed engine manifests (plate_engines owns them; skipped until they carry sizes)."""
    loader = pytest.importorskip("plate_engines.loader")
    try:
        manifests = loader.load_manifests()
    except loader.ManifestError as exc:  # the manifests' own suite reports this
        pytest.skip(f"engine manifests do not load: {exc}")
    with_sizes = {k: m for k, m in manifests.items() if getattr(m, "sizes", None) is not None}
    if not with_sizes:
        pytest.skip("no engine manifest declares sizes yet")
    for m in with_sizes.values():
        legal = sizes.legal_sizes(m)
        assert list(legal) or legal.arbitrary is not None
        size, mode = sizes.pick_size(3840, 2160, legal)
        assert mode in ("exact", "pad", "arbitrary") and size.width > 0
    nb = with_sizes.get("nano-banana-2.1")
    if nb is not None:
        size, mode = sizes.pick_size(3840, 2160, sizes.legal_sizes(nb))
        assert mode == "exact" and size.ratio == "16:9" and size.width >= 3840


# --------------------------------------------------------------------------- egress


def test_png_is_8bit_srgb_with_icc_and_no_metadata():
    plate = archviz_plate(90, 160)
    png, sm = egress.prepare(plate, tier_c(160, 90), FIXED_169)
    assert png[:8] == icc.PNG_SIGNATURE  # never JPEG
    assert png_header(png)[2:] == (8, 2)  # 8-bit truecolour RGB
    chunks = icc.png_chunk_types(png)
    assert chunks[0] == "IHDR" and chunks[1] == "iCCP" and chunks[-1] == "IEND"
    assert set(chunks) <= {"IHDR", "iCCP", "IDAT", "IEND"}
    assert icc.read_png_icc(png) == icc.SRGB_ICC


def test_tier_a_overscan_frame_goes_without_pad():
    plate = archviz_plate(360, 640, seed=3)
    png, sm = egress.prepare(plate, tier_a_overscan(640, 360, 268), TIERED)
    assert sm.mode == "exact" and sm.pad == (0, 0, 0, 0) and sm.pad_mode == "overscan"
    assert sm.dst == (640, 360) and sm.scale == 1.0  # the 1K tier covers it: native, never upsampled
    assert png_header(png)[:2] == (640, 360)
    assert sm.engine_size["label"] == "1K" and sm.engine_size["ratio"] == "16:9"


def test_edge_extend_pad_replicates_and_never_reflects(tmp_path):
    plate = archviz_plate(400, 956, seed=4)  # 2.39:1
    png, sm = egress.prepare(plate, tier_c(956, 400), FIXED_169, pad_mask_dir=tmp_path)
    assert sm.mode == "pad" and sm.pad_mode == "edge_extend"
    assert sm.pad == (0, 69, 0, 69) and sm.synthetic == sm.pad
    assert sm.dst == (956, 538) and sm.scale == 1.0
    sent = decode_png(png).astype(np.int32)
    first = egress.quantise8(plate[0:1])[0].astype(np.int32)  # the codes egress sends for plate row 0
    second = egress.quantise8(plate[1:2])[0].astype(np.int32)
    assert np.array_equal(sent[0], first) and np.array_equal(sent[68], first)  # replicated edge row
    assert np.array_equal(sent[69], first) and np.array_equal(sent[-1], egress.quantise8(plate[-1:])[0])
    assert not np.array_equal(sent[68], second)  # a reflection would put row 1 here
    mask = sm.pad_mask()
    assert mask.shape == (538, 956) and mask[:69].all() and mask[-69:].all() and not mask[69:-69].any()
    path = Path(sm.pad_mask_path)
    assert path.parent == tmp_path and ":" not in path.name and path.suffix == ".png"
    on_disk = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
    assert np.array_equal(on_disk, mask)


def test_downscale_to_preset_uses_area_and_exact_inverse_size():
    plate = archviz_plate(1440, 2560, seed=5)
    png, sm = egress.prepare(plate, tier_c(2560, 1440), FIXED_169)
    assert sm.dst == (1920, 1080) and sm.scale == pytest.approx(0.75)
    assert png_header(png)[:2] == (1920, 1080)
    out, rep = ingress.restore(png, sm, plate)
    assert out.shape == plate.shape and out.dtype == np.uint16
    assert rep["registered"] and rep["resample"] == "lanczos4"


def test_scene_linear_plate_is_refused():
    lin = np.full((64, 96, 3), 3.5, np.float32)
    with pytest.raises(EgressError, match="scene-linear"):
        egress.prepare(lin, None, FIXED_169)


def test_plate_not_at_rendered_resolution_is_refused():
    with pytest.raises(EgressError, match="rendered"):
        egress.prepare(archviz_plate(180, 320), tier_c(640, 360), FIXED_169)


def test_sizemap_json_round_trip():
    _, sm = egress.prepare(archviz_plate(400, 956), tier_c(956, 400), FIXED_169)
    text = sm.to_json()
    back = SizeMap.from_dict(json.loads(text))
    assert back == sm and back.quantise == "de00"
    assert egress.engine_size_of(back).aspect == pytest.approx(16 / 9)
    _, sm = egress.prepare(archviz_plate(400, 956), tier_c(956, 400), FIXED_169, quantise="round")
    assert SizeMap.from_dict(json.loads(sm.to_json())).quantise == "round"
    with pytest.raises(EgressError):
        egress.prepare(archviz_plate(400, 956), tier_c(956, 400), FIXED_169, quantise="jpeg")


def test_quantise8_is_de00_nearest_and_beats_rounding():
    """Rounding each channel alone puts near-neutral archviz colours 0.5 to 1.0 dE00 off; the dE00-nearest
    floor/ceil triple keeps p99 under 0.45 and never moves a channel by a whole code."""
    plate = archviz_plate(400, 956, seed=15)
    f = as_float(plate)
    q = egress.quantise8(plate)
    rounded = egress.quantise8(plate, "round")
    assert q.dtype == np.uint8 and q.shape == plate.shape
    assert np.array_equal(rounded, np.round(f * 255).astype(np.uint8))
    assert np.abs(q.astype(np.float64) - f * 255).max() < 1.0
    ours, naive = de00_p99(q, plate), de00_p99(rounded, plate)
    assert ours <= 0.45 and ours < 0.75 * naive


def test_quantise8_matches_an_exhaustive_de00_search():
    rng = np.random.default_rng(16)
    v = np.concatenate(
        [
            rng.random((4000, 3)),  # anything
            0.5 + 0.03 * rng.standard_normal((4000, 3)),  # near-neutral mid greys
            np.clip(rng.random((2000, 3)) * [0.2, 0.35, 0.3] + [0.0, 0.2, 0.65], 0, 1),  # skies (R_T region)
            rng.random((2000, 3)) * 0.05,  # deep shadows (linear segments of sRGB and Lab)
        ]
    )[:, None, :]
    q = egress.quantise8(v).astype(np.float64)
    lab = srgb_to_lab(v * 255)
    lo = np.floor(v * 255)
    best = np.full(v.shape[:2], np.inf)
    for k in range(8):
        cand = np.clip(lo + np.array([(k >> i) & 1 for i in range(3)]), 0, 255)
        best = np.minimum(best, ciede2000(lab, srgb_to_lab(cand)))
    ours = ciede2000(lab, srgb_to_lab(q))
    assert float(np.mean(ours <= best + 1e-6)) >= 0.97  # the linearised metric picks the true optimum
    assert float((ours - best).max()) <= 0.05


def test_quantise8_keeps_exact_codes_and_extremes():
    codes = np.arange(256, dtype=np.uint16)
    grid = np.stack(np.meshgrid(codes, codes[::17], codes[::51], indexing="ij"), -1).reshape(-1, 1, 3)
    assert np.array_equal(egress.quantise8(grid * 257), grid.astype(np.uint8))  # 8-bit-sourced plates
    assert np.array_equal(egress.quantise8(grid.astype(np.uint8)), grid.astype(np.uint8))
    with pytest.raises(EgressError):
        egress.quantise8(grid * 257, "dither")


def test_quantiser_uses_the_gates_colour_constants():
    from plate_gates import color

    assert np.array_equal(egress._M_XYZ, color._M) and np.array_equal(egress._WHITE, color._WHITE)


# --------------------------------------------------------------------------- region crop


def test_region_crop_box_is_bbox_grown_25pct_and_clamped():
    mask = np.zeros((360, 640), bool)
    mask[150:198, 300:364] = True  # 64 x 48
    assert egress.region_box(mask) == (300 - 16, 150 - 12, 364 + 16, 198 + 12)
    corner = np.zeros((360, 640), bool)
    corner[0:48, 600:640] = True  # 40 x 48 at the top-right corner
    assert egress.region_box(corner) == (590, 0, 640, 60)
    with pytest.raises(EgressError):
        egress.region_box(np.zeros((360, 640), bool))


def test_region_crop_pads_with_real_context_before_replicating():
    plate = archviz_plate(360, 640, seed=6)
    mask = np.zeros((360, 640), bool)
    mask[150:198, 300:364] = True
    png, sm = egress.prepare(plate, tier_c(640, 360), ARBITRARY, mask=mask, region_crop=True)
    assert sm.region == (284, 138, 380, 210)
    assert sm.mode == "arbitrary" and sm.pad == (0, 0, 0, 0) and sm.pad_mode == "overscan"
    png, sm = egress.prepare(plate, tier_c(640, 360), FIXED_169, mask=mask, region_crop=True)
    assert sm.mode == "pad" and sm.synthetic == (0, 0, 0, 0) and sm.pad_mode == "overscan"
    x0, y0, x1, y1 = sm.padded_rect
    assert (x1 - x0) / (y1 - y0) == pytest.approx(16 / 9, abs=0.02)
    sent = decode_png(png)
    expect = egress.quantise8(plate[y0:y1, x0:x1])
    assert np.array_equal(sent, expect)  # every sent pixel is a real plate pixel


def test_region_crop_is_never_upsampled():
    plate = archviz_plate(360, 640, seed=7)
    mask = np.zeros((360, 640), bool)
    mask[150:198, 300:364] = True
    # Presets larger than the crop, engine not arbitrary: native size, scale 1.
    _, sm = egress.prepare(plate, tier_c(640, 360), FIXED_169, mask=mask, region_crop=True)
    assert sm.scale == 1.0 and sm.dst == sm.padded_size and "native" in sm.note
    # Arbitrary engine: native size.
    _, sm = egress.prepare(plate, tier_c(640, 360), ARBITRARY, mask=mask, region_crop=True)
    assert sm.scale == 1.0 and sm.dst == (96, 72)
    # A preset smaller than the crop: the largest preset <= crop, downsampled.
    big = np.zeros((360, 640), bool)
    big[60:300, 100:540] = True
    _, sm = egress.prepare(plate, tier_c(640, 360), SMALL_PRESETS, mask=big, region_crop=True)
    assert sm.dst == (512, 288) and sm.scale < 1.0


def test_region_crop_round_trip_leaves_the_outside_untouched():
    plate = archviz_plate(360, 640, seed=8)
    mask = np.zeros((360, 640), bool)
    mask[120:220, 250:400] = True
    png, sm = egress.prepare(plate, tier_c(640, 360), FIXED_169, mask=mask, region_crop=True)
    out, rep = ingress.restore(png, sm, plate)
    assert out.shape == plate.shape
    x0, y0, x1, y1 = sm.region
    outside = np.ones((360, 640), bool)
    outside[y0:y1, x0:x1] = False
    assert np.array_equal(out[outside], plate[outside])  # the plate, bit for bit
    assert de00_p99(out[y0:y1, x0:x1], plate[y0:y1, x0:x1]) <= 0.5
    assert rep["registered"] and rep["region"] == list(sm.region)


@pytest.mark.parametrize("kind", ["float64", "float32", "uint8"])
def test_region_crop_outside_is_the_plate_for_every_depth(kind):
    """No float32 detour outside the region: float64 stays float64 bit for bit, uint8 becomes code x 257."""
    base = archviz_plate(360, 640, seed=17)
    plate = {
        "float64": as_float(base),
        "float32": as_float(base).astype(np.float32),
        "uint8": np.round(as_float(base) * 255).astype(np.uint8),
    }[kind]
    mask = np.zeros((360, 640), bool)
    mask[120:220, 250:400] = True
    png, sm = egress.prepare(plate, tier_c(640, 360), FIXED_169, mask=mask, region_crop=True)
    out, rep = ingress.restore(png, sm, plate)
    x0, y0, x1, y1 = sm.region
    outside = np.ones((360, 640), bool)
    outside[y0:y1, x0:x1] = False
    expect = plate.astype(np.uint16) * 257 if kind == "uint8" else plate
    assert out.dtype == expect.dtype and np.array_equal(out[outside], expect[outside])
    assert rep["registered"]


# --------------------------------------------------------------------------- identity round trip and restore


def test_identity_round_trip_239_plate_padded_to_169():
    plate = archviz_plate(400, 956, seed=9)
    png, sm = egress.prepare(plate, tier_c(956, 400), FIXED_169)
    assert sm.pad_mode == "edge_extend"
    out, rep = ingress.restore(png, sm, plate)
    assert out.shape == plate.shape and out.dtype == np.uint16
    assert de00_p99(out, plate) <= 0.5
    assert rep["registered"] and not rep["camera_drift"] and rep["shift_px"] < 0.05
    # Nothing but 8-bit quantisation: the restored pixels are exactly the sent codes, promoted to 16 bits.
    sent = decode_png(png)[sm.pad[1] : sm.pad[1] + 400]
    assert np.array_equal(out, sent.astype(np.uint16) * 257)


def test_identity_round_trip_tier_a_overscan_float_plate():
    plate = as_float(archviz_plate(360, 640, seed=10)).astype(np.float32)
    png, sm = egress.prepare(plate, tier_a_overscan(640, 360, 268), TIERED)
    out, rep = ingress.restore(png, sm, plate)
    assert out.shape == plate.shape and out.dtype == np.float32
    assert de00_p99(out, plate) <= 0.5 and rep["registered"]


def test_identity_round_trip_room_a_fixture():
    tif = ROOM_A / "display.tif"
    if not tif.is_file():
        pytest.skip("fixtures/packets/room_a not rendered yet (fixtures/blender/render.sh)")
    from plate_pixels import imageio

    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    plate = imageio.read(tif).rgb
    png, sm = egress.prepare(plate, manifest, TIERED)
    out, rep = ingress.restore(png, sm, plate)
    assert out.shape == plate.shape
    assert de00_p99(out, plate) <= 0.5
    assert rep["registered"] and not rep["camera_drift"]


def test_restore_recovers_a_3px_shift_and_aligns():
    plate = archviz_plate(360, 640, seed=11)
    png, sm = egress.prepare(plate, tier_c(640, 360), TIERED)
    out, rep = ingress.restore(shifted(plate, 3, 0), sm, plate)
    assert rep["dx"] == pytest.approx(3.0, abs=0.1) and abs(rep["dy"]) < 0.1
    assert rep["registered"] and rep["warp_applied"] and not rep["camera_drift"]
    inner = np.zeros(plate.shape[:2], bool)
    inner[8:-8, 8:-8] = True
    assert de00_p99(out, plate, inner) <= 1.0


def test_restore_flags_a_9px_shift_as_camera_drift():
    plate = archviz_plate(360, 640, seed=12)
    png, sm = egress.prepare(plate, tier_c(640, 360), TIERED)
    out, rep = ingress.restore(shifted(plate, 9, 0), sm, plate)
    assert rep["shift_px"] > 8.0
    assert rep["camera_drift"] is True and rep["registered"] is False and rep["warp_applied"] is False
    assert out.shape == plate.shape
    json.dumps(rep, allow_nan=False)


def test_restore_maps_a_vendor_tier_larger_than_sent_back_with_area():
    """The vendor answered a native 640 x 360 send at its 2x tier, with detail of its own: box-filtered back."""
    hi = archviz_plate(720, 1280, seed=13)
    down = cv2.resize(as_float(hi), (640, 360), interpolation=cv2.INTER_AREA)
    plate = np.round(down * 65535).astype(np.uint16)
    png, sm = egress.prepare(plate, tier_c(640, 360), TIERED)
    assert sm.dst == (640, 360)
    vendor = np.round(as_float(hi) * 255).astype(np.uint8)
    out, rep = ingress.restore(vendor, sm, plate)
    assert out.shape == plate.shape and rep["resample"] == "area" and rep["registered"]
    assert rep["result_size"] == [1280, 720] and rep["shift_px"] < 0.1
    assert de00_p99(out, plate) <= 1.0


def test_restore_maps_snapped_vendor_sizes_back_exactly():
    """A vendor snapped the send to a slightly larger preset (Lanczos back) or to a slightly wider frame
    (centre-cropped to the sent aspect first, never squashed)."""
    plate = archviz_plate(360, 640, seed=13)
    png, sm = egress.prepare(plate, tier_c(640, 360), TIERED)
    sent = decode_png(png)
    snapped = cv2.resize(sent, (704, 396), interpolation=cv2.INTER_LANCZOS4)
    out, rep = ingress.restore(snapped, sm, plate)
    assert out.shape == plate.shape and rep["resample"] == "lanczos4" and rep["registered"]
    assert rep["shift_px"] < 0.05 and not rep["warp_applied"]  # mapped back on the grid, not nudged
    # Exactly the documented resample (Lanczos4 to plate pixels, promoted to 16 bits)...
    expect = cv2.resize(snapped.astype(np.float32) / 255.0, (640, 360), interpolation=cv2.INTER_LANCZOS4)
    expect = np.round(np.clip(expect, 0, 1) * 65535).astype(np.int64)
    assert np.abs(out.astype(np.int64) - expect).max() <= 1
    # ...whose own cost is Lanczos 1.1x up and back on near-Nyquist texture: ~1.6 dE00 p99 on this plate,
    # against ~0.37 for the 8-bit send alone. The bound catches a squash or a half-pixel offset (both > 3).
    assert de00_p99(out, plate) <= 2.0
    wider = cv2.copyMakeBorder(sent, 0, 0, 4, 4, cv2.BORDER_REPLICATE)  # 648 x 360, aspect off by 1.25%
    out, rep = ingress.restore(wider, sm, plate)
    assert out.shape == plate.shape and rep["aspect_crop"] == [4, 0, 4, 0] and rep["registered"]
    assert rep["resample"] == "none" and np.array_equal(out, sent.astype(np.uint16) * 257)


def test_restore_accepts_the_sizemap_as_a_dict_and_checks_the_plate():
    plate = archviz_plate(120, 200, seed=14)
    png, sm = egress.prepare(plate, None, TIERED)
    out, _ = ingress.restore(png, sm.to_dict(), plate)
    assert out.shape == plate.shape
    with pytest.raises(ingress.IngressError):
        ingress.restore(png, sm, archviz_plate(100, 200))
    with pytest.raises(ingress.IngressError):
        ingress.restore(b"not an image", sm, plate)
