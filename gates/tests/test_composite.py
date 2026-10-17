"""composite_region (feathered paste, outside untouched) and pushin_gate / contact_sheet."""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from PIL import Image

from plate_gates.composite import composite_region
from plate_gates.crops import load_rgb
from plate_gates.sheets import contact_sheet, pushin_gate
from plate_gates.video import write_test_clip


def _noise(w: int, h: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (h, w, 3), dtype=np.uint8)


def test_outside_the_feather_is_the_base_and_the_centre_is_the_patch():
    base, patch = _noise(640, 480, 1), _noise(640, 480, 2)
    box, fe = (120, 100, 520, 380), 40
    out = composite_region(base, patch, box, feather_px=fe)
    assert out.dtype == np.uint8 and out.shape == base.shape
    x0, y0, x1, y1 = box
    keep = np.ones(base.shape[:2], bool)
    keep[max(y0 - 2 * fe, 0) : y1 + 2 * fe, max(x0 - 2 * fe, 0) : x1 + 2 * fe] = False
    assert keep.any()
    assert np.array_equal(out[keep], base[keep])
    cy, cx = (y0 + y1) // 2, (x0 + x1) // 2
    centre = np.abs(
        out[cy - 5 : cy + 5, cx - 5 : cx + 5].astype(int) - patch[cy - 5 : cy + 5, cx - 5 : cx + 5]
    )
    assert centre.max() <= 1


def test_feather_blends_between_base_and_patch_at_the_box_edge():
    base = np.full((200, 200, 3), 0, np.uint8)
    patch = np.full((200, 200, 3), 200, np.uint8)
    out = composite_region(base, patch, (50, 50, 150, 150), feather_px=20)
    edge = int(out[100, 50, 0])
    assert 60 < edge < 140  # half-way across the blurred edge
    assert int(out[100, 100, 0]) >= 199 and int(out[5, 5, 0]) == 0


def test_patch_of_another_size_is_resized_to_the_base():
    base = _noise(320, 200, 3)
    patch = np.asarray(Image.fromarray(_noise(160, 100, 4)).resize((160, 100)))
    out = composite_region(base, patch, (40, 30, 280, 170), feather_px=0)
    assert out.shape == base.shape
    expected = np.asarray(Image.fromarray(patch).resize((320, 200), Image.LANCZOS))
    assert np.abs(out[100, 160].astype(int) - expected[100, 160]).max() <= 1
    assert np.array_equal(out[:20], base[:20])  # hard edge: outside the box is untouched


def test_composite_rejects_non_rgb_input():
    with pytest.raises(ValueError):
        composite_region(np.zeros((10, 10), np.uint8), np.zeros((10, 10), np.uint8), (0, 0, 5, 5))


def _scene(w: int, h: int, shift: int = 0) -> np.ndarray:
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    r = 60 + 120 * (xs / w)
    g = 80 + 100 * (ys / h)
    b = np.full_like(r, 110)
    img = np.stack([r, g, b], axis=-1).astype(np.uint8)
    cv2.rectangle(img, (40 + shift, 40), (140 + shift, 120), (230, 220, 200), -1)
    cv2.circle(img, (220 + shift, 100), 40, (180, 60, 50), -1)
    return img


def test_pushin_gate_lands_on_start_and_end(tmp_path):
    w, h, n = 320, 180, 48
    start = _scene(w, h)
    # end crop: the centre 1/2 of the start, scaled back up (a 2x push-in)
    end = np.asarray(Image.fromarray(start).crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4)).resize((w, h)))
    frames = []
    for i in range(n):
        a = i / (n - 1)
        frames.append(np.clip(start * (1 - a) + end * a + 0.5, 0, 255).astype(np.uint8))
    clip = write_test_clip(tmp_path / "push.mp4", frames, fps=24)
    sheet = tmp_path / "gate.jpg"
    res = pushin_gate(clip, Image.fromarray(start), Image.fromarray(end), sheet_out=sheet)
    assert res["size"] == (w, h)
    assert abs(res["duration_s"] - 2.0) < 0.1
    assert res["first_vs_start_255"] <= 3
    assert res["last_vs_end_255"] <= 3
    assert res["last_vs_start_255"] > res["last_vs_end_255"]
    assert sheet.is_file() and load_rgb(sheet).width == 2 * 960


def test_pushin_gate_accepts_paths(tmp_path):
    w, h = 160, 90
    still = _scene(w, h)
    clip = write_test_clip(tmp_path / "hold.mp4", [still] * 12, fps=24)
    Image.fromarray(still).save(tmp_path / "start.png")
    Image.fromarray(still).save(tmp_path / "end.png")
    res = pushin_gate(clip, tmp_path / "start.png", tmp_path / "end.png")
    assert res["first_vs_start_255"] <= 3 and res["last_vs_end_255"] <= 3


def test_contact_sheet_layout_and_validation():
    frames = [Image.new("RGB", (640, 360), (i * 20, 40, 90)) for i in range(5)]
    sheet = contact_sheet(frames, [f"t={i}" for i in range(5)], cols=3, tile_w=200, bar=30)
    assert sheet.width == 3 * 200
    assert sheet.height == 2 * (112 + 30)  # 640x360 -> 200x112, two rows
    with pytest.raises(ValueError):
        contact_sheet(frames, ["only one"])
    with pytest.raises(ValueError):
        contact_sheet([], [])
