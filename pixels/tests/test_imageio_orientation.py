"""EXIF orientation and header-first size checks in `plate_pixels.imageio` (router plan T137).

`cv2.imdecode(..., IMREAD_UNCHANGED)` ignores a JPEG's EXIF Orientation, so a phone photo (Orientation 6, stored
200 x 100, shown 100 x 200) used to become a sideways Tier C packet. `imageio.read` applies the tag to JPEG
input and leaves every other format as stored. The oracle is PIL's own transpose of the SAME decoded pixels:
the file is written twice from one array, with the tag and without it, so both decode to identical stored
pixels and the only difference is the orientation.

`declared_size` / `check_pixel_budget` read the size from the header alone: a hand-built PNG header that
declares 11000 x 11000 is refused with `TooManyPixels` without a pixel being allocated.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
import tifffile
from PIL import Image, ImageOps

from plate_pixels import imageio

T = Image.Transpose
# EXIF Orientation -> the PIL transpose that turns the stored pixels into the displayed ones.
PIL_TRANSPOSE = {
    2: T.FLIP_LEFT_RIGHT,
    3: T.ROTATE_180,
    4: T.FLIP_TOP_BOTTOM,
    5: T.TRANSPOSE,
    6: T.ROTATE_270,
    7: T.TRANSVERSE,
    8: T.ROTATE_90,
}
WIDTH, HEIGHT = 200, 100


def _array(width: int = WIDTH, height: int = HEIGHT) -> np.ndarray:
    """A lopsided RGB gradient: every flip, turn and transpose of it is a different picture."""
    y, x = np.mgrid[0:height, 0:width]
    return np.stack([x * 255 // (width - 1), y * 255 // (height - 1), (x * 3 + y * 5) % 256], axis=-1).astype(
        np.uint8
    )


def _exif(orientation: int) -> bytes:
    exif = Image.Exif()
    exif[imageio.EXIF_ORIENTATION_TAG] = orientation
    return exif.tobytes()


def _jpeg(path: Path, arr: np.ndarray, orientation: int | None = None) -> Path:
    kwargs = {} if orientation is None else {"exif": _exif(orientation)}
    Image.fromarray(arr).save(path, "JPEG", quality=95, subsampling=0, **kwargs)
    return path


def _u8(image: imageio.Image) -> np.ndarray:
    assert image.bit_depth == 8 and image.encoding == "srgb"
    return np.rint(image.rgb * 255.0).astype(np.uint8)


def _png_with_header(path: Path, width: int, height: int) -> Path:
    """A PNG that is only a signature, an IHDR for `width` x `height` and an IEND: all a header reader needs."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b""))
    return path


# ---------------------------------------------------------------------------- EXIF orientation


@pytest.mark.parametrize("orientation", sorted(PIL_TRANSPOSE))
def test_a_jpeg_with_an_orientation_tag_reads_upright(tmp_path, orientation):
    arr = _array()
    plain = imageio.read(_jpeg(tmp_path / "plain.jpg", arr))
    tagged = imageio.read(_jpeg(tmp_path / f"o{orientation}.jpg", arr, orientation))

    want = np.asarray(Image.fromarray(_u8(plain)).transpose(PIL_TRANSPOSE[orientation]))
    got = _u8(tagged)
    swapped = orientation in (5, 6, 7, 8)
    assert got.shape[:2] == ((WIDTH, HEIGHT) if swapped else (HEIGHT, WIDTH))
    assert np.array_equal(got, want)  # the pixels are upright, not just the shape
    assert tagged.rgb.flags["C_CONTIGUOUS"]


@pytest.mark.parametrize("orientation", [3, 6, 8])
def test_the_orientation_agrees_with_pil_exif_transpose(tmp_path, orientation):
    path = _jpeg(tmp_path / "t.jpg", _array(), orientation)
    with Image.open(path) as im:
        want = np.asarray(ImageOps.exif_transpose(im).convert("RGB"))
    got = _u8(imageio.read(path))
    assert got.shape == want.shape
    # two JPEG decoders may differ by a level here and there; a wrong turn differs by tens of levels
    assert np.abs(got.astype(int) - want.astype(int)).mean() < 2.0


def test_a_jpeg_without_a_tag_or_with_a_broken_exif_block_is_as_stored(tmp_path):
    arr = _array()
    plain = imageio.read(_jpeg(tmp_path / "plain.jpg", arr))
    assert plain.rgb.shape[:2] == (HEIGHT, WIDTH)
    raw = (tmp_path / "plain.jpg").read_bytes()
    # an APP1 "Exif" segment whose TIFF header is garbage: no orientation, no crash
    broken = raw[:2] + b"\xff\xe1\x00\x10Exif\x00\x00" + b"\x00" * 8 + raw[2:]
    (tmp_path / "broken.jpg").write_bytes(broken)
    assert imageio.exif_orientation(broken) == 1
    assert imageio.read(tmp_path / "broken.jpg").rgb.shape[:2] == (HEIGHT, WIDTH)


def test_a_value_outside_one_to_eight_is_ignored(tmp_path):
    path = _jpeg(tmp_path / "o9.jpg", _array(), 9)
    assert imageio.exif_orientation(path.read_bytes()) == 1
    assert imageio.read(path).rgb.shape[:2] == (HEIGHT, WIDTH)


def test_png_and_tiff_are_read_as_stored_whatever_their_orientation_tag(tmp_path):
    arr = _array()
    png = tmp_path / "o6.png"
    Image.fromarray(arr).save(png, "PNG", exif=_exif(6))  # an eXIf chunk with Orientation 6
    assert imageio.read(png).rgb.shape[:2] == (HEIGHT, WIDTH)
    assert np.array_equal(_u8(imageio.read(png)), arr)

    tif = tmp_path / "o6.tif"
    tifffile.imwrite(str(tif), arr, photometric="rgb", extratags=[(274, "H", 1, 6, True)])
    with tifffile.TiffFile(tif) as tf:
        assert tf.pages[0].tags[274].value == 6  # the tag is really there
    assert np.array_equal(_u8(imageio.read(tif)), arr)


def test_apply_orientation_is_identity_for_one_and_for_junk():
    a = _array(8, 4)
    for value in (1, 0, 9, -1):
        assert imageio.apply_orientation(a, value) is a


# ---------------------------------------------------------------------------- header-first size checks


def test_declared_size_comes_from_the_header_of_png_jpeg_and_tiff(tmp_path):
    arr = _array()
    Image.fromarray(arr).save(tmp_path / "a.png")
    assert imageio.declared_size(tmp_path / "a.png") == (WIDTH, HEIGHT)
    assert imageio.declared_size(_jpeg(tmp_path / "a.jpg", arr)) == (WIDTH, HEIGHT)
    tifffile.imwrite(str(tmp_path / "a.tif"), arr, photometric="rgb")
    assert imageio.declared_size(tmp_path / "a.tif") == (WIDTH, HEIGHT)
    assert imageio.declared_size(_png_with_header(tmp_path / "big.png", 11000, 9000)) == (11000, 9000)


def test_a_tiff_whose_first_ifd_is_past_the_first_mebibyte_is_sized_through_tifffile(tmp_path):
    rng = np.random.default_rng(7)
    big = rng.integers(0, 65536, (700, 900, 3), dtype=np.uint16)  # 3.7 MB of incompressible strips
    path = tmp_path / "big.tif"
    tifffile.imwrite(str(path), big, photometric="rgb")
    assert path.stat().st_size > 2 * imageio.HEADER_PROBE_BYTES
    assert imageio.declared_size(path) == (900, 700)


def test_declared_size_is_none_for_a_file_that_is_not_an_image(tmp_path):
    (tmp_path / "junk.png").write_bytes(b"not an image at all")
    (tmp_path / "empty.png").write_bytes(b"")
    assert imageio.declared_size(tmp_path / "junk.png") is None
    assert imageio.declared_size(tmp_path / "empty.png") is None


def test_check_pixel_budget_refuses_from_the_header_and_read_never_decodes(tmp_path, monkeypatch):
    bomb = _png_with_header(tmp_path / "bomb.png", 11000, 11000)
    with pytest.raises(imageio.TooManyPixels) as e:
        imageio.check_pixel_budget(bomb, 100_000_000)
    assert (e.value.width, e.value.height, e.value.limit) == (11000, 11000, 100_000_000)
    assert isinstance(e.value, ValueError) and str(e.value).startswith("too_many_pixels:")

    def never(*_a, **_k):
        raise AssertionError("the decoder ran on a file over the pixel budget")

    monkeypatch.setattr(imageio, "_decode_cv", never)
    with pytest.raises(imageio.TooManyPixels):
        imageio.read(bomb, max_pixels=100_000_000)
    # exactly at the budget passes the check (the header is a valid size)
    assert imageio.check_pixel_budget(bomb, 11000 * 11000) == (11000, 11000)


def test_read_with_a_budget_still_reads_a_small_image(tmp_path):
    path = _jpeg(tmp_path / "ok.jpg", _array())
    image = imageio.read(path, max_pixels=WIDTH * HEIGHT)
    assert image.rgb.shape[:2] == (HEIGHT, WIDTH)
    with pytest.raises(imageio.TooManyPixels):
        imageio.read(path, max_pixels=WIDTH * HEIGHT - 1)
