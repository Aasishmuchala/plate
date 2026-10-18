"""Lineage embedding (EXR, PNG, TIFF, MP4/MOV) and the gate-report versioning service (T26)."""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import threading
import zlib
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from plate_daemon import lineage as L
from plate_daemon.gate_reports import NoReport, ReportError, Service

LIN = L.Lineage(
    project_id="tula-clubhouse",
    node_id="node_7",
    run_id="run_01HZX",
    packet_id="pkt_room_a",
    engine_id="nano-banana-2.1",
    engine_version="2.1.3",
    gate_report="gates/run_01HZX.1.json",
    created_utc="2026-10-17T09:41:07.123456Z",
)
SHIP_TIME = datetime(2026, 10, 17, 9, 41, 7, tzinfo=UTC)
LIN_B = L.Lineage("p2", "n2", "run_2", "pkt_2", "gpt-image-2.5", None, {"verdict": "pass", "n": 2})
NON_AI = L.Lineage("p3", "n3", "run_3", "pkt_3", "plate-numeric", None, None)


def _ffmpeg() -> str | None:
    return L._find_tool("ffmpeg", "PLATE_FFMPEG")


def _ffprobe() -> str | None:
    return L._find_tool("ffprobe", "PLATE_FFPROBE")


needs_ffmpeg = pytest.mark.skipif(_ffmpeg() is None, reason="ffmpeg not found (set PLATE_FFMPEG)")


# ---------------------------------------------------------------------------- the record


def test_json_is_compact_sorted_ascii_and_round_trips():
    text = LIN.to_json()
    assert text.isascii() and "\n" not in text and ", " not in text
    assert list(json.loads(text)) == sorted(json.loads(text))
    assert L.Lineage.from_json(text) == LIN
    assert L.Lineage.from_json(LIN_B.to_json()) == LIN_B
    nonascii = L.Lineage("p", "n", "r", "k", "e", engine_version="vé")
    assert nonascii.to_json().isascii() and L.Lineage.from_json(nonascii.to_json()) == nonascii


@pytest.mark.parametrize(
    "patch",
    [
        {"project_id": ""},
        {"run_id": 7},
        {"node_id": "a\nb"},
        {"packet_id": "x" * 300},
        {"engine_version": 3},
        {"gate_report": ["not", "valid"]},
        {"created_utc": None},
    ],
)
def test_lineage_rejects_bad_fields(patch):
    base = dict(LIN.to_dict())
    base.pop("schema")
    base.update(patch)
    with pytest.raises(L.LineageError):
        L.Lineage.from_dict(base)


def test_lineage_rejects_missing_oversize_and_non_object_payloads():
    with pytest.raises(L.LineageError, match="missing"):
        L.Lineage.from_dict({"project_id": "p"})
    with pytest.raises(L.LineageError, match="JSON object"):
        L.Lineage.from_json("[1, 2]")
    with pytest.raises(L.LineageError, match="not JSON"):
        L.Lineage.from_json("{nope")
    with pytest.raises(L.LineageError, match="too large"):
        L.Lineage.from_json(" " * (L.MAX_PAYLOAD_BYTES + 1))
    with pytest.raises(L.LineageError, match="limit"):
        L.Lineage("p", "n", "r", "k", "e", gate_report={"blob": "x" * L.MAX_PAYLOAD_BYTES}).to_json()
    with pytest.raises(L.LineageError, match="JSON"):
        L.Lineage("p", "n", "r", "k", "e", gate_report={"v": math.nan}).to_json()
    assert L.Lineage.from_dict({**LIN.to_dict(), "future_field": 1, "schema": 9}) == LIN


# ---------------------------------------------------------------------------- PNG


def _srgb_icc() -> bytes:
    from PIL import ImageCms

    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _png_chunks(path: Path) -> list[tuple[bytes, bytes]]:
    raw = path.read_bytes()
    assert raw[:8] == L.PNG_SIGNATURE
    pos, out = 8, []
    while pos < len(raw):
        length, ctype = struct.unpack(">I4s", raw[pos : pos + 8])
        data = raw[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", raw[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(ctype + data) & 0xFFFFFFFF, f"bad CRC on {ctype!r}"
        out.append((ctype, data))
        pos += 12 + length
    return out


def _make_png(path: Path, *, icc: bool = False) -> np.ndarray:
    from PIL import Image

    rng = np.random.default_rng(3)
    arr = (rng.random((24, 40, 3)) * 255).astype(np.uint8)
    Image.fromarray(arr).save(path, icc_profile=_srgb_icc() if icc else None, dpi=(300, 300))
    return arr


def test_png_round_trip_keeps_icc_and_pixels(tmp_path):
    from PIL import Image

    p = tmp_path / "out.png"
    arr = _make_png(p, icc=True)
    before = _png_chunks(p)
    assert L.read(p) is None
    assert L.embed(p, LIN) == p
    assert L.read(p) == LIN
    after = _png_chunks(p)
    kinds = [c for c, _ in after]
    # the lineage chunk, and (nano-banana is a generative engine) the XMP chunk with the IPTC marker (T86)
    assert kinds.count(b"iTXt") == 2 and kinds.index(b"iTXt") < kinds.index(b"IDAT")
    assert [c for c in after if c[0] != b"iTXt"] == before  # iCCP, pHYs, IDAT, IEND: byte for byte
    assert b"iCCP" in kinds
    with Image.open(p) as im:
        assert np.array_equal(np.asarray(im), arr)
        assert L.Lineage.from_json(im.text[L.KEY]) == LIN  # a stock PNG reader sees the keyword
    assert sorted(x.name for x in tmp_path.iterdir()) == ["out.png"]


def test_png_16bit_embed_twice_replaces_the_first(tmp_path):
    import cv2

    p = tmp_path / "deep.png"
    rng = np.random.default_rng(4)
    arr = (rng.random((16, 16, 3)) * 65535).astype(np.uint16)
    assert cv2.imwrite(str(p), arr)
    L.embed(p, LIN)
    L.embed(p, LIN_B)
    assert L.read(p) == LIN_B
    assert [c for c, _ in _png_chunks(p)].count(b"iTXt") == 2  # lineage + XMP marker, each replaced not added
    L.embed(p, NON_AI)
    assert L.read(p) == NON_AI and not L.ai_marker(p)
    assert [c for c, _ in _png_chunks(p)].count(b"iTXt") == 1  # a take no model touched drops the marker
    assert np.array_equal(cv2.imread(str(p), cv2.IMREAD_UNCHANGED), arr)


def _insert_chunk_before_idat(path: Path, chunk: bytes) -> None:
    raw = path.read_bytes()
    pos = 8
    while raw[pos + 4 : pos + 8] != b"IDAT":
        pos += 12 + struct.unpack(">I", raw[pos : pos + 4])[0]
    path.write_bytes(raw[:pos] + chunk + raw[pos:])


def _chunk(ctype: bytes, data: bytes, *, crc: int | None = None) -> bytes:
    good = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", good if crc is None else crc)


def test_png_reads_a_compressed_itxt_and_ignores_bad_ones(tmp_path):
    key = L.KEY.encode() + b"\x00"
    packed = key + b"\x01\x00" + b"\x00" + b"\x00" + zlib.compress(LIN.to_json().encode())
    plain = key + b"\x00\x00" + b"\x00" + b"\x00" + LIN_B.to_json().encode()
    other = b"Comment\x00\x00\x00\x00\x00" + b"hello"

    p = tmp_path / "z.png"
    _make_png(p)
    _insert_chunk_before_idat(p, _chunk(b"iTXt", other) + _chunk(b"iTXt", packed))
    assert L.read(p) == LIN

    bad_crc = tmp_path / "crc.png"
    _make_png(bad_crc)
    _insert_chunk_before_idat(bad_crc, _chunk(b"iTXt", plain, crc=12345))
    assert L.read(bad_crc) is None

    huge = tmp_path / "huge.png"
    _make_png(huge)
    _insert_chunk_before_idat(
        huge, _chunk(b"iTXt", key + b"\x00\x00\x00\x00" + b"x" * (L.MAX_PAYLOAD_BYTES * 2))
    )
    assert L.read(huge) is None

    bomb = tmp_path / "bomb.png"
    _make_png(bomb)
    inflated = key + b"\x01\x00\x00\x00" + zlib.compress(b" " * (L.MAX_PAYLOAD_BYTES * 8))
    _insert_chunk_before_idat(bomb, _chunk(b"iTXt", inflated))
    assert L.read(bomb) is None

    wrong = tmp_path / "wrong.png"
    _make_png(wrong)
    _insert_chunk_before_idat(wrong, _chunk(b"iTXt", key + b"\x00\x00\x00\x00" + b'{"run_id": 5}'))
    assert L.read(wrong) is None


# ---------------------------------------------------------------------------- TIFF


def _tags(path: Path) -> dict[int, object]:
    import tifffile

    with tifffile.TiffFile(path) as tf:
        return {t.code: t.value for t in tf.pages[0].tags.values()}


def _same_value(a, b) -> bool:
    return (
        bool(np.array_equal(np.asarray(a, dtype=object), np.asarray(b, dtype=object)))
        if not isinstance(a, (bytes, str))
        else a == b
    )


@pytest.mark.parametrize(
    "opts",
    [
        {},
        {"bigtiff": True, "byteorder": ">"},
        {"byteorder": ">"},
        {"tile": (16, 16)},
    ],
    ids=["classic-le", "bigtiff-be", "classic-be", "tiled"],
)
def test_tiff_embed_is_lossless_and_adds_only_the_xmp_tag(tmp_path, opts):
    import tifffile

    p = tmp_path / "grade.tif"
    rng = np.random.default_rng(5)
    arr = (rng.random((40, 56, 3)) * 65535).astype(np.uint16)
    tifffile.imwrite(
        p,
        arr,
        photometric="rgb",
        compression="zlib",
        metadata=None,
        software=False,
        iccprofile=_srgb_icc(),
        **opts,
    )
    before = _tags(p)
    assert L.read(p) is None
    L.embed(p, LIN)
    assert L.read(p) == LIN
    after = _tags(p)
    assert set(after) == set(before) | {700}
    for code, value in before.items():
        assert _same_value(value, after[code]), code
    with tifffile.TiffFile(p) as tf:
        assert np.array_equal(tf.asarray(), arr)
        assert len(tf.pages) == 1
    xmp = bytes(after[700]).decode("utf-8")
    assert f'xmlns:plate="{L.XMP_NS}"' in xmp and xmp.count("<plate:lineage>") == 1
    L.embed(p, LIN_B)  # replaced, not stacked
    assert L.read(p) == LIN_B
    assert bytes(_tags(p)[700]).decode("utf-8").count("<plate:lineage>") == 1
    assert sorted(x.name for x in tmp_path.iterdir()) == ["grade.tif"]


def test_tiff_with_two_pages_gets_the_tag_on_the_first_only(tmp_path):
    import tifffile

    p = tmp_path / "two.tif"
    rng = np.random.default_rng(6)
    pages = [(rng.random((20, 30)) * 255).astype(np.uint8) for _ in range(2)]
    with tifffile.TiffWriter(p) as tw:
        for page in pages:
            tw.write(page, photometric="minisblack", metadata=None, software=False)
    L.embed(p, LIN)
    assert L.read(p) == LIN
    with tifffile.TiffFile(p) as tf:
        assert len(tf.pages) == 2
        assert 700 not in tf.pages[1].tags
        for page, expected in zip(tf.pages, pages, strict=True):
            assert np.array_equal(page.asarray(), expected)


FOREIGN_XMP = (
    b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>Sthyra</dc:creator>'
    b"</rdf:Description></rdf:RDF></x:xmpmeta>\n"
    b'<?xpacket end="w"?>'
)


def _tif_with_xmp(path: Path, xmp: bytes) -> None:
    import tifffile

    arr = np.zeros((8, 8, 3), np.uint8)
    tifffile.imwrite(
        path, arr, photometric="rgb", metadata=None, software=False, extratags=[(700, 1, len(xmp), xmp, True)]
    )


def test_tiff_keeps_a_foreign_xmp_packet_and_extends_it(tmp_path):
    p = tmp_path / "x.tif"
    _tif_with_xmp(p, FOREIGN_XMP)
    L.embed(p, LIN)
    L.embed(p, LIN_B)
    assert L.read(p) == LIN_B
    xmp = bytes(_tags(p)[700]).decode("utf-8")
    assert "<dc:creator>Sthyra</dc:creator>" in xmp and xmp.count("<plate:lineage>") == 1
    assert xmp.startswith("<?xpacket begin=") and xmp.endswith('<?xpacket end="w"?>')


def test_tiff_xmp_with_entities_or_garbage_reads_as_none(tmp_path):
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaaaaaaaa">]>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><lineage>&a;</lineage></x:xmpmeta>'
    )
    for name, xmp in {"bomb": bomb, "garbage": b"\x00\x01 not xml", "empty-ish": b"<a/>"}.items():
        p = tmp_path / f"{name}.tif"
        _tif_with_xmp(p, xmp)
        assert L.read(p) is None, name


def test_tiff_refuses_things_that_are_not_tiffs(tmp_path):
    p = tmp_path / "fake.tif"
    p.write_bytes(b"II*\x00" + b"\x00" * 64)  # a header pointing at nothing
    before = p.read_bytes()
    with pytest.raises(L.LineageError):
        L.embed(p, LIN)
    assert p.read_bytes() == before and L.read(p) is None
    q = tmp_path / "not.tif"
    q.write_bytes(b"hello world, definitely not a tiff")
    with pytest.raises(L.LineageError, match="not a TIFF"):
        L.embed(q, LIN)


# ---------------------------------------------------------------------------- EXR


def _header(path: Path) -> dict:
    """The first part's header, read in a function so the OpenEXR File is freed on return. A File object
    kept alive past its `with` block still holds the file open on Windows, and the next embed's os.replace
    then fails with "Access is denied"."""
    import OpenEXR

    with OpenEXR.File(str(path), header_only=True) as exr:
        return dict(exr.header())


def _channels(path: Path) -> dict[str, np.ndarray]:
    import OpenEXR

    with OpenEXR.File(str(path), separate_channels=True) as exr:
        return {
            f"{i}:{k}": v.pixels.copy() for i, part in enumerate(exr.parts) for k, v in part.channels.items()
        }


def test_exr_single_part_round_trip_changes_nothing_but_the_header(tmp_path):
    import OpenEXR

    rng = np.random.default_rng(7)
    channels = {
        "R": rng.random((18, 26), dtype=np.float32).astype(np.float16),
        "G": rng.random((18, 26), dtype=np.float32).astype(np.float16),
        "B": rng.random((18, 26), dtype=np.float32).astype(np.float16),
        "Z": rng.random((18, 26), dtype=np.float32),
    }
    header = {
        "compression": OpenEXR.PIZ_COMPRESSION,
        "type": OpenEXR.scanlineimage,
        "cryptomatte/abc/name": "CryptoObject",
    }
    p = tmp_path / "beauty.exr"
    OpenEXR.File(header, channels).write(str(p))
    before = _channels(p)
    assert L.read(p) is None
    L.embed(p, LIN)
    assert L.read(p) == LIN
    after = _channels(p)
    assert list(after) == list(before)
    for k in before:
        assert after[k].dtype == before[k].dtype and np.array_equal(after[k], before[k]), k
    h = _header(p)
    assert h["cryptomatte/abc/name"] == "CryptoObject"
    assert h["compression"] == OpenEXR.PIZ_COMPRESSION
    assert h[L.KEY] == LIN.to_json()
    L.embed(p, LIN_B)
    assert L.read(p) == LIN_B
    assert sorted(x.name for x in tmp_path.iterdir()) == ["beauty.exr"]


def test_exr_multipart_gets_the_attribute_on_every_part(tmp_path):
    import OpenEXR

    rng = np.random.default_rng(8)
    rgb = {n: rng.random((12, 20), dtype=np.float32) for n in "RGB"}
    z = {"Z": rng.random((12, 20), dtype=np.float32)}
    parts = [
        OpenEXR.Part({"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage}, rgb, "beauty"),
        OpenEXR.Part({"compression": OpenEXR.PIZ_COMPRESSION, "type": OpenEXR.scanlineimage}, z, "depth"),
    ]
    p = tmp_path / "multi.exr"
    OpenEXR.File(parts).write(str(p))
    before = _channels(p)
    L.embed(p, LIN)
    assert L.read(p) == LIN
    after = _channels(p)
    assert set(after) == set(before) and all(np.array_equal(after[k], before[k]) for k in before)
    with OpenEXR.File(str(p), header_only=True) as exr:
        assert [part.header.get(L.KEY) for part in exr.parts] == [LIN.to_json()] * 2
        assert [part.header["compression"] for part in exr.parts] == [
            OpenEXR.ZIP_COMPRESSION,
            OpenEXR.PIZ_COMPRESSION,
        ]


def test_exr_attribute_that_is_not_a_lineage_reads_as_none(tmp_path):
    import OpenEXR

    p = tmp_path / "odd.exr"
    OpenEXR.File(
        {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage, L.KEY: '{"run_id": "r"}'},
        {"R": np.zeros((4, 4), np.float32)},
    ).write(str(p))
    assert L.read(p) is None


# ---------------------------------------------------------------------------- MP4 / MOV


def _clip(path: Path) -> None:
    ffmpeg = _ffmpeg()
    common = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "smptebars=size=160x90:rate=12",
    ]
    tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
    for codec in ("libx264", "mpeg4"):
        proc = subprocess.run(
            [*common, "-t", "1", "-c:v", codec, "-pix_fmt", "yuv420p", *tags, str(path)],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            return
    pytest.skip("this ffmpeg has neither libx264 nor mpeg4")


def _probe(path: Path) -> dict | None:
    ffprobe = _ffprobe()
    if ffprobe is None:
        return None
    out = subprocess.run(
        [ffprobe, "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        check=True,
    ).stdout
    return json.loads(out)


@needs_ffmpeg
@pytest.mark.parametrize("ext", [".mp4", ".mov"])
def test_video_embed_is_a_stream_copy_with_the_comment_in_the_container(tmp_path, ext):
    p = tmp_path / f"clip{ext}"
    _clip(tmp_path / "src.mp4")
    shutil.copyfile(tmp_path / "src.mp4", p)
    before = _probe(p)
    assert L.read(p) is None
    L.embed(p, LIN)
    assert L.read(p) == LIN
    L.embed(p, LIN_B)
    assert L.read(p) == LIN_B
    after = _probe(p)
    if before is not None:
        keys = (
            "codec_name",
            "width",
            "height",
            "nb_read_frames",
            "pix_fmt",
            "color_space",
            "color_primaries",
            "color_transfer",
            "color_range",
        )
        assert [{k: s.get(k) for k in keys} for s in after["streams"]] == [
            {k: s.get(k) for k in keys} for s in before["streams"]
        ]
        assert after["format"]["tags"]["comment"] == f"{L.KEY}={LIN_B.to_json()}"
    assert sorted(x.name for x in tmp_path.iterdir()) == sorted([f"clip{ext}", "src.mp4"])


@needs_ffmpeg
def test_video_without_ffmpeg_fails_loudly_and_keeps_the_file(tmp_path, monkeypatch):
    p = tmp_path / "clip.mp4"
    _clip(p)
    before = p.read_bytes()
    monkeypatch.setenv("PLATE_FFMPEG", str(tmp_path / "missing-ffmpeg"))
    with pytest.raises(L.LineageError, match="PLATE_FFMPEG"):
        L.embed(p, LIN)
    assert p.read_bytes() == before
    assert sorted(x.name for x in tmp_path.iterdir()) == ["clip.mp4"]


@needs_ffmpeg
def test_video_that_ffmpeg_rejects_keeps_the_original(tmp_path):
    p = tmp_path / "broken.mp4"
    p.write_bytes(b"\x00\x00\x00\x08free" + b"junk" * 100)
    before = p.read_bytes()
    with pytest.raises(L.LineageError, match="ffmpeg"):
        L.embed(p, LIN)
    assert p.read_bytes() == before and L.read(p) is None
    assert sorted(x.name for x in tmp_path.iterdir()) == ["broken.mp4"]


def test_video_boxes_with_a_foreign_comment_read_as_none(tmp_path):
    def box(t: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", 8 + len(payload), t) + payload

    text = b"shot on a phone"
    cmt = box(b"\xa9cmt", struct.pack(">HH", len(text), 0x15C7) + text)
    p = tmp_path / "phone.mov"
    p.write_bytes(box(b"ftyp", b"qt  \x00\x00\x00\x00qt  ") + box(b"moov", box(b"udta", cmt)))
    assert L.read(p) is None
    ours = f"{L.KEY}={LIN.to_json()}".encode()
    q = tmp_path / "ours.mov"
    q.write_bytes(
        box(b"ftyp", b"qt  \x00\x00\x00\x00qt  ")
        + box(b"moov", box(b"udta", box(b"\xa9cmt", struct.pack(">HH", len(ours), 0x15C7) + ours)))
    )
    assert L.read(q) == LIN
    r = tmp_path / "trunc.mp4"
    r.write_bytes(q.read_bytes()[:30])
    assert L.read(r) is None


# ---------------------------------------------------------------------------- the API's edges


def test_unsupported_extension_and_missing_files(tmp_path):
    gif = tmp_path / "derivative.gif"  # a JPEG has a carrier since T86 (APP1 XMP); a GIF does not
    gif.write_bytes(b"GIF89a not really")
    with pytest.raises(L.UnsupportedFormat, match="no lineage carrier"):
        L.embed(gif, LIN)
    assert L.read(gif) is None and not L.supported(gif)
    jpg = tmp_path / "derivative.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0 not really")
    with pytest.raises(L.LineageError, match="JPEG"):
        L.embed(jpg, LIN)
    assert jpg.read_bytes() == b"\xff\xd8\xff\xe0 not really" and L.read(jpg) is None
    assert L.supported("A.PNG") and L.supported("b.Exr") and L.supported("c.tiff") and L.supported("d.mov")
    assert L.supported("e.JPG") and L.supported("f.jpeg")
    with pytest.raises(FileNotFoundError):
        L.embed(tmp_path / "gone.png", LIN)
    assert L.read(tmp_path / "gone.png") is None
    assert L.read(tmp_path) is None


def test_a_failed_embed_leaves_the_original_and_no_temp_files(tmp_path):
    p = tmp_path / "fake.png"
    p.write_bytes(b"this is not a png at all")
    with pytest.raises(L.LineageError, match="not a PNG"):
        L.embed(p, LIN)
    assert p.read_bytes() == b"this is not a png at all"
    nodat = tmp_path / "nodat.png"
    nodat.write_bytes(L.PNG_SIGNATURE + _chunk(b"IHDR", b"\x00" * 13) + _chunk(b"IEND", b""))
    with pytest.raises(L.LineageError, match="no IDAT"):
        L.embed(nodat, LIN)
    e = tmp_path / "bad.exr"
    e.write_bytes(b"not an exr")
    with pytest.raises(L.LineageError, match="could not embed lineage"):
        L.embed(e, LIN)
    assert sorted(x.name for x in tmp_path.iterdir()) == ["bad.exr", "fake.png", "nodat.png"]


def test_embed_goes_through_replace_with_retry(tmp_path, monkeypatch):
    p = tmp_path / "r.png"
    _make_png(p)
    calls: list[tuple[Path, Path]] = []
    real = L.replace_with_retry
    monkeypatch.setattr(
        L, "replace_with_retry", lambda src, dst, *a, **k: (calls.append((src, dst)), real(src, dst))[1]
    )
    L.embed(p, LIN)
    assert (
        len(calls) == 1
        and calls[0][1] == p
        and calls[0][0].parent == p.parent
        and ":" not in calls[0][0].name
    )


def test_lineage_survives_the_cas_but_a_new_embed_is_a_new_hash(tmp_path):
    from plate_daemon.cas import CAS, hash_file

    p = tmp_path / "o.png"
    _make_png(p)
    plain = hash_file(p)[0]
    L.embed(p, LIN)
    key = CAS(tmp_path / "assets").put_file(p)
    assert key != plain and key == hash_file(p)[0]
    assert L.read(CAS(tmp_path / "assets").path_for(key, "png")) == LIN


# ---------------------------------------------------------------------------- gate reports


def _report(**over):
    base = {
        "gate_version": "0.4.0",
        "metric_code_hash": "ab" * 32,
        "threshold_set_id": "thr-2026-10-17",
        "engine": {"id": "nano-banana-2.1", "version": "2.1.3"},
        "tier": "A",
        "metrics": {"texture.lapvar": 412.0, "ral.de00": 0.4},
        "checks": [],
        "verdict": "pass",
    }
    base.update(over)
    return base


def _dumped(data: dict) -> bytes:
    return (json.dumps(data, indent=1, allow_nan=False) + "\n").encode("utf-8")


def test_reports_are_numbered_never_overwritten_and_read_back(tmp_path):
    svc = Service(tmp_path)
    assert svc.latest("run_1") is None and svc.versions("run_1") == [] and svc.history("run_1") == []
    p1 = svc.write("run_1", _report())
    p2 = svc.write("run_1", _report(verdict="fail"))
    other = svc.write("run_2", _report())
    assert [p.name for p in (p1, p2, other)] == ["run_1.1.json", "run_1.2.json", "run_2.1.json"]
    assert p1.parent == tmp_path / "gates"
    assert p1.read_bytes() == _dumped(_report())  # the layout plate_gates.report.write produces
    assert b"\r" not in p1.read_bytes()
    latest = svc.latest("run_1")
    assert (latest.n, latest.path, latest.report["verdict"], latest.shipped) == (2, p2, "fail", False)
    assert [r.n for r in svc.history("run_1")] == [1, 2] and svc.versions("run_2") == [1]
    assert svc.get("run_1", 1).report == _report()
    with pytest.raises(NoReport):
        svc.get("run_1", 3)
    # An existing file is never overwritten, even when the directory scan misses it
    (tmp_path / "gates" / "run_3.1.json").write_text("{}", encoding="utf-8")
    svc2 = Service(tmp_path)
    monkey_versions = svc2.versions
    svc2.versions = lambda run_id: [] if run_id == "run_3" else monkey_versions(run_id)  # type: ignore[method-assign]
    assert svc2.write("run_3", _report()).name == "run_3.2.json"
    assert (tmp_path / "gates" / "run_3.1.json").read_text(encoding="utf-8") == "{}"


class _Model:
    """Stands in for plate_gates.report.GateReport: anything with model_dump(mode='json')."""

    def model_dump(self, mode: str = "python") -> dict:
        assert mode == "json"
        return _report(tier="C")


def test_reports_accept_models_and_refuse_anonymous_or_non_strict_ones(tmp_path):
    svc = Service(tmp_path)
    svc.write("run_m", _Model())
    assert svc.get("run_m", 1).report["tier"] == "C"
    for missing in ("gate_version", "metric_code_hash", "threshold_set_id"):
        bad = _report()
        bad.pop(missing)
        with pytest.raises(ReportError, match=missing):
            svc.write("run_x", bad)
    with pytest.raises(ReportError, match="strict JSON"):
        svc.write("run_x", _report(metrics={"nan": math.nan}))
    with pytest.raises(ReportError, match="dict or a GateReport"):
        svc.write("run_x", "a string")
    assert svc.versions("run_x") == []
    for bad_id in ("../escape", "a:b", "run 1", "", "x.1", "a/b"):
        with pytest.raises(ReportError, match="run_id"):
            svc.write(bad_id, _report())
    assert not (tmp_path.parent / "escape.1.json").exists()


def test_a_report_dict_is_copied_not_mutated(tmp_path):
    svc = Service(tmp_path)
    rep = _report()
    svc.write("run_1", rep)
    svc.rescore("run_1", rep)
    assert "rescored_from" not in rep and rep == _report()


def test_rescore_writes_the_next_version_and_records_where_from(tmp_path):
    svc = Service(tmp_path)
    with pytest.raises(NoReport):
        svc.rescore("run_1", _report())
    svc.write("run_1", _report())
    p = svc.rescore("run_1", _report(metric_code_hash="cd" * 32, verdict="fail"))
    assert p.name == "run_1.2.json"
    got = svc.latest("run_1")
    assert got.report["rescored_from"] == 1 and got.report["verdict"] == "fail"
    assert "rescored_from_shipped" not in got.report
    third = svc.rescore("run_1", _report(), from_n=1)
    assert third.name == "run_1.3.json" and svc.latest("run_1").report["rescored_from"] == 1
    assert svc.get("run_1", 1).report == _report()  # the first report is as written
    assert "rescored_from" not in svc.get("run_1", 1).report
    with pytest.raises(NoReport):
        svc.rescore("run_1", _report(), from_n=9)


def test_rescore_after_ship_leaves_the_shipped_file_byte_identical(tmp_path):
    svc = Service(tmp_path, clock=lambda: SHIP_TIME)
    first = svc.write("run_1", _report())
    shipped = svc.shipped("run_1")
    assert shipped.n == 1 and shipped.shipped and shipped.path == first
    sidecar = tmp_path / "gates" / "run_1.1.shipped.json"
    mark = json.loads(sidecar.read_text(encoding="utf-8"))
    assert mark["shipped_utc"] == shipped.shipped_utc and mark["n"] == 1 and mark["report_file"] == first.name
    shipped_bytes = first.read_bytes()
    sidecar_bytes = sidecar.read_bytes()
    assert svc.verify_shipped("run_1")

    again = svc.shipped("run_1", 1)  # idempotent: keeps the first timestamp, writes nothing new
    assert again.shipped_utc == shipped.shipped_utc and sidecar.read_bytes() == sidecar_bytes

    rescored = svc.rescore("run_1", _report(metric_code_hash="ef" * 32, verdict="advisory"))
    assert rescored.name == "run_1.2.json" and rescored != first
    assert first.read_bytes() == shipped_bytes  # never touched
    assert sidecar.read_bytes() == sidecar_bytes
    new = svc.latest("run_1")
    assert new.n == 2 and not new.shipped
    assert new.report["rescored_from"] == 1 and new.report["rescored_from_shipped"] is True
    assert [r.n for r in svc.shipped_versions("run_1")] == [1]
    assert svc.verify_shipped("run_1") and svc.verify_shipped("run_1", 1)

    # A second rescore keeps writing new files and the shipped one stays put
    svc.rescore("run_1", _report())
    assert svc.versions("run_1") == [1, 2, 3] and first.read_bytes() == shipped_bytes

    # Shipping the re-scored version marks that one, not the first
    svc.shipped("run_1", 3)
    assert [r.n for r in svc.shipped_versions("run_1")] == [1, 3]


def test_tampering_with_a_shipped_report_is_detected(tmp_path):
    svc = Service(tmp_path)
    p = svc.write("run_1", _report())
    with pytest.raises(NoReport, match="no shipped"):
        svc.verify_shipped("run_1")
    svc.shipped("run_1")
    assert svc.verify_shipped("run_1")
    p.write_text(p.read_text(encoding="utf-8").replace("pass", "fail"), encoding="utf-8", newline="\n")
    assert svc.verify_shipped("run_1") is False
    with pytest.raises(NoReport, match="never marked"):
        svc.verify_shipped("run_1", 2)
    with pytest.raises(NoReport):
        Service(tmp_path / "empty").shipped("run_9")


def test_the_sidecar_does_not_look_like_a_report_to_the_gates_package(tmp_path):
    svc = Service(tmp_path)
    svc.write("run_1", _report())
    svc.shipped("run_1")
    names = sorted(p.name for p in (tmp_path / "gates").iterdir())
    assert names == ["run_1.1.json", "run_1.1.shipped.json"]
    assert svc.versions("run_1") == [1]  # plate_gates.report.write uses the same ^run\.(\d+)\.json$ rule


def test_concurrent_writers_each_get_their_own_version(tmp_path):
    svc = Service(tmp_path)
    paths: list[Path] = []
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            paths.append(svc.write("run_1", _report(metrics={"i": i})))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(p.name for p in paths) == sorted(f"run_1.{n}.json" for n in range(1, 13))
    assert sorted(r.report["metrics"]["i"] for r in svc.history("run_1")) == list(range(12))


def test_the_ship_time_comes_from_the_injected_clock_and_is_not_in_a_file_name(tmp_path):
    svc = Service(tmp_path, clock=lambda: SHIP_TIME)
    svc.write("run_1", _report())
    got = svc.shipped("run_1")
    assert got.shipped_utc == "2026-10-17T09:41:07.000000Z"
    assert all(":" not in p.name for p in (tmp_path / "gates").iterdir())
