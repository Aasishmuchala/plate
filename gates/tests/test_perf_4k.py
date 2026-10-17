"""The per-material gate at 4K (router plan T67).

Two things are held here. First, the one-pass `material_de.per_material` gives the numbers of the per-id
implementation it replaced: `_per_material_reference` below is that implementation as it stood before T67 (a
full-frame mask, crop, blur, Lab and CIEDE2000 for every legend id), kept in the test and not in the module.
On room_a, for an untouched output, a material edit, a feathered composite that reaches into the neighbours,
a whole-frame edit, split and shared Cryptomatte coverage, the global blur variant and hero targets, every id
must agree within 0.05 dE00 (the counts exactly).

Second, a gate run on a 3840 x 2160 still stays inside its budget on CPU. The 4K packet is built in tmp_path
from room_a (EXR layers nearest-neighbour x6, the display TIFF Lanczos x6). The packet is warmed the way the
daemon's PacketView cache warms it (G-buffer edges and the material Cryptomatte decoded once per packet, then
passed to every gate run of that packet, as `Pipeline.gate` does) and one gate run on the 640 x 360 packet
warms the imports; then one `gating.run_gates` at 3840 x 2160 is timed: wall <= 10 s, per_material <= 2.5 s.
It prints `run_gates 3840x2160: wall <n>s per_material <m>s` (inline and in the terminal summary) and is
skipped under CI=1, since GitHub runners are slower.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from plate_gates import material_de
from plate_gates.color import ciede2000, srgb_to_lab
from plate_gates.masks import erode

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
PANEL = "m:ca5b90f3"  # ARCH_panel_3020, a mid-frame panel with neighbours on every side
SCALE = 6  # 640 x 360 -> 3840 x 2160
WALL_BUDGET_S = 10.0
PER_MATERIAL_BUDGET_S = 2.5
TOL_DE = 0.05

_summary: list[str] = []


# ---------------------------------------------------------------------------- the frozen per-id implementation


def _blur_lab_reference(rgb8: np.ndarray, weight: np.ndarray | None, sigma: float) -> np.ndarray:
    f = rgb8.astype(np.float32)
    if weight is None:
        blurred = cv2.GaussianBlur(f, (0, 0), sigma)
    else:
        num = cv2.GaussianBlur(f * weight[..., None], (0, 0), sigma)
        den = cv2.GaussianBlur(weight, (0, 0), sigma)
        blurred = num / np.maximum(den, 1e-6)[..., None]
    return srgb_to_lab(blurred)


def _per_material_reference(
    plate_rgb: np.ndarray,
    test_rgb: np.ndarray,
    crypto_material_ranks: np.ndarray,
    coverage: np.ndarray,
    legend: Any,
    targets: dict[str, Any] | None = None,
    *,
    kind: str | None = "material",
    sigma: float = material_de.SIGMA,
    erode_px: int = material_de.ERODE_PX,
    min_px: int = material_de.MIN_PX,
    threshold: float = material_de.MASK_THRESHOLD,
    isolate: bool = True,
) -> dict[str, dict]:
    """material_de.per_material before T67, verbatim but for the names: one full-frame pass per legend id."""
    plate = material_de._rgb8(plate_rgb, "plate_rgb")
    test = material_de._rgb8(test_rgb, "test_rgb")
    ranks = np.asarray(crypto_material_ranks)
    cov = np.asarray(coverage)
    if ranks.ndim == 2:
        ranks, cov = ranks[..., None], cov[..., None]
    want = {str(k).lower(): np.asarray(v, dtype=np.float64) for k, v in (targets or {}).items()}

    de_global = None
    if not isolate:
        de_global = ciede2000(_blur_lab_reference(plate, None, sigma), _blur_lab_reference(test, None, sigma))
    lab_plate = srgb_to_lab(plate)
    lab_test = srgb_to_lab(test)
    pad = int(np.ceil(4.0 * sigma)) + 1
    h, w = plate.shape[:2]

    out: dict[str, dict] = {}
    for entry in material_de.legend_entries(legend):
        if kind is not None and entry.get("kind") not in (None, kind):
            continue
        cid = str(entry["id"])
        if cid in out:
            continue
        target = np.uint32(material_de.id_to_u32(cid))
        raw = np.where(ranks == target, cov, 0.0).sum(axis=2) >= threshold
        if int(raw.sum()) < min_px:
            continue
        m = erode(raw, erode_px)
        n_px = int(m.sum())
        if n_px < min_px:
            continue
        ys, xs = np.nonzero(raw)
        y0, y1 = max(int(ys.min()) - pad, 0), min(int(ys.max()) + 1 + pad, h)
        x0, x1 = max(int(xs.min()) - pad, 0), min(int(xs.max()) + 1 + pad, w)
        sl = (slice(y0, y1), slice(x0, x1))
        if isolate:
            weight = raw[sl].astype(np.float32)
            de = ciede2000(
                _blur_lab_reference(plate[sl], weight, sigma), _blur_lab_reference(test[sl], weight, sigma)
            )
            de_m = de[m[sl]]
        else:
            de_m = de_global[m]
        mean_plate = lab_plate[m].mean(axis=0)
        mean_test = lab_test[m].mean(axis=0)
        row: dict[str, Any] = {
            "de00_median": float(np.median(de_m)),
            "de00_p90": float(np.percentile(de_m, 90)),
            "n_px": n_px,
            "lab_mean_plate": [float(v) for v in mean_plate],
            "lab_mean_test": [float(v) for v in mean_test],
        }
        goal = want.get(cid.lower())
        if goal is not None:
            row["target_de"] = float(ciede2000(mean_test[None, :], goal[None, :])[0])
        out[cid] = row
    return out


# ---------------------------------------------------------------------------- room_a and its edits


def _read_packet(root: Path) -> dict[str, Any]:
    import tifffile

    from plate_pixels.exr import read_cryptomatte

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    legend = json.loads((root / "id_legend.json").read_text(encoding="utf-8"))
    plate16 = np.ascontiguousarray(tifffile.imread(root / "display.tif")[..., :3])
    ranks, cov = read_cryptomatte(root / "beauty.exr", manifest, "material")
    return {
        "root": root,
        "manifest": manifest,
        "legend": legend,
        "plate16": plate16,
        "ranks": ranks,
        "cov": cov,
    }


@pytest.fixture(scope="module")
def room_a() -> dict[str, Any]:
    if not (ROOM_A / "beauty.exr").is_file():
        pytest.skip(f"fixture packet missing: {ROOM_A}")
    return _read_packet(ROOM_A)


def _plate8(pk: dict[str, Any]) -> np.ndarray:
    return ((pk["plate16"].astype(np.uint32) + 128) // 257).astype(np.uint8)


def _panel(pk: dict[str, Any], cid: str = PANEL) -> np.ndarray:
    from plate_pixels.exr import id_mask

    return id_mask(pk["ranks"], pk["cov"], cid)


def _shift(img8: np.ndarray, where: np.ndarray, rgb: tuple[int, int, int]) -> np.ndarray:
    out = img8.copy()
    out[where] = np.clip(out[where].astype(np.int16) + np.array(rgb, np.int16), 0, 255).astype(np.uint8)
    return out


def _whole_frame(img: np.ndarray, seed: int = 7) -> np.ndarray:
    """A re-photograph of a uint8 or uint16 image: a warm tone shift, a little contrast and grain on every pixel."""
    top = float(np.iinfo(img.dtype).max)
    s = top / 255.0
    rng = np.random.default_rng(seed)
    f = img.astype(np.float32)
    f = (f - 128.0 * s) * 1.06 + 128.0 * s + np.array([6.0, 1.0, -5.0], np.float32) * s
    f += rng.normal(0.0, 2.0 * s, f.shape).astype(np.float32)
    return np.clip(np.rint(f), 0, top).astype(img.dtype)


def _composite(
    plate: np.ndarray, ai: np.ndarray, region: np.ndarray, grow_px: int, feather_px: float
) -> tuple[np.ndarray, np.ndarray]:
    """(output, editable mask): `ai` composited through `region` grown by grow_px, feathered inside the mask
    and bit-identical to the plate outside it, as the compositor guarantees."""
    from plate_gates.masks import feather_inside

    mask = cv2.dilate(region.astype(np.uint8), np.ones((2 * grow_px + 1,) * 2, np.uint8)) > 0
    a = feather_inside(mask, feather_px)[..., None]
    mixed = plate.astype(np.float32) * (1 - a) + ai.astype(np.float32) * a
    out = np.clip(np.rint(mixed), 0, np.iinfo(plate.dtype).max).astype(plate.dtype)
    out[~mask] = plate[~mask]
    return out, mask


def _feathered(p8: np.ndarray, pk: dict[str, Any], grow_px: int, feather_px: float) -> np.ndarray:
    """The re-photograph composited into room_a's panel through a grown, feathered mask."""
    return _composite(p8, _whole_frame(p8), _panel(pk), grow_px, feather_px)[0]


def _assert_same(fast: dict[str, dict], ref: dict[str, dict]) -> float:
    """Every id within TOL_DE dE00 and the counts exact; returns the largest dE00 difference seen."""
    assert list(fast) == list(ref)
    worst = 0.0
    for cid, r in ref.items():
        f = fast[cid]
        assert set(f) == set(r), cid
        assert f["n_px"] == r["n_px"], cid
        for key in ("de00_median", "de00_p90", "target_de"):
            if key in r:
                d = abs(f[key] - r[key])
                worst = max(worst, d)
                assert d <= TOL_DE, (cid, key, f[key], r[key])
        for key in ("lab_mean_plate", "lab_mean_test"):
            assert np.allclose(f[key], r[key], rtol=0, atol=1e-6), (cid, key)
    return worst


# ---------------------------------------------------------------------------- equal to the per-id implementation


def test_untouched_output_measures_zero_on_every_material(room_a):
    p8 = _plate8(room_a)
    fast = material_de.per_material(p8, p8, room_a["ranks"], room_a["cov"], room_a["legend"])
    ref = _per_material_reference(p8, p8, room_a["ranks"], room_a["cov"], room_a["legend"])
    _assert_same(fast, ref)
    assert len(fast) >= 8
    for row in fast.values():
        assert row["de00_median"] == 0.0 and row["de00_p90"] == 0.0
        assert row["lab_mean_plate"] == row["lab_mean_test"]


def test_material_edit_matches_the_per_id_implementation(room_a):
    p8 = _plate8(room_a)
    out = _shift(p8, _panel(room_a), (14, -4, -10))
    fast = material_de.per_material(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"])
    ref = _per_material_reference(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"])
    assert _assert_same(fast, ref) < 1e-3
    assert fast[PANEL]["de00_median"] > 3.0
    for cid, row in fast.items():
        if cid != PANEL:  # isolation: the edit does not bleed into the panel's neighbours
            assert row["de00_p90"] == 0.0 and row["lab_mean_plate"] == row["lab_mean_test"]


def test_feathered_composite_reaching_the_neighbours_matches(room_a):
    p8 = _plate8(room_a)
    out = _feathered(p8, room_a, 10, 8)
    fast = material_de.per_material(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"])
    ref = _per_material_reference(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"])
    assert _assert_same(fast, ref) < 1e-3
    touched = [cid for cid, row in fast.items() if cid != PANEL and row["de00_p90"] > 0]
    assert touched  # the grown mask did reach a neighbour, so the near-change path ran on it


def test_whole_frame_edit_matches(room_a):
    p8 = _plate8(room_a)
    out = _whole_frame(p8)
    fast = material_de.per_material(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"])
    ref = _per_material_reference(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"])
    assert _assert_same(fast, ref) < 1e-3
    assert all(row["de00_median"] > 0.5 for row in fast.values())


def test_global_blur_variant_and_targets_match(room_a):
    p8 = _plate8(room_a)
    out = _shift(p8, _panel(room_a), (14, -4, -10))
    targets = {PANEL: [40.0, 2.0, 10.0], "m:57b3d007": [70.0, 0.0, 0.0]}
    for isolate in (False, True):
        fast = material_de.per_material(
            p8, out, room_a["ranks"], room_a["cov"], room_a["legend"], targets, isolate=isolate
        )
        ref = _per_material_reference(
            p8, out, room_a["ranks"], room_a["cov"], room_a["legend"], targets, isolate=isolate
        )
        assert _assert_same(fast, ref) < 1e-3
        assert "target_de" in fast[PANEL] and "target_de" in fast["m:57b3d007"]


def test_split_and_shared_coverage_match(room_a):
    """Pixels where an id's coverage is split over two ranks, or two ids share a pixel at exactly 0.5 each."""
    p8 = _plate8(room_a)
    out = _feathered(p8, room_a, 6, 4)
    ranks, cov = room_a["ranks"].copy(), room_a["cov"].copy()
    panel = _panel(room_a)
    wall = material_de.id_to_u32("m:57b3d007")  # ARCH_wall_N
    pid = material_de.id_to_u32(PANEL)
    ys, xs = np.nonzero(panel)
    split = (ys % 5 == 0) & (xs % 3 == 0)  # the panel's own id over ranks 0 and 2: 0.35 + 0.35
    ranks[ys[split], xs[split], 0], cov[ys[split], xs[split], 0] = pid, 0.35
    ranks[ys[split], xs[split], 2], cov[ys[split], xs[split], 2] = pid, 0.35
    shared = (ys % 7 == 1) & (xs % 4 == 1)  # 0.5 panel + 0.5 wall: the pixel belongs to both
    ranks[ys[shared], xs[shared], 0], cov[ys[shared], xs[shared], 0] = pid, 0.5
    ranks[ys[shared], xs[shared], 1], cov[ys[shared], xs[shared], 1] = wall, 0.5
    lone = (ys % 11 == 3) & (xs % 2 == 0)  # 0.3 alone: no longer the panel's
    ranks[ys[lone], xs[lone], 0], cov[ys[lone], xs[lone], 0] = pid, 0.3
    ranks[ys[lone], xs[lone], 1], cov[ys[lone], xs[lone], 1] = 0, 0.0
    for isolate in (True, False):
        fast = material_de.per_material(p8, out, ranks, cov, room_a["legend"], isolate=isolate)
        ref = _per_material_reference(p8, out, ranks, cov, room_a["legend"], isolate=isolate)
        assert _assert_same(fast, ref) < 1e-3
    assert (
        fast[PANEL]["n_px"]
        != material_de.per_material(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"])[PANEL]["n_px"]
    )


def test_object_kind_and_duplicate_ids_match(room_a):
    """kind=None scores the object and the material entries that share one Cryptomatte hash, each once."""
    p8 = _plate8(room_a)
    out = _shift(p8, _panel(room_a), (-8, 6, 12))
    legend = dict(room_a["legend"])
    legend["entries"] = room_a["legend"]["entries"] + room_a["legend"]["entries"][:3]  # repeated entries
    fast = material_de.per_material(p8, out, room_a["ranks"], room_a["cov"], legend, kind=None)
    ref = _per_material_reference(p8, out, room_a["ranks"], room_a["cov"], legend, kind=None)
    assert _assert_same(fast, ref) < 1e-3
    assert any(cid.startswith("o:") for cid in fast)


@pytest.mark.parametrize(
    "params",
    [
        {"sigma": 2.0, "erode_px": 0, "min_px": 10},
        {"sigma": 3.5, "erode_px": 5, "threshold": 0.4},
        {"sigma": 6.0, "erode_px": 1, "min_px": 2000},
    ],
)
def test_other_parameters_match(room_a, params):
    p8 = _plate8(room_a)
    out = _feathered(p8, room_a, 10, 8)
    fast = material_de.per_material(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"], **params)
    ref = _per_material_reference(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"], **params)
    assert _assert_same(fast, ref) < 1e-3


def test_cell_by_cell_blur_and_thread_count_change_nothing(room_a, monkeypatch):
    """A small tile forces the sparse-id path on every id; one thread gives the threaded numbers bit for bit."""
    p8 = _plate8(room_a)
    for out in (_whole_frame(p8), _feathered(p8, room_a, 10, 8)):
        base = material_de.per_material(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"])
        monkeypatch.setattr(material_de, "_TILE", 24)
        tiled = material_de.per_material(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"])
        ref = _per_material_reference(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"])
        assert _assert_same(tiled, ref) < 1e-3
        monkeypatch.setattr(material_de, "_THREADS", 1)
        assert material_de.per_material(p8, out, room_a["ranks"], room_a["cov"], room_a["legend"]) == tiled
        monkeypatch.undo()
        assert _assert_same(base, ref) < 1e-3


# ---------------------------------------------------------------------------- the 4K gate run


class _SummaryLines:
    """Repeats the timing line at the end of the run, where `pytest ... | tail` shows it."""

    def pytest_terminal_summary(self, terminalreporter) -> None:
        for line in _summary:
            terminalreporter.write_line(line)


def _build_4k(dst: Path) -> None:
    """room_a at 3840 x 2160: the EXR layers the gates read, nearest-neighbour x6; the display TIFF, Lanczos x6."""
    import OpenEXR
    import tifffile

    from plate_pixels.exr import channel_name

    dst.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    exr_ref = manifest["files"]["beauty_exr"]
    keep = {"beauty", "zdepth", "normals", "cryptomatte_object", "cryptomatte_material"}
    exr_ref["elements"] = [e for e in exr_ref["elements"] if e["element"] in keep]
    src = OpenEXR.File(str(ROOM_A / exr_ref["path"]), separate_channels=True).parts[0]
    channels = {}
    for e in exr_ref["elements"]:
        for ch in e["channels"]:
            name = channel_name(e["layer"], ch)
            channels[name] = np.ascontiguousarray(
                np.repeat(np.repeat(src.channels[name].pixels, SCALE, 0), SCALE, 1)
            )
    header = {k: v for k, v in src.header.items() if k.startswith("cryptomatte/")}
    header.update({"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage})
    OpenEXR.File(header, channels).write(str(dst / exr_ref["path"]))

    plate = tifffile.imread(ROOM_A / manifest["files"]["display_tiff16"]["path"])
    h, w = plate.shape[:2]
    big = cv2.resize(plate, (w * SCALE, h * SCALE), interpolation=cv2.INTER_LANCZOS4)
    tifffile.imwrite(dst / manifest["files"]["display_tiff16"]["path"], big)
    (dst / "id_legend.json").write_bytes((ROOM_A / "id_legend.json").read_bytes())

    cam = manifest["camera"]
    cam["resolution"]["width"], cam["resolution"]["height"] = w * SCALE, h * SCALE
    ip = cam["intrinsics_px"]
    ip["fx"], ip["fy"] = ip["fx"] * SCALE, ip["fy"] * SCALE
    ip["principal_point"] = [v * SCALE for v in ip["principal_point"]]
    ip["K"] = [
        [ip["fx"], 0.0, ip["principal_point"][0]],
        [0.0, ip["fy"], ip["principal_point"][1]],
        [0.0, 0.0, 1.0],
    ]
    if cam.get("safe_frame"):
        cam["safe_frame"] = {k: v * SCALE for k, v in cam["safe_frame"].items()}
    for role in ("beauty_exr", "display_tiff16"):
        manifest["files"][role]["width"], manifest["files"][role]["height"] = w * SCALE, h * SCALE
        manifest["files"][role]["bytes"] = (dst / manifest["files"][role]["path"]).stat().st_size
    with open(dst / "manifest.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=1)


def _warm(root: Path) -> dict[str, Any]:
    """A packet as the daemon's PacketView cache holds it: plate, legend, G-buffer edges, material Cryptomatte."""
    from plate_gates import gbuffer

    pk = _read_packet(root)
    pk["edges"] = gbuffer.from_packet(root, manifest=pk["manifest"])
    return pk


def _gate(pk: dict[str, Any], out16: np.ndarray, mask: np.ndarray):
    from plate_daemon import gating

    return gating.run_gates(
        pk["plate16"],
        out16,
        {"path": pk["root"], "manifest": pk["manifest"]},
        pk["legend"],
        "A",
        {"id": "perf-4k", "version": None},
        mask=mask,
        edges=pk["edges"],
        crypto_material=(pk["ranks"], pk["cov"]),
    )


@pytest.fixture(scope="module")
def packets_4k(tmp_path_factory) -> tuple[dict[str, Any], dict[str, Any]]:
    """(room_a, room_a at 4K), both warmed. OpenEXR's thread pool is raised for the build and the decode only."""
    if os.environ.get("CI", "").strip().lower() in ("1", "true", "yes"):
        pytest.skip("CI is set: the 4K gate budget is measured on the studio Mac, GitHub runners are slower")
    if not (ROOM_A / "beauty.exr").is_file():
        pytest.skip(f"fixture packet missing: {ROOM_A}")
    import OpenEXR

    before = OpenEXR.global_thread_count()
    OpenEXR.set_global_thread_count(os.cpu_count() or 1)
    try:
        root = tmp_path_factory.mktemp("room_a_4k")
        _build_4k(root)
        return _warm(ROOM_A), _warm(root)
    finally:
        OpenEXR.set_global_thread_count(before)


def test_gate_run_on_a_4k_still_inside_the_budget(packets_4k, monkeypatch, request):
    small, big = packets_4k
    h, w = big["plate16"].shape[:2]
    assert (w, h) == (3840, 2160)
    region_small = _panel(small)
    out_small, mask_small = _composite(small["plate16"], _whole_frame(small["plate16"]), region_small, 4, 3)
    _gate(small, out_small, mask_small)  # warms the imports and the lazily built kernels

    out16, mask = _composite(big["plate16"], _whole_frame(big["plate16"]), _panel(big), 24, 12)
    spent: list[float] = []
    real = material_de.per_material

    def timed(*args: Any, **kwargs: Any) -> dict[str, dict]:
        t0 = time.perf_counter()
        try:
            return real(*args, **kwargs)
        finally:
            spent.append(time.perf_counter() - t0)

    monkeypatch.setattr(material_de, "per_material", timed)
    t0 = time.perf_counter()
    report = _gate(big, out16, mask)
    wall = time.perf_counter() - t0
    assert len(spent) == 1
    line = f"run_gates {w}x{h}: wall {wall:.2f}s per_material {spent[0]:.2f}s"
    print(f"\n{line}")
    _summary.append(line)
    if not request.config.pluginmanager.has_plugin("plate-perf-4k"):
        request.config.pluginmanager.register(_SummaryLines(), "plate-perf-4k")

    per = report.metrics["material"]["per_id"]
    assert per[PANEL]["de00_median"] > 1.0  # the edit is measured
    assert report.metrics["material"]["locked"]  # and the locked materials around it are held
    assert wall <= WALL_BUDGET_S, line
    assert spent[0] <= PER_MATERIAL_BUDGET_S, line
