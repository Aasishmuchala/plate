"""`plate_pixels.imghdr`: an engine result's container and declared size from its first bytes (router plan T130).

Every header here is built by hand, byte for byte, so the parser is checked against the format and not against
an encoder; real encodes (cv2, PIL) are a second check where the library writes the format. The last block is
`ingress.decode_result` refusing an empty, undecodable or over-cap result before it allocates anything.
"""

from __future__ import annotations

import io
import struct
import tracemalloc
import zlib

import cv2
import numpy as np
import pytest
from PIL import Image

from plate_pixels import imghdr, ingress
from plate_pixels.imghdr import DEFAULT_MAX_PIXELS, MAX_PIXELS_ENV, header_size, magic, max_pixels

# ---------------------------------------------------------------------------- hand-built headers


def png_header(w: int, h: int, *, depth: int = 8, colour: int = 2) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, depth, colour, 0, 0, 0)
    crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + crc


def png_bomb(w: int, h: int) -> bytes:
    """A complete, valid 1-bit greyscale PNG of w x h zeros: tiny on disk, w * h pixels once decoded."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    row = b"\x00" * (1 + (w + 7) // 8)
    z = zlib.compressobj(9)
    idat = b"".join(z.compress(row) for _ in range(h)) + z.flush()
    ihdr = struct.pack(">IIBBBBB", w, h, 1, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def jpeg_header(w: int, h: int, sof: int = 0xC0, *, app_bytes: int = 14) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", app_bytes + 2) + b"J" * app_bytes
    dqt = b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + bytes(64)
    sof_seg = (
        bytes([0xFF, sof]) + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + dqt + sof_seg + b"\xff\xda\x00\x08"


def riff(chunk: bytes, payload: bytes) -> bytes:
    body = b"WEBP" + chunk + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_vp8(w: int, h: int) -> bytes:
    frame = b"\x9d\x01\x2a" + struct.pack(
        "<HH", w | (1 << 14), h | (2 << 14)
    )  # scale bits must be masked off
    return riff(b"VP8 ", b"\x10\x02\x00" + frame + bytes(8))


def webp_vp8l(w: int, h: int) -> bytes:
    bits = (w - 1) | ((h - 1) << 14) | (1 << 28)  # alpha hint and version bits above the sizes
    return riff(b"VP8L", b"\x2f" + bits.to_bytes(4, "little") + bytes(8))


def webp_vp8x(w: int, h: int) -> bytes:
    return riff(b"VP8X", b"\x10\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little"))


def bmp_header(w: int, h: int, *, core: bool = False) -> bytes:
    if core:
        info = struct.pack("<IHHHH", 12, w, h, 1, 24)
    else:
        info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, 0, 2835, 2835, 0, 0)
    return b"BM" + struct.pack("<IHHI", 14 + len(info), 0, 0, 14 + len(info)) + info


def tiff_header(w: int, h: int, *, order: str = "<", short: bool = False, extra_first: bool = True) -> bytes:
    mark = b"II*\x00" if order == "<" else b"MM\x00*"
    entries = []
    if extra_first:
        entries.append(struct.pack(order + "HHII", 254, 4, 1, 0))  # NewSubfileType before the sizes
    for tag, value in ((256, w), (257, h)):
        if short:
            entries.append(struct.pack(order + "HHIH", tag, 3, 1, value) + b"\x00\x00")
        else:
            entries.append(struct.pack(order + "HHII", tag, 4, 1, value))
    ifd = struct.pack(order + "H", len(entries)) + b"".join(entries) + struct.pack(order + "I", 0)
    return mark + struct.pack(order + "I", 8) + ifd


def bigtiff_header(w: int, h: int, order: str = "<") -> bytes:
    """BigTIFF: 8-byte offsets, 20-byte entries; width as LONG8, height as a LONG left-justified in 8 bytes."""
    mark = b"II+\x00" if order == "<" else b"MM\x00+"
    entries = [
        struct.pack(order + "HHQQ", 256, 16, 1, w),
        struct.pack(order + "HHQI", 257, 4, 1, h) + bytes(4),
    ]
    ifd = struct.pack(order + "Q", len(entries)) + b"".join(entries) + struct.pack(order + "Q", 0)
    return mark + struct.pack(order + "HH", 8, 0) + struct.pack(order + "Q", 16) + ifd


# ---------------------------------------------------------------------------- magic


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (png_header(4, 3), "png"),
        (jpeg_header(4, 3), "jpeg"),
        (webp_vp8(4, 3), "webp"),
        (webp_vp8l(4, 3), "webp"),
        (bmp_header(4, 3), "bmp"),
        (tiff_header(4, 3), "tiff"),
        (tiff_header(4, 3, order=">"), "tiff"),
        (bigtiff_header(4, 3), "tiff"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2", "mp4"),
        (b"\x00\x00\x00\x1cftypqt  ", "mp4"),
        (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01", "webm"),
    ],
)
def test_magic_names_every_container_plate_takes(data, kind):
    assert magic(data) == kind


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x89PN",
        b"<!DOCTYPE html><html><body>502 Bad Gateway</body></html>",
        b'{"error": {"code": 429, "message": "quota"}}',
        b"GIF89a\x01\x00\x01\x00",
        b"RIFF\x00\x00\x00\x00WAVEfmt ",
        b"\x00" * 64,
        b"PLATE-FAKE-RESULT fake-000001 key\n",
    ],
)
def test_anything_else_has_no_magic(data):
    assert magic(data) is None and header_size(data) is None


# ---------------------------------------------------------------------------- header_size


@pytest.mark.parametrize(
    ("data", "size"),
    [
        (png_header(640, 360), (640, 360)),
        (png_header(16000, 16000, depth=1, colour=0), (16000, 16000)),
        (jpeg_header(1920, 1080), (1920, 1080)),
        (jpeg_header(1920, 1080, 0xC1), (1920, 1080)),
        (jpeg_header(4000, 3000, 0xC2), (4000, 3000)),
        (jpeg_header(321, 123, 0xC3, app_bytes=4000), (321, 123)),  # a big APP segment before the frame
        (webp_vp8(1024, 768), (1024, 768)),
        (webp_vp8l(5, 16383), (5, 16383)),
        (webp_vp8x(16000, 9000), (16000, 9000)),
        (bmp_header(800, 600), (800, 600)),
        (bmp_header(800, -600), (800, 600)),  # top-down
        (bmp_header(300, 200, core=True), (300, 200)),
        (tiff_header(7000, 5000), (7000, 5000)),
        (tiff_header(7000, 5000, order=">"), (7000, 5000)),
        (tiff_header(640, 480, short=True), (640, 480)),
        (tiff_header(640, 480, order=">", short=True, extra_first=False), (640, 480)),
        (bigtiff_header(70000, 50000), (70000, 50000)),
        (bigtiff_header(70000, 50000, ">"), (70000, 50000)),
    ],
)
def test_header_size_reads_the_declared_size_without_decoding(data, size):
    assert header_size(data) == size


@pytest.mark.parametrize(
    "data",
    [
        png_header(640, 360)[:20],  # IHDR cut short
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIDAT" + bytes(8),  # first chunk is not IHDR
        png_header(0, 360),  # a zero side is not an image
        b"\xff\xd8\xff\xe0\x00\x10JFIF",  # no frame header yet
        b"\xff\xd8\xff\xda\x00\x08" + bytes(8),  # scan before any frame
        b"\xff\xd8\xff\xd9",  # end of image at once
        jpeg_header(0, 0),
        riff(b"VP8 ", bytes(20)),  # no start code
        riff(b"VP8L", b"\x00" + bytes(12)),  # wrong signature byte
        riff(b"ALPH", bytes(20)),
        b"BM" + bytes(30),  # no known info header size
        b"II*\x00" + struct.pack("<I", 4096) + bytes(8),  # IFD past the end
        tiff_header(640, 480)[:20],  # entries cut short
        b"II*\x00"
        + struct.pack("<I", 8)
        + struct.pack("<H", 1)
        + struct.pack("<HHII", 256, 4, 1, 640),  # no 257
        b"II*\x00"
        + struct.pack("<I", 8)
        + struct.pack("<H", 2)
        + struct.pack("<HHII", 256, 2, 1, 640)
        + struct.pack("<HHII", 257, 4, 1, 480),  # ASCII width
    ],
)
def test_a_truncated_or_malformed_header_is_none_never_an_exception(data):
    assert header_size(data) is None


def test_header_size_never_raises_on_noise():
    rng = np.random.default_rng(7)
    prefixes = [
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"RIFF\x00\x00\x00\x00WEBP",
        b"BM",
        b"II*\x00",
        b"MM\x00*",
        b"II+\x00",
        b"MM\x00+",
    ]
    for i in range(400):
        tail = rng.integers(0, 256, int(rng.integers(0, 80)), dtype=np.uint8).tobytes()
        got = header_size(prefixes[i % len(prefixes)] + tail)
        assert got is None or (isinstance(got, tuple) and got[0] > 0 and got[1] > 0)


@pytest.mark.parametrize("ext", [".png", ".jpg", ".webp", ".bmp", ".tiff"])
def test_header_size_agrees_with_real_encodes(ext):
    img = np.zeros((37, 53, 3), np.uint8)
    ok, enc = cv2.imencode(ext, img)
    assert ok
    data = bytes(enc)
    assert (
        magic(data) == {".png": "png", ".jpg": "jpeg", ".webp": "webp", ".bmp": "bmp", ".tiff": "tiff"}[ext]
    )
    assert header_size(data) == (53, 37)


@pytest.mark.parametrize(
    ("fmt", "kwargs"), [("WEBP", {"lossless": True}), ("TIFF", {}), ("JPEG", {"progressive": True})]
)
def test_header_size_agrees_with_pil_encodes(fmt, kwargs):
    buf = io.BytesIO()
    Image.new("RGB", (29, 41)).save(buf, fmt, **kwargs)
    assert header_size(buf.getvalue()) == (29, 41)


# ---------------------------------------------------------------------------- the cap


def test_max_pixels_defaults_and_follows_the_environment(monkeypatch):
    monkeypatch.delenv(MAX_PIXELS_ENV, raising=False)
    assert MAX_PIXELS_ENV == "PLATE_RESULT_MAX_PIXELS" and DEFAULT_MAX_PIXELS == 64_000_000
    assert max_pixels() == 64_000_000
    monkeypatch.setenv(MAX_PIXELS_ENV, "1000000")
    assert max_pixels() == 1_000_000
    monkeypatch.setenv(MAX_PIXELS_ENV, "100_000_000")
    assert max_pixels() == 100_000_000


@pytest.mark.parametrize("bad", ["zero", "0", "-5", "1.5e9", " "])
def test_a_bad_cap_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv(MAX_PIXELS_ENV, bad)
    assert max_pixels() == DEFAULT_MAX_PIXELS


def test_the_module_is_the_package_one_not_the_removed_stdlib_imghdr():
    assert imghdr.__name__ == "plate_pixels.imghdr" and hasattr(imghdr, "header_size")


# ---------------------------------------------------------------------------- ingress.decode_result


def test_decode_result_refuses_empty_bytes():
    with pytest.raises(ingress.IngressError, match=r"^engine result is empty \(0 bytes\)$"):
        ingress.decode_result(b"")


def test_decode_result_names_length_and_magic_of_an_undecodable_blob():
    with pytest.raises(
        ingress.IngressError, match=r"cannot decode the engine result \(13 bytes, magic unknown\)"
    ):
        ingress.decode_result(b"<html>oops</>")
    truncated = png_header(64, 64)  # a PNG signature and header, no pixels
    with pytest.raises(ingress.IngressError, match=rf"\({len(truncated)} bytes, magic png\)"):
        ingress.decode_result(truncated)


def test_decode_result_refuses_a_decompression_bomb_before_allocating():
    bomb = png_bomb(16000, 16000)
    assert len(bomb) < 1_000_000 and header_size(bomb) == (16000, 16000)
    tracemalloc.start()
    try:
        with pytest.raises(ingress.IngressError) as e:
            ingress.decode_result(bomb)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert str(e.value) == (
        "engine result declares 16000x16000 = 256000000 px; the cap is 64000000 (PLATE_RESULT_MAX_PIXELS)"
    )
    assert peak < 20 * 1024 * 1024  # a decode would have cost hundreds of MB


def test_decode_result_cap_is_a_keyword_and_follows_the_environment(monkeypatch):
    small = cv2.imencode(".png", np.zeros((10, 20, 3), np.uint8))[1].tobytes()
    assert ingress.decode_result(small).shape == (10, 20, 3)
    with pytest.raises(ingress.IngressError, match=r"declares 20x10 = 200 px; the cap is 199"):
        ingress.decode_result(small, max_pixels=199)
    monkeypatch.setenv(MAX_PIXELS_ENV, "150")
    with pytest.raises(ingress.IngressError, match=r"the cap is 150"):
        ingress.decode_result(small)
    assert ingress.decode_result(small, max_pixels=200).shape == (10, 20, 3)


def test_decode_result_still_takes_arrays_unchecked():
    arr = np.zeros((3, 4, 3), np.uint8)
    assert ingress.decode_result(arr, max_pixels=1).shape == (3, 4, 3)
