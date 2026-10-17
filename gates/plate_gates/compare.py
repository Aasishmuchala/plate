"""Reference-vs-test image comparison: structure, colour drift, ID masks, mask-locked diffs, multi-view runs.

Ported from the driver section of ~/Desktop/test/vizmaker-dossier/tools/fidelity_bench.py (load, compare,
idmask, erode_mask, maskdiff, views). The metric keys and numerics are unchanged. Differences from the
script: errors raise exceptions instead of SystemExit, image I/O goes through imdecode/imencode so
non-ASCII Windows paths work, and views() resolves manifest-relative paths against a base directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from .color import ciede2000, hex_to_rgb8, srgb_to_lab
from .structure import best_shift, canny, long_edges, ssim_gray, tol_metrics


def read_image(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray:
    """cv2.imread replacement that survives non-ASCII Windows paths. Raises on missing/undecodable files."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"cannot read {p}")
    im = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), flags)
    if im is None:
        raise ValueError(f"cannot decode image {p}")
    return im


def write_png(path: str | Path, img: np.ndarray, compression: int = 3) -> None:
    """cv2.imwrite replacement for PNG that survives non-ASCII Windows paths."""
    ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, int(compression)])
    if not ok:
        raise ValueError(f"cannot encode PNG for {path}")
    Path(path).write_bytes(buf.tobytes())


def load(path: str | Path, width: int | None) -> np.ndarray:
    """Load a BGR image, resized to `width` (area when shrinking, cubic when growing) if given."""
    im = read_image(path, cv2.IMREAD_COLOR)
    h, w = im.shape[:2]
    if width and w != width:
        im = cv2.resize(
            im,
            (width, int(round(h * width / w))),
            interpolation=cv2.INTER_AREA if width < w else cv2.INTER_CUBIC,
        )
    return im


def idmask(idpass: str | Path | np.ndarray, rgb, tol: int = 0) -> np.ndarray:
    """Binary (0/255 uint8) mask of pixels within `tol` per channel of colour `rgb` in a colour-ID pass.

    `idpass` is a path or a BGR array; `rgb` is (R, G, B).
    """
    im = read_image(idpass, cv2.IMREAD_COLOR) if isinstance(idpass, (str, Path)) else idpass
    b, g, r = int(rgb[2]), int(rgb[1]), int(rgb[0])
    d = np.abs(im.astype(int) - np.array([b, g, r])).max(axis=2)
    return ((d <= tol) * 255).astype(np.uint8)


def erode_mask(m: np.ndarray, px: int = 3) -> np.ndarray:
    """Erode a mask by px (ignores edge blending); falls back to the raw mask if it would vanish."""
    k = np.ones((2 * px + 1, 2 * px + 1), np.uint8)
    e = cv2.erode(m.astype(np.uint8), k) > 0
    return e if e.sum() >= 100 else m.astype(bool)


def _to_ref_size(test_bgr: np.ndarray, W: int, H: int) -> np.ndarray:
    return cv2.resize(
        test_bgr, (W, H), interpolation=cv2.INTER_AREA if test_bgr.shape[1] > W else cv2.INTER_CUBIC
    )


def maskdiff(ref_bgr: np.ndarray, test_bgr: np.ndarray, mask: np.ndarray) -> dict:
    """Is everything OUTSIDE a masked edit unchanged? Per-region identity fractions and dE00 stats."""
    H, W = ref_bgr.shape[:2]
    test_bgr = _to_ref_size(test_bgr, W, H)
    m = cv2.resize(mask, (W, H), interpolation=cv2.INTER_NEAREST) > 127
    out_ = ~m
    d = np.abs(ref_bgr.astype(int) - test_bgr.astype(int)).max(axis=2)
    de = ciede2000(
        srgb_to_lab(cv2.cvtColor(ref_bgr, cv2.COLOR_BGR2RGB)),
        srgb_to_lab(cv2.cvtColor(test_bgr, cv2.COLOR_BGR2RGB)),
    )
    res = dict(mask_frac=float(m.mean()))
    for name, sel in (("outside", out_), ("inside", m)):
        if sel.sum() == 0:
            continue
        res[name] = dict(
            px=int(sel.sum()),
            frac_identical=float((d[sel] == 0).mean()),
            frac_within_2_levels=float((d[sel] <= 2).mean()),
            mean_abs_max_channel=float(d[sel].mean()),
            de00_mean=float(de[sel].mean()),
            de00_p99=float(np.percentile(de[sel], 99)),
        )
    return res


def compare(ref_bgr, test_bgr, mask=None, ral_lab=None, sig=6.0) -> dict:
    """Geometry + colour drift between a reference and a test image (BGR uint8).

    Test is resized to the reference size. Keys: size, aspect_ref, aspect_test, ssim_gray, ssim_gray_blur8,
    edge_px, canny_all_r{0,2,4,8}, canny_long_r{0,2,4,8}, best_shift_long, de00_blur, and when applicable
    flat_frac_of_ref + de00_flat (flat regions) and de00_mask_vs_ral (mask + target Lab given).
    """
    H, W = ref_bgr.shape[:2]
    aspect_ref = ref_bgr.shape[1] / ref_bgr.shape[0]
    aspect_test = test_bgr.shape[1] / test_bgr.shape[0]
    test_bgr = _to_ref_size(test_bgr, W, H)
    gr = cv2.cvtColor(ref_bgr, cv2.COLOR_BGR2GRAY)
    gt = cv2.cvtColor(test_bgr, cv2.COLOR_BGR2GRAY)
    out = dict(size=[W, H], aspect_ref=round(aspect_ref, 4), aspect_test=round(aspect_test, 4))
    out["ssim_gray"] = ssim_gray(gr, gt)
    # SSIM of heavily blurred images ~ layout/tonal agreement, insensitive to texture
    out["ssim_gray_blur8"] = ssim_gray(cv2.GaussianBlur(gr, (0, 0), 8), cv2.GaussianBlur(gt, (0, 0), 8))
    er_all = canny(gr)
    et_all = canny(gt)
    er = long_edges(er_all)
    et = long_edges(et_all)
    out["edge_px"] = dict(
        ref_all=int(er_all.sum()), test_all=int(et_all.sum()), ref_long=int(er.sum()), test_long=int(et.sum())
    )
    for r in (0, 2, 4, 8):
        out[f"canny_all_r{r}"] = tol_metrics(er_all, et_all, r)
        out[f"canny_long_r{r}"] = tol_metrics(er, et, r)
    out["best_shift_long"] = best_shift(er, et)
    rgb_r = cv2.cvtColor(ref_bgr, cv2.COLOR_BGR2RGB)
    rgb_t = cv2.cvtColor(test_bgr, cv2.COLOR_BGR2RGB)
    br = cv2.GaussianBlur(rgb_r, (0, 0), sig)
    bt = cv2.GaussianBlur(rgb_t, (0, 0), sig)
    de = ciede2000(srgb_to_lab(br), srgb_to_lab(bt))
    out["de00_blur"] = dict(
        median=float(np.median(de)), p90=float(np.percentile(de, 90)), mean=float(de.mean())
    )
    grad = cv2.GaussianBlur(gr, (0, 0), 2.0).astype(np.float32)
    gx = cv2.Sobel(grad, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(grad, cv2.CV_32F, 0, 1)
    flat = cv2.erode((np.hypot(gx, gy) < 6).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    if flat.sum() > 500:
        out["flat_frac_of_ref"] = float(flat.mean())
        out["de00_flat"] = dict(
            median=float(np.median(de[flat])),
            p90=float(np.percentile(de[flat], 90)),
            mean=float(de[flat].mean()),
        )
    if mask is not None and ral_lab is not None:
        # 3 px erosion: ignore edge blending
        m = erode_mask(cv2.resize(mask, (W, H), interpolation=cv2.INTER_NEAREST) > 127)
        lab_t = srgb_to_lab(rgb_t)
        tgt = np.broadcast_to(np.array(ral_lab, float), lab_t.shape)
        d = ciede2000(lab_t, tgt)[m]
        mean_lab = lab_t[m].mean(0)
        out["de00_mask_vs_ral"] = dict(
            mask_px=int(m.sum()),
            mean_lab=[float(x) for x in mean_lab],
            de_of_mean=float(ciede2000(mean_lab[None], np.array([ral_lab], float))[0]),
            per_px_median=float(np.median(d)),
            per_px_p90=float(np.percentile(d, 90)),
            lab_std=[float(x) for x in lab_t[m].std(0)],
        )
    return out


def views(manifest: dict | str | Path, base_dir: Path | None = None) -> dict:
    """N cameras: per-view metrics plus cross-view colour consistency of the masked material.

    manifest = {"ral_hex": "#RRGGBB" (optional), "views": [{"name", "ref", "test", "idpass"?, "id_rgb"?,
    "tol"?}, ...]} as a dict or a path to its JSON file. Relative image paths resolve against `base_dir`
    (default: the manifest file's folder, or the working directory for a dict).
    """
    if isinstance(manifest, (str, Path)):
        mpath = Path(manifest)
        man = json.loads(mpath.read_text(encoding="utf-8"))
        base = Path(base_dir) if base_dir is not None else mpath.parent
    else:
        man = manifest
        base = Path(base_dir) if base_dir is not None else Path()
    ral = man.get("ral_hex")
    ral_lab = list(srgb_to_lab(hex_to_rgb8(ral))[0]) if ral else None
    per = []
    mean_labs = []
    for v in man["views"]:
        ref = load(base / v["ref"], 1500)
        test = read_image(base / v["test"], cv2.IMREAD_COLOR)
        mask = idmask(base / v["idpass"], v["id_rgb"], v.get("tol", 0)) if v.get("idpass") else None
        r = compare(ref, test, mask, ral_lab)
        r["name"] = v["name"]
        if mask is not None and mask.sum() > 0:
            H, W = ref.shape[:2]
            tt = cv2.resize(test, (W, H), interpolation=cv2.INTER_CUBIC)
            m = erode_mask(cv2.resize(mask, (W, H), interpolation=cv2.INTER_NEAREST) > 127)
            lab_t = srgb_to_lab(cv2.cvtColor(tt, cv2.COLOR_BGR2RGB))
            lab_r = srgb_to_lab(cv2.cvtColor(ref, cv2.COLOR_BGR2RGB))
            ml = lab_t[m].mean(0)
            rl = lab_r[m].mean(0)
            r["mask_mean_lab_test"] = [float(x) for x in ml]
            r["mask_mean_lab_ref"] = [float(x) for x in rl]
            r["de00_mask_test_vs_ref"] = float(ciede2000(ml[None], rl[None])[0])
            mean_labs.append(ml)
        per.append(r)
    out = dict(per_view=per)
    if len(mean_labs) > 1:
        n = len(mean_labs)
        M = np.zeros((n, n))
        for i in range(n):
            for j in range(n):
                M[i, j] = ciede2000(mean_labs[i][None], mean_labs[j][None])[0]
        out["cross_view_de00_matrix"] = M.round(3).tolist()
        out["cross_view_de00_max"] = float(M.max())
    return out
