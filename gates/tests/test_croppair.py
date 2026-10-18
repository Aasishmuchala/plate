"""croppair.cut_crops: the cut_crops.py geometry, zoom validation (push-in, pull-back, locked), the end centre of a
slider drift, PIL / ndarray handling, crop_pair delegation."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from plate_gates import croppair
from plate_gates.crops import crop_pair, crop_tiles, point_tiles

AMPHI = dict(W=5504, H=3072, cx=3633, cy=2174)


def test_amphi_n2b_boxes_are_the_recorded_ones():
    b0 = croppair.box_for(AMPHI["W"], AMPHI["H"], AMPHI["cx"], AMPHI["cy"], 1.5)
    b1 = croppair.box_for(AMPHI["W"], AMPHI["H"], AMPHI["cx"], AMPHI["cy"], 2.3)
    assert b0 == (1799, 1024, 3668, 2048)  # PUSH_start
    assert b1 == (2437, 1507, 2392, 1334)  # PUSH_end


def test_cut_crops_matches_the_original_script_pixel_for_pixel():
    """The original: w = int(W/z)//2*2 ..., x0 = min(max(cx - w//2, 0), W - w), im.crop((x0, y0, x0+w, y0+h))."""
    rng = np.random.default_rng(3)
    arr = rng.integers(0, 256, (307, 550, 3), dtype=np.uint8)  # 1/10 scale of the amphi frame
    im = Image.fromarray(arr)
    W, H = im.size
    start, end, boxes = croppair.cut_crops(im, 363, 217, 1.5, 2.3)

    def original(z, cx=363, cy=217):
        w, h = int(W / z) // 2 * 2, int(H / z) // 2 * 2
        x0 = min(max(cx - w // 2, 0), W - w)
        y0 = min(max(cy - h // 2, 0), H - h)
        return im.crop((x0, y0, x0 + w, y0 + h)), (x0, y0, w, h)

    for got, box, z in ((start, boxes[0], 1.5), (end, boxes[1], 2.3)):
        want, wbox = original(z)
        assert box == wbox and isinstance(got, Image.Image)
        assert np.array_equal(np.asarray(got), np.asarray(want))


def test_ndarray_in_gives_ndarray_copies_out():
    arr = np.arange(100 * 160 * 3, dtype=np.uint16).reshape(100, 160, 3)
    start, end, boxes = croppair.cut_crops(arr, 80, 50, 1.0, 2.0)
    assert isinstance(start, np.ndarray) and start.shape == (100, 160, 3) and boxes[0] == (0, 0, 160, 100)
    assert end.shape == (50, 80, 3) and end.dtype == np.uint16
    x0, y0, w, h = boxes[1]
    assert np.array_equal(end, arr[y0 : y0 + h, x0 : x0 + w])
    end[0, 0, 0] = 9999
    assert arr[y0, x0, 0] != 9999  # a copy, not a view


@pytest.mark.parametrize(
    "z0,z1", [(0.9, 2.0), (1.5, 1.5), (2.0, 0.9), (1.0, 3.2), (3.2, 1.0), (-1.0, 2.0), (float("nan"), 2.0)]
)
def test_zoom_pair_validation(z0, z1):
    with pytest.raises(ValueError, match="zoom pair"):
        croppair.cut_crops(Image.new("RGB", (64, 36)), 32, 18, z0, z1)


def test_zoom_pair_validation_accepts_the_limits_and_the_house_moves():
    croppair.validate_zooms(1.0, 3.0)
    croppair.validate_zooms(1.5, 2.3)  # amphi
    croppair.validate_zooms(1.0, 1.18)  # couple-dining
    croppair.validate_zooms(1.25, 1.0)  # the as02 reveal reversed: a pull-back
    croppair.validate_zooms(3.0, 1.0)


def test_an_equal_pair_is_a_locked_crop_only_when_asked_for():
    with pytest.raises(ValueError, match="allow_equal"):
        croppair.validate_zooms(1.0, 1.0)
    croppair.validate_zooms(1.0, 1.0, allow_equal=True)
    croppair.validate_zooms(3.0, 3.0, allow_equal=True)
    with pytest.raises(ValueError, match="zoom pair"):
        croppair.validate_zooms(0.5, 0.5, allow_equal=True)
    arr = np.arange(36 * 64 * 3, dtype=np.uint8).reshape(36, 64, 3)
    start, end, boxes = croppair.cut_crops(arr, 32, 18, 1.0, 1.0, allow_equal=True)
    assert (
        boxes == ((0, 0, 64, 36), (0, 0, 64, 36))
        and np.array_equal(start, end)
        and np.array_equal(start, arr)
    )


def test_direction_names_the_zoom():
    assert croppair.direction(1.0, 1.18) == "in"
    assert croppair.direction(1.25, 1.0) == "out"
    assert croppair.direction(1.1, 1.1) == "locked"


def test_a_pull_back_is_the_push_in_with_its_boxes_swapped():
    rng = np.random.default_rng(5)
    arr = rng.integers(0, 256, (307, 550, 3), dtype=np.uint8)
    p_start, p_end, p_boxes = croppair.cut_crops(arr, 363, 217, 1.5, 2.3)
    b_start, b_end, b_boxes = croppair.cut_crops(arr, 363, 217, 2.3, 1.5)
    assert b_boxes == (p_boxes[1], p_boxes[0])
    assert np.array_equal(b_start, p_end) and np.array_equal(b_end, p_start)
    assert b_start.shape[1] < b_end.shape[1]  # the start is the tighter crop: the frame widens


def test_an_end_centre_slides_the_end_crop_and_leaves_the_start_alone():
    rng = np.random.default_rng(9)
    arr = rng.integers(0, 256, (360, 640, 3), dtype=np.uint8)
    start, end, boxes = croppair.cut_crops(arr, 320, 180, 1.1, 1.1, allow_equal=True, cx1=339.2, cy1=180)
    ref_start, _, ref_boxes = croppair.cut_crops(arr, 320, 180, 1.1, 1.2)
    assert boxes[0] == ref_boxes[0] and np.array_equal(start, ref_start)
    assert boxes[1] == croppair.box_for(640, 360, 339.2, 180, 1.1)
    (x0, y0, w, h), (x1, y1, w1, h1) = boxes
    assert (w, h) == (w1, h1) and y1 == y0 and x1 > x0  # same zoom, the frame slides right
    assert np.array_equal(end, arr[y1 : y1 + h1, x1 : x1 + w1])
    # a centre past the edge clamps inside the frame, never pads
    _, _, clamped = croppair.cut_crops(arr, 320, 180, 1.1, 1.1, allow_equal=True, cx1=10_000, cy1=-50)
    cx0, cy0, cw, ch = clamped[1]
    assert cx0 + cw == 640 and cy0 == 0


def test_boxes_are_even_clamped_and_nested():
    for cx, cy in ((0, 0), (5504, 3072), (2752, 1536), (10, 3000)):
        b0, b1 = croppair.box_for(5504, 3072, cx, cy, 1.5), croppair.box_for(5504, 3072, cx, cy, 2.3)
        for x0, y0, w, h in (b0, b1):
            assert w % 2 == 0 and h % 2 == 0 and x0 >= 0 and y0 >= 0 and x0 + w <= 5504 and y0 + h <= 3072


def test_crop_pair_delegates_to_the_same_geometry():
    im = Image.new("RGB", (550, 307), (10, 20, 30))
    (s, sbox), (e, ebox) = crop_pair(im, 363, 217, 1.5, 2.3)
    _, _, boxes = croppair.cut_crops(im, 363, 217, 1.5, 2.3)
    assert (sbox, ebox) == boxes and s.size == (sbox[2], sbox[3])


def test_point_tiles_are_native_pixels_clamped_inside_the_frame():
    arr = np.zeros((300, 400, 3), np.uint8)
    arr[100:110, 200:210] = 255
    tiles = point_tiles(arr, [(205, 105), (0, 0), (399, 299)], size=64)
    assert [t.size for t in tiles] == [(64, 64)] * 3
    assert np.asarray(tiles[0])[32, 32].tolist() == [255, 255, 255]  # centred on the flagged pixel
    assert np.asarray(tiles[1]).sum() == 0
    small = point_tiles(Image.new("RGB", (30, 20)), [(10, 10)], size=64)
    assert small[0].size == (30, 20)  # smaller only when the frame is
    with pytest.raises(ValueError):
        point_tiles(arr, [(1, 1)], size=1)


def test_crop_tiles_single_image_form_returns_the_point_tiles():
    im = Image.new("RGB", (400, 300), (5, 5, 5))
    tiles = crop_tiles(im, [(100, 100), (300, 200)], size=128)
    assert isinstance(tiles, list) and [t.size for t in tiles] == [(128, 128)] * 2
    assert [t.size for t in crop_tiles(im, [(100, 100)], 96)] == [(96, 96)]  # size as the third positional
    grid = crop_tiles([im], [(0.0, 0.0, 0.5, 0.5)], ["a"], tile_w=200)
    assert isinstance(grid, Image.Image) and grid.width == 206
    with pytest.raises(ValueError):
        crop_tiles([im], [(0.0, 0.0, 0.5, 0.5)])  # grid form needs labels
