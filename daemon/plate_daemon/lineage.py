"""Lineage: every output carries the ids that made it, inside the file (BUILD_PLAN decision 26).

`Lineage` is the small record `{project_id, node_id, run_id, packet_id, engine_id, engine_version,
gate_report, created_utc}`; `embed(path, lineage)` writes it into the file and `read(path)` gets it back.
The payload is the record as compact, key-sorted, pure-ASCII JSON under the key `plate:lineage`:

    .exr          header attribute `plate:lineage` (string) on every part, via OpenEXR 3.x, rewritten
                  through a temp file (pixels, channel set, compression and other attributes unchanged)
    .png          iTXt chunk, keyword `plate:lineage`, inserted before the first IDAT; every other chunk
                  (iCCP, gAMA, the pixel data) is copied byte for byte
    .tif/.tiff    XMP packet in tag 700 with `<plate:lineage>` in the namespace `XMP_NS`; an existing XMP
                  packet is kept and extended. The file is patched, not re-encoded: the copy gets a new
                  first IFD (one entry more) appended at its end and the header pointer moved to it, so
                  strips, tiles, ICC, compression and every other tag stay bit-identical. The old IFD and
                  any old tag-700 bytes are left behind as unreferenced slack (about 1 KB per embed).
    .mp4/.mov     `comment=plate:lineage=<json>` container metadata through `ffmpeg -c copy` (`-map 0`,
                  `+faststart`): `moov/udta/meta/ilst/(c)cmt` for mp4, `moov/udta/(c)cmt` for mov. Read
                  back by walking the boxes, so no ffprobe is needed. An existing `comment` is replaced.

Embedding changes the file's bytes, so embed before the file goes into the CAS (the CAS key is then the
hash of what was delivered). Every embed is written to a sibling temp file, read back and compared, and
only then moved over the original with `os.replace` (retried on Windows), so a failure leaves the original
untouched. A stripped derivative (a JPEG, a screenshot) has no payload: `phash.resolve_drop` falls back
to the content hash and the perceptual hash for those.

The payload of a dropped file is untrusted input: `read` returns None for anything that is not a
well-formed Lineage (wrong types, a payload over `MAX_PAYLOAD_BYTES`, a DOCTYPE in the XMP, a bad CRC) and
never raises on a hostile file. EXR embedding loads the whole image, which is fine for finished outputs
(RGB or RGBA) and not meant for the multi-element packet EXRs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO
from xml.sax.saxutils import escape as xml_escape

from .fsutil import replace_with_retry, temp_sibling
from .timeutil import iso, utcnow

__all__ = ["KEY", "XMP_NS", "Lineage", "LineageError", "UnsupportedFormat", "embed", "read", "supported"]

KEY = "plate:lineage"
SCHEMA = 1
XMP_NS = "https://ns.sthyra.com/plate/1.0/"
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_FIELD_CHARS = 256
MAX_REPORT_REF_CHARS = 1024
CHUNK = 1 << 20
FFMPEG_TIMEOUT_S = 900


class LineageError(ValueError):
    pass


class UnsupportedFormat(LineageError):
    pass


# ---------------------------------------------------------------------------- the record


def _text_field(name: str, value: Any, *, required: bool = True, limit: int = MAX_FIELD_CHARS) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str):
        raise LineageError(f"lineage.{name} must be a string")
    if required and not value:
        raise LineageError(f"lineage.{name} must not be empty")
    if len(value) > limit or not value.isprintable():
        raise LineageError(f"lineage.{name} is too long or has control characters")


@dataclass(frozen=True)
class Lineage:
    """`gate_report` is a reference (e.g. `gates/<run_id>.<n>.json`) or a small summary dict, not the report."""

    project_id: str
    node_id: str
    run_id: str
    packet_id: str
    engine_id: str
    engine_version: str | None = None
    gate_report: str | dict[str, Any] | None = None
    created_utc: str = field(default_factory=lambda: iso(utcnow()))

    def __post_init__(self) -> None:
        for name in ("project_id", "node_id", "run_id", "packet_id", "engine_id", "created_utc"):
            _text_field(name, getattr(self, name))
        _text_field("engine_version", self.engine_version, required=False)
        gr = self.gate_report
        if isinstance(gr, str):
            _text_field("gate_report", gr, limit=MAX_REPORT_REF_CHARS)
        elif gr is not None and not isinstance(gr, dict):
            raise LineageError("lineage.gate_report must be a string, an object or null")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "project_id": self.project_id,
            "node_id": self.node_id,
            "run_id": self.run_id,
            "packet_id": self.packet_id,
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "gate_report": self.gate_report,
            "created_utc": self.created_utc,
        }

    def to_json(self) -> str:
        """Compact, key-sorted, pure ASCII (safe in an ffmpeg argument, an EXR string, an XMP element)."""
        try:
            text = json.dumps(
                self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            )
        except (TypeError, ValueError) as e:
            raise LineageError(f"lineage is not JSON-serialisable: {e}") from e
        if len(text) > MAX_PAYLOAD_BYTES:
            raise LineageError(f"lineage payload is {len(text)} bytes; the limit is {MAX_PAYLOAD_BYTES}")
        return text

    @classmethod
    def from_dict(cls, data: Any) -> Lineage:
        if not isinstance(data, dict):
            raise LineageError("lineage must be a JSON object")
        missing = [k for k in ("project_id", "node_id", "run_id", "packet_id", "engine_id") if k not in data]
        if missing:
            raise LineageError(f"lineage is missing {missing}")
        kwargs = {
            k: data[k]
            for k in (
                "project_id",
                "node_id",
                "run_id",
                "packet_id",
                "engine_id",
                "engine_version",
                "gate_report",
            )
            if k in data
        }
        if "created_utc" in data:
            kwargs["created_utc"] = data["created_utc"]
        return cls(**kwargs)

    @classmethod
    def from_json(cls, text: str | bytes) -> Lineage:
        if len(text) > MAX_PAYLOAD_BYTES:
            raise LineageError("lineage payload is too large")
        try:
            data = json.loads(text)
        except (ValueError, RecursionError) as e:
            raise LineageError(f"lineage payload is not JSON: {e}") from e
        return cls.from_dict(data)


# ---------------------------------------------------------------------------- shared helpers


def _copy_n(src: BinaryIO, dst: BinaryIO, n: int) -> None:
    while n > 0:
        buf = src.read(min(CHUNK, n))
        if not buf:
            raise LineageError("truncated file")
        dst.write(buf)
        n -= len(buf)


def _find_tool(name: str, env_var: str) -> str | None:
    """`$env_var` (a file, or a folder holding the tool) wins; otherwise PATH. None when absent."""
    override = os.environ.get(env_var)
    if override:
        p = Path(override)
        if p.is_dir():
            p = p / (name + (".exe" if os.name == "nt" else ""))
        return str(p) if p.is_file() else None
    return shutil.which(name)


# ---------------------------------------------------------------------------- EXR


def _exr_embed(src: Path, dst: Path, payload: str) -> None:
    import OpenEXR

    with OpenEXR.File(str(src), separate_channels=True) as exr:
        for part in exr.parts:
            if part.header.get("type") in (OpenEXR.deepscanline, OpenEXR.deeptile):
                raise LineageError("deep EXR is not supported")
            part.header[KEY] = payload
        exr.write(str(dst))


def _exr_read(path: Path) -> str | None:
    import OpenEXR

    with OpenEXR.File(str(path), header_only=True) as exr:
        for part in exr.parts:
            value = part.header.get(KEY)
            if isinstance(value, str):
                return value
    return None


# ---------------------------------------------------------------------------- PNG

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_KEY_B = KEY.encode("latin-1") + b"\x00"


def _itxt_chunk(payload: str) -> bytes:
    # keyword NUL, compression flag 0, method 0, empty language tag NUL, empty translated keyword NUL, text
    data = _KEY_B + b"\x00\x00" + b"\x00" + b"\x00" + payload.encode("utf-8")
    body = b"iTXt" + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _png_chunk_head(fh: BinaryIO) -> tuple[int, bytes]:
    head = fh.read(8)
    if len(head) < 8:
        raise LineageError("truncated PNG: no IEND chunk")
    length, ctype = struct.unpack(">I4s", head)
    if length > 0x7FFFFFFF:
        raise LineageError("corrupt PNG chunk length")
    return length, ctype


def _png_embed(src: Path, dst: Path, payload: str) -> None:
    new_chunk = _itxt_chunk(payload)
    inserted = False
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        sig = fi.read(8)
        if sig != PNG_SIGNATURE:
            raise LineageError(f"{src.name} is not a PNG")
        fo.write(sig)
        while True:
            length, ctype = _png_chunk_head(fi)
            if ctype == b"IDAT" and not inserted:
                fo.write(new_chunk)
                inserted = True
            if ctype == b"iTXt":
                start = fi.tell()
                ours = fi.read(min(length, len(_KEY_B))) == _KEY_B
                fi.seek(start)
                if ours:  # an earlier embed: drop it, the new chunk replaces it
                    fi.seek(length + 4, os.SEEK_CUR)
                    continue
            fo.write(struct.pack(">I4s", length, ctype))
            _copy_n(fi, fo, length + 4)
            if ctype == b"IEND":
                break
    if not inserted:
        raise LineageError(f"{src.name} has no IDAT chunk")


def _png_read(path: Path) -> str | None:
    with open(path, "rb") as fh:
        if fh.read(8) != PNG_SIGNATURE:
            return None
        while True:
            try:
                length, ctype = _png_chunk_head(fh)
            except LineageError:
                return None
            if ctype == b"iTXt" and len(_KEY_B) < length <= MAX_PAYLOAD_BYTES + 256:
                data = fh.read(length)
                crc = fh.read(4)
                if len(crc) == 4 and struct.unpack(">I", crc)[0] == zlib.crc32(b"iTXt" + data) & 0xFFFFFFFF:
                    text = _itxt_text(data)
                    if text is not None:
                        return text
                continue
            fh.seek(length + 4, os.SEEK_CUR)
            if ctype == b"IEND":
                return None


def _itxt_text(data: bytes) -> str | None:
    if not data.startswith(_KEY_B) or len(data) < len(_KEY_B) + 2:
        return None
    flag = data[len(_KEY_B)]
    rest = data[len(_KEY_B) + 2 :]  # skip compression flag and method
    parts = rest.split(b"\x00", 2)  # language tag, translated keyword, text
    if len(parts) != 3:
        return None
    text = parts[2]
    if flag == 1:
        inflater = zlib.decompressobj()
        text = inflater.decompress(text, MAX_PAYLOAD_BYTES + 1)
        if inflater.unconsumed_tail:
            return None
    elif flag != 0:
        return None
    return text.decode("utf-8")


# ---------------------------------------------------------------------------- TIFF (XMP in tag 700)

TIFF_XMP_TAG = 700
MAX_XMP_BYTES = 1 << 20
_XMP_BEGIN = '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
_XMP_END = '<?xpacket end="w"?>'
_PLATE_DESCRIPTION = re.compile(
    r"[ \t]*<rdf:Description\b[^>]*\bxmlns:plate=\"" + re.escape(XMP_NS) + r"\"[^>]*>\s*"
    r"<plate:lineage>.*?</plate:lineage>\s*</rdf:Description>[ \t]*\n?",
    re.DOTALL,
)


def _xmp_description(payload: str) -> str:
    return (
        f'  <rdf:Description rdf:about="" xmlns:plate="{XMP_NS}">\n'
        f"   <plate:lineage>{xml_escape(payload)}</plate:lineage>\n"
        "  </rdf:Description>\n"
    )


def _xmp_packet(payload: str, existing: bytes | None) -> bytes:
    """A new packet, or `existing` with our Description replaced/added and everything else kept."""
    block = _xmp_description(payload)
    if existing:
        try:
            text = existing.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        text = _PLATE_DESCRIPTION.sub("", text)
        close = text.rfind("</rdf:RDF>")
        if close != -1:
            return (text[:close] + block + text[close:]).encode("utf-8")
    text = (
        f"{_XMP_BEGIN}\n"
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        f"{block}"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        f"{_XMP_END}"
    )
    return text.encode("utf-8")


def _xmp_lineage(xmp: bytes) -> str | None:
    if not xmp or len(xmp) > MAX_XMP_BYTES:
        return None
    low = xmp.lower()
    if b"<!doctype" in low or b"<!entity" in low:  # no entity tricks from a dropped file
        return None
    root = ET.fromstring(xmp)
    for el in root.iter():
        if el.tag == f"{{{XMP_NS}}}lineage" and el.text:
            return el.text
        attr = el.get(f"{{{XMP_NS}}}lineage")
        if attr:
            return attr
    return None


_TIFF_TYPE_SIZE = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    6: 1,
    7: 1,
    8: 2,
    9: 4,
    10: 8,
    11: 4,
    12: 8,
    16: 8,
    17: 8,
    18: 8,
}


def _tiff_embed(src: Path, dst: Path, payload: str) -> None:
    shutil.copyfile(src, dst)
    with open(dst, "r+b") as fh:
        head = fh.read(16)
        order = {b"II": "<", b"MM": ">"}.get(head[:2])
        if order is None or len(head) < 8:
            raise LineageError(f"{src.name} is not a TIFF")
        magic = struct.unpack(order + "H", head[2:4])[0]
        if magic == 42:
            big, ptr_pos, off_fmt, cnt_fmt, entry_size = False, 4, "I", "H", 12
            first = struct.unpack(order + "I", head[4:8])[0]
        elif magic == 43 and len(head) == 16 and struct.unpack(order + "H", head[4:6])[0] == 8:
            big, ptr_pos, off_fmt, cnt_fmt, entry_size = True, 8, "Q", "Q", 20
            first = struct.unpack(order + "Q", head[8:16])[0]
        else:
            raise LineageError(f"{src.name} is not a classic or BigTIFF file")
        inline = 8 if big else 4
        cnt_size = struct.calcsize(cnt_fmt)
        off_size = struct.calcsize(off_fmt)
        if first == 0:
            raise LineageError(f"{src.name} has no image directory")
        fh.seek(first)
        (count,) = struct.unpack(order + cnt_fmt, fh.read(cnt_size))
        body = fh.read(count * entry_size)
        tail = fh.read(off_size)
        if len(body) != count * entry_size or len(tail) != off_size:
            raise LineageError(f"{src.name}: truncated TIFF directory")
        entries: list[tuple[int, bytes]] = []
        existing: bytes | None = None
        for i in range(count):
            entry = body[i * entry_size : (i + 1) * entry_size]
            (tag,) = struct.unpack(order + "H", entry[:2])
            if tag != TIFF_XMP_TAG:
                entries.append((tag, entry))
                continue
            etype = struct.unpack(order + "H", entry[2:4])[0]
            n_items = struct.unpack(order + off_fmt, entry[4 : 4 + off_size])[0]
            size = n_items * _TIFF_TYPE_SIZE.get(etype, 1)
            value = entry[4 + off_size :]
            if size <= inline:
                existing = value[:size]
            elif size <= MAX_XMP_BYTES:
                (where,) = struct.unpack(order + off_fmt, value)
                fh.seek(where)
                existing = fh.read(size)
        xmp = _xmp_packet(payload, existing)
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        ifd_off = end + (end & 1)
        ifd_size = cnt_size + (len(entries) + 1) * entry_size + off_size
        data_off = ifd_off + ifd_size
        if not big and data_off + len(xmp) >= 1 << 32:
            raise LineageError(f"{src.name} is too large for a classic TIFF to take another directory")
        new_entry = struct.pack(order + "HH" + off_fmt, TIFF_XMP_TAG, 1, len(xmp)) + struct.pack(
            order + off_fmt, data_off
        )
        entries.append((TIFF_XMP_TAG, new_entry))
        entries.sort(key=lambda e: e[0])
        ifd = struct.pack(order + cnt_fmt, len(entries)) + b"".join(e for _, e in entries) + tail
        fh.write(b"\x00" * (ifd_off - end))
        fh.write(ifd)
        fh.write(xmp)
        fh.seek(ptr_pos)
        fh.write(struct.pack(order + off_fmt, ifd_off))
        fh.flush()
        os.fsync(fh.fileno())


def _tiff_read(path: Path) -> str | None:
    import tifffile

    with tifffile.TiffFile(str(path)) as tf:
        tag = tf.pages[0].tags.get(TIFF_XMP_TAG)
        value = tag.value if tag is not None else None
    if isinstance(value, str):
        value = value.encode("utf-8")
    elif isinstance(value, (tuple, list)):
        value = bytes(value)
    return _xmp_lineage(value) if isinstance(value, (bytes, bytearray)) else None


# ---------------------------------------------------------------------------- MP4 / MOV

_COMMENT_PREFIX = KEY + "="
_MAX_MOOV_BYTES = 64 << 20


def _mov_embed(src: Path, dst: Path, payload: str) -> None:
    ffmpeg = _find_tool("ffmpeg", "PLATE_FFMPEG")
    if ffmpeg is None:
        raise LineageError("ffmpeg not found: put it on PATH or set PLATE_FFMPEG to embed lineage in video")
    container = "mov" if src.suffix.lower() == ".mov" else "mp4"
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-map",
        "0",
        "-map_metadata",
        "0",
        "-c",
        "copy",
        "-metadata",
        f"comment={_COMMENT_PREFIX}{payload}",
        "-movflags",
        "+faststart",
        "-f",
        container,
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as e:
        raise LineageError(f"ffmpeg timed out after {FFMPEG_TIMEOUT_S} s remuxing {src.name}") from e
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise LineageError(f"ffmpeg could not remux {src.name}: {' | '.join(tail)}")


def _boxes(fh: BinaryIO, start: int, end: int):
    """(type, payload_start, box_end) for each box in [start, end); stops at the first malformed one."""
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        size, btype = struct.unpack(">I4s", fh.read(8))
        header = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size, header = struct.unpack(">Q", ext)[0], 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield btype, pos + header, pos + size
        pos += size


def _mov_comment_texts(fh: BinaryIO, start: int, end: int):
    """Text of every `(c)cmt` under a `udta` payload [start, end): QuickTime-style and iTunes-style."""
    for btype, pstart, bend in _boxes(fh, start, end):
        if btype == b"\xa9cmt":  # QuickTime: uint16 length, uint16 language, text
            fh.seek(pstart)
            raw = fh.read(min(bend - pstart, MAX_PAYLOAD_BYTES + 64))
            if len(raw) >= 4:
                (n,) = struct.unpack(">H", raw[:2])
                yield raw[4 : 4 + n].decode("utf-8", "replace")
        elif btype == b"meta":  # full box: 4 bytes of version/flags, then hdlr + ilst
            for mtype, mstart, mend in _boxes(fh, pstart + 4, bend):
                if mtype != b"ilst":
                    continue
                for itype, istart, iend in _boxes(fh, mstart, mend):
                    if itype != b"\xa9cmt":
                        continue
                    for dtype, dstart, dend in _boxes(fh, istart, iend):
                        if dtype == b"data" and 8 < dend - dstart <= MAX_PAYLOAD_BYTES + 64:
                            fh.seek(dstart + 8)  # type indicator + locale
                            yield fh.read(dend - dstart - 8).decode("utf-8", "replace")


def _mov_read(path: Path) -> str | None:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        for btype, pstart, bend in _boxes(fh, 0, size):
            if btype != b"moov" or bend - pstart > _MAX_MOOV_BYTES:
                continue
            for ctype, cstart, cend in _boxes(fh, pstart, bend):
                if ctype != b"udta":
                    continue
                for text in _mov_comment_texts(fh, cstart, cend):
                    if text.startswith(_COMMENT_PREFIX):
                        return text[len(_COMMENT_PREFIX) :]
    return None


# ---------------------------------------------------------------------------- public API

_Writer = Callable[[Path, Path, str], None]
_Reader = Callable[[Path], "str | None"]

_FORMATS: dict[str, tuple[_Writer, _Reader]] = {
    ".exr": (_exr_embed, _exr_read),
    ".png": (_png_embed, _png_read),
    ".tif": (_tiff_embed, _tiff_read),
    ".tiff": (_tiff_embed, _tiff_read),
    ".mp4": (_mov_embed, _mov_read),
    ".mov": (_mov_embed, _mov_read),
}


def supported(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in _FORMATS


def embed(path: str | os.PathLike[str], lineage: Lineage) -> Path:
    """Write `lineage` into the file in place (see the module docstring) and return its path.
    Raises UnsupportedFormat for an extension with no carrier (a JPEG has none) and LineageError when the
    file cannot be rewritten; the original is left as it was in both cases."""
    path = Path(path)
    carrier = _FORMATS.get(path.suffix.lower())
    if carrier is None:
        raise UnsupportedFormat(
            f"{path.name}: no lineage carrier for {path.suffix or 'this file'}; use one of {sorted(_FORMATS)}"
        )
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"{path} is not a regular file")
    write, read_back = carrier
    payload = lineage.to_json()
    tmp = temp_sibling(path)
    try:
        try:
            write(path, tmp, payload)
            written = read_back(tmp)
        except (LineageError, OSError):
            raise
        except Exception as e:  # a codec library's own error type
            raise LineageError(f"{path.name}: could not embed lineage ({type(e).__name__}: {e})") from e
        if written != payload:
            raise LineageError(f"{path.name}: lineage did not read back after writing; original kept")
        replace_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def read(path: str | os.PathLike[str]) -> Lineage | None:
    """The Lineage inside the file, or None: no carrier, no payload, or a payload that is not a well-formed
    Lineage. Never raises for a bad or hostile file."""
    path = Path(path)
    carrier = _FORMATS.get(path.suffix.lower())
    if carrier is None:
        return None
    try:
        raw = carrier[1](path)
        return Lineage.from_json(raw) if raw else None
    except Exception:  # untrusted file: every parser's failure mode means "no lineage here"
        return None
