"""crop_pair geometry and the two 100% sheet builders."""

from __future__ import annotations

import pytest
from PIL import Image

from plate_gates.crops import crop_pair, crop_tiles, label_font, region_tiles


def _img(w: int = 1100, h: int = 620, color=(120, 100, 80)) -> Image.Image:
    im = Image.new("RGB", (w, h), color)
    for x in range(0, w, 50):
        im.paste((x % 255, 30, 200 - x % 200), (x, 0, x + 25, h))
    return im


@pytest.mark.parametrize(
    "cx,cy",
    [(550, 310), (0, 0), (1100, 620), (10, 600), (1090, 15), (300, 500)],
)
def test_crop_pair_boxes_are_even_inside_and_nested(cx, cy):
    im = _img()
    (start, sbox), (end, ebox) = crop_pair(im, cx, cy, 1.5, 3.0)
    for crop, (x0, y0, w, h) in ((start, sbox), (end, ebox)):
        assert w % 2 == 0 and h % 2 == 0
        assert crop.size == (w, h)
        assert x0 >= 0 and y0 >= 0 and x0 + w <= im.width and y0 + h <= im.height
    sx0, sy0, sw, sh = sbox
    ex0, ey0, ew, eh = ebox
    assert ew < sw and eh < sh
    assert sx0 <= ex0 and sy0 <= ey0 and ex0 + ew <= sx0 + sw and ey0 + eh <= sy0 + sh


def test_crop_pair_zoom_one_is_the_whole_frame():
    (full, box), _ = crop_pair(_img(1100, 620), 500, 300, 1.0, 2.0)
    assert box == (0, 0, 1100, 620) and full.size == (1100, 620)


def test_crop_pair_rejects_zoom_below_one():
    with pytest.raises(ValueError):
        crop_pair(_img(), 500, 300, 0.5, 2.0)


def test_crop_tiles_width_and_height():
    images = [_img(), _img(1400, 800, (60, 60, 90))]
    boxes = [(0.0, 0.0, 0.5, 0.5), (0.25, 0.25, 0.75, 0.75), (0.5, 0.5, 1.0, 1.0)]
    sheet = crop_tiles(images, boxes, ["one.png", "two.png"], tile_w=300)
    assert sheet.width == len(boxes) * (300 + 6)
    assert sheet.height > 2 * 34
    assert sheet.mode == "RGB"


def test_crop_tiles_downscales_only_wide_crops():
    # 20% crop of a 1100-wide image is 220 px: shown at 100%, not stretched to tile_w
    sheet = crop_tiles([_img()], [(0.0, 0.0, 0.2, 0.2)], ["a"], tile_w=900)
    assert sheet.width == 906
    assert sheet.height == 124 + 34  # tile height is the 620*0.2 crop, plus the label strip


def test_region_tiles_scales_boxes_per_image_and_has_expected_width():
    ref = (3840, 2160)
    regions = {"sign": (2340, 1080, 3560, 1700), "water": (2200, 1500, 3300, 2160)}
    images = [_img(1920, 1080), _img(960, 540, (10, 10, 10))]
    sheet = region_tiles(images, regions, ref_size=ref, tile_w=380, labels=["plate", "take"])
    assert sheet.width == len(regions) * (380 + 6)
    assert sheet.height >= 2 * (38 + 6)


def test_region_tiles_default_labels_and_validation():
    sheet = region_tiles([_img()], {"r": (0, 0, 1920, 1080)}, tile_w=200)
    assert sheet.width == 206
    with pytest.raises(ValueError):
        region_tiles([], {"r": (0, 0, 1, 1)})
    with pytest.raises(ValueError):
        region_tiles([_img()], {"r": (0, 0, 1, 1)}, labels=["a", "b"])


def test_label_font_falls_back_to_the_default_when_the_font_path_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_FONT", str(tmp_path / "no-such-font.ttf"))
    assert label_font(22) is not None
