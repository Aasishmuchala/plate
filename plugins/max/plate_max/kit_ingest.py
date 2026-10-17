"""Client packet kit ingest: the kit's output folder plus the client's plate become a tier A Shot Packet.

plugins/max/maxscript/plate_client_kit.ms runs on the CLIENT's machine and leaves, in one folder,
``<stem>_packet.exr`` (Z, normals, Cryptomatte object and material, wire colour, diffuse filter),
``<stem>_camera.json`` and ``<stem>_legend.txt``. ``kit_to_packet(folder, camera, plate_path)`` turns that
and the client's plate (their approved image, the display master) into a packet folder that
``plate_spec.validate_packet_dir`` accepts, so a tier C job becomes tier A (PACKET.md section 11).

This module lives in plate_max, which is stdlib-only (test_stdlib_only), so it contains no EXR pixel
decoding, no BLAKE3 and no image conversion. What needs them is passed in by the caller (the daemon route
in T38, or a test) as keyword arguments:

- ``hasher(source) -> hex``: BLAKE3 of a ``Path`` (stream the file) or of ``bytes``; a mapping with a "hex"
  key is accepted too (the daemon's ``hash_contained_file`` result).
- ``pixel_shares``: ``{"o:<8 hex>": share}`` of the frame each object id covers at rank 0, or a callable
  ``(exr_path, elements) -> that mapping``. ``elements`` is the manifest's ``files.beauty_exr.elements`` list,
  which is what ``plate_pixels.exr.read_cryptomatte(path, {"files": {"beauty_exr": {"elements": elements}}})``
  needs. Coverage comes from the render, never from the kit's legend.
- ``plate_to_tiff16(src, dst)``: only for a plate that is not already a 16-bit RGB(A) TIFF. It must write a
  16-bit TIFF of the same size; 8-bit values are promoted exactly (v * 257).

The EXR header is read here with the standard library (channels, dtype, compression, Cryptomatte manifest),
so the manifest's ``elements`` describe the file that is actually there and a missing layer, a half-float
Cryptomatte or a lossy EXR is refused with a message instead of failing later in a gate.

Honest gaps, all recorded in ``kit_flags.json`` (file role ``x_kit_flags``) next to the manifest: the schema
fixes ``display_tiff16.bit_depth`` at 16, so an 8-bit plate is promoted and flagged ``client_plate_8bit``;
``depth_kind`` is not written by V-Ray's element and defaults to planar (``depth_kind_assumed``, a box
measurement); a camera without an f-number or focus distance gets nominal values while DOF is off
(``f_number_nominal``, ``focus_distance_nominal``); whether VFB corrections are baked into the plate is
unknown (``vfb_corrections_unknown``).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import autotag, digest, packet
from . import legend as legend_mod
from . import policy as policy_mod
from .fsutil import (
    REPLACE_FIRST_DELAY_S,
    REPLACE_TRIES,
    atomic_write_bytes,
    canonical_json_bytes,
    dump_json_bytes,
)
from .packet_id import PACKET_ID_RE, new_packet_id

KIT_NAME = "plate_client_kit"
SUPPORTED_KIT_VERSIONS = ("0.1",)
# scene_hash.digest_version of a kit packet: the hash is over the kit's own description of the scene (camera,
# units, renderer, node names), which is the interim digest family, not the content digest of a Max-side Send.
KIT_DIGEST_VERSION = "0"
NOMINAL_F_NUMBER = 8.0
NOMINAL_FOCUS_DISTANCE = 1.0
FOV_TOL_DEG = 0.05

EXR_MAGIC = 20000630
EXR_PIXEL_TYPES = {0: "uint", 1: "half", 2: "float"}
EXR_COMPRESSION = {
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
}
LOSSLESS_EXR = ("none", "rle", "zip", "zips", "piz")
MAX_EXR_ATTRIBUTE_BYTES = 64 << 20

# Metres per Max SystemType unit; the packet schema takes the first five only.
METERS_PER_TYPE = {
    "millimeters": 0.001,
    "centimeters": 0.01,
    "meters": 1.0,
    "inches": 0.0254,
    "feet": 0.3048,
}

# What the kit configures, per manifest element: (element, default layer, channels, filtered).
DEFAULT_LAYERS = {
    "beauty": "RGBA",
    "zdepth": "VRayZDepth",
    "normals": "VRayNormals",
    "cryptomatte_object": "CryptoObject",
    "cryptomatte_material": "CryptoMaterial",
    "wire_colour": "VRayWireColor",
    "diffuse_filter": "VRayDiffuseFilter",
}
PLAIN_ELEMENTS = (
    ("normals", ("X", "Y", "Z"), True),
    ("wire_colour", ("R", "G", "B"), False),
    ("diffuse_filter", ("R", "G", "B"), True),
)

Hasher = Callable[[Any], Any]
PixelShares = Mapping[str, float] | Callable[[Path, list[dict[str, Any]]], Mapping[str, float]]
PlateConverter = Callable[[Path, Path], None]

__all__ = [
    "KitError",
    "KitFiles",
    "KitResult",
    "PlateInfo",
    "ExrHeader",
    "convert_kit",
    "inspect_plate",
    "kit_files",
    "kit_stem",
    "kit_to_packet",
    "parse_legend_txt",
    "read_exr_header",
]


class KitError(ValueError):
    """The kit output (or the plate) cannot become a valid packet; the message says what to fix."""


# --------------------------------------------------------------------------- files


def kit_stem(camera: str) -> str:
    """The file-name stem the kit uses for a camera: every character outside [A-Za-z0-9_.-] becomes "_"."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", camera)


@dataclass(frozen=True)
class KitFiles:
    camera_json: Path
    legend_txt: Path
    exr: Path


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise KitError(
            f"missing {path.name} in the kit folder: run the kit's Export for this camera"
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise KitError(f"{path.name} is not UTF-8 JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise KitError(f"{path.name} must hold a JSON object")
    return doc


def kit_files(folder: Path | str, camera: str) -> tuple[KitFiles, dict[str, Any]]:
    """The three files the kit wrote for ``camera`` and the parsed camera JSON. File names come from the
    JSON's ``exr`` and ``legend`` keys; they must stay inside the folder."""
    root = Path(folder)
    stem = kit_stem(camera)
    cam_path = root / f"{stem}_camera.json"
    if not cam_path.is_file() and (root / f"{camera}_camera.json").is_file():
        cam_path = root / f"{camera}_camera.json"
    doc = _read_json_file(cam_path)

    def named(key: str, default: str) -> Path:
        rel = doc.get(key) or default
        if not isinstance(rel, str) or not rel or any(c in rel for c in "/\\:") or rel in (".", ".."):
            raise KitError(f"{cam_path.name}: {key!r} must be a plain file name, not {rel!r}")
        return root / rel

    files = KitFiles(cam_path, named("legend", f"{stem}_legend.txt"), named("exr", f"{stem}_packet.exr"))
    for path in (files.legend_txt, files.exr):
        if not path.is_file():
            raise KitError(f"missing {path.name} in the kit folder: run the kit's Export for this camera")
    return files, doc


# --------------------------------------------------------------------------- OpenEXR header (stdlib)


@dataclass
class ExrPart:
    name: str | None
    compression: str
    width: int
    height: int
    channels: dict[str, str]  # full channel name -> "half" | "float" | "uint"


@dataclass
class ExrHeader:
    parts: list[ExrPart]
    strings: dict[str, str] = field(
        default_factory=dict
    )  # every string attribute, e.g. cryptomatte/<id>/name

    @property
    def channels(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in self.parts:
            out.update(part.channels)
        return out


def _cstr(fh: Any) -> str:
    out = bytearray()
    while True:
        b = fh.read(1)
        if not b:
            raise KitError("the EXR header is truncated")
        if b == b"\x00":
            return out.decode("utf-8", "replace")
        out += b
        if len(out) > 255:
            raise KitError("the EXR header has an attribute name that is too long: not an OpenEXR file?")


def _read_channels(value: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    pos = 0
    while pos < len(value) and value[pos] != 0:
        end = value.index(b"\x00", pos)
        name = value[pos:end].decode("utf-8", "replace")
        pos = end + 1
        if pos + 16 > len(value):
            raise KitError("the EXR channel list is truncated")
        ptype = struct.unpack_from("<i", value, pos)[0]
        out[name] = EXR_PIXEL_TYPES.get(ptype, "unknown")
        pos += 16  # pixel type, pLinear + 3 reserved bytes, xSampling, ySampling
    return out


def _read_part_header(fh: Any) -> tuple[dict[str, tuple[str, bytes]], bool]:
    """Attributes of one header as {name: (type, raw value)}; the flag is False when the header was empty,
    which in a multipart file is the marker that ends the header list."""
    attrs: dict[str, tuple[str, bytes]] = {}
    while True:
        name = _cstr(fh)
        if name == "":
            return attrs, bool(attrs)
        kind = _cstr(fh)
        raw_size = fh.read(4)
        if len(raw_size) < 4:
            raise KitError("the EXR header is truncated")
        size = struct.unpack("<i", raw_size)[0]
        if not 0 <= size <= MAX_EXR_ATTRIBUTE_BYTES:
            raise KitError(f"the EXR attribute {name!r} has an impossible size: not an OpenEXR file?")
        value = fh.read(size)
        if len(value) != size:
            raise KitError("the EXR header is truncated")
        attrs[name] = (kind, value)


def read_exr_header(path: Path | str) -> ExrHeader:
    """Channels (with half/float), compression, data window and string attributes of every part of an
    OpenEXR file, from the header only (no pixel data is read)."""
    with Path(path).open("rb") as fh:
        head = fh.read(8)
        if len(head) < 8 or struct.unpack("<I", head[:4])[0] != EXR_MAGIC:
            raise KitError(f"{Path(path).name} is not an OpenEXR file")
        flags = struct.unpack("<I", head[4:8])[0] & ~0xFF
        if flags & 0x800:
            raise KitError("deep OpenEXR files are not supported")
        multipart = bool(flags & 0x1000)
        raw_parts: list[dict[str, tuple[str, bytes]]] = []
        while True:
            attrs, nonempty = _read_part_header(fh)
            if not nonempty:
                break
            raw_parts.append(attrs)
            if not multipart:
                break
    if not raw_parts:
        raise KitError("the EXR has no header")
    parts: list[ExrPart] = []
    strings: dict[str, str] = {}
    for attrs in raw_parts:
        if "channels" not in attrs or "dataWindow" not in attrs:
            raise KitError("the EXR header has no channel list or data window")
        x0, y0, x1, y1 = struct.unpack("<4i", attrs["dataWindow"][1])
        comp = attrs.get("compression", ("compression", b"\xff"))[1][0]
        part_name = attrs["name"][1].decode("utf-8", "replace") if "name" in attrs else None
        parts.append(
            ExrPart(
                name=part_name,
                compression=EXR_COMPRESSION.get(comp, "unknown"),
                width=x1 - x0 + 1,
                height=y1 - y0 + 1,
                channels=_read_channels(attrs["channels"][1]),
            )
        )
        for key, (kind, value) in attrs.items():
            if kind == "string":
                strings[key] = value.decode("utf-8", "replace")
    return ExrHeader(parts, strings)


def _crypto_manifest(header: ExrHeader, layer: str) -> dict[str, str] | None:
    """The Cryptomatte name -> hex id manifest of the layer called ``layer`` (header metadata), or None."""
    for key, value in header.strings.items():
        m = re.fullmatch(r"cryptomatte/([0-9a-fA-F]+)/name", key)
        if m and value == layer:
            raw = header.strings.get(f"cryptomatte/{m.group(1)}/manifest")
            if raw is None:
                return None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return {str(k): str(v).lower() for k, v in parsed.items()} if isinstance(parsed, dict) else None
    return None


def _elements(header: ExrHeader, names: Mapping[str, str], flags: list[str]) -> list[dict[str, Any]]:
    """The manifest's ``files.beauty_exr.elements`` for the default set, taken from what the EXR holds."""
    chans = header.channels

    def need(element: str, layer: str, channel: str) -> str:
        full = channel if layer == "RGBA" else f"{layer}.{channel}"
        if full not in chans:
            raise KitError(
                f"the EXR has no channel {full!r} ({element}): the kit could not add that render element "
                "(see the warnings in the camera JSON and the 3ds Max Listener)"
            )
        return full

    def dtype_of(element: str, full: list[str], must_be_float: bool) -> str:
        kinds = {chans[c] for c in full}
        if len(kinds) != 1 or not kinds <= {"half", "float"}:
            raise KitError(f"{element}: channels mix pixel types {sorted(kinds)}")
        kind = kinds.pop()
        if must_be_float and kind != "float":
            raise KitError(
                f"{element} is stored as {kind}: it must be 32-bit float (half-float corrupts Cryptomatte ids and depth)"
            )
        return kind

    out: list[dict[str, Any]] = []
    layer = names["beauty"]
    beauty = ["R", "G", "B"] + (["A"] if "A" in chans else [])
    full = [need("beauty", layer, c) for c in beauty]
    out.append(
        {
            "element": "beauty",
            "layer": layer,
            "channels": beauty,
            "dtype": dtype_of("beauty", full, False),
            "filtered": True,
            "denoised": False,
        }
    )
    layer = names["zdepth"]
    full = [need("zdepth", layer, "Z")]
    out.append(
        {
            "element": "zdepth",
            "layer": layer,
            "channels": ["Z"],
            "dtype": dtype_of("zdepth", full, True),
            "filtered": False,
            "denoised": False,
            "depth_kind": "planar",
            "normalized": False,
            "clamped": False,
            "background_value": None,
        }
    )
    flags.append("depth_kind_assumed")
    for element, channels, filtered in PLAIN_ELEMENTS:
        layer = names[element]
        full = [need(element, layer, c) for c in channels]
        entry: dict[str, Any] = {
            "element": element,
            "layer": layer,
            "channels": list(channels),
            "dtype": dtype_of(element, full, False),
            "filtered": filtered,
            "denoised": False,
        }
        if element == "normals":
            entry.update({"space": "camera", "encoding": "signed"})
        out.append(entry)
    for element in ("cryptomatte_object", "cryptomatte_material"):
        base = names[element]
        numbers = sorted(
            {int(m.group(1)) for c in chans if (m := re.fullmatch(rf"{re.escape(base)}(\d\d)\.[RGBA]", c))}
        )
        if not numbers:
            raise KitError(
                f"the EXR has no {base}NN.R/G/B/A channels ({element}): add the Cryptomatte render element"
            )
        listed = [f"{base}{n:02d}.{c}" for n in numbers for c in "RGBA"]
        missing = [c for c in listed if c not in chans]
        if missing:
            raise KitError(f"{element}: the EXR is missing {missing[0]!r}")
        if numbers != list(range(len(numbers))):
            raise KitError(f"{element}: layers {numbers} are not 00..NN without gaps")
        out.append(
            {
                "element": element,
                "layer": base,
                "channels": listed,
                "dtype": dtype_of(element, listed, True),
                "filtered": True,
                "denoised": False,
                "rank_levels": 2 * len(numbers),
            }
        )
    return out


# --------------------------------------------------------------------------- plate (stdlib sniffing)


@dataclass(frozen=True)
class PlateInfo:
    format: str  # "png" | "tiff" | "jpeg"
    width: int
    height: int
    bit_depth: int
    channels: str  # "RGB" | "RGBA" | "L" | "LA" | "P" | "CMYK"
    icc: bool = False
    srgb: bool = False

    @property
    def is_tiff16_rgb(self) -> bool:
        return self.format == "tiff" and self.bit_depth == 16 and self.channels in ("RGB", "RGBA")


def _inspect_png(fh: Any) -> PlateInfo:
    fh.seek(8)
    width = height = depth = ctype = None
    icc = srgb = False
    while True:
        head = fh.read(8)
        if len(head) < 8:
            break
        length, ctag = struct.unpack(">I4s", head)
        if ctag == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", fh.read(10))
            fh.seek(length - 10 + 4, 1)
            continue
        if ctag == b"iCCP":
            icc = True
        elif ctag == b"sRGB":
            srgb = True
        elif ctag in (b"IDAT", b"IEND"):
            break
        fh.seek(length + 4, 1)
    channels = {0: "L", 2: "RGB", 3: "P", 4: "LA", 6: "RGBA"}.get(ctype)
    if width is None or channels is None:
        raise KitError("the PNG plate has no valid IHDR chunk")
    return PlateInfo("png", width, height, depth, channels, icc, srgb)


_TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _tiff_values(bo: str, typ: int, count: int, data: bytes) -> list[int]:
    code = {1: "B", 3: "H", 4: "I"}.get(typ)
    return list(struct.unpack(f"{bo}{count}{code}", data[: count * struct.calcsize(code)])) if code else []


def _inspect_tiff(fh: Any) -> PlateInfo:
    fh.seek(0)
    head = fh.read(8)
    bo = {b"II": "<", b"MM": ">"}.get(head[:2])
    if bo is None or len(head) < 8:
        raise KitError("the TIFF plate has no valid header")
    magic = struct.unpack(f"{bo}H", head[2:4])[0]
    if magic == 43:
        raise KitError("BigTIFF plates are not supported: save a classic TIFF")
    if magic != 42:
        raise KitError("the TIFF plate has no valid header")
    fh.seek(struct.unpack(f"{bo}I", head[4:8])[0])
    raw_count = fh.read(2)
    if len(raw_count) < 2:
        raise KitError("the TIFF plate is truncated")
    n = struct.unpack(f"{bo}H", raw_count)[0]
    raw = fh.read(12 * n)
    if n == 0 or len(raw) < 12 * n:
        raise KitError("the TIFF plate is truncated")
    tags: dict[int, list[int]] = {}
    for i in range(n):
        tag, typ, count, field_bytes = struct.unpack(f"{bo}HHI4s", raw[i * 12 : (i + 1) * 12])
        size = _TIFF_TYPE_SIZE.get(typ)
        if size is None or tag not in (256, 257, 258, 262, 277, 338, 339, 34675):
            continue
        if tag == 34675:
            tags[tag] = [1]
        elif size * count <= 4:
            tags[tag] = _tiff_values(bo, typ, count, field_bytes)
        elif count <= 16:
            here = fh.tell()
            fh.seek(struct.unpack(f"{bo}I", field_bytes)[0])
            tags[tag] = _tiff_values(bo, typ, count, fh.read(size * count))
            fh.seek(here)
    for needed in (256, 257):
        if needed not in tags or not tags[needed]:
            raise KitError("the TIFF plate has no image size")
    samples = (tags.get(277) or [1])[0]
    bits = set(tags.get(258) or [1])
    photometric = (tags.get(262) or [1])[0]
    floating = (tags.get(339) or [1])[0] != 1
    if len(bits) != 1 or floating:
        depth, channels = 0, "?"  # mixed or floating-point samples: needs the converter
    else:
        depth = bits.pop()
        channels = {(2, 3): "RGB", (2, 4): "RGBA", (1, 1): "L", (3, 1): "P", (5, 4): "CMYK"}.get(
            (photometric, samples), "?"
        )
    return PlateInfo("tiff", tags[256][0], tags[257][0], depth, channels, icc=34675 in tags)


def _inspect_jpeg(fh: Any) -> PlateInfo:
    fh.seek(2)
    while True:
        b = fh.read(1)
        if not b:
            break
        if b != b"\xff":
            continue
        marker = fh.read(1)
        while marker == b"\xff":
            marker = fh.read(1)
        if not marker:
            break
        code = marker[0]
        if code == 0x01 or 0xD0 <= code <= 0xD9:
            continue
        seg = fh.read(2)
        if len(seg) < 2:
            break
        length = struct.unpack(">H", seg)[0]
        if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
            precision, height, width, comps = struct.unpack(">BHHB", fh.read(6))
            return PlateInfo("jpeg", width, height, precision, {1: "L", 3: "RGB", 4: "CMYK"}.get(comps, "?"))
        fh.seek(length - 2, 1)
    raise KitError("the JPEG plate has no frame header")


def inspect_plate(path: Path | str) -> PlateInfo:
    """Format, size, bit depth and channels of a PNG, classic TIFF or JPEG plate, from the headers."""
    p = Path(path)
    try:
        with p.open("rb") as fh:
            magic = fh.read(8)
            if magic.startswith(b"\x89PNG\r\n\x1a\n"):
                return _inspect_png(fh)
            if magic[:2] in (b"II", b"MM"):
                return _inspect_tiff(fh)
            if magic[:2] == b"\xff\xd8":
                return _inspect_jpeg(fh)
    except FileNotFoundError:
        raise KitError(f"plate not found: {p}") from None
    except (struct.error, ValueError) as exc:
        raise KitError(f"{p.name} is not a readable plate: {exc}") from exc
    raise KitError(f"{p.name}: the plate must be a PNG, TIFF or JPEG")


# --------------------------------------------------------------------------- small helpers


def _hex_of(value: Any) -> str:
    hex_value = value.get("hex") if isinstance(value, Mapping) else value
    if not isinstance(hex_value, str) or not re.fullmatch(r"[0-9a-f]{64}", hex_value):
        raise KitError("the hasher must return a 64-character lowercase BLAKE3 hex digest")
    return hex_value


def _copy_atomic(src: Path, dst: Path) -> None:
    """Streamed copy to a temp file next to dst, then os.replace with the retry Windows needs."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dst.parent), prefix="plate-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            shutil.copyfileobj(inp, out, 1 << 20)
        delay = REPLACE_FIRST_DELAY_S
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, dst)
                return
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(delay)
                delay *= 2
    finally:
        if tmp.exists():
            tmp.unlink()


def _number(
    doc: Mapping[str, Any], key: str, *, positive: bool = False, default: float | None = None
) -> float:
    value = doc.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise KitError(f"camera JSON: {key!r} must be a number, not {value!r}")
    if positive and value <= 0:
        raise KitError(f"camera JSON: {key!r} must be positive, not {value!r}")
    return float(value)


def _optional_number(doc: Mapping[str, Any], key: str) -> float | None:
    value = doc.get(key)
    if value is None:
        return None
    return _number(doc, key)


def parse_legend_txt(text: str) -> list[dict[str, Any]]:
    """``node|layer|material|r,g,b`` lines (percent-encoded fields) to [{name, layer, material, wirecolor}].
    Blank lines and lines starting with # are skipped."""
    nodes: list[dict[str, Any]] = []
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.rstrip("\r\n").split("|")
        if len(parts) != 4:
            raise KitError(
                f"legend line {number}: expected node|layer|material|wirecolor, got {len(parts)} fields"
            )
        rgb = parts[3].strip().split(",")
        try:
            wire = [int(v) for v in rgb]
        except ValueError:
            wire = []
        if len(wire) != 3 or not all(0 <= v <= 255 for v in wire):
            raise KitError(f"legend line {number}: wirecolor must be r,g,b in 0..255, got {parts[3]!r}")
        name = unquote(parts[0])
        if not name:
            raise KitError(f"legend line {number}: empty node name")
        nodes.append(
            {
                "name": name,
                "layer": unquote(parts[1]) or None,
                "material": unquote(parts[2]) or None,
                "wirecolor": wire,
            }
        )
    return nodes


# --------------------------------------------------------------------------- camera


def _camera_digest(doc: Mapping[str, Any], flags: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(camera digest for packet.build_camera_block, scene dict for build_manifest) from the camera JSON."""
    version = str(doc.get("kit_version", ""))
    if (
        doc.get("kit") != KIT_NAME
        or not re.fullmatch(r"\d+\.\d+\.\d+", version)
        or version.rsplit(".", 1)[0] not in SUPPORTED_KIT_VERSIONS
    ):
        raise KitError(
            f"camera JSON: not written by {KIT_NAME} {'/'.join(SUPPORTED_KIT_VERSIONS)}.x (kit_version {version!r})"
        )
    name = doc.get("camera")
    if not isinstance(name, str) or not name:
        raise KitError("camera JSON: 'camera' must be the camera node name")
    rows = doc.get("transform")
    if not (
        isinstance(rows, list) and len(rows) == 4 and all(isinstance(r, list) and len(r) == 3 for r in rows)
    ):
        raise KitError("camera JSON: 'transform' must be 4 rows of 3 numbers (Max Matrix3 row1..row4)")
    try:
        world_from_camera = digest.world_from_camera_from_max_rows([[float(v) for v in r] for r in rows])
    except (TypeError, ValueError) as exc:
        raise KitError(f"camera JSON: 'transform' holds a non-number: {exc}") from exc
    film = _number(doc, "film_width", positive=True, default=36.0)
    zoom = _number(doc, "zoom_factor", positive=True, default=1.0)
    focal = _number(doc, "focal_length_mm", positive=True)
    fov = _optional_number(doc, "fov")
    if fov is not None:
        expect = math.degrees(2 * math.atan(film / (2 * focal * zoom)))
        if abs(expect - fov) > FOV_TOL_DEG:
            raise KitError(
                f"camera JSON: fov {fov:.4f} deg disagrees with focal {focal} mm, film {film} mm, zoom {zoom} "
                f"({expect:.4f} deg): a V-Ray 'specify FOV' camera is not supported, set the focal length instead"
            )
    width, height = (
        int(_number(doc, "render_width", positive=True)),
        int(_number(doc, "render_height", positive=True)),
    )
    frame = int(_number(doc, "frame", default=0))
    dof = doc.get("dof_enabled") is True
    f_number = _optional_number(doc, "f_number")
    focus = _optional_number(doc, "focus_distance")
    if f_number is None or f_number <= 0:
        if dof:
            raise KitError("camera JSON: depth of field is on but the f-number is missing")
        f_number = NOMINAL_F_NUMBER
        flags.append("f_number_nominal")
    if focus is None or focus <= 0:
        if dof:
            raise KitError("camera JSON: depth of field is on but the focus distance is missing")
        focus = NOMINAL_FOCUS_DISTANCE
        flags.append("focus_distance_nominal")
    iso = _optional_number(doc, "iso")
    shutter_speed = _optional_number(doc, "shutter_speed")  # V-Ray: 1/seconds
    lens = doc.get("lens_shift") if isinstance(doc.get("lens_shift"), dict) else {}
    tilt = doc.get("tilt") if isinstance(doc.get("tilt"), dict) else {}
    system = str((doc.get("units") or {}).get("system_type", "")).lstrip("#").lower()
    scale = (doc.get("units") or {}).get("system_scale")
    if system not in METERS_PER_TYPE:
        raise KitError(
            f"camera JSON: scene units {system!r} are not sendable; use one of {', '.join(METERS_PER_TYPE)}"
        )
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
        raise KitError("camera JSON: units.system_scale must be a positive number")
    cam = {
        "name": name,
        "node_type": doc.get("camera_class") or None,
        "world_from_camera": world_from_camera,
        "fov_deg": fov,
        "focal_mm": focal,
        "film_gate_mm": film,
        "zoom_factor": zoom,
        "focus_distance": focus,
        "f_number": f_number,
        "dof_enabled": dof,
        "exposure": {"iso": iso, "shutter_s": (1.0 / shutter_speed) if shutter_speed else None},
        "white_balance_k": None,
        "lens_shift": {
            "horizontal": _number(lens, "horizontal", default=0.0),
            "vertical": _number(lens, "vertical", default=0.0),
            "raw_unit": "vray_physical",
        },
        "tilt": {
            "vertical": _number(tilt, "vertical", default=0.0),
            "horizontal": _number(tilt, "horizontal", default=0.0),
            "auto": tilt.get("auto") is True,
        },
        "distortion": {"amount": _number(doc, "distortion", default=0.0), "type": 0},
        "pixel_aspect": _number(doc, "pixel_aspect", positive=True, default=1.0),
        "resolution": [width, height],
        "safe_frame": {"x": 0, "y": 0, "width": width, "height": height},
        "frame_range": [frame, frame],
        "hero_frame": frame,
        "fps": _number(doc, "fps", positive=True, default=25.0),
        "warnings": [str(w) for w in doc.get("warnings", [])],
    }
    scene = {"units_system": system, "meters_per_unit": METERS_PER_TYPE[system] * float(scale)}
    return cam, scene


def _host_and_renderer(doc: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    cls = str(doc.get("renderer", ""))
    if not cls.startswith("V_Ray"):
        raise KitError(f"camera JSON: the render used {cls!r}, not V-Ray")
    version = cls.removeprefix("V_Ray_").replace("_", " ").strip() or cls
    mv = doc.get("max_version")
    host_version = "unknown"
    if isinstance(mv, list) and mv and isinstance(mv[0], int) and mv[0] >= 1000:
        host_version = str(1998 + mv[0] // 1000)  # Max 2026 reports 28000
    return {"app": "3dsmax", "version": host_version}, {"name": "vray", "version": version}


def _colour_block(
    doc: Mapping[str, Any], plate: PlateInfo, override: Mapping[str, Any] | None, flags: list[str]
) -> dict[str, Any]:
    flags.append("vfb_corrections_unknown")
    if override is not None:
        return json.loads(json.dumps(override))
    mode = doc.get("colour_mode")
    if mode == "legacy_gamma":
        gamma = doc.get("gamma")
        if isinstance(gamma, bool) or not isinstance(gamma, (int, float)) or gamma <= 0:
            raise KitError(
                "camera JSON: the colour mode is legacy gamma but the gamma value is unreadable: pass colour="
            )
        if plate.icc:
            encoding = {"primaries": "unknown", "transfer": "unknown", "icc": "embedded"}
        elif plate.srgb:
            encoding = {"primaries": "rec709", "transfer": "srgb", "icc": "none"}
        else:
            encoding = {"primaries": "unknown", "transfer": "unknown", "icc": "none"}
        return {
            "max_colour_mode": "legacy_gamma",
            "legacy_gamma": float(gamma),
            "working_space": "lin_rec709",
            "master": "display_tiff16",
            "vfb_corrections_baked": False,
            "display_encoding": encoding,
        }
    if mode == "ocio":
        raise KitError(
            "the scene uses OCIO: pass colour= with the ocio block (config_name, config_hash, display, view)"
        )
    raise KitError(
        "the kit could not read the Max colour mode: pass colour= (a packet with an unknown mode is not sent)"
    )


# --------------------------------------------------------------------------- the conversion


@dataclass
class KitResult:
    packet_dir: Path
    packet_id: str
    flags: list[str]
    warnings: list[str]
    manifest: dict[str, Any]


def _node_shares(
    nodes: list[dict[str, Any]],
    shares: Mapping[str, float],
    names_by_hex: Mapping[str, str],
) -> list[dict[str, Any]]:
    """The visible nodes: kit legend nodes the render covers, plus covered ids the legend did not name
    (named from the EXR's Cryptomatte manifest, on no layer so they stay unlabelled and locked)."""
    out: list[dict[str, Any]] = []
    known: set[str] = set()
    for node in nodes:
        oid = legend_mod.object_id(node["name"])
        known.add(oid)
        share = float(shares.get(oid, 0.0))
        if share > 0:
            out.append({**node, "tags": [], "pixel_share": share})
    for oid, share in sorted(shares.items()):
        if oid in known or not oid.startswith("o:") or not share or share <= 0:
            continue
        name = names_by_hex.get(oid[2:].lower())
        if name is None:
            raise KitError(
                f"the render covers {oid} ({share:.2%} of the frame) but neither the kit legend nor the EXR's "
                "Cryptomatte manifest names it"
            )
        out.append({"name": name, "layer": None, "material": None, "tags": [], "pixel_share": float(share)})
    return out


def _autotag(legend_doc: dict[str, Any]) -> dict[str, Any]:
    """Give unlabelled objects an autotag class and make each material as restrictive as its objects. A guess
    never makes anything editable (policy.py still needs an editable layer prefix)."""
    doc = autotag.apply_autotag_to_legend(legend_doc)
    by_material: dict[str, list[str]] = {}
    for e in doc["entries"]:
        if e["kind"] == "object" and e.get("material"):
            by_material.setdefault(e["material"], []).append(e["class"])
    for e in doc["entries"]:
        if e["kind"] == "material" and e["name"] in by_material:
            classes = by_material[e["name"]]
            e["class"] = next((c for c in policy_mod.NEVER_EDITABLE_CLASSES if c in classes), classes[0])
    return doc


def convert_kit(
    folder: Path | str,
    camera: str,
    plate_path: Path | str,
    *,
    hasher: Hasher | None = None,
    pixel_shares: PixelShares | None = None,
    plate_to_tiff16: PlateConverter | None = None,
    project_id: str = "client-kit",
    shot_id: str | None = None,
    out_dir: Path | str | None = None,
    packet_id: str | None = None,
    supersedes: str | None = None,
    created_utc: str | None = None,
    colour: Mapping[str, Any] | None = None,
    custody: Mapping[str, Any] | None = None,
    max_unlabelled_share: float = policy_mod.DEFAULT_MAX_UNLABELLED_SHARE,
) -> KitResult:
    """Build the packet folder and return where it is, its id and the flags it carries.

    Raises KitError for anything that must stop the job: a missing or inconsistent kit file, an EXR without
    the default layers or with lossy compression or half-float data passes, a plate whose size is not the
    render's (the plate is never resampled), unlabelled IDs over the limit, or a missing capability.
    """
    if hasher is None:
        raise KitError(
            "hasher is required: plate_max has no BLAKE3, pass the daemon's (see the module docstring)"
        )
    if pixel_shares is None:
        raise KitError(
            "pixel_shares is required: Cryptomatte coverage comes from the EXR pixels, which plate_max cannot decode"
        )
    files, doc = kit_files(folder, camera)
    flags: list[str] = []

    cam, scene = _camera_digest(doc, flags)
    try:
        camera_block = packet.build_camera_block(cam)
    except packet.ManifestError as exc:
        raise KitError(str(exc)) from exc
    width, height = cam["resolution"]
    host, renderer = _host_and_renderer(doc)
    nodes = parse_legend_txt(files.legend_txt.read_text(encoding="utf-8-sig"))

    header = read_exr_header(files.exr)
    first = header.parts[0]
    if (first.width, first.height) != (width, height):
        raise KitError(
            f"the EXR is {first.width}x{first.height} but the camera JSON says the render was {width}x{height}"
        )
    for part in header.parts:
        if part.compression not in LOSSLESS_EXR:
            raise KitError(
                f"the EXR is {part.compression}-compressed: only lossless compression ({', '.join(LOSSLESS_EXR)}) keeps "
                "Cryptomatte ids and depth intact"
            )
    names = {
        **DEFAULT_LAYERS,
        **{
            k: v for k, v in (doc.get("elements") or {}).items() if k in DEFAULT_LAYERS and isinstance(v, str)
        },
    }
    elements = _elements(header, names, flags)
    covered = {
        c if (e["layer"] == "RGBA" or "." in c) else f"{e['layer']}.{c}"
        for e in elements
        for c in e["channels"]
    }
    unlisted = sorted({c.split(".")[0] for c in header.channels if c not in covered and "." in c})

    plate_src = Path(plate_path)
    plate = inspect_plate(plate_src)
    if (plate.width, plate.height) != (width, height):
        raise KitError(
            f"the plate is {plate.width}x{plate.height} but the render is {width}x{height}: re-render at the plate's "
            "size (a plate is never resampled)"
        )
    direct = plate.is_tiff16_rgb
    if not direct and plate_to_tiff16 is None:
        raise KitError(
            f"the plate is a {plate.bit_depth}-bit {plate.format.upper()} ({plate.channels}): pass plate_to_tiff16 to make the 16-bit TIFF master"
        )
    if plate.bit_depth == 8:
        flags.append("client_plate_8bit")
    if plate.format == "jpeg":
        flags.append("client_plate_jpeg")

    pid = packet_id or new_packet_id()
    if not PACKET_ID_RE.match(pid):
        raise KitError("packet_id must be pkt_ + a 26 character ULID")
    slug = shot_id or packet.slugify(camera)
    root = Path(out_dir) if out_dir is not None else Path(folder) / pid
    if root.exists() and any(root.iterdir()):
        raise KitError(f"{root} already holds files: packets are written to a new folder")
    created = not root.exists()
    try:
        root.mkdir(parents=True, exist_ok=True)
        exr_dst = root / "beauty.exr"
        _copy_atomic(files.exr, exr_dst)
        beauty_hash = _hex_of(hasher(exr_dst))

        tif_dst = root / "display.tif"
        if direct:
            _copy_atomic(plate_src, tif_dst)
            master = plate
        else:
            plate_to_tiff16(plate_src, tif_dst)  # type: ignore[misc]
            master = inspect_plate(tif_dst)
            if not master.is_tiff16_rgb or (master.width, master.height) != (width, height):
                raise KitError("plate_to_tiff16 must write a 16-bit RGB(A) TIFF of the plate's size")
        tif_hash = _hex_of(hasher(tif_dst))

        shares = pixel_shares(exr_dst, elements) if callable(pixel_shares) else pixel_shares
        obj_layer = next(e["layer"] for e in elements if e["element"] == "cryptomatte_object")
        mat_layer = next(e["layer"] for e in elements if e["element"] == "cryptomatte_material")
        obj_manifest = _crypto_manifest(header, obj_layer)
        mat_manifest = _crypto_manifest(header, mat_layer)
        if obj_manifest is None:
            flags.append("cryptomatte_manifest_missing")
        else:
            overlap = [n for n in nodes if n["name"] in obj_manifest]
            if nodes and not overlap:
                raise KitError(
                    f"the {obj_layer} Cryptomatte layer names none of the kit legend's nodes: its ID type is not node name"
                )
            if any(obj_manifest[n["name"]] != legend_mod.cryptomatte_hash(n["name"]) for n in overlap):
                flags.append("exr_manifest_hash_differs")
            if len(overlap) < len(nodes):
                flags.append("legend_nodes_not_in_exr")
        mat_names = {n["material"] for n in nodes if n["material"]}
        if mat_manifest is not None and mat_names and not mat_names & set(mat_manifest):
            flags.append("material_layer_names_unmatched")
        names_by_hex = {v: k for k, v in (obj_manifest or {}).items()}

        visible = _node_shares(nodes, shares, names_by_hex)

        def digest_fn(data: bytes) -> str:
            return _hex_of(hasher(data))

        try:
            legend_doc = legend_mod.build_id_legend(visible, pid, digest_fn)
        except legend_mod.LegendError as exc:
            raise KitError(f"legend: {exc}") from exc
        legend_doc = _autotag(legend_doc)
        pol = policy_mod.derive_policy(legend_doc)

        colour_block = _colour_block(doc, master, colour, flags)
        render = {
            "session": "interactive",
            "sampler": "unknown",
            "time_s": _optional_number(doc, "render_time_s"),
            "denoiser": {"beauty": "unknown", "denoised_elements": []},
        }
        lb = packet.legend_bytes(legend_doc)
        beauty_ref = packet.file_ref(
            "beauty.exr",
            exr_dst.stat().st_size,
            beauty_hash,
            "exr",
            width=width,
            height=height,
            compression=first.compression,
            parts="multi" if len(header.parts) > 1 else "single",
            elements=elements,
        )
        display_ref = packet.file_ref(
            "display.tif",
            tif_dst.stat().st_size,
            tif_hash,
            "tiff",
            width=width,
            height=height,
            bit_depth=16,
            channels=master.channels,
        )
        flags_doc = {
            "source": KIT_NAME,
            "kit_version": doc.get("kit_version"),
            "flags": sorted(set(flags)),
            "client_plate": {
                "format": plate.format,
                "bit_depth": plate.bit_depth,
                "channels": plate.channels,
                "blake3": _hex_of(hasher(plate_src)),
                "promoted_to_tiff16": not direct,
            },
            "unlisted_exr_layers": unlisted,
            "kit_warnings": cam["warnings"],
        }
        fb = dump_json_bytes(flags_doc)
        atomic_write_bytes(root / "kit_flags.json", fb)
        files_block = {
            "beauty_exr": beauty_ref,
            "display_tiff16": display_ref,
            "id_legend": packet.file_ref("id_legend.json", len(lb), _hex_of(hasher(lb)), "json"),
            "x_kit_flags": packet.file_ref("kit_flags.json", len(fb), _hex_of(hasher(fb)), "json"),
        }

        scene_payload = canonical_json_bytes(
            {
                "method": "kit-v0",
                "kit_version": doc.get("kit_version"),
                "camera": cam["name"],
                "renderer": doc.get("renderer"),
                "units": doc.get("units"),
                "nodes": sorted([n["name"], n["layer"], n["material"]] for n in nodes),
            }
        )
        view_payload = packet.view_hash_payload(
            camera_block,
            legend_doc,
            colour_block,
            render,
            {"renderer": doc.get("renderer"), "kit": doc.get("kit_version")},
        )
        try:
            manifest = packet.build_manifest(
                cam,
                scene,
                files_block,
                legend_doc,
                pol,
                project_id,
                slug,
                host,
                renderer,
                colour_block,
                render,
                scene_hash={"hex": _hex_of(hasher(scene_payload)), "digest_version": KIT_DIGEST_VERSION},
                view_hash=_hex_of(hasher(view_payload)),
                supersedes=supersedes,
                created_utc=created_utc,
                custody=dict(custody) if custody else None,
                max_unlabelled_share=max_unlabelled_share,
            )
        except packet.ManifestError as exc:
            raise KitError(str(exc)) from exc
        manifest["host"]["plugin"] = {"name": KIT_NAME, "version": str(doc.get("kit_version"))}
        packet.write_packet(root, manifest, legend_doc)
    except BaseException:
        if created:
            shutil.rmtree(root, ignore_errors=True)
        raise
    return KitResult(root, pid, sorted(set(flags)), list(cam["warnings"]), manifest)


def kit_to_packet(folder: Path | str, camera: str, plate_path: Path | str, **options: Any) -> Path:
    """convert_kit(...) returning only the packet folder; see convert_kit for the keyword arguments."""
    return convert_kit(folder, camera, plate_path, **options).packet_dir
