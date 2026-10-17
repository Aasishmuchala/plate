"""ICC profile and PNG chunk helpers.

``SRGB_ICC`` is a minimal sRGB ICC v2.1 matrix/TRC profile generated in code (D50-adapted colorants, a
1024-entry ``curv`` table of the sRGB transfer curve), so no profile file is downloaded or committed. The PNG
helpers read and embed the ``iCCP`` chunk and strip every ancillary chunk that can carry client metadata
(tEXt, iTXt, zTXt, eXIf, tIME, ...) before a PNG leaves the box (BUILD_PLAN section 5, egress).
"""

from __future__ import annotations

import hashlib
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Chunks that survive strip_metadata_png: the image itself plus colour description. tRNS is kept as well:
# it is pixel data (palette transparency), not metadata, and dropping it would change what the PNG shows.
PNG_KEEP_CHUNKS = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND", b"iCCP", b"sRGB", b"gAMA", b"cHRM", b"tRNS"})

_CURVE_POINTS = 1024


# --------------------------------------------------------------------------- the generated sRGB profile


def _s15(x: float) -> bytes:
    return struct.pack(">i", int(round(x * 65536.0)))


def _xyz_tag(x: float, y: float, z: float) -> bytes:
    return b"XYZ \x00\x00\x00\x00" + _s15(x) + _s15(y) + _s15(z)


def _srgb_eotf(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _curv_tag() -> bytes:
    table = [int(round(65535.0 * _srgb_eotf(i / (_CURVE_POINTS - 1)))) for i in range(_CURVE_POINTS)]
    return (
        b"curv\x00\x00\x00\x00" + struct.pack(">I", _CURVE_POINTS) + struct.pack(f">{_CURVE_POINTS}H", *table)
    )


def _text_tag(text: str) -> bytes:
    return b"text\x00\x00\x00\x00" + text.encode("ascii") + b"\x00"


def _desc_tag(text: str) -> bytes:
    """ICC v2 textDescriptionType: ASCII description, empty Unicode and ScriptCode parts."""
    raw = text.encode("ascii") + b"\x00"
    return (
        b"desc\x00\x00\x00\x00"
        + struct.pack(">I", len(raw))
        + raw
        + struct.pack(">II", 0, 0)  # unicode language code, unicode count
        + struct.pack(">HB", 0, 0)  # scriptcode code, macintosh count
        + b"\x00" * 67
    )


def _pad4(b: bytes) -> bytes:
    return b + b"\x00" * (-len(b) % 4)


def build_srgb_profile() -> bytes:
    """Build the minimal sRGB v2.1 display profile. Deterministic: a fixed creation date and no clock."""
    wtpt = _xyz_tag(0.9642, 1.0, 0.8249)
    trc = _curv_tag()
    # (signature, data); the three TRC tags share one data block.
    blocks: list[tuple[bytes, bytes]] = [
        (b"cprt", _text_tag("Public domain")),
        (b"desc", _desc_tag("sRGB IEC61966-2.1 (PLATE minimal)")),
        (b"wtpt", wtpt),
        (b"rXYZ", _xyz_tag(0.43607, 0.22249, 0.01392)),
        (b"gXYZ", _xyz_tag(0.38515, 0.71687, 0.09708)),
        (b"bXYZ", _xyz_tag(0.14307, 0.06061, 0.71410)),
        (b"rTRC", trc),
        (b"gTRC", trc),
        (b"bTRC", trc),
    ]
    table_size = 4 + 12 * len(blocks)
    offset = 128 + table_size
    entries = b""
    body = b""
    placed: dict[bytes, int] = {}
    for sig, data in blocks:
        if data in placed:
            off = placed[data]
        else:
            off = offset + len(body)
            placed[data] = off
            body += _pad4(data)
        entries += sig + struct.pack(">II", off, len(data))
    tag_area = struct.pack(">I", len(blocks)) + entries + body
    size = 128 + len(tag_area)

    header = bytearray(128)
    header[0:4] = struct.pack(">I", size)
    header[8:12] = b"\x02\x10\x00\x00"  # v2.1.0
    header[12:16] = b"mntr"
    header[16:20] = b"RGB "
    header[20:24] = b"XYZ "
    header[24:36] = struct.pack(">6H", 2026, 10, 17, 0, 0, 0)
    header[36:40] = b"acsp"
    header[64:68] = struct.pack(">I", 0)  # perceptual
    header[68:80] = _s15(0.9642) + _s15(1.0) + _s15(0.8249)  # D50 illuminant
    # Profile ID: MD5 of the whole profile with the ID field (and flags, intent) zeroed; those are all zero here.
    header[84:100] = hashlib.md5(bytes(header) + tag_area).digest()
    return bytes(header) + tag_area


SRGB_ICC: bytes = build_srgb_profile()


# --------------------------------------------------------------------------- PNG chunks


def iter_png_chunks(data: bytes):
    """Yield (type, payload) for every chunk. Raises ValueError for a non-PNG or a truncated file."""
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG (bad signature)")
    pos = 8
    n = len(data)
    while pos < n:
        if pos + 8 > n:
            raise ValueError("truncated PNG chunk header")
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        end = pos + 8 + length + 4
        if end > n:
            raise ValueError(f"truncated PNG chunk {ctype!r}")
        yield ctype, data[pos + 8 : pos + 8 + length]
        pos = end


def _chunk(ctype: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)


def png_chunk_types(data: bytes) -> list[str]:
    """Chunk type names in file order (tests and the egress audit use this)."""
    return [c.decode("ascii") for c, _ in iter_png_chunks(data)]


def strip_metadata_png(data: bytes) -> bytes:
    """Keep only the image and colour chunks (``PNG_KEEP_CHUNKS``); drop tEXt/iTXt/zTXt/eXIf/tIME and the rest."""
    out = [PNG_SIGNATURE]
    for ctype, payload in iter_png_chunks(data):
        if ctype in PNG_KEEP_CHUNKS:
            out.append(_chunk(ctype, payload))
    return b"".join(out)


def read_png_icc(data: bytes) -> bytes | None:
    """The embedded ICC profile of a PNG (decompressed ``iCCP`` payload), or None."""
    for ctype, payload in iter_png_chunks(data):
        if ctype == b"iCCP":
            sep = payload.find(b"\x00")
            if sep < 1 or sep + 2 > len(payload):
                return None
            if payload[sep + 1] != 0:  # compression method must be 0 (zlib)
                return None
            try:
                return zlib.decompress(payload[sep + 2 :])
            except zlib.error:
                return None
        if ctype in (b"IDAT", b"IEND"):
            break  # iCCP must precede IDAT
    return None


def embed_png_icc(data: bytes, icc: bytes, name: str = "ICC") -> bytes:
    """Return the PNG with ``icc`` as its only colour-profile chunk (replaces any iCCP and sRGB chunk).

    The chunk goes right after IHDR, which is where the PNG spec requires it (before PLTE and IDAT).
    """
    keyword = name.encode("latin-1")[:79] or b"ICC"
    iccp = _chunk(b"iCCP", keyword + b"\x00\x00" + zlib.compress(icc, 9))
    out = [PNG_SIGNATURE]
    for ctype, payload in iter_png_chunks(data):
        if ctype in (b"iCCP", b"sRGB"):
            continue
        out.append(_chunk(ctype, payload))
        if ctype == b"IHDR":
            out.append(iccp)
    return b"".join(out)
