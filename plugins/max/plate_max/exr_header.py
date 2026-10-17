"""Reads what Send needs out of V-Ray's multilayer EXR with the standard library only: the header
(attributes, channel list, compression, data window), the Cryptomatte manifests V-Ray writes into
it, and the rank-0 Cryptomatte coverage per ID that becomes each legend entry's pixel_share.

Max's Python has no OpenEXR module, so this is a small parser of the OpenEXR 2 file layout: the
magic number 0x762f3101, the version word (2, plus the tiled / long-name / deep / multi-part flag
bits), then each part's attributes (name\\0 type\\0 int32 size, value) until an empty name; a
multi-part file repeats that per part and ends the list with one more empty name. Cryptomatte
manifests are the string attributes cryptomatte/<key>/manifest next to cryptomatte/<key>/name
(the layer, e.g. CryptoObject), /hash (MurmurHash3_32) and /conversion (uint32_to_float32).

rank0_coverage() decodes only the two channels it needs (<layer>00.R, the rank-0 ID as float32
bits, and <layer>00.G, its coverage) from scanline chunks stored without compression, ZIPS or ZIP
(zlib, then OpenEXR's byte predictor and even/odd split, undone here for just those byte ranges).
That is what render.py asks V-Ray for (ZIP); any other compression, tiles, deep data or
subsampled channels raise CoverageUnsupported with the reason, so Send can stop instead of
writing a legend with invented shares. Cost: one pass over the file (zlib in C, a running byte sum
in C, a Python loop over the two channels): about 8 s for a 3840x2160 EXR with the default element
set (976 MB of incompressible test data, Python 3.11 on an M-series Mac). Nothing here imports pymxs.
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from array import array
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import accumulate, repeat
from operator import and_
from pathlib import Path
from typing import Any, BinaryIO

MAGIC = b"\x76\x2f\x31\x01"
TILED_FLAG = 0x200
LONG_NAMES_FLAG = 0x400
NON_IMAGE_FLAG = 0x800  # deep data
MULTIPART_FLAG = 0x1000

# OpenEXR's compression enum (ImfCompression.h, OpenEXR 3.4).
COMPRESSION_NAMES = {
    0: "none",
    1: "rle",
    2: "zips",
    3: "zip",
    4: "piz",
    5: "pxr24",
    6: "b44",
    7: "b44a",
    8: "dwaa",
    9: "dwab",
    10: "htj2k256",
    11: "htj2k32",
    12: "lj2k",
    13: "zstd",
}
LINES_PER_BLOCK = {0: 1, 1: 1, 2: 1, 3: 16, 4: 32, 5: 16, 6: 32, 7: 32, 8: 32, 9: 256}
DECODABLE = (0, 2, 3)  # none, zips, zip: what rank0_coverage reads
PIXEL_TYPES = {0: "uint", 1: "half", 2: "float"}
PIXEL_SIZES = {0: 4, 1: 2, 2: 4}

MAX_NAME = 256  # attribute names are at most 255 bytes with the long-names flag (31 without)
CRYPTO_PREFIX = "cryptomatte/"
CRYPTO_HASH = "MurmurHash3_32"
CRYPTO_CONVERSION = "uint32_to_float32"

_TO_SIGNED = bytes((b - 128) & 0xFF for b in range(256))  # predictor bytes d -> d - 128 (mod 256)


class ExrHeaderError(ValueError):
    """The file is not an OpenEXR file this parser can read (bad magic, truncated header)."""


class CoverageUnsupported(ValueError):
    """The EXR stores the Cryptomatte channels in a way the stdlib reader cannot decode."""


# --------------------------------------------------------------------------- header model


@dataclass(frozen=True)
class Channel:
    name: str
    pixel_type: int
    linear: bool = False
    x_sampling: int = 1
    y_sampling: int = 1

    @property
    def dtype(self) -> str:
        """uint | half | float (the manifest's elements[].dtype uses the last two)."""
        return PIXEL_TYPES.get(self.pixel_type, f"type{self.pixel_type}")

    @property
    def size(self) -> int:
        return PIXEL_SIZES.get(self.pixel_type, 4)

    @property
    def layer(self) -> str:
        """Everything before the last '.', "" for a bare channel (the beauty's R, G, B, A)."""
        return self.name.rsplit(".", 1)[0] if "." in self.name else ""

    @property
    def suffix(self) -> str:
        return self.name.rsplit(".", 1)[1] if "." in self.name else self.name


@dataclass
class ExrPart:
    """One part's attributes, decoded where the type is known (raw bytes otherwise)."""

    attributes: dict[str, Any] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)

    @property
    def channels(self) -> list[Channel]:
        return list(self.attributes.get("channels") or [])

    @property
    def compression(self) -> int | None:
        value = self.attributes.get("compression")
        return int(value) if isinstance(value, int) else None

    @property
    def compression_name(self) -> str:
        code = self.compression
        return COMPRESSION_NAMES.get(code, f"unknown({code})") if code is not None else "unknown"

    @property
    def data_window(self) -> tuple[int, int, int, int] | None:
        box = self.attributes.get("dataWindow")
        return tuple(box) if isinstance(box, tuple) and len(box) == 4 else None  # type: ignore[return-value]

    @property
    def width(self) -> int | None:
        box = self.data_window
        return box[2] - box[0] + 1 if box else None

    @property
    def height(self) -> int | None:
        box = self.data_window
        return box[3] - box[1] + 1 if box else None

    @property
    def name(self) -> str | None:
        value = self.attributes.get("name")
        return value if isinstance(value, str) else None

    @property
    def storage(self) -> str | None:
        """The multi-part "type" attribute: scanlineimage, tiledimage, deepscanline, deeptile."""
        value = self.attributes.get("type")
        return value if isinstance(value, str) else None

    @property
    def chunk_count(self) -> int | None:
        value = self.attributes.get("chunkCount")
        return int(value) if isinstance(value, int) else None


@dataclass
class ExrHeader:
    path: str
    version: int
    flags: int
    parts: list[ExrPart]
    offsets_at: int  # file position of the first part's chunk offset table

    @property
    def multipart(self) -> bool:
        return bool(self.flags & MULTIPART_FLAG)

    @property
    def tiled(self) -> bool:
        return bool(self.flags & TILED_FLAG) or any(
            p.storage in ("tiledimage", "deeptile") for p in self.parts
        )

    @property
    def deep(self) -> bool:
        return bool(self.flags & NON_IMAGE_FLAG) or any(
            p.storage in ("deepscanline", "deeptile") for p in self.parts
        )

    @property
    def attributes(self) -> dict[str, Any]:
        """Every part's attributes merged (the first part wins): where Cryptomatte metadata lives."""
        merged: dict[str, Any] = {}
        for part in reversed(self.parts):
            merged.update(part.attributes)
        return merged

    @property
    def channels(self) -> list[Channel]:
        return [ch for part in self.parts for ch in part.channels]

    def find_channel(self, name: str) -> tuple[int, Channel] | None:
        for index, part in enumerate(self.parts):
            for ch in part.channels:
                if ch.name == name:
                    return index, ch
        return None

    @property
    def compression_name(self) -> str:
        """The first part's compression ("zip", "piz", ...): the manifest's files.beauty_exr.compression."""
        return self.parts[0].compression_name if self.parts else "unknown"

    @property
    def width(self) -> int | None:
        return self.parts[0].width if self.parts else None

    @property
    def height(self) -> int | None:
        return self.parts[0].height if self.parts else None


# --------------------------------------------------------------------------- attribute decoding


def _cstr(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _chlist(data: bytes) -> list[Channel]:
    out: list[Channel] = []
    pos = 0
    while pos < len(data):
        end = data.find(b"\x00", pos)
        if end < 0 or end == pos:  # the list ends with an empty name
            break
        name = data[pos:end].decode("utf-8", errors="replace")
        pos = end + 1
        if pos + 16 > len(data):
            raise ExrHeaderError(f"channel list truncated at {name!r}")
        pixel_type, linear, xs, ys = struct.unpack_from("<iB3xii", data, pos)
        pos += 16
        out.append(Channel(name, pixel_type, bool(linear), xs, ys))
    return out


def _decode(kind: str, data: bytes) -> Any:
    """The value of one attribute; unknown types (and malformed known ones) come back as bytes."""
    try:
        if kind == "string":
            return data.decode("utf-8", errors="replace")
        if kind == "int":
            return struct.unpack("<i", data)[0]
        if kind == "float":
            return struct.unpack("<f", data)[0]
        if kind == "double":
            return struct.unpack("<d", data)[0]
        if kind in ("compression", "lineOrder", "envmap", "deepImageState"):
            return data[0]
        if kind == "box2i":
            return struct.unpack("<4i", data)
        if kind == "box2f":
            return struct.unpack("<4f", data)
        if kind == "v2i":
            return struct.unpack("<2i", data)
        if kind == "v2f":
            return struct.unpack("<2f", data)
        if kind == "v3i":
            return struct.unpack("<3i", data)
        if kind == "v3f":
            return struct.unpack("<3f", data)
        if kind == "m33f":
            return struct.unpack("<9f", data)
        if kind == "m44f":
            return struct.unpack("<16f", data)
        if kind == "rational":
            return struct.unpack("<iI", data)
        if kind == "chlist":
            return _chlist(data)
        if kind == "stringvector":
            items, pos = [], 0
            while pos + 4 <= len(data):
                (n,) = struct.unpack_from("<i", data, pos)
                pos += 4
                items.append(data[pos : pos + n].decode("utf-8", errors="replace"))
                pos += n
            return items
    except (struct.error, IndexError):
        return bytes(data)
    return bytes(data)


def _read_name(fh: BinaryIO) -> str:
    raw = bytearray()
    while True:
        ch = fh.read(1)
        if not ch:
            raise ExrHeaderError("header truncated (end of file inside an attribute name)")
        if ch == b"\x00":
            return raw.decode("utf-8", errors="replace")
        raw += ch
        if len(raw) >= MAX_NAME:
            raise ExrHeaderError("attribute name longer than 255 bytes: not an OpenEXR header")


def _read_part(fh: BinaryIO, file_size: int, first_name: str | None = None) -> ExrPart:
    part = ExrPart()
    name = first_name if first_name is not None else _read_name(fh)
    while name:
        kind = _read_name(fh)
        raw_size = fh.read(4)
        if len(raw_size) != 4:
            raise ExrHeaderError(f"header truncated in attribute {name!r}")
        (size,) = struct.unpack("<i", raw_size)
        if size < 0 or fh.tell() + size > file_size:
            raise ExrHeaderError(f"attribute {name!r} claims {size} bytes, beyond the end of the file")
        data = fh.read(size)
        part.attributes[name] = _decode(kind, data)
        part.types[name] = kind
        name = _read_name(fh)
    return part


def read_header(path: Path | str) -> ExrHeader:
    """The header of every part of an OpenEXR file. Raises ExrHeaderError for anything else."""
    target = Path(path)
    try:
        file_size = target.stat().st_size
        with target.open("rb") as fh:
            head = fh.read(8)
            if len(head) != 8 or head[:4] != MAGIC:
                raise ExrHeaderError(f"{target.name}: not an OpenEXR file (bad magic number)")
            (word,) = struct.unpack("<I", head[4:])
            version, flags = word & 0xFF, word & ~0xFF
            if version != 2:
                raise ExrHeaderError(f"{target.name}: OpenEXR file version {version}, expected 2")
            parts = [_read_part(fh, file_size)]
            if flags & MULTIPART_FLAG:
                while True:
                    name = _read_name(fh)
                    if not name:  # the empty header that ends the list
                        break
                    parts.append(_read_part(fh, file_size, first_name=name))
            offsets_at = fh.tell()
    except OSError as exc:
        raise ExrHeaderError(f"{target.name}: unreadable ({exc})") from exc
    if not parts[0].channels:
        raise ExrHeaderError(f"{target.name}: the header has no channel list")
    return ExrHeader(str(target), version, flags, parts, offsets_at)


# --------------------------------------------------------------------------- layers and Cryptomatte


def layer_channels(header: ExrHeader) -> dict[str, list[Channel]]:
    """Channels grouped by layer (the name before the last '.'; "" holds the bare beauty R, G, B, A)."""
    out: dict[str, list[Channel]] = {}
    for ch in header.channels:
        out.setdefault(ch.layer, []).append(ch)
    return out


@dataclass
class CryptoLayer:
    """One cryptomatte/<key>/* attribute group. manifest maps a name (node or material) to its 8-hex id."""

    key: str
    name: str | None = None
    hash: str | None = None
    conversion: str | None = None
    manifest: dict[str, str] | None = None
    manif_file: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and self.manifest is not None


def cryptomatte_layers(header: ExrHeader | dict[str, Any]) -> dict[str, CryptoLayer]:
    """{layer name: CryptoLayer} from the header's cryptomatte/<key>/{name,hash,conversion,manifest,
    manif_file} attributes. A group without a name is keyed by its key. problems lists a hash or
    conversion other than Cryptomatte's MurmurHash3_32 / uint32_to_float32, a manifest that is not
    a JSON object of 8-hex strings, and a sidecar-only manifest (manif_file), which Send does not read."""
    attrs = header.attributes if isinstance(header, ExrHeader) else header
    groups: dict[str, CryptoLayer] = {}
    for name, value in attrs.items():
        if not name.startswith(CRYPTO_PREFIX):
            continue
        rest = name[len(CRYPTO_PREFIX) :]
        if "/" not in rest:
            continue
        key, prop = rest.split("/", 1)
        group = groups.setdefault(key, CryptoLayer(key))
        text = value if isinstance(value, str) else _cstr(value) if isinstance(value, bytes) else str(value)
        if prop == "name":
            group.name = text
        elif prop == "hash":
            group.hash = text
        elif prop == "conversion":
            group.conversion = text
        elif prop == "manifest":
            try:
                parsed = json.loads(text) if text.strip() else {}
            except ValueError:
                group.problems.append(f"cryptomatte/{key}/manifest is not JSON")
                continue
            if not isinstance(parsed, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
            ):
                group.problems.append(f"cryptomatte/{key}/manifest is not a name -> hex map")
                continue
            group.manifest = {k: v.lower() for k, v in parsed.items()}
        elif prop == "manif_file":
            group.manif_file = text
    out: dict[str, CryptoLayer] = {}
    for key, group in groups.items():
        label = group.name or key
        if group.hash and group.hash != CRYPTO_HASH:
            group.problems.append(f"{label}: hash {group.hash!r}, expected {CRYPTO_HASH}")
        if group.conversion and group.conversion != CRYPTO_CONVERSION:
            group.problems.append(f"{label}: conversion {group.conversion!r}, expected {CRYPTO_CONVERSION}")
        if group.manifest is None and group.manif_file:
            group.problems.append(
                f"{label}: the manifest is a sidecar file ({group.manif_file}); embed it in the EXR "
                "(VRayCryptomatte manifest option, TODO-verify)"
            )
        elif group.manifest is None and not any(p.endswith("not JSON") for p in group.problems):
            group.problems.append(f"{label}: no manifest in the EXR header")
        out[label] = group
    return out


# --------------------------------------------------------------------------- rank-0 coverage


@dataclass
class Coverage:
    """Rank-0 coverage of one Cryptomatte layer: shares maps an 8-hex id to the fraction of the frame
    (the data window) it covers at rank 0 (the sum of its rank-0 coverage over the pixel count)."""

    layer: str
    shares: dict[str, float]
    pixels: int
    compression: str

    @property
    def covered(self) -> float:
        return sum(self.shares.values())


def _zip_ranges(raw: bytes, ranges: list[tuple[int, int]]) -> list[bytes]:
    """The decoded bytes [a, b) of each range of a ZIP/ZIPS block whose zlib output is raw.

    OpenEXR stores the block's bytes split (even positions first, odd positions second) and
    delta-coded (t[i] = t[i-1] + d[i] - 128). Only the requested ranges are rebuilt: the running
    sum over the bytes in between is taken with sum(), which runs in C."""
    n = len(raw)
    half = (n + 1) // 2
    d = raw.translate(_TO_SIGNED)
    pieces: list[tuple[int, int, int, int]] = []  # (t start, t end, range index, first out offset)
    for index, (a, b) in enumerate(ranges):
        if not 0 <= a <= b <= n:
            raise CoverageUnsupported(f"byte range {a}..{b} outside a {n}-byte block")
        e0 = a + (a & 1)
        if e0 < b:
            count = (b - 1 - e0) // 2 + 1
            pieces.append((e0 // 2, e0 // 2 + count, index, e0 - a))
        o0 = a + (1 - (a & 1))
        if o0 < b:
            count = (b - 1 - o0) // 2 + 1
            pieces.append((half + o0 // 2, half + o0 // 2 + count, index, o0 - a))
    pieces.sort()
    bufs = [bytearray(b - a) for a, b in ranges]
    cursor, total = 0, 128  # t[p] = 128 + sum(d[0..p]) (mod 256)
    for start, end, index, first in pieces:
        if start < cursor:
            raise CoverageUnsupported("overlapping channel ranges in one block")
        total += sum(d[cursor:start])
        segment = d[start:end]
        values = bytes(map(and_, accumulate(segment, initial=total), repeat(0xFF)))[1:]
        bufs[index][first::2] = values
        total += sum(segment)
        cursor = end
    return [bytes(buf) for buf in bufs]


def _floats(data: bytes, pixel_type: int) -> Iterable[float]:
    if pixel_type == 2:
        values = array("f")
        values.frombytes(data)
        if sys.byteorder == "big":
            values.byteswap()
        return values
    if pixel_type == 1:
        return struct.unpack(f"<{len(data) // 2}e", data)
    raise CoverageUnsupported(f"coverage channel pixel type {PIXEL_TYPES.get(pixel_type, pixel_type)}")


def _ids(data: bytes) -> array:
    values = array("I")
    if values.itemsize != 4:  # pragma: no cover (every CPython build Max ships has a 4-byte unsigned int)
        values = array("L")
    values.frombytes(data)
    if sys.byteorder == "big":
        values.byteswap()
    return values


def rank0_coverage(path: Path | str, layer: str, header: ExrHeader | None = None) -> Coverage:
    """Rank-0 coverage per ID of Cryptomatte layer `layer` (e.g. "CryptoObject"): the channels
    <layer>00.R (ID, float32 bits) and <layer>00.G (coverage). Raises CoverageUnsupported when the
    channels are missing or stored in a way this reader cannot decode (tiles, deep data,
    subsampling, a compression other than none/zips/zip), ExrHeaderError for a broken file."""
    header = header or read_header(path)
    id_name, cov_name = f"{layer}00.R", f"{layer}00.G"
    hit = header.find_channel(id_name)
    if hit is None or header.find_channel(cov_name) is None:
        raise CoverageUnsupported(f"no {id_name} / {cov_name} channels in the EXR")
    part_index = hit[0]
    if header.find_channel(cov_name)[0] != part_index:  # type: ignore[index]
        raise CoverageUnsupported(f"{id_name} and {cov_name} are in different parts")
    part = header.parts[part_index]
    if header.tiled or header.deep:
        raise CoverageUnsupported("tiled or deep EXR: render a scanline EXR")
    code = part.compression
    if code not in DECODABLE:
        raise CoverageUnsupported(
            f"EXR compression {part.compression_name}: the plugin reads none, zips or zip "
            "(set V-Ray's raw EXR compression to ZIP)"
        )
    channels = part.channels
    if any(ch.x_sampling != 1 or ch.y_sampling != 1 for ch in channels):
        raise CoverageUnsupported("subsampled channels in the EXR")
    box = part.data_window
    if box is None:
        raise CoverageUnsupported("no dataWindow in the EXR header")
    width, height = box[2] - box[0] + 1, box[3] - box[1] + 1
    if width <= 0 or height <= 0:
        raise CoverageUnsupported("empty data window")
    offsets: dict[str, int] = {}
    position = 0
    for ch in channels:
        offsets[ch.name] = position
        position += width * ch.size
    line_bytes = position
    id_ch = next(ch for ch in channels if ch.name == id_name)
    cov_ch = next(ch for ch in channels if ch.name == cov_name)
    if id_ch.pixel_type != 2:
        raise CoverageUnsupported(f"{id_name} is {id_ch.dtype}: Cryptomatte IDs must be 32-bit float")
    lines_per_block = LINES_PER_BLOCK[code]
    counts = []
    for p in header.parts:
        p_box = p.data_window
        p_height = p_box[3] - p_box[1] + 1 if p_box else 0
        p_lpb = LINES_PER_BLOCK.get(p.compression if p.compression is not None else -1, 1)
        counts.append(p.chunk_count if p.chunk_count is not None else -(-p_height // p_lpb))
    chunk_count = counts[part_index]

    acc: dict[int, float] = {}
    get = acc.get
    try:
        with Path(path).open("rb") as fh:
            fh.seek(header.offsets_at + 8 * sum(counts[:part_index]))
            table = fh.read(8 * chunk_count)
            if len(table) != 8 * chunk_count:
                raise CoverageUnsupported("chunk offset table truncated")
            for (offset,) in struct.iter_unpack("<Q", table):
                fh.seek(offset)
                if header.multipart:
                    fh.read(4)  # the part number
                head = fh.read(8)
                if len(head) != 8:
                    raise CoverageUnsupported("chunk truncated")
                y, size = struct.unpack("<ii", head)
                lines = min(lines_per_block, box[3] - y + 1)
                if lines <= 0 or size < 0:
                    raise CoverageUnsupported(f"chunk at y={y} is outside the data window")
                expected = lines * line_bytes
                data = fh.read(size)
                if len(data) != size:
                    raise CoverageUnsupported(f"chunk at y={y} truncated")
                ranges = []
                for line in range(lines):
                    base = line * line_bytes
                    ranges.append((base + offsets[id_name], base + offsets[id_name] + width * 4))
                    ranges.append((base + offsets[cov_name], base + offsets[cov_name] + width * cov_ch.size))
                if code == 0 or size == expected:  # OpenEXR stores a block raw when zlib does not shrink it
                    if size != expected:
                        raise CoverageUnsupported(
                            f"uncompressed chunk at y={y} has {size} bytes, not {expected}"
                        )
                    slices = [data[a:b] for a, b in ranges]
                else:
                    try:
                        raw = zlib.decompress(data)
                    except zlib.error as exc:
                        raise CoverageUnsupported(f"chunk at y={y}: zlib error {exc}") from exc
                    if len(raw) != expected:
                        raise CoverageUnsupported(
                            f"chunk at y={y} inflates to {len(raw)} bytes, not {expected}"
                        )
                    slices = _zip_ranges(raw, ranges)
                for line in range(lines):
                    for bits, cover in zip(
                        _ids(slices[2 * line]), _floats(slices[2 * line + 1], cov_ch.pixel_type), strict=True
                    ):
                        if cover > 0.0 and bits:
                            acc[bits] = get(bits, 0.0) + cover
    except OSError as exc:
        raise ExrHeaderError(f"{Path(path).name}: unreadable ({exc})") from exc
    pixels = width * height
    return Coverage(
        layer=layer,
        shares={f"{bits:08x}": total / pixels for bits, total in acc.items()},
        pixels=pixels,
        compression=part.compression_name,
    )
