"""Self-check for the gate core: CIEDE2000 reference pairs, identity/shift/RAL/maskdiff/views synthetics.

Ported from the selftest() in ~/Desktop/test/vizmaker-dossier/tools/fidelity_bench.py. It is the first CI
job (`make selftest`): if the numerics drift, nothing downstream is trustworthy.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import cv2
import numpy as np

from .color import ciede2000, hex_to_rgb8, srgb_to_lab
from .compare import compare, maskdiff, views, write_png

# CIEDE2000 reference pairs from Sharma, Wu, Dalal (2005) Table 1 (a selection of rows)
_SHARMA = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


def run_selftest(verbose: bool = True) -> bool:
    """Run every check; print a log and `SELFTEST PASS` / `SELFTEST FAIL` when verbose. Returns the verdict."""

    def say(msg: str) -> None:
        if verbose:
            print(msg)

    ok = True
    for l1, l2, exp in _SHARMA:
        got = float(ciede2000(np.array([l1]), np.array([l2]))[0])
        flag = abs(got - exp) < 2e-4
        ok &= flag
        say(f"dE00 {l1} vs {l2} -> {got:.4f} (expected {exp:.4f}) {'OK' if flag else 'FAIL'}")

    white = srgb_to_lab(np.array([[255, 255, 255]], np.uint8))[0]
    say(f"sRGB white Lab: {white.round(2)}")
    ok &= abs(white[0] - 100.0) < 0.01 and abs(white[1]) < 0.01 and abs(white[2]) < 0.01

    rng = np.random.default_rng(0)
    img = (rng.random((300, 450, 3)) * 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (0, 0), 3)
    cv2.rectangle(img, (60, 60), (300, 220), (20, 20, 20), 4)
    cv2.circle(img, (340, 150), 50, (240, 240, 240), 5)
    same = compare(img, img.copy())
    say(
        f"identity: ssim={same['ssim_gray']:.4f} iou_r0={same['canny_long_r0']['iou']:.3f} "
        f"recall_r2={same['canny_long_r2']['recall']:.3f} dE_median={same['de00_blur']['median']:.3f}"
    )
    ok &= (
        same["ssim_gray"] > 0.999
        and same["canny_long_r0"]["iou"] > 0.999
        and same["de00_blur"]["median"] < 1e-6
    )

    sh = compare(img, np.roll(img, 3, axis=1))
    say(
        f"3px shift: ssim={sh['ssim_gray']:.3f} iou_r0={sh['canny_long_r0']['iou']:.3f} "
        f"iou_r4={sh['canny_long_r4']['iou']:.3f} best_shift={sh['best_shift_long']}"
    )
    ok &= sh["canny_long_r4"]["recall"] > 0.95 and sh["best_shift_long"]["dx"] == -3

    # colour mask test: a mask region exactly the RAL 7016 hex, and one with +10 red
    hexv = "#383E42"  # RAL 7016 anthracite grey, commonly published sRGB approximation
    lab_t = srgb_to_lab(hex_to_rgb8(hexv))[0]
    blk = np.zeros((200, 200, 3), np.uint8)
    blk[:] = hex_to_rgb8(hexv)[0][::-1]  # BGR
    m = np.full((200, 200), 255, np.uint8)
    c = compare(blk, blk, mask=m, ral_lab=lab_t)
    say(f"RAL exact: dE of mean = {c['de00_mask_vs_ral']['de_of_mean']:.4f}")
    ok &= c["de00_mask_vs_ral"]["de_of_mean"] < 1e-6
    off = blk.copy()
    off[:, :, 2] = np.clip(off[:, :, 2].astype(int) + 10, 0, 255)  # +10 red
    c2 = compare(blk, off, mask=m, ral_lab=lab_t)
    say(f"RAL +10 red: dE of mean = {c2['de00_mask_vs_ral']['de_of_mean']:.3f}")
    ok &= c2["de00_mask_vs_ral"]["de_of_mean"] > 1.0

    # maskdiff: edit only inside the mask -> outside must be 100% identical
    base = (rng.random((120, 160, 3)) * 255).astype(np.uint8)
    mk = np.zeros((120, 160), np.uint8)
    mk[30:80, 40:100] = 255
    edited = base.copy()
    edited[30:80, 40:100] = 255 - edited[30:80, 40:100]
    md = maskdiff(base, edited, mk)
    say(
        f"maskdiff outside identical={md['outside']['frac_identical']:.3f} "
        f"inside identical={md['inside']['frac_identical']:.3f}"
    )
    ok &= md["outside"]["frac_identical"] == 1.0 and md["inside"]["frac_identical"] < 0.05

    # views: 3 synthetic cameras, material colour drifting 0 / ~3 / ~12 dE between views
    with tempfile.TemporaryDirectory(prefix="fb_selftest_") as tmp:
        td = Path(tmp)
        man: dict = dict(ral_hex=hexv, views=[])
        for k, dr in enumerate((0, 8, 30)):
            ref = np.zeros((1000, 1500, 3), np.uint8)
            ref[:] = (180, 180, 180)
            idp = np.zeros((1000, 1500, 3), np.uint8)
            idp[250:750, 300:1200] = (255, 0, 0)  # BGR -> RGB (0,0,255)
            t = ref.copy()
            col = np.array(hex_to_rgb8(hexv)[0][::-1], int) + np.array([0, 0, dr])
            t[250:750, 300:1200] = np.clip(col, 0, 255)
            cv2.rectangle(ref, (300, 250), (1200, 750), (60, 60, 60), 3)
            for nm, im in (("ref", ref), ("test", t), ("id", idp)):
                write_png(td / f"v{k}_{nm}.png", im, compression=1)
            man["views"].append(
                dict(
                    name=f"cam{k}",
                    ref=f"v{k}_ref.png",
                    test=f"v{k}_test.png",
                    idpass=f"v{k}_id.png",
                    id_rgb=[0, 0, 255],
                )
            )
        vr = views(man, base_dir=td)
    per_view_de = [round(v["de00_mask_vs_ral"]["de_of_mean"], 2) for v in vr["per_view"]]
    say(f"views: cross-view dE00 max={vr['cross_view_de00_max']:.2f} ; per-view RAL dE(mean)={per_view_de}")
    ok &= vr["per_view"][0]["de00_mask_vs_ral"]["de_of_mean"] < 0.05 and vr["cross_view_de00_max"] > 5

    ok = bool(ok)
    say("SELFTEST " + ("PASS" if ok else "FAIL"))
    return ok
