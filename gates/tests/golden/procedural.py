"""Procedural golden images and the metric sections frozen in expected_procedural.json.

Nothing here is read from disk: every image is built from numpy.random.default_rng(seed) (PCG64, stable across
platforms) plus cv2 primitives (gradients, anti-aliased rectangles, a lettered plaque via cv2.putText with
FONT_HERSHEY_SIMPLEX, multi-octave value noise), so a fresh clone and CI run the same goldens as the dev box.
The client-derived goldens next to it stay gitignored; T54 adds Blender-rendered ones on top.

`sections()` returns {section: {case: flat metrics}}; test_golden_procedural.py compares it with the committed
expected_procedural.json using the tolerance rules in `tolerance()`. Regenerate with
`pytest gates/tests/test_golden_procedural.py --update-goldens` (CI never passes the flag).
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from plate_gates import io as pio
from plate_gates import texture
from plate_gates.color import ciede2000, srgb_to_lab
from plate_gates.compare import compare, maskdiff
from plate_gates.composite import composite_masked, match_to_ring
from plate_gates.labmatch import reinhard
from plate_gates.masks import rect, ring
from plate_gates.video import camera_lock_phasecorr, duration_check, flicker, landing, write_test_clip

EXPECTED = Path(__file__).resolve().parent / "expected_procedural.json"

STILL_W = 640  # compare / maskdiff / composite / labmatch scenes
TEXTURE_W = 1024  # texture.stats(width=1024)


# --- image builders ---------------------------------------------------------------------------------------


def noise_field(w: int, h: int, seed: int, octaves: int = 5, persistence: float = 0.55, base: int = 6):
    """Multi-octave value noise in 0..1 (float32): bicubic-upsampled random grids, halving the cell size."""
    rng = np.random.default_rng(seed)
    out = np.zeros((h, w), np.float32)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        gw = base * 2**o
        gh = max(int(round(gw * h / w)), 2)
        grid = rng.random((gh + 1, gw + 1)).astype(np.float32)
        out += amp * cv2.resize(grid, (w, h), interpolation=cv2.INTER_CUBIC)
        total += amp
        amp *= persistence
    out /= total
    return (out - out.min()) / (out.max() - out.min() + 1e-9)


def gradient(w: int, h: int, top: tuple, bottom: tuple) -> np.ndarray:
    """Vertical BGR gradient, uint8 (H, W, 3)."""
    t = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
    col = np.array(top, np.float32) * (1 - t) + np.array(bottom, np.float32) * t
    return np.repeat(np.round(col).astype(np.uint8), w, axis=1)


def scene(width: int = STILL_W, seed: int = 0, aspect: float = 16 / 9) -> np.ndarray:
    """A small interior: warm wall, textured floor, framed windows, a lamp, a lettered plaque. BGR uint8."""
    w, h = int(width), int(round(width / aspect))
    img = gradient(w, h, (196, 188, 178), (164, 156, 148))
    floor_y = int(h * 0.68)
    img[floor_y:] = gradient(w, h - floor_y, (92, 104, 118), (58, 68, 80))
    # floor texture: multi-octave noise, amplitude ~22 levels
    tex = (noise_field(w, h - floor_y, seed + 11) - 0.5) * 44.0
    img[floor_y:] = np.clip(img[floor_y:].astype(np.float32) + tex[..., None], 0, 255).astype(np.uint8)
    rng = np.random.default_rng(seed)
    # windows
    for i in range(3):
        x0 = int(w * (0.08 + 0.17 * i)) + int(rng.integers(0, 6))
        y0 = int(h * 0.12) + int(rng.integers(0, 6))
        ww, hh = int(w * 0.13), int(h * 0.38)
        cv2.rectangle(img, (x0, y0), (x0 + ww, y0 + hh), (236, 232, 222), -1, cv2.LINE_AA)
        cv2.rectangle(img, (x0, y0), (x0 + ww, y0 + hh), (48, 52, 58), 3, cv2.LINE_AA)
        cv2.line(img, (x0 + ww // 2, y0), (x0 + ww // 2, y0 + hh), (48, 52, 58), 2, cv2.LINE_AA)
        cv2.line(img, (x0, y0 + hh // 2), (x0 + ww, y0 + hh // 2), (48, 52, 58), 2, cv2.LINE_AA)
    # lamp and skirting line
    cv2.circle(img, (int(w * 0.78), int(h * 0.30)), int(h * 0.09), (70, 78, 206), -1, cv2.LINE_AA)
    cv2.line(img, (0, floor_y), (w, floor_y - int(h * 0.03)), (40, 44, 50), 3, cv2.LINE_AA)
    # plaque with lettering
    px0, py0, px1, py1 = int(w * 0.62), int(h * 0.50), int(w * 0.92), int(h * 0.62)
    cv2.rectangle(img, (px0, py0), (px1, py1), (36, 36, 42), -1, cv2.LINE_AA)
    cv2.putText(
        img,
        f"PLATE {400 + seed:04d}",
        (px0 + 10, py1 - 14),
        cv2.FONT_HERSHEY_SIMPLEX,
        w / 900,
        (236, 236, 236),
        2,
        cv2.LINE_AA,
    )
    # whole-frame fine grain, ~4 levels
    grain = (noise_field(w, h, seed + 23, octaves=3, base=60) - 0.5) * 8.0
    return np.clip(img.astype(np.float32) + grain[..., None], 0, 255).astype(np.uint8)


def crunch(img: np.ndarray) -> np.ndarray:
    """Unsharp mask x3 plus sigma-0.8 noise: the 'too sharp' failure of an over-detailed AI still."""
    out = img.astype(np.float32)
    for _ in range(3):
        out = out + 1.5 * (out - cv2.GaussianBlur(out, (0, 0), 1.2))
    out += np.random.default_rng(5).normal(0.0, 0.8, out.shape).astype(np.float32)
    return np.clip(out, 0, 255).astype(np.uint8)


def variant(img: np.ndarray, kind: str) -> np.ndarray:
    h, w = img.shape[:2]
    if kind == "identity":
        return img.copy()
    if kind == "shift3":
        return np.roll(img, 3, axis=1)
    if kind == "tint":  # BGR offset: roughly a few dE00 warm
        return np.clip(img.astype(np.int16) + np.array([-6, 0, 12], np.int16), 0, 255).astype(np.uint8)
    if kind == "blur2":
        return cv2.GaussianBlur(img, (0, 0), 2.0)
    if kind == "window_added":
        out = img.copy()
        x0, y0 = int(w * 0.50), int(h * 0.14)
        cv2.rectangle(
            out, (x0, y0), (x0 + int(w * 0.10), y0 + int(h * 0.30)), (236, 232, 222), -1, cv2.LINE_AA
        )
        cv2.rectangle(out, (x0, y0), (x0 + int(w * 0.10), y0 + int(h * 0.30)), (48, 52, 58), 3, cv2.LINE_AA)
        return out
    if kind == "crunch":
        return crunch(img)
    raise KeyError(kind)


def edit_mask(shape: tuple[int, int]) -> np.ndarray:
    h, w = shape
    return rect((h, w), (int(w * 0.10), int(h * 0.10), int(w * 0.40), int(h * 0.55)))


# --- metric sections ----------------------------------------------------------------------------------------


def _flat_compare(res: dict) -> dict:
    out = {"ssim_gray": res["ssim_gray"], "ssim_gray_blur8": res["ssim_gray_blur8"]}
    for r in (0, 2, 4):
        out[f"long_recall_r{r}"] = res[f"canny_long_r{r}"]["recall"]
        out[f"long_precision_r{r}"] = res[f"canny_long_r{r}"]["precision"]
    out["best_shift_dx"] = res["best_shift_long"]["dx"]
    out["best_shift_dy"] = res["best_shift_long"]["dy"]
    out["best_shift_recall_r2"] = res["best_shift_long"]["recall_r2"]
    out["de00_blur_median"] = res["de00_blur"]["median"]
    out["de00_blur_p90"] = res["de00_blur"]["p90"]
    if "de00_flat" in res:
        out["de00_flat_median"] = res["de00_flat"]["median"]
        out["flat_frac_of_ref"] = res["flat_frac_of_ref"]
    return out


COMPARE_CASES = {
    "identity": ("identity", 0, None),
    "shift3": ("shift3", 0, None),
    "shift3_scaled_long_edges": ("shift3", 0, STILL_W),
    "tint": ("tint", 0, None),
    "blur2": ("blur2", 0, None),
    "window_added": ("window_added", 0, None),
}


def compare_case(name: str) -> dict:
    kind, seed, lew = COMPARE_CASES[name]
    ref = scene(STILL_W, seed)
    return _flat_compare(compare(ref, variant(ref, kind), long_edge_width=lew))


def cross_case() -> dict:
    return _flat_compare(compare(scene(STILL_W, 0), scene(STILL_W, 1)))


TEXTURE_CASES = {"soft": 0, "crunch": 1, "other_scene": 2}


def texture_case(name: str) -> dict:
    img = scene(TEXTURE_W, 0 if name != "other_scene" else 3)
    if name == "crunch":
        img = crunch(img)
    s = texture.stats(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), width=TEXTURE_W)
    return {k: v for k, v in s.items() if k != "eval_width"}


def maskdiff_case() -> dict:
    ref = scene(STILL_W, 0)
    mask = (edit_mask(ref.shape[:2]) * 255).astype(np.uint8)
    edited = ref.copy()
    m = mask > 127
    edited[m] = 255 - edited[m]
    md = maskdiff(ref, edited, mask)
    return {
        "mask_frac": md["mask_frac"],
        "outside_frac_identical": md["outside"]["frac_identical"],
        "outside_de00_p99": md["outside"]["de00_p99"],
        "inside_frac_identical": md["inside"]["frac_identical"],
        "inside_de00_mean": md["inside"]["de00_mean"],
    }


def composite_case() -> dict:
    plate = cv2.cvtColor(scene(STILL_W, 0), cv2.COLOR_BGR2RGB).astype(np.uint16) * 257
    ai = np.clip(plate.astype(np.int32) * 1.06 + 900, 0, 65535).astype(np.uint16)  # brighter everywhere
    m = edit_mask(plate.shape[:2])
    ai[m] = 65535 - ai[m]  # the 'edit'
    out = composite_masked(plate, ai, m, feather_px=8, tone_match_ring=True)
    matched = match_to_ring(plate, ai, m)
    band = ring(m, 0, 24)
    ys, xs = np.nonzero(m)
    deep = np.zeros_like(m)
    deep[ys.min() + 20 : ys.max() - 20, xs.min() + 20 : xs.max() - 20] = True

    def ring_de(layer: np.ndarray) -> float:
        d = ciede2000(srgb_to_lab(layer[band] / 257.0), srgb_to_lab(plate[band] / 257.0))
        return float(d.mean())

    return {
        "outside_max_abs_diff": float(np.abs(out[~m].astype(np.int32) - plate[~m]).max()),
        "outside_identical": bool(np.array_equal(out[~m], plate[~m])),
        "inside_mean_abs_diff_vs_plate_8bit": float(
            np.abs(out[m].astype(np.float64) - plate[m]).mean() / 257.0
        ),
        "deep_mean_abs_tone_shift_8bit": float(
            np.abs(matched[deep].astype(np.float64) - ai[deep]).mean() / 257.0
        ),
        "de00_ring_raw_mean": ring_de(ai.astype(np.float64)),
        "de00_ring_matched_mean": ring_de(matched.astype(np.float64)),
    }


def labmatch_case() -> dict:
    src = cv2.cvtColor(scene(STILL_W, 0), cv2.COLOR_BGR2RGB)
    ref = cv2.cvtColor(variant(scene(STILL_W, 1), "tint"), cv2.COLOR_BGR2RGB)
    out = {}
    for strength in (0.5, 1.0):
        res = reinhard(src, ref, strength)
        d = ciede2000(srgb_to_lab(res).mean((0, 1))[None], srgb_to_lab(ref).mean((0, 1))[None])[0]
        out[f"de00_mean_to_ref_s{strength}"] = float(d)
    flat = reinhard(np.full((20, 20, 3), 60, np.uint8), np.full((20, 20, 3), 180, np.uint8))
    out["flat_grey_result_mean"] = float(flat.mean())
    return out


def _still43(seed: int, w: int = 480) -> np.ndarray:
    return cv2.cvtColor(scene(w, seed, aspect=4 / 3), cv2.COLOR_BGR2RGB)


def _fit_43_to_169(still: np.ndarray, w: int, h: int, gain: float) -> np.ndarray:
    """What an aspect-correct engine returns for a 4:3 still in a 16:9 clip: centre crop, resize, small offset."""
    sh, sw = still.shape[:2]
    ch = int(round(sw * h / w))
    y0 = (sh - ch) // 2
    crop = still[y0 : y0 + ch]
    out = cv2.resize(crop, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32) + gain
    return np.clip(out, 0, 255).astype(np.uint8)


def landing_case(tmp: Path) -> dict:
    """A 24-frame 320x180 clip whose first / last frames are aspect-correct renderings of two 4:3 stills
    (+4 and +6 levels), written losslessly through ffmpeg rawvideo; landing() must read them back at ~4 / ~6."""
    w, h = 320, 180
    start, end = _still43(0), _still43(1)
    f0, f1 = _fit_43_to_169(start, w, h, 4.0), _fit_43_to_169(end, w, h, 6.0)
    frames = (
        [f0]
        + [
            np.clip(f0.astype(np.float32) * (1 - t) + f1.astype(np.float32) * t, 0, 255).astype(np.uint8)
            for t in np.linspace(0, 1, 22)[1:-1]
        ]
        + [f1]
    )
    clip = pio.write_clip(tmp / "landing.mp4", frames, fps=24)
    res = landing(clip, start, end)
    return {"first": res["first"], "last": res["last"], "last_vs_start": res["last_vs_start"]}


def flicker_case(tmp: Path) -> dict:
    w, h = 160, 90
    base = _fit_43_to_169(_still43(0), w, h, 0.0)
    steady = pio.write_clip(tmp / "steady.mp4", [base] * 24, fps=24)
    pump = [np.clip(base.astype(np.int16) + (8 if i % 2 else 0), 0, 255).astype(np.uint8) for i in range(24)]
    pumping = pio.write_clip(tmp / "pumping.mp4", pump, fps=24)
    a, b = flicker(steady), flicker(pumping)
    dur = duration_check(steady, 1.0)
    return {
        "steady_mean_abs_luma_delta": a["mean_abs_luma_delta"],
        "pumping_mean_abs_luma_delta": b["mean_abs_luma_delta"],
        "pumping_p95": b["p95"],
        "duration_s": dur["duration_s"],
        "duration_ok": dur["ok"],
        "duration_short_ok": duration_check(steady, 2.0)["ok"],
    }


def camera_lock_case(tmp: Path) -> dict:
    """Textured plate that holds for the first half and then translates 5 px: regions must report dx = 5."""
    w, h = 320, 180
    canvas = cv2.cvtColor(scene(w + 8, 4, aspect=(w + 8) / h), cv2.COLOR_BGR2RGB)
    frames = []
    for i in range(24):
        s = 0 if i < 12 else 5
        frames.append(np.ascontiguousarray(canvas[:, 8 - s : 8 - s + w]))
    clip = write_test_clip(tmp / "lock.mp4", frames, fps=24)
    regions = [(10, 10, 120, 70), (190, 10, 120, 70), (100, 20, 120, 100)]
    res = camera_lock_phasecorr(clip, regions, samples=4)
    out = {"verdict": res["verdict"]}
    for i, r in enumerate(res["regions"]):
        out[f"region{i}_dx"] = r["dx"]
        out[f"region{i}_dy"] = r["dy"]
    return out


def sections(tmp: Path, ffmpeg_ok: bool = True) -> dict:
    """Every golden section as {section: {case: flat metrics}}."""
    s: dict = {
        "compare": {**{n: compare_case(n) for n in COMPARE_CASES}, "cross_scene": cross_case()},
        "texture": {n: texture_case(n) for n in TEXTURE_CASES},
        "maskdiff": {"edit_inside_mask": maskdiff_case()},
        "composite_masked": {"brighter_ai_with_edit": composite_case()},
        "labmatch": {"scene_to_tinted_scene": labmatch_case()},
        "camera_lock": {"pan_after_half": camera_lock_case(tmp)},
    }
    if ffmpeg_ok:
        s["landing"] = {"aspect_correct_43_in_169": landing_case(tmp)}
        s["flicker"] = {"steady_vs_pumping": flicker_case(tmp)}
    return s


# --- tolerance rules ----------------------------------------------------------------------------------------


# Mirrors tolerance() below; written into expected_procedural.json as "_tolerances" so the frozen file is
# self-describing, and test_golden_procedural asserts the two stay equal.
TOLERANCE_DOC = {
    "ssim*": "abs 0.005",
    "*recall* / *precision* / *frac*": "abs 0.01",
    "*de00* / *tone_shift*": "abs 0.05",
    "*mean_abs*": "abs 0.1",
    "lapvar / fine_ratio / luma_mean / luma_std / sat_mean": "rel 0.02",
    "first / last / last_vs_start / *luma_delta* / *p95*": "abs 0.1",
    "int / bool / str leaves (shifts, verdicts, flags)": "exact",
    "anything else": "rel 0.001",
}


def tolerance(key: str, value) -> tuple[float, float]:
    """(absolute, relative) tolerance for one leaf. Exact (0, 0) for ints, bools, strings and shifts."""
    if isinstance(value, (bool, str)) or isinstance(value, int):
        return 0.0, 0.0
    k = key.lower()
    if "ssim" in k:
        return 0.005, 0.0
    if "recall" in k or "precision" in k or "frac" in k:
        return 0.01, 0.0
    if "de00" in k or "tone_shift" in k:
        return 0.05, 0.0
    if "mean_abs" in k:
        return 0.1, 0.0
    if k in {"lapvar", "fine_ratio", "luma_mean", "luma_std", "sat_mean"}:
        return 0.0, 0.02
    if k in {"first", "last", "last_vs_start"} or "luma_delta" in k or "p95" in k:
        return 0.1, 0.0
    return 0.0, 1e-3


def compare_to_expected(actual: dict, expected: dict, where: str = "") -> list[str]:
    """Diff two flat-or-nested metric dicts; returns human-readable mismatches (empty = match)."""
    bad = []
    if set(actual) != set(expected):
        bad.append(f"{where}: keys differ: {sorted(set(actual) ^ set(expected))}")
    for k, exp in expected.items():
        if k not in actual:
            continue
        got = actual[k]
        path = f"{where}.{k}" if where else k
        if isinstance(exp, dict):
            bad += compare_to_expected(got, exp, path)
        elif isinstance(exp, (bool, str, int)) and not isinstance(exp, float):
            if got != exp:
                bad.append(f"{path}: {got!r} != {exp!r}")
        else:
            a, r = tolerance(k, exp)
            if abs(float(got) - float(exp)) > a + r * abs(float(exp)):
                bad.append(f"{path}: {got} vs frozen {exp} (tol abs {a}, rel {r})")
    return bad


def write_expected(data: dict, path: Path = EXPECTED) -> None:
    text = json.dumps({"_tolerances": TOLERANCE_DOC, **data}, indent=1, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read_expected(path: Path = EXPECTED) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
