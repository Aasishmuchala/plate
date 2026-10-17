"""edge_map / edge_agreement on synthetic scenes."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from plate_gates.align import edge_agreement, edge_map


def _scene() -> np.ndarray:
    im = Image.new("RGB", (640, 360), (70, 80, 95))
    d = ImageDraw.Draw(im)
    d.rectangle((60, 50, 200, 300), fill=(200, 190, 170))
    d.rectangle((260, 80, 300, 320), fill=(30, 40, 50))
    d.rectangle((340, 60, 380, 300), fill=(220, 220, 225))
    d.ellipse((430, 90, 590, 250), fill=(180, 60, 50))
    d.rectangle((420, 270, 600, 330), fill=(90, 160, 110))
    return np.asarray(im)


def test_identical_images_agree():
    img = _scene()
    agree, overlay = edge_agreement(img, img)
    assert agree >= 0.99
    assert overlay.dtype == np.uint8 and overlay.shape == (1080, 1920, 3)
    # identical edges: white (both) wherever there is any edge, no pure red or pure cyan
    red_only = (overlay[..., 0] > 0) & (overlay[..., 1] == 0)
    cyan_only = (overlay[..., 0] == 0) & (overlay[..., 1] > 0)
    assert not red_only.any() and not cyan_only.any()


def test_shifted_image_disagrees():
    img = _scene()
    agree, overlay = edge_agreement(img, np.roll(img, 12, axis=1))
    assert agree < 0.7
    assert (overlay[..., 0] > 0).any() and (overlay[..., 1] > 0).any()


def test_native_width_keeps_the_image_size():
    img = _scene()
    agree, overlay = edge_agreement(img, img, width=None)
    assert overlay.shape == (360, 640, 3) and agree >= 0.99


def test_relit_of_a_different_size_is_resized_to_the_plate():
    img = _scene()
    small = np.asarray(Image.fromarray(img).resize((320, 180), Image.LANCZOS))
    agree, overlay = edge_agreement(img, small, width=640)
    assert overlay.shape == (360, 640, 3)
    assert 0.0 <= agree <= 1.0


def test_edge_map_is_binary_float32_and_accepts_pil_or_array():
    img = _scene()
    e1 = edge_map(Image.fromarray(img))
    e2 = edge_map(img)
    assert e1.dtype == np.float32 and set(np.unique(e1)) <= {0.0, 1.0}
    assert np.array_equal(e1, e2)
    assert e1.sum() > 0
    assert edge_map(np.full((50, 50), 120, np.uint8)).shape == (50, 50)  # flat input must not crash
