"""What an engine result is and how big it says it is, from its first bytes alone (router plan T130, decision 76).

``magic(data)`` names the container: ``png``, ``jpeg``, ``webp``, ``bmp``, ``tiff`` (both byte orders, BigTIFF
too), ``mp4`` (an ISO-BMFF ``ftyp`` box at offset 4) or ``webm`` (the EBML magic ``1A 45 DF A3``); anything else
is None. ``header_size(data)`` reads ``(width, height)`` from the header without decoding a pixel: the PNG
``IHDR``, the JPEG start-of-frame marker (SOF0/1/2 and the other SOFn, which share its layout), the WebP
``VP8 `` / ``VP8L`` / ``VP8X`` chunk, the BMP info header and the TIFF first IFD (tags 256 and 257); a clip, an
unknown container or a truncated or malformed header is None. Neither function raises on any input.

A vendor result is checked against ``max_pixels()`` before anything decodes it: a 775 KB solid-colour PNG can
declare 16000 x 16000 (about 7.7 GB once decoded to float), so the declared size is what is capped. The cap is
``PLATE_RESULT_MAX_PIXELS`` (default 64 000 000, i.e. 8000 x 8000); a value that is not a positive integer is
ignored with a warning.
"""

from __future__ import annotations

import logging
import os
import struct

__all__ = [
    "DEFAULT_MAX_PIXELS",
    "IMAGE_KINDS",
    "MAX_PIXELS_ENV",
    "VIDEO_KINDS",
    "header_size",
    "magic",
    "max_pixels",
]

log = logging.getLogger(__name__)

MAX_PIXELS_ENV = "PLATE_RESULT_MAX_PIXELS"
DEFAULT_MAX_PIXELS = 64_000_000

IMAGE_KINDS = frozenset({"png", "jpeg", "webp", "bmp", "tiff"})
VIDEO_KINDS = frozenset({"mp4", "webm"})

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TIFF_MAGICS = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
# Start-of-frame markers: SOF0 (baseline), SOF1, SOF2 (progressive) and the rarer lossless / arithmetic ones.
# All carry precision (1 byte), height (2), width (2) right after the segment length.
_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
# Markers with no length field: TEM, RST0-7, SOI, EOI.
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8), 0xD8, 0xD9})
_BMP_INFO_SIZES = frozenset({12, 16, 40, 52, 56, 64, 108, 124})


def magic(data: bytes | bytearray | memoryview) -> str | None:
    """The container of ``data`` by its signature, or None when it is not one PLATE takes as a result."""
    b = bytes(data[:16])
    if b.startswith(PNG_SIGNATURE):
        return "png"
    if b.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b.startswith(b"BM"):
        return "bmp"
    if b[:4] in _TIFF_MAGICS:
        return "tiff"
    if len(b) >= 8 and b[4:8] == b"ftyp":
        return "mp4"
    if b.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    return None


def header_size(data: bytes | bytearray | memoryview) -> tuple[int, int] | None:
    """``(width, height)`` declared by an image header, or None (a clip, an unknown format, a bad header)."""
    b = bytes(data) if not isinstance(data, bytes) else data
    try:
        kind = magic(b)
        if kind == "png":
            size = _png(b)
        elif kind == "jpeg":
            size = _jpeg(b)
        elif kind == "webp":
            size = _webp(b)
        elif kind == "bmp":
            size = _bmp(b)
        elif kind == "tiff":
            size = _tiff(b)
        else:
            return None
    except (struct.error, IndexError, ValueError):
        return None
    if size is None or size[0] <= 0 or size[1] <= 0:
        return None
    return int(size[0]), int(size[1])


def max_pixels() -> int:
    """The declared-pixel cap for an engine result: ``PLATE_RESULT_MAX_PIXELS``, else 64 000 000."""
    raw = os.environ.get(MAX_PIXELS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_PIXELS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        log.warning("%s=%r is not a positive integer; using %d", MAX_PIXELS_ENV, raw, DEFAULT_MAX_PIXELS)
        return DEFAULT_MAX_PIXELS
    return value


# ---------------------------------------------------------------------------- the formats


def _png(b: bytes) -> tuple[int, int] | None:
    # The signature, then the IHDR chunk: length (4), type (4), width (4), height (4).
    if len(b) < 24 or b[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", b[16:24])


def _jpeg(b: bytes) -> tuple[int, int] | None:
    i, n = 2, len(b)
    while i < n:
        if b[i] != 0xFF:
            return None  # not at a marker: the stream is not a well-formed JPEG header
        while i < n and b[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            return None
        marker = b[i]
        i += 1
        if marker in _JPEG_STANDALONE:
            if marker == 0xD9:
                return None  # end of image before any frame header
            continue
        if marker == 0xDA:
            return None  # start of scan before any frame header
        if i + 2 > n:
            return None
        (length,) = struct.unpack(">H", b[i : i + 2])
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            if i + 7 > n:
                return None
            h, w = struct.unpack(">HH", b[i + 3 : i + 7])
            return w, h
        i += length
    return None


def _webp(b: bytes) -> tuple[int, int] | None:
    if len(b) < 30:
        return None
    chunk = b[12:16]
    if chunk == b"VP8 ":
        # Frame tag (3 bytes), start code 9D 01 2A, then 14-bit width and height (2 scale bits each).
        if b[23:26] != b"\x9d\x01\x2a":
            return None
        w, h = struct.unpack("<HH", b[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L":
        if b[20] != 0x2F:
            return None
        bits = int.from_bytes(b[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        # Flags (1), reserved (3), canvas width - 1 (24-bit), canvas height - 1 (24-bit).
        return int.from_bytes(b[24:27], "little") + 1, int.from_bytes(b[27:30], "little") + 1
    return None


def _bmp(b: bytes) -> tuple[int, int] | None:
    if len(b) < 26:
        return None
    (info,) = struct.unpack("<I", b[14:18])
    if info not in _BMP_INFO_SIZES:
        return None
    if info == 12:  # BITMAPCOREHEADER: 16-bit unsigned width and height
        return struct.unpack("<HH", b[18:22])
    w, h = struct.unpack("<ii", b[18:26])
    return w, abs(h)  # a negative height is a top-down bitmap


def _tiff(b: bytes) -> tuple[int, int] | None:
    order = "<" if b[:2] == b"II" else ">"
    big = b[2:4] in (b"+\x00", b"\x00+")
    if big:
        (ifd,) = struct.unpack(order + "Q", b[8:16])
        (count,) = struct.unpack(order + "Q", b[ifd : ifd + 8])
        first, entry, value_at = ifd + 8, 20, 12
    else:
        (ifd,) = struct.unpack(order + "I", b[4:8])
        (count,) = struct.unpack(order + "H", b[ifd : ifd + 2])
        first, entry, value_at = ifd + 2, 12, 8
    if count <= 0 or first + count * entry > len(b):
        return None
    found: dict[int, int] = {}
    for k in range(count):
        e = first + k * entry
        tag, typ = struct.unpack(order + "HH", b[e : e + 4])
        if tag not in (256, 257):
            continue
        v = b[e + value_at : e + value_at + 8]
        if typ == 3:  # SHORT, left-justified in the value field
            (found[tag],) = struct.unpack(order + "H", v[:2])
        elif typ == 4:  # LONG
            (found[tag],) = struct.unpack(order + "I", v[:4])
        elif typ == 16 and big:  # LONG8 (BigTIFF)
            (found[tag],) = struct.unpack(order + "Q", v[:8])
        else:
            return None
        if len(found) == 2:
            break
    if 256 not in found or 257 not in found:
        return None
    return found[256], found[257]
