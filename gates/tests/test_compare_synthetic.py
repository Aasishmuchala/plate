"""compare / maskdiff / idmask / views on synthetic images (no external files)."""

from __future__ import annotations

import json

import cv2
import numpy as np

from plate_gates.color import hex_to_rgb8
from plate_gates.compare import compare, idmask, maskdiff, views, write_png


def _synthetic() -> np.ndarray:
    rng = np.random.default_rng(0)
    img = (rng.random((300, 450, 3)) * 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (0, 0), 3)
    cv2.rectangle(img, (60, 60), (300, 220), (20, 20, 20), 4)
    cv2.circle(img, (340, 150), 50, (240, 240, 240), 5)
    return img


def test_identity():
    img = _synthetic()
    res = compare(img, img.copy())
    assert res["ssim_gray"] > 0.999
    assert res["canny_long_r0"]["iou"] > 0.999
    assert res["de00_blur"]["median"] < 1e-6
    assert res["size"] == [450, 300]


def test_documented_keys():
    img = _synthetic()
    res = compare(img, img.copy())
    expected = {"size", "aspect_ref", "aspect_test", "ssim_gray", "ssim_gray_blur8", "edge_px"}
    expected |= {f"canny_all_r{r}" for r in (0, 2, 4, 8)} | {f"canny_long_r{r}" for r in (0, 2, 4, 8)}
    expected |= {"best_shift_long", "de00_blur"}
    assert expected <= set(res)
    assert set(res["best_shift_long"]) == {"recall_r2", "dx", "dy"}
    assert set(res["canny_long_r2"]) == {"iou", "recall", "precision"}


def test_three_pixel_shift_is_found():
    img = _synthetic()
    res = compare(img, np.roll(img, 3, axis=1))
    assert res["best_shift_long"]["dx"] == -3
    assert res["canny_long_r4"]["recall"] > 0.95


def test_test_image_is_resized_to_reference():
    img = _synthetic()
    small = cv2.resize(img, (225, 150), interpolation=cv2.INTER_AREA)
    res = compare(img, small)
    assert res["size"] == [450, 300]
    assert res["aspect_test"] == res["aspect_ref"]


def test_maskdiff_inverted_interior():
    rng = np.random.default_rng(0)
    base = (rng.random((120, 160, 3)) * 255).astype(np.uint8)
    mk = np.zeros((120, 160), np.uint8)
    mk[30:80, 40:100] = 255
    edited = base.copy()
    edited[30:80, 40:100] = 255 - edited[30:80, 40:100]
    md = maskdiff(base, edited, mk)
    assert md["outside"]["frac_identical"] == 1.0
    assert md["inside"]["frac_identical"] < 0.05
    assert md["mask_frac"] == np.float64(50 * 60) / (120 * 160)


def test_idmask_from_path_and_array(tmp_path):
    idp = np.zeros((40, 60, 3), np.uint8)
    idp[10:20, 10:30] = (255, 0, 0)  # BGR blue == RGB (0, 0, 255)
    write_png(tmp_path / "id.png", idp)
    from_arr = idmask(idp, (0, 0, 255))
    from_path = idmask(tmp_path / "id.png", (0, 0, 255))
    assert from_arr.dtype == np.uint8 and set(np.unique(from_arr)) == {0, 255}
    assert (from_arr == from_path).all()
    assert int((from_arr > 0).sum()) == 10 * 20
    assert int((idmask(idp, (0, 0, 250), tol=5) > 0).sum()) == 10 * 20
    assert int((idmask(idp, (0, 0, 250), tol=0) > 0).sum()) == 0


def _write_views(td, drifts):
    hexv = "#383E42"
    man = {"ral_hex": hexv, "views": []}
    for k, dr in enumerate(drifts):
        ref = np.zeros((1000, 1500, 3), np.uint8)
        ref[:] = (180, 180, 180)
        idp = np.zeros((1000, 1500, 3), np.uint8)
        idp[250:750, 300:1200] = (255, 0, 0)
        t = ref.copy()
        col = np.array(hex_to_rgb8(hexv)[0][::-1], int) + np.array([0, 0, dr])
        t[250:750, 300:1200] = np.clip(col, 0, 255)
        cv2.rectangle(ref, (300, 250), (1200, 750), (60, 60, 60), 3)
        for nm, im in (("ref", ref), ("test", t), ("id", idp)):
            write_png(td / f"v{k}_{nm}.png", im, compression=1)
        man["views"].append(
            {
                "name": f"cam{k}",
                "ref": f"v{k}_ref.png",
                "test": f"v{k}_test.png",
                "idpass": f"v{k}_id.png",
                "id_rgb": [0, 0, 255],
            }
        )
    return man


def test_views_relative_paths_with_base_dir(tmp_path):
    man = _write_views(tmp_path, (0, 8, 30))
    res = views(man, base_dir=tmp_path)
    assert len(res["per_view"]) == 3
    assert res["per_view"][0]["de00_mask_vs_ral"]["de_of_mean"] < 0.05
    assert res["cross_view_de00_max"] > 5
    assert np.array(res["cross_view_de00_matrix"]).shape == (3, 3)
    assert [v["name"] for v in res["per_view"]] == ["cam0", "cam1", "cam2"]


def test_views_manifest_file_defaults_base_dir_to_its_folder(tmp_path):
    man = _write_views(tmp_path, (0, 30))
    mpath = tmp_path / "man.json"
    mpath.write_text(json.dumps(man), encoding="utf-8", newline="\n")
    for arg in (mpath, str(mpath)):
        res = views(arg)
        assert res["cross_view_de00_max"] > 5
