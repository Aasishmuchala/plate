"""Blender-rendered gate goldens: committed pairs derived from the fixture packets, plus their frozen metrics.

Unlike the client-derived goldens (stills/, texture/, gitignored) these come from Sthyra-owned Blender renders
(fixtures/packets/room_a, room_b, dolly_12f), so the PNGs and the clip are committed and the golden suite runs
on a fresh clone with no client data.

Derivation (everything under gates/tests/golden/blender/):
  base        display.tif -> 8-bit -> cv2.fastNlMeansDenoisingColored(h=5): the fixture renders carry Cycles
              sample noise that makes a 640 px PNG 400-440 KB; a production render is denoised, and the cap is
              300 KB per file, so every golden starts from the denoised 8-bit base. room_a feeds identity,
              shift3, blur2 and material_b6; room_b feeds window_added, texture_crunch and pad_2391.
  identity    (base, copy)
  shift3      (base, np.roll(base, 3, axis=1))
  blur2       (base, GaussianBlur sigma 2)
  window_added  room_b: an anti-aliased window (4x supersampled) placed inside the largest rectangle of the
              ARCH_wall_N Cryptomatte object mask; window_added_mask.png is that wall mask
  material_b6 room_a: +6 b* in CIE Lab inside the ARCH_panel_7016 Cryptomatte material mask (a near-neutral dark
              panel, where +6 b* reads as the largest dE00 a 6-unit shift can give); material_b6_mask.png is it
  texture_crunch  room_b base. Only the soft side is committed: unsharp mask x3 plus sigma 0.8 noise (crunch())
              is a deterministic function of it and a lossless PNG of the result is 500-600 KB, past the
              300 KB cap. texture.stats(width=1024) resizes the 640 px stills itself, so the stats are the
              1024-wide numbers of the plan.
  pad_2391    room_b: edge-extend (BORDER_REPLICATE) from 16:9 to 2.39:1 (110 px each side), then un-pad
  dolly.mp4   dolly_12f clay PNGs, 12 fps in, 24 fps out (1 s), libx264 CRF 18 yuv420p; dolly_first.png and
              dolly_last.png are clay.0000 / clay.0011 as 8-bit PNG, the landing stills

Frozen metrics live in blender/expected.json with the tolerance rules of golden/procedural.py. Rebuild with

    .venv/bin/python gates/tests/golden/make_goldens_blender.py            # PNGs + clip + expected.json
    .venv/bin/python gates/tests/golden/make_goldens_blender.py --expected-only   # re-freeze from the files

or `pytest gates/tests/test_golden_blender.py --update-goldens`. CI never passes the flag.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for _p in (HERE, REPO / "gates", REPO / "pixels"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import procedural as P  # noqa: E402  (tolerance rules shared with the procedural goldens)

from plate_gates import io as pio  # noqa: E402
from plate_gates import texture  # noqa: E402
from plate_gates.color import ciede2000, lab_to_srgb, srgb_to_lab  # noqa: E402
from plate_gates.compare import compare, maskdiff, read_image, write_png  # noqa: E402
from plate_gates.composite import composite_masked, match_to_ring  # noqa: E402
from plate_gates.masks import erode, ring  # noqa: E402
from plate_gates.video import camera_lock_phasecorr, duration_check, flicker, landing  # noqa: E402

PACKETS = REPO / "fixtures" / "packets"
OUT = HERE / "blender"
EXPECTED = OUT / "expected.json"

DENOISE_H = 5  # NLM strength: smallest that keeps every committed PNG under the 300 KB cap
PNG_CAP_KB = 300
TOTAL_CAP_KB = 3000
TEX_WIDTH = 1024  # texture.stats(width=...)
PAD_ASPECT = 2.39
B_SHIFT = 6.0  # +b* of the material_b6 case

WALL_ID = "o:57b3d007"  # ARCH_wall_N, the same Cryptomatte id in every fixture packet
PANEL_7016_ID = "m:735a8655"  # ARCH_panel_7016 material
CLAY_FRAMES = 12  # dolly_12f
DOLLY_FPS_OUT = 24

# every file committed under blender/ (the expected.json is added below); test_golden_blender checks them in git
PNG_FILES = [
    "identity_a.png",
    "identity_b.png",
    "shift3_a.png",
    "shift3_b.png",
    "blur2_a.png",
    "blur2_b.png",
    "window_added_a.png",
    "window_added_b.png",
    "window_added_mask.png",
    "material_b6_a.png",
    "material_b6_b.png",
    "material_b6_mask.png",
    "texture_crunch_a.png",
    "pad_2391_a.png",
    "pad_2391_b.png",
    "dolly_first.png",
    "dolly_last.png",
]
VIDEO_FILES = ["dolly.mp4"]
COMMITTED_FILES = [*PNG_FILES, *VIDEO_FILES, "expected.json"]


# --- image derivation ---------------------------------------------------------------------------------------


def base_still(room: str) -> np.ndarray:
    """The denoised 8-bit BGR display master of a fixture packet (640 x 360)."""
    rgb8 = pio.to_uint8(pio.load_rgb(PACKETS / room / "display.tif"))
    bgr = cv2.cvtColor(rgb8, cv2.COLOR_RGB2BGR)
    return cv2.fastNlMeansDenoisingColored(bgr, None, DENOISE_H, DENOISE_H, 5, 15)


def crypto_mask(room: str, entry_id: str) -> np.ndarray:
    """bool (H, W) Cryptomatte mask of an `o:` (object) or `m:` (material) id of a fixture packet."""
    from plate_pixels import exr  # lazy: only the rebuild needs OpenEXR, the tests read the stored masks

    d = PACKETS / room
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    which = "object" if entry_id.startswith("o:") else "material"
    ranks, cov = exr.read_cryptomatte(d / "beauty.exr", manifest, which)
    return exr.id_mask(ranks, cov, entry_id)


def largest_rect(mask: np.ndarray) -> tuple[int, int, int, int]:
    """Largest axis-aligned all-True rectangle of a bool mask as (x0, y0, x1, y1), x1 / y1 exclusive."""
    h, w = mask.shape
    heights = np.zeros(w, np.int64)
    best, box = 0, (0, 0, 0, 0)
    for y in range(h):
        heights = np.where(mask[y], heights + 1, 0)
        stack: list[tuple[int, int]] = []  # (start x, height)
        for x in range(w + 1):
            cur = int(heights[x]) if x < w else 0
            start = x
            while stack and stack[-1][1] >= cur:
                sx, sh = stack.pop()
                if sh * (x - sx) > best:
                    best, box = sh * (x - sx), (sx, y - sh + 1, x, y + 1)
                start = sx
            stack.append((start, cur))
    return box


def window_overlay(shape: tuple[int, int], box: tuple[int, int, int, int], ss: int = 4):
    """(alpha, colour) of an anti-aliased window with a dark frame and a cross mullion, drawn at ss x
    supersampling and area-reduced: alpha float32 (H, W) in 0..1, colour float32 (H, W, 3) BGR."""
    h, w = shape
    x0, y0, x1, y1 = (int(v) * ss for v in box)
    glass = np.zeros((h * ss, w * ss, 3), np.float32)
    alpha = np.zeros((h * ss, w * ss), np.float32)
    frame = int(3 * ss)
    cv2.rectangle(alpha, (x0, y0), (x1, y1), 1.0, -1)
    cv2.rectangle(glass, (x0, y0), (x1, y1), (48.0, 52.0, 58.0), -1)  # frame colour fills the whole box
    cv2.rectangle(glass, (x0 + frame, y0 + frame), (x1 - frame, y1 - frame), (236.0, 232.0, 222.0), -1)
    mx, my = (x0 + x1) // 2, (y0 + y1) // 2
    cv2.line(glass, (mx, y0), (mx, y1), (48.0, 52.0, 58.0), int(2 * ss))
    cv2.line(glass, (x0, my), (x1, my), (48.0, 52.0, 58.0), int(2 * ss))
    # area reduction: colour premultiplied by alpha so the AA edge blends with the wall, not with black
    a = cv2.resize(alpha, (w, h), interpolation=cv2.INTER_AREA)
    pre = cv2.resize(glass * alpha[..., None], (w, h), interpolation=cv2.INTER_AREA)
    col = pre / np.maximum(a[..., None], 1e-6)
    return a, col


def add_window(img: np.ndarray, wall: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Paint the window inside the largest rectangle of the wall mask (eroded 4 px, box inset 10 px)."""
    inner = cv2.erode(wall.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    x0, y0, x1, y1 = largest_rect(inner)
    ins = 10
    box = (x0 + ins, y0 + ins, x1 - ins, y1 - ins)
    if box[2] - box[0] < 24 or box[3] - box[1] < 24:
        raise RuntimeError(f"wall rectangle too small for a window: {(x0, y0, x1, y1)}")
    a, col = window_overlay(img.shape[:2], box)
    out = img.astype(np.float32) * (1.0 - a[..., None]) + col * a[..., None]
    return np.clip(np.rint(out), 0, 255).astype(np.uint8), box


def shift_b_star(bgr: np.ndarray, mask: np.ndarray, db: float = B_SHIFT) -> np.ndarray:
    """+db b* in CIE Lab on the masked pixels only; everything outside the mask stays bit-identical."""
    out = bgr.copy()
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    lab = srgb_to_lab(rgb[mask])
    lab[:, 2] += db
    shifted = np.clip(np.rint(lab_to_srgb(lab)), 0, 255).astype(np.uint8)
    out[mask] = shifted[:, ::-1]
    return out


def crunch(bgr: np.ndarray) -> np.ndarray:
    """Unsharp mask x3 (amount 1.5, sigma 1.2) plus sigma 0.8 noise: the over-sharpened AI still."""
    out = bgr.astype(np.float32)
    for _ in range(3):
        out = out + 1.5 * (out - cv2.GaussianBlur(out, (0, 0), 1.2))
    out += np.random.default_rng(5).normal(0.0, 0.8, out.shape).astype(np.float32)
    return np.clip(out, 0, 255).astype(np.uint8)


def pad_to_aspect(bgr: np.ndarray, aspect: float = PAD_ASPECT) -> tuple[np.ndarray, int]:
    """Edge-extend (BORDER_REPLICATE) left and right to the target aspect; returns (padded, pad per side)."""
    h, w = bgr.shape[:2]
    side = max(int(round((h * aspect - w) / 2)), 0)
    return cv2.copyMakeBorder(bgr, 0, 0, side, side, cv2.BORDER_REPLICATE), side


def unpad(padded: np.ndarray, side: int) -> np.ndarray:
    return np.ascontiguousarray(padded[:, side : padded.shape[1] - side])


def variant(kind: str, a: np.ndarray) -> np.ndarray:
    if kind == "identity":
        return a.copy()
    if kind == "shift3":
        return np.roll(a, 3, axis=1)
    if kind == "blur2":
        return cv2.GaussianBlur(a, (0, 0), 2.0)
    raise KeyError(kind)


def mask_png(mask: np.ndarray) -> np.ndarray:
    """bool mask -> 0/255 uint8 (the stored mask format and what compare.maskdiff takes)."""
    return mask.astype(np.uint8) * 255


# --- file IO ------------------------------------------------------------------------------------------------


def read_png(name: str, out: Path = OUT) -> np.ndarray:
    return read_image(out / name, cv2.IMREAD_COLOR)


def read_mask(name: str, out: Path = OUT) -> np.ndarray:
    return read_image(out / name, cv2.IMREAD_GRAYSCALE) > 127


def encode_dolly(out: Path) -> Path:
    """1 s 24 fps libx264 CRF 18 clip from the 12 clay PNGs (12 fps in, frames doubled by the fps filter)."""
    tool = pio.ffmpeg()
    if tool is None:
        raise FileNotFoundError("ffmpeg not found: install it or set PLATE_FFMPEG")
    clip = out / "dolly.mp4"
    cmd = [str(tool), "-v", "error", "-nostdin", "-y", "-framerate", "12"]
    cmd += ["-i", str(PACKETS / "dolly_12f" / "motion" / "clay.%04d.png")]
    cmd += ["-vf", f"fps={DOLLY_FPS_OUT},format=yuv420p", "-c:v", "libx264", "-crf", "18", "-preset", "slow"]
    cmd += ["-an", "-map_metadata", "-1", "-fflags", "+bitexact", "-movflags", "+faststart", str(clip)]
    res = subprocess.run(cmd, capture_output=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr.decode('utf-8', 'replace').strip()}")
    return clip


def build(out: Path = OUT) -> None:
    """Derive every PNG and the clip from the fixture packets into `out`."""
    out.mkdir(parents=True, exist_ok=True)

    def put(name: str, img: np.ndarray) -> None:
        write_png(out / name, img, compression=9)

    room_a, room_b = base_still("room_a"), base_still("room_b")
    for kind in ("identity", "shift3", "blur2"):
        put(f"{kind}_a.png", room_a)
        put(f"{kind}_b.png", variant(kind, room_a))

    wall = crypto_mask("room_b", WALL_ID)
    windowed, _ = add_window(room_b, wall)
    put("window_added_a.png", room_b)
    put("window_added_b.png", windowed)
    put("window_added_mask.png", mask_png(wall))

    panel = crypto_mask("room_a", PANEL_7016_ID)
    put("material_b6_a.png", room_a)
    put("material_b6_b.png", shift_b_star(room_a, panel))
    put("material_b6_mask.png", mask_png(panel))

    put("texture_crunch_a.png", room_b)

    padded, side = pad_to_aspect(room_b)
    put("pad_2391_a.png", room_b)
    put("pad_2391_b.png", unpad(padded, side))

    clay = PACKETS / "dolly_12f" / "motion"
    for name, idx in (("dolly_first.png", 0), ("dolly_last.png", CLAY_FRAMES - 1)):
        rgb = pio.to_uint8(pio.load_rgb(clay / f"clay.{idx:04d}.png"))
        put(name, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    encode_dolly(out)


# --- metrics ------------------------------------------------------------------------------------------------


def flat_compare(res: dict) -> dict:
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


def flat_maskdiff(md: dict) -> dict:
    return {
        "mask_frac": md["mask_frac"],
        "outside_frac_identical": md["outside"]["frac_identical"],
        "outside_de00_p99": md["outside"]["de00_p99"],
        "inside_frac_identical": md["inside"]["frac_identical"],
        "inside_de00_mean": md["inside"]["de00_mean"],
    }


def inside_mask_de(a_bgr: np.ndarray, b_bgr: np.ndarray, mask: np.ndarray) -> dict:
    """dE00 and Lab distance (dE76) between two images over the masked pixels."""
    la = srgb_to_lab(cv2.cvtColor(a_bgr, cv2.COLOR_BGR2RGB)[mask])
    lb = srgb_to_lab(cv2.cvtColor(b_bgr, cv2.COLOR_BGR2RGB)[mask])
    de00 = ciede2000(la, lb)
    de76 = np.linalg.norm(la - lb, axis=1)
    return {
        "mask_px": int(mask.sum()),
        "de00_inside_median": float(np.median(de00)),
        "de00_inside_p90": float(np.percentile(de00, 90)),
        "de76_inside_median": float(np.median(de76)),
        "db_star_median": float(np.median(lb[:, 2] - la[:, 2])),
    }


def composite_case(plate_bgr: np.ndarray, edited_bgr: np.ndarray, mask: np.ndarray) -> dict:
    """The generated layer is a globally brighter copy of the edited image; composite_masked must keep every
    pixel outside the mask as the plate and tone-match the inside to the surrounding ring."""
    plate = cv2.cvtColor(plate_bgr, cv2.COLOR_BGR2RGB).astype(np.uint16) * 257
    edit = cv2.cvtColor(edited_bgr, cv2.COLOR_BGR2RGB).astype(np.uint16) * 257
    ai = np.clip(edit.astype(np.int32) * 1.06 + 900, 0, 65535).astype(np.uint16)
    out = composite_masked(plate, ai, mask, feather_px=8, tone_match_ring=True)
    matched = match_to_ring(plate, ai, mask)
    band = ring(mask, 0, 24)
    deep = erode(mask, 8)

    def ring_de(layer: np.ndarray) -> float:
        d = ciede2000(srgb_to_lab(layer[band] / 257.0), srgb_to_lab(plate[band] / 257.0))
        return float(d.mean())

    return {
        "outside_max_abs_diff": float(np.abs(out[~mask].astype(np.int32) - plate[~mask]).max()),
        "outside_identical": bool(np.array_equal(out[~mask], plate[~mask])),
        "inside_mean_abs_diff_vs_plate_8bit": float(
            np.abs(out[mask].astype(np.float64) - plate[mask]).mean() / 257.0
        ),
        "deep_mean_abs_tone_shift_8bit": float(
            np.abs(matched[deep].astype(np.float64) - ai[deep]).mean() / 257.0
        ),
        "de00_ring_raw_mean": ring_de(ai.astype(np.float64)),
        "de00_ring_matched_mean": ring_de(matched.astype(np.float64)),
    }


def pad_case(a_bgr: np.ndarray, b_bgr: np.ndarray) -> dict:
    padded, side = pad_to_aspect(a_bgr)
    left, right = padded[:, :side], padded[:, padded.shape[1] - side :]
    return {
        "pad_aspect": padded.shape[1] / padded.shape[0],
        "pad_each_side": side,
        "padded_width": int(padded.shape[1]),
        "edge_replicated": bool(
            np.array_equal(left, np.repeat(a_bgr[:, :1], side, axis=1))
            and np.array_equal(right, np.repeat(a_bgr[:, -1:], side, axis=1))
        ),
        "unpad_identical": bool(np.array_equal(unpad(padded, side), a_bgr)),
        "stored_roundtrip_identical": bool(np.array_equal(a_bgr, b_bgr)),
    }


# regions (x, y, w, h) of the 320 x 180 dolly frames used by the phase-correlation lock
DOLLY_REGIONS = [(10, 10, 120, 70), (190, 10, 120, 70), (100, 90, 120, 80)]


def lock_sections(out: Path) -> dict:
    """Phase-correlation camera lock on the dolly clip (OpenCV decode: no ffmpeg binary needed)."""
    lock = camera_lock_phasecorr(out / "dolly.mp4", DOLLY_REGIONS, samples=4)
    return {
        "camera_lock": {
            "dolly_moves": {
                "verdict": lock["verdict"],
                "n_moved": int(sum(r["moved"] for r in lock["regions"])),
            }
        }
    }


def video_sections(out: Path) -> dict:
    """landing / flicker / duration on the dolly clip (ffprobe + ffmpeg pipes)."""
    clip = out / "dolly.mp4"
    first, last = read_png("dolly_first.png", out), read_png("dolly_last.png", out)
    lan = landing(clip, cv2.cvtColor(first, cv2.COLOR_BGR2RGB), cv2.cvtColor(last, cv2.COLOR_BGR2RGB))
    fl = flicker(clip)
    dur = duration_check(clip, 1.0)
    return {
        "landing": {
            "dolly": {"first": lan["first"], "last": lan["last"], "last_vs_start": lan["last_vs_start"]}
        },
        "flicker": {
            "dolly": {
                "mean_abs_luma_delta": fl["mean_abs_luma_delta"],
                "p95": fl["p95"],
                "frames": fl["frames"],
            }
        },
        "duration": {
            "dolly_1s": {
                "duration_s": dur["duration_s"],
                "nb_frames": dur["nb_frames"],
                "ok_for_1s": dur["ok"],
                "ok_for_2s": duration_check(clip, 2.0)["ok"],
            }
        },
    }


def sections(out: Path = OUT, with_video: bool = True) -> dict:
    """Every golden section as {section: {case: flat metrics}} computed from the files in `out`.
    with_video=False leaves out landing / flicker / duration, the sections that need the ffmpeg binary."""
    ident_a, ident_b = read_png("identity_a.png", out), read_png("identity_b.png", out)
    shift_a, shift_b = read_png("shift3_a.png", out), read_png("shift3_b.png", out)
    blur_a, blur_b = read_png("blur2_a.png", out), read_png("blur2_b.png", out)
    win_a, win_b = read_png("window_added_a.png", out), read_png("window_added_b.png", out)
    wall = read_mask("window_added_mask.png", out)
    mat_a, mat_b = read_png("material_b6_a.png", out), read_png("material_b6_b.png", out)
    panel = read_mask("material_b6_mask.png", out)
    tex_a = read_png("texture_crunch_a.png", out)
    pad_a, pad_b = read_png("pad_2391_a.png", out), read_png("pad_2391_b.png", out)

    def rgb(img: np.ndarray) -> np.ndarray:
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    tex = {}
    for name, img in (("soft", tex_a), ("crunch", crunch(tex_a))):
        tex[name] = texture.stats(rgb(img), width=TEX_WIDTH)
    s: dict = {
        "compare": {
            "identity": flat_compare(compare(ident_a, ident_b)),
            "shift3": flat_compare(compare(shift_a, shift_b)),
            "blur2": flat_compare(compare(blur_a, blur_b)),
            "window_added": flat_compare(compare(win_a, win_b)),
            "material_b6": flat_compare(compare(mat_a, mat_b)),
            "pad_2391": flat_compare(compare(pad_a, pad_b)),
            "cross_view": flat_compare(compare(ident_a, pad_a)),  # room_a vs room_b: same scene, two cameras
        },
        "maskdiff": {
            "window_added_in_wall": flat_maskdiff(maskdiff(win_a, win_b, mask_png(wall))),
            "material_b6_in_panel": flat_maskdiff(maskdiff(mat_a, mat_b, mask_png(panel))),
        },
        "material": {"b6_in_panel_7016": inside_mask_de(mat_a, mat_b, panel)},
        "composite_masked": {"material_b6_brighter_ai": composite_case(mat_a, mat_b, panel)},
        "texture": tex,
        "pad": {"pad_2391": pad_case(pad_a, pad_b)},
        **lock_sections(out),
    }
    if with_video:
        s.update(video_sections(out))
    return s


def write_expected(data: dict, path: Path = EXPECTED) -> None:
    text = json.dumps({"_tolerances": P.TOLERANCE_DOC, **data}, indent=1, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read_expected(path: Path = EXPECTED) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sizes_kb(out: Path = OUT) -> dict[str, float]:
    return {p.name: p.stat().st_size / 1024 for p in sorted(out.iterdir()) if p.is_file()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--expected-only", action="store_true", help="keep the files, re-freeze expected.json")
    ap.add_argument(
        "--out", type=Path, default=OUT, help="output folder (default: gates/tests/golden/blender)"
    )
    args = ap.parse_args(argv)
    if not args.expected_only:
        build(args.out)
    data = sections(args.out)
    write_expected(data, args.out / "expected.json")
    for name, kb in sizes_kb(args.out).items():
        print(f"{name:28s} {kb:8.1f} KB")
    print(f"total {sum(sizes_kb(args.out).values()):.0f} KB (cap {TOTAL_CAP_KB}); wrote expected.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
