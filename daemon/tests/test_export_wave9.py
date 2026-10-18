"""Export and delivery, Wave 9 (router plan T86): batch export, social cutdowns, the review cover, and the IPTC
AI-provenance marker in every lineage carrier.

The route tests run on the committed Blender packet `room_a` with an `EchoEngine` that hands the PNG egress sent
it back (shifted by `shift_px`, which fails the camera-drift gate). Clip and previs takes are made on the run
table directly. ffmpeg-dependent tests skip when the tool or the encoder is absent. No client data is read.
"""

from __future__ import annotations

import base64
import json
import re
import struct
import subprocess
import zipfile
import zlib
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import cutdown, lineage, review_bundle
from plate_daemon import lineage as L
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.db import tx
from plate_daemon.delivery import guard, tools
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.gate_reports import Service
from plate_daemon.ledger import Ledger
from plate_daemon.machine import annotate, append_note, create_run, transition
from plate_daemon.models import QUOTE_TTL, Quote, RunCreate
from plate_daemon.quotes import insert_quote
from plate_daemon.routes.export_batch import DEFAULT_PATTERN
from plate_daemon.states import RunState
from plate_daemon.timeutil import utcnow
from plate_pixels import imageio

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
FAKE_ROUTE = {"engine_id": "fake", "provider": "fake", "region": "local"}
REASON = "client signed off the drift"
DST = L.DIGITAL_SOURCE_TYPE
S = RunState

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)

AI = L.Lineage(
    "proj",
    "node_1",
    "run_ai",
    "pkt_1",
    "nano-banana-2.1",
    "2.1.3",
    "gates/run_ai.1.json",
    "2026-10-18T09:00:00Z",
)


def _lin(engine_id: str, **kw) -> L.Lineage:
    return L.Lineage("proj", "node_1", "run_x", "pkt_1", engine_id, **kw)


def _ffmpeg_ok(*encoders: str) -> bool:
    try:
        tools.ffmpeg_path()
        tools.ffprobe_path()
    except guard.ToolMissing:
        return False
    listing = subprocess.run(
        [tools.ffmpeg_path(), "-hide_banner", "-encoders"], capture_output=True, check=False
    ).stdout.decode("utf-8", "replace")
    return all(re.search(rf"\s{re.escape(enc)}\s", listing) for enc in encoders)


needs_x264 = pytest.mark.skipif(not _ffmpeg_ok("libx264"), reason="ffmpeg with libx264 not installed")
needs_x265 = pytest.mark.skipif(
    not _ffmpeg_ok("libx264", "libx265"), reason="ffmpeg with libx265 not installed"
)


def _png_chunks(path: Path) -> list[tuple[bytes, bytes]]:
    raw = path.read_bytes()
    pos, out = 8, []
    while pos < len(raw):
        length, ctype = struct.unpack(">I4s", raw[pos : pos + 8])
        out.append((ctype, raw[pos + 8 : pos + 8 + length]))
        pos += 12 + length
    return out


def _itxt_keywords(path: Path) -> list[bytes]:
    return [data.split(b"\x00", 1)[0] for ctype, data in _png_chunks(path) if ctype == b"iTXt"]


def _rgb16(w: int = 640, h: int = 360, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.random((h, w, 3)) * 65535).astype(np.uint16)


def _clip(path: Path, size: str = "640x360", seconds: int = 2) -> Path:
    tools.run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate=24:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(path),
        ]
    )
    return path


# ---------------------------------------------------------------------------- the crop box


def test_crop_box_vectors():
    assert cutdown.crop_box(3840, 2160, "9:16") == (1312, 0, 1214, 2160)
    assert cutdown.crop_box(3840, 2160, "1:1") == (840, 0, 2160, 2160)
    # The plan lists 4:5 at cx 0.25 as (0, 0, 864, 1080), but centring 864 px on 0.25 x 1920 = 480 gives a left
    # edge of 48. The box it names is what the clamp gives for any cx up to 0.225.
    assert cutdown.crop_box(1920, 1080, "4:5", cx=0.2) == (0, 0, 864, 1080)
    assert cutdown.crop_box(1920, 1080, "4:5", cx=0.25) == (48, 0, 864, 1080)


@pytest.mark.parametrize("preset", list(cutdown.PRESETS))
@pytest.mark.parametrize("size", [(3840, 2160), (1921, 1081), (640, 360), (1080, 1920)])
def test_crop_box_is_even_inside_the_frame_and_the_largest_of_its_aspect(preset, size):
    width, height = size
    for cx, cy in ((0.5, 0.5), (0.0, 0.0), (1.0, 1.0), (0.31, 0.77)):
        x0, y0, w, h = cutdown.crop_box(width, height, preset, cx=cx, cy=cy)
        assert w % 2 == 0 and h % 2 == 0 and x0 % 2 == 0 and y0 % 2 == 0
        assert 0 <= x0 and 0 <= y0 and x0 + w <= width and y0 + h <= height
        assert abs(w / h - cutdown.PRESETS[preset]) < 3.0 / min(w, h)
        assert w >= width - 3 or h >= height - 3  # one side is as large as the frame allows


def test_crop_box_stays_inside_the_safe_frame_and_refuses_nonsense():
    safe = {"x": 0, "y": 46, "width": 640, "height": 268}  # room_a: a 2.39:1 band in a 16:9 frame
    x0, y0, w, h = cutdown.crop_box(640, 360, "16:9", safe_frame=safe)
    assert (w, h) == (476, 268) and y0 == 46 and 0 <= x0 <= 640 - w
    x0, y0, w, h = cutdown.crop_box(640, 360, "9:16", cy=0.0, safe_frame=(0, 46, 640, 268))
    assert y0 == 46 and h == 268
    for bad in (
        lambda: cutdown.crop_box(640, 360, "3:2"),
        lambda: cutdown.crop_box(640, 360, "1:1", cx=1.5),
        lambda: cutdown.crop_box(640, 360, "1:1", safe_frame=(0, 0, 700, 360)),
        lambda: cutdown.crop_box(1, 360, "1:1"),
    ):
        with pytest.raises(cutdown.CutdownError):
            bad()


# ---------------------------------------------------------------------------- still cutdown


def test_still_cutdown_1x1_png_has_lineage_the_xmp_marker_and_a_sidecar(tmp_path):
    from PIL import Image

    rgb = _rgb16()
    box = cutdown.crop_box(640, 360, "1:1")
    assert box == (140, 0, 360, 360)
    out = cutdown.still_cutdown(rgb, box, tmp_path / "a_1x1.png", lineage=AI, preset="1:1")
    im = cv2.imread(str(out), cv2.IMREAD_UNCHANGED)
    assert im.shape == (360, 360, 3) and im.dtype == np.uint8
    want = np.rint(rgb[0:360, 140:500].astype(np.float32) / 65535.0 * 255.0).astype(np.uint8)
    assert np.array_equal(im[..., ::-1], want)  # a crop and one quantisation, nothing else
    assert lineage.read(out) == AI and lineage.ai_marker(out)
    assert b"XML:com.adobe.xmp" in _itxt_keywords(out) and b"plate:lineage" in _itxt_keywords(out)
    with Image.open(out) as pil:
        xmp = pil.info["XML:com.adobe.xmp"]
        assert "icc_profile" in pil.info  # tagged sRGB
    assert "<Iptc4xmpExt:DigitalSourceType>" + DST + "</Iptc4xmpExt:DigitalSourceType>" in xmp
    assert 'xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/"' in xmp
    side = json.loads((tmp_path / "a_1x1.cutdown.json").read_text(encoding="utf-8"))
    assert side == {
        "source_run": "run_ai",
        "box": [140, 0, 360, 360],
        "preset": "1:1",
        "format": "png",
        "gate_report_ref": "gates/run_ai.1.json",
    }
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "a_1x1.cutdown.json",
        "a_1x1.png",
    ]  # no staging left


def test_still_cutdown_jpeg_carries_lineage_marker_and_the_srgb_profile(tmp_path):
    from PIL import Image

    box = cutdown.crop_box(640, 360, "4:5")
    out = cutdown.still_cutdown(
        _rgb16(), box, tmp_path / "b.jpg", "jpeg", quality=88, lineage=AI, preset="4:5"
    )
    assert lineage.read(out) == AI and lineage.ai_marker(out)
    with Image.open(out) as pil:
        assert pil.size == (box[2], box[3]) and pil.format == "JPEG"
        assert pil.info.get("icc_profile")
    assert json.loads((tmp_path / "b.cutdown.json").read_text(encoding="utf-8"))["format"] == "jpeg"
    with pytest.raises(cutdown.CutdownError, match="jpeg"):
        cutdown.still_cutdown(_rgb16(), box, tmp_path / "wrong.png", "jpeg")  # the name must match the format


def test_still_cutdown_never_upscales_and_shrinks_with_area_filter(tmp_path):
    rgb = _rgb16()
    box = cutdown.crop_box(640, 360, "1:1")
    with pytest.raises(cutdown.CutdownError, match="cutdowns never upscale"):
        cutdown.still_cutdown(rgb, box, tmp_path / "up.png", long_side=2000)
    with pytest.raises(cutdown.CutdownError, match="never upscale"):
        cutdown.still_cutdown(rgb, box, tmp_path / "up2.png", long_side=361)
    assert not list(tmp_path.iterdir())
    out = cutdown.still_cutdown(rgb, box, tmp_path / "small.png", long_side=180)
    got = cv2.imread(str(out), cv2.IMREAD_UNCHANGED)
    assert got.shape == (180, 180, 3)
    crop = rgb[0:360, 140:500].astype(np.float32) / 65535.0
    want = np.rint(np.clip(cv2.resize(crop, (180, 180), interpolation=cv2.INTER_AREA), 0, 1) * 255).astype(
        np.uint8
    )
    assert np.array_equal(got[..., ::-1], want)
    assert cv2.imread(str(cutdown.still_cutdown(rgb, box, tmp_path / "same.png", long_side=360))).shape == (
        360,
        360,
        3,
    )


def test_a_cutdown_never_overwrites_and_rejects_a_box_outside_the_frame(tmp_path):
    rgb = _rgb16()
    box = (0, 0, 100, 100)
    out = tmp_path / "keep.png"
    cutdown.still_cutdown(rgb, box, out)
    before = out.read_bytes()
    with pytest.raises(FileExistsError):
        cutdown.still_cutdown(rgb, box, out)
    assert out.read_bytes() == before
    with pytest.raises(cutdown.CutdownError):
        cutdown.still_cutdown(rgb, (600, 0, 100, 100), tmp_path / "oob.png")
    with pytest.raises(cutdown.CutdownError):
        cutdown.still_cutdown(rgb, box, tmp_path / "x.png", "bmp")


# ---------------------------------------------------------------------------- clip cutdown


def _probe_tags(path: Path) -> dict:
    out = subprocess.run(
        [
            tools.ffprobe_path(),
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream=width,height,codec_name,pix_fmt,color_primaries,color_transfer,color_space,color_range",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    ).stdout
    doc = json.loads(out)
    return {**doc["streams"][0], "tags": doc["format"].get("tags", {})}


@needs_x264
def test_clip_cutdown_9x16_keeps_the_height_tags_bt709_and_embeds_lineage(tmp_path):
    src = _clip(tmp_path / "src.mp4")
    box = cutdown.crop_box(640, 360, "9:16")
    out = cutdown.clip_cutdown(src, box, tmp_path / "clip_9x16.mp4", lineage=AI, preset="9:16")
    got = _probe_tags(out)
    assert got["height"] == 360 and got["width"] == box[2] == 202
    assert (got["color_primaries"], got["color_transfer"], got["color_space"], got["color_range"]) == (
        "bt709",
        "bt709",
        "bt709",
        "tv",
    )
    assert got["codec_name"] == "h264" and got["pix_fmt"] == "yuv420p"
    assert lineage.read(out) == AI and lineage.ai_marker(out)
    assert got["tags"]["digital_source_type"] == DST and got["tags"]["comment"].startswith("plate:lineage=")
    side = json.loads((tmp_path / "clip_9x16.cutdown.json").read_text(encoding="utf-8"))
    assert side == {
        "source_run": "run_ai",
        "box": list(box),
        "preset": "9:16",
        "codec": "h264",
        "gate_report_ref": "gates/run_ai.1.json",
    }
    small = cutdown.clip_cutdown(src, box, tmp_path / "small.mp4", long_side=180)
    assert (_probe_tags(small)["width"], _probe_tags(small)["height"]) == (100, 180)
    with pytest.raises(cutdown.CutdownError, match="never upscale"):
        cutdown.clip_cutdown(src, box, tmp_path / "up.mp4", long_side=720)
    with pytest.raises(FileExistsError):
        cutdown.clip_cutdown(src, box, out)
    with pytest.raises(cutdown.CutdownError):
        cutdown.clip_cutdown(src, (1, 0, 202, 360), tmp_path / "odd.mp4")  # an odd offset is not a 4:2:0 crop
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


@needs_x265
def test_clip_cutdown_hevc_and_a_non_bt709_source_are_converted_not_relabelled(tmp_path):
    src = tmp_path / "bt601.mp4"
    tools.run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=24:duration=1",
            "-vf",
            "scale=out_color_matrix=bt601:out_range=tv,format=yuv420p",
            "-c:v",
            "libx264",
            "-colorspace",
            "smpte170m",
            "-color_primaries",
            "smpte170m",
            "-color_trc",
            "smpte170m",
            "-color_range",
            "tv",
            str(src),
        ]
    )
    assert _probe_tags(src)["color_space"] == "smpte170m"
    out = cutdown.clip_cutdown(
        src, (0, 0, 640, 360), tmp_path / "hevc.mp4", "hevc", long_side=320, lineage=AI
    )
    got = _probe_tags(out)
    assert got["codec_name"] == "hevc" and (got["width"], got["height"]) == (320, 180)
    assert (got["color_primaries"], got["color_space"], got["color_range"]) == ("bt709", "bt709", "tv")
    assert lineage.read(out) == AI

    def mean_rgb(path: Path) -> np.ndarray:
        raw = subprocess.run(
            [
                tools.ffmpeg_path(),
                "-v",
                "error",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=320:180",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout
        return np.frombuffer(raw, np.uint8).reshape(180, 320, 3).astype(np.float32)

    # The picture survives: decoded with each file's own tags, source and cutdown agree closely.
    assert np.abs(mean_rgb(src) - mean_rgb(out)).mean() < 6.0


# ---------------------------------------------------------------------------- lineage and the AI marker


@pytest.mark.parametrize(
    ("engine_id", "expected"),
    [
        ("nano-banana-2.1", True),
        ("gpt-image-2.5", True),
        ("fake", True),
        ("import:magnific", True),
        ("plate-numeric", False),
        ("plate-numeric/numeric-local", False),
        ("plate-plugin", False),
        ("import:manual", False),
        ("local-recomposite", False),
    ],
)
def test_ai_touched_follows_the_engine_id_and_is_in_every_record(engine_id, expected):
    lin = _lin(engine_id)
    assert lin.ai_touched is expected
    assert json.loads(lin.to_json())["ai_touched"] is expected
    assert L.Lineage.from_json(lin.to_json()) == lin


def _make(kind: str, tmp_path: Path) -> Path:
    path = tmp_path / f"f.{kind}"
    rgb = _rgb16(48, 32)
    if kind == "png":
        imageio.write_png16(path, rgb)
    elif kind == "tif":
        imageio.write_tiff16(path, rgb)
    elif kind == "jpg":
        assert cv2.imwrite(str(path), (rgb >> 8).astype(np.uint8)[..., ::-1])
    elif kind == "exr":
        import OpenEXR

        planes = {c: np.full((8, 8), 0.25, np.float16) for c in "RGB"}
        with OpenEXR.File(
            {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage}, planes
        ) as f:
            f.write(str(path))
    elif kind == "mp4":
        _clip(path, "128x72", 1)
    return path


STILL_KINDS = ["png", "tif", "jpg", "exr"]


@pytest.mark.parametrize("kind", STILL_KINDS)
def test_the_marker_is_written_for_ai_and_removed_for_a_take_no_model_touched(kind, tmp_path):
    path = _make(kind, tmp_path)
    assert lineage.read(path) is None and not lineage.ai_marker(path)
    lineage.embed(path, AI)
    assert lineage.read(path) == AI
    assert lineage.digital_source_types(path) == [DST]
    manual = _lin("import:manual")
    lineage.embed(path, manual)
    assert lineage.read(path) == manual and lineage.digital_source_types(path) == []
    lineage.embed(path, AI)
    lineage.embed(path, AI)  # embedding twice replaces, never doubles
    assert lineage.digital_source_types(path) == [DST]


@pytest.mark.parametrize("kind", STILL_KINDS)
def test_a_non_ai_embed_into_a_fresh_file_writes_no_marker(kind, tmp_path):
    path = _make(kind, tmp_path)
    lineage.embed(path, _lin("plate-numeric"))
    assert lineage.read(path) is not None and not lineage.ai_marker(path)
    if kind == "png":
        assert _itxt_keywords(path) == [b"plate:lineage"]
    if kind == "exr":
        import OpenEXR

        with OpenEXR.File(str(path), header_only=True) as exr:
            assert all("iptc:DigitalSourceType" not in part.header for part in exr.parts)


def test_exr_marker_is_a_header_attribute_and_pixels_survive(tmp_path):
    import OpenEXR

    path = _make("exr", tmp_path)
    lineage.embed(path, AI)
    with OpenEXR.File(str(path), separate_channels=True) as exr:
        (part,) = exr.parts
        assert part.header["iptc:DigitalSourceType"] == DST
        assert np.allclose(part.channels["R"].pixels.astype(np.float32), 0.25)


def test_tiff_marker_sits_in_the_xmp_packet_beside_the_lineage_and_pixels_survive(tmp_path):
    import tifffile

    path = _make("tif", tmp_path)
    before = tifffile.imread(str(path))
    lineage.embed(path, AI)
    with tifffile.TiffFile(str(path)) as tf:
        xmp = bytes(tf.pages[0].tags[700].value).decode("utf-8")
    assert f"<Iptc4xmpExt:DigitalSourceType>{DST}</Iptc4xmpExt:DigitalSourceType>" in xmp
    assert "<plate:lineage>" in xmp and xmp.count("<rdf:RDF") == 1
    assert np.array_equal(tifffile.imread(str(path)), before)


def test_jpeg_embed_keeps_the_picture_the_jfif_header_and_other_segments(tmp_path):
    path = _make("jpg", tmp_path)
    raw = path.read_bytes()
    exif = b"\xff\xe1" + struct.pack(">H", 2 + 8) + b"Exif\x00\x00AB"  # a stand-in Exif segment after JFIF
    jfif_len = struct.unpack(">H", raw[4:6])[0]
    raw = raw[: 4 + jfif_len] + exif + raw[4 + jfif_len :]
    path.write_bytes(raw)
    pixels = cv2.imread(str(path))
    lineage.embed(path, AI)
    out = path.read_bytes()
    assert out[:2] == b"\xff\xd8" and out[2:4] == b"\xff\xe0"  # JFIF still first
    assert exif in out and out.index(exif) < out.index(b"http://ns.adobe.com/xap/1.0/\x00")
    assert np.array_equal(cv2.imread(str(path)), pixels)  # the scan data is untouched
    assert out.endswith(raw[raw.index(b"\xff\xda") :])  # from SOS to EOI, byte for byte
    assert lineage.read(path) == AI and lineage.ai_marker(path)


def test_jpeg_hostile_and_truncated_files(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\xff\xd8\xff\xe1\x00\xff" + b"x" * 10)
    assert lineage.read(bad) is None and lineage.digital_source_types(bad) == []
    with pytest.raises(lineage.LineageError):
        lineage.embed(bad, AI)
    assert bad.read_bytes().startswith(b"\xff\xd8\xff\xe1")
    nonjpeg = tmp_path / "n.jpg"
    nonjpeg.write_bytes(b"GIF89a")
    assert lineage.read(nonjpeg) is None


def _foreign_xmp() -> bytes:
    return (
        b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b"<dc:creator>Kabir Studio</dc:creator></rdf:Description>"
        b"</rdf:RDF></x:xmpmeta><?xpacket end='w'?>"
    )


def _insert_before_idat(path: Path, chunk: bytes) -> None:
    raw = path.read_bytes()
    pos = 8
    while raw[pos + 4 : pos + 8] != b"IDAT":
        pos += 12 + struct.unpack(">I", raw[pos : pos + 4])[0]
    path.write_bytes(raw[:pos] + chunk + raw[pos:])


def test_png_merges_into_a_foreign_xmp_packet_and_keeps_it_when_the_marker_goes(tmp_path):
    path = _make("png", tmp_path)
    data = b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + _foreign_xmp()
    body = b"iTXt" + data
    _insert_before_idat(path, struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body)))
    lineage.embed(path, AI)
    assert _itxt_keywords(path).count(b"XML:com.adobe.xmp") == 1  # merged, not a second packet
    xmp = lineage._png_text(path, b"XML:com.adobe.xmp\x00", lineage.MAX_XMP_BYTES).decode("utf-8")
    assert "Kabir Studio" in xmp and DST in xmp
    lineage.embed(path, _lin("plate-plugin"))
    xmp = lineage._png_text(path, b"XML:com.adobe.xmp\x00", lineage.MAX_XMP_BYTES).decode("utf-8")
    assert "Kabir Studio" in xmp and DST not in xmp and not lineage.ai_marker(path)


def test_png_with_an_xmp_packet_too_large_to_merge_is_left_untouched(tmp_path):
    path = _make("png", tmp_path)
    data = b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + b"x" * (lineage.MAX_XMP_BYTES + 1024)
    body = b"iTXt" + data
    _insert_before_idat(path, struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body)))
    before = path.read_bytes()
    with pytest.raises(lineage.LineageError, match="too large"):
        lineage.embed(path, AI)
    assert path.read_bytes() == before and not [p for p in tmp_path.iterdir() if p.name != "f.png"]


@pytest.mark.skipif(not _ffmpeg_ok(), reason="ffmpeg not installed")
@pytest.mark.parametrize("suffix", [".mp4", ".mov"])
def test_video_marker_is_a_container_tag_next_to_the_comment(suffix, tmp_path):
    src = _make("mp4", tmp_path)
    path = tmp_path / f"v{suffix}"
    tools.run_ffmpeg(["-i", str(src), "-c", "copy", str(path)])
    lineage.embed(path, AI)
    assert lineage.read(path) == AI and lineage.digital_source_types(path) == [DST]
    tags = _probe_tags(path)["tags"]
    assert tags["digital_source_type"] == DST and tags["comment"].startswith("plate:lineage=")
    lineage.embed(path, _lin("import:manual"))
    assert lineage.read(path) is not None and lineage.digital_source_types(path) == []
    assert "digital_source_type" not in _probe_tags(path)["tags"]


@needs_x264
def test_the_marker_and_the_colour_tags_both_survive_the_remux(tmp_path):
    out = cutdown.clip_cutdown(_clip(tmp_path / "s.mp4"), (0, 0, 640, 360), tmp_path / "o.mp4", lineage=AI)
    first = _probe_tags(out)
    lineage.embed(out, AI)  # a second embed over a file that already has the mdta keys
    again = _probe_tags(out)
    assert first["color_primaries"] == again["color_primaries"] == "bt709"
    assert again["tags"]["digital_source_type"] == DST and lineage.read(out) == AI


def test_the_consent_template_discloses_ai_assisted_finishing_on_social_platforms():
    text = (ROOT / "docs" / "consent-template.md").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "Disclosure on social platforms" in ln)
    assert "AI-assisted" in line and "machine-readable" in line


# ---------------------------------------------------------------------------- the route rig


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent, moved `shift_px` to the right."""

    def __init__(self, *args, shift_px: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.shift_px = shift_px
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        data = self.sent[result_url[len(RESULT_SCHEME) :]]
        if self.shift_px:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
            data = bytes(cv2.imencode(".png", np.roll(img, self.shift_px, axis=1))[1])
        return data


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


def _client(settings: Settings, engine: EchoEngine) -> TestClient:
    c = TestClient(create_app(settings, engine), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _setup(c: TestClient, auth: dict, nodes: tuple[str, ...] = ("e", "e2", "e3")) -> str:
    """Project + room_a ingested + Plate -> an Edit per name in `nodes` on the fake engine. The packet id."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    graph = [{"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}]
    graph += [{"id": n, "kind": "Edit", "label": n, "params": {"engine": "fake"}} for n in nodes]
    edges = [{"id": f"p->{n}", "source": "p", "target": n} for n in nodes]
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": graph, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _run(c: TestClient, auth: dict, node: str) -> dict:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/{node}/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    return c.get(f"/api/v0/runs/{run_id}", headers=auth).json()


def _forge(c: TestClient, packet_id: str, out_key: str, *, node: str, inputs: dict | None = None) -> str:
    """A DONE run of `node` whose take is `out_key` in the project's CAS, with a stored passing gate report."""
    index = c.app.state.index
    project_dir = c.app.state.pipeline.project_dir(PROJECT)
    now = utcnow()
    with index.project(PROJECT) as conn:
        spec = RunCreate(
            project_id=PROJECT,
            packet_id=packet_id,
            node_id=node,
            inputs={"take": out_key, **(inputs or {})},
            params={},
            route=FAKE_ROUTE,
            attempt=1,
        )
        run = create_run(conn, spec, now=now)
        index.remember(run.id, PROJECT)
        q = Quote(
            run_id=run.id,
            provider="fake",
            model="fake",
            region="local",
            custody_grade="C0",
            price_usd=0.0,
            eta_s=0.0,
            created_utc=now,
            expires_utc=now + QUOTE_TTL,
        )
        with tx(conn):
            insert_quote(conn, q)
            transition(conn, run.id, S.QUOTED, actor="test", quote_id=q.id, quote_json=q.terms_json())
        transition(conn, run.id, S.APPROVED, actor="human", approved_by="human")
        transition(conn, run.id, S.QUEUED, actor="test")
        transition(conn, run.id, S.STAGING, actor="test")
        annotate(conn, run.id, expect=S.STAGING, provider_job_id=f"job-{run.id}")
        transition(conn, run.id, S.SUBMITTED, actor="test")
        transition(conn, run.id, S.RUNNING, actor="test")
        transition(conn, run.id, S.FETCHING, actor="test", result_url="fake://take")
        transition(conn, run.id, S.NORMALIZING, actor="test", result_key=out_key)
        transition(conn, run.id, S.GATING, actor="test", normalized={"outputs": [out_key], **(inputs or {})})
        report = {
            "gate_version": "0.1.0",
            "metric_code_hash": "ab" * 32,
            "threshold_set_id": "test",
            "verdict": "pass",
            "checks": [{"metric_id": "texture.lapvar", "value": 412.0, "status": "pass"}],
        }
        path = Service(project_dir).write(run.id, report)
        report["report_path"] = Path(path).relative_to(project_dir).as_posix()
        transition(conn, run.id, S.DONE, actor="test", gate_report=report)
    return run.id


def _accept(c: TestClient, run_id: str) -> None:
    with c.app.state.index.project(PROJECT) as conn:
        note = json.dumps({"scrub_log_id": "scrub-1", "decision": "accepted"})
        append_note(conn, run_id, kind="accept", actor="human", reason=note)


def _records(settings: Settings, kind: str, run_id: str) -> list[dict]:
    led = Ledger(settings.projects_dir / PROJECT)
    assert led.verify() == (True, None)
    return [r for r in led.records() if r["kind"] == kind and r.get("run_id") == run_id]


def _export_url() -> str:
    return f"/api/v0/projects/{PROJECT}/export"


# ---------------------------------------------------------------------------- batch export


@needs_room_a
def test_batch_export_writes_two_files_with_sidecars_and_refuses_the_gate_fail_run(
    sync_settings, auth, tmp_path
):
    engine = EchoEngine()
    with _client(sync_settings, engine) as c:
        _setup(c, auth)
        a, b = _run(c, auth, "e"), _run(c, auth, "e2")
        engine.shift_px = 12
        bad = _run(c, auth, "e3")
        assert (a["state"], b["state"], bad["state"]) == ("DONE", "DONE", "GATE_FAIL")
        out = tmp_path / "out"
        ids = [a["id"], b["id"], bad["id"], "run_missing"]
        r = c.post(_export_url(), json={"run_ids": ids, "out_dir": str(out), "format": "png16"}, headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert [e["run_id"] for e in body["exported"]] == [a["id"], b["id"]]
        assert [(x["run_id"], x["error"]) for x in body["refused"]] == [
            (bad["id"], "not_exportable"),
            ("run_missing", "not_found"),
        ]
        assert body["refused"][0]["code"] == "GATE_FAIL"
        for done, run in zip(body["exported"], (a, b), strict=True):
            path, sidecar = Path(done["path"]), Path(done["sidecar"])
            assert path.parent == out and path.name == f"{run['node_id']}_{run['id'][-8:]}.png"
            assert path.is_file() and sidecar.is_file() and sidecar.name.endswith(".gate.json")
            lin = lineage.read(path)
            assert lin is not None and lin.run_id == run["id"] and lin.ai_touched
            assert lineage.ai_marker(path)  # the fake engine is a generative route
            assert json.loads(sidecar.read_text(encoding="utf-8"))["export"]["run_id"] == run["id"]
            (rec,) = _records(sync_settings, "export", run["id"])
            assert rec["data"]["batch"] is True and rec["data"]["dest_name"] == path.name
        assert sorted(p.name for p in out.iterdir() if p.suffix == ".png") == sorted(
            Path(e["path"]).name for e in body["exported"]
        )
        assert not (out / f"e3_{bad['id'][-8:]}.png").exists()

        # exporting the same run again never overwrites (T127, decision 74): the file already holds exactly
        # these bytes, so it is a repeat that writes nothing and is recorded as one
        first = Path(body["exported"][0]["path"])
        before = (first.read_bytes(), first.stat().st_mtime_ns)
        r = c.post(
            _export_url(), json={"run_ids": [a["id"]], "out_dir": str(out), "format": "png16"}, headers=auth
        )
        assert r.status_code == 200 and r.json()["refused"] == [], r.text
        (again,) = r.json()["exported"]
        assert again["run_id"] == a["id"] and again["repeat"] is True and Path(again["path"]) == first
        assert (first.read_bytes(), first.stat().st_mtime_ns) == before
        recs = _records(sync_settings, "export", a["id"])
        assert len(recs) == 2 and "repeat" not in recs[0]["data"] and recs[1]["data"]["repeat"] is True
        # another run onto that name is still refused
        r = c.post(
            _export_url(),
            json={"run_ids": [b["id"]], "out_dir": str(out), "format": "png16", "name_pattern": first.stem},
            headers=auth,
        )
        assert r.json()["exported"] == [] and r.json()["refused"][0]["error"] == "not_exportable", r.text

        # an override recorded first lets the failed run out
        r = c.post(f"/api/v0/runs/{bad['id']}/override", json={"reason": REASON}, headers=auth)
        assert r.status_code == 200, r.text
        r = c.post(
            _export_url(),
            json={"run_ids": [bad["id"]], "out_dir": str(out), "format": "tiff16"},
            headers=auth,
        )
        assert r.status_code == 200 and [e["run_id"] for e in r.json()["exported"]] == [bad["id"]], r.text
        (rec,) = _records(sync_settings, "export", bad["id"])
        assert rec["data"]["override_reason"] == REASON


@needs_room_a
def test_batch_export_accepted_selects_by_accept_note_and_names_by_pattern(sync_settings, auth, tmp_path):
    with _client(sync_settings, EchoEngine()) as c:
        _setup(c, auth)
        a, b = _run(c, auth, "e"), _run(c, auth, "e2")
        _accept(c, a["id"])
        out = tmp_path / "accepted"
        r = c.post(
            _export_url(), json={"accepted": True, "out_dir": str(out), "format": "png16"}, headers=auth
        )
        assert r.status_code == 200, r.text
        assert [e["run_id"] for e in r.json()["exported"]] == [a["id"]] and r.json()["refused"] == []

        # run_ids plus accepted: the run without an accept note is refused, not silently dropped
        out2 = tmp_path / "both"
        body = {
            "run_ids": [a["id"], b["id"]],
            "accepted": True,
            "out_dir": str(out2),
            "format": "png16",
            "name_pattern": "{n}_{node}",
        }
        r = c.post(_export_url(), json=body, headers=auth)
        assert [Path(e["path"]).name for e in r.json()["exported"]] == ["001_e.png"]
        assert [(x["run_id"], x["error"]) for x in r.json()["refused"]] == [(b["id"], "not_accepted")]

        # request-level problems are 422/404 before anything is written
        for bad_body in (
            {"out_dir": str(out2)},  # neither run_ids nor accepted
            {"run_ids": [a["id"]], "out_dir": "relative/dir"},
            {"run_ids": [a["id"]], "out_dir": str(out2), "format": "bmp"},
            {"run_ids": [a["id"]], "out_dir": str(out2), "name_pattern": "{nope}"},
            {"run_ids": [a["id"]], "out_dir": str(out2), "name_pattern": "a:b"},
        ):
            assert c.post(_export_url(), json=bad_body, headers=auth).status_code == 422, bad_body
        assert (
            c.post(
                "/api/v0/projects/absent/export", json={"run_ids": ["x"], "out_dir": str(out2)}, headers=auth
            ).status_code
            == 404
        )
        assert c.post(_export_url(), json={"run_ids": [a["id"]], "out_dir": str(out2)}).status_code == 401


@needs_room_a
def test_batch_export_refuses_previs_and_a_name_that_two_runs_share(sync_settings, auth, tmp_path):
    with _client(sync_settings, EchoEngine()) as c:
        packet_id = _setup(c, auth)
        key = c.app.state.cas_for(PROJECT).put_bytes((ROOM_A / "display.tif").read_bytes(), "tif")
        previs = _forge(c, packet_id, key, node="motion", inputs={"previs": True})
        a, b = _run(c, auth, "e"), _run(c, auth, "e2")
        out = tmp_path / "out"
        body = {
            "run_ids": [previs, a["id"], b["id"]],
            "out_dir": str(out),
            "format": "png16",
            "name_pattern": "take",
        }
        r = c.post(_export_url(), json=body, headers=auth)
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["refused"][0] == {
            "run_id": previs,
            "error": "not_exportable",
            "detail": got["refused"][0]["detail"],
            "code": "PREVIS_NOT_DELIVERABLE",
        }
        assert [e["run_id"] for e in got["exported"]] == [a["id"]]
        assert (b["id"], "not_exportable") == (got["refused"][1]["run_id"], got["refused"][1]["error"])
        assert "already exists" in got["refused"][1]["detail"]
        assert sorted(p.name for p in out.iterdir()) == ["take.gate.json", "take.png"]


# ---------------------------------------------------------------------------- the cutdown route


@needs_room_a
def test_cutdown_route_crops_inside_the_safe_frame_and_writes_a_png_with_marker_and_sidecar(
    sync_settings, auth, tmp_path
):
    with _client(sync_settings, EchoEngine()) as c:
        _setup(c, auth)
        run = _run(c, auth, "e")
        out = tmp_path / "social"
        url = f"/api/v0/runs/{run['id']}/cutdown"
        r = c.post(url, json={"preset": "1:1", "out_dir": str(out)}, headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        x0, y0, w, h = body["box"]
        assert (w, h) == (268, 268) and 46 <= y0 and y0 + h <= 46 + 268  # room_a's 2.39:1 safe band
        path, sidecar = Path(body["path"]), Path(body["sidecar"])
        assert path.name == f"{run['id']}_1x1.png" and sidecar.name == f"{run['id']}_1x1.cutdown.json"
        assert cv2.imread(str(path)).shape == (268, 268, 3)
        lin = lineage.read(path)
        assert lin is not None and lin.run_id == run["id"] and lineage.ai_marker(path)
        side = json.loads(sidecar.read_text(encoding="utf-8"))
        assert side["source_run"] == run["id"] and side["box"] == body["box"] and side["preset"] == "1:1"
        assert side["format"] == "png" and side["gate_report_ref"] == f"gates/{run['id']}.1.json"
        (rec,) = _records(sync_settings, "export", run["id"])
        assert rec["data"]["cutdown"] is True and rec["data"]["box"] == body["box"] and rec["data"]["shipped"]

        r = c.post(
            url,
            json={
                "preset": "4:5",
                "cx": 0.2,
                "out_dir": str(out),
                "name": "feed",
                "format": "jpeg",
                "long_side": 200,
            },
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert Path(r.json()["path"]).name == "feed.jpg"
        assert (
            max(cv2.imread(r.json()["path"]).shape[:2]) == 200
            and lineage.read(Path(r.json()["path"])) is not None
        )

        # refusals
        assert (
            c.post(url, json={"preset": "1:1", "out_dir": str(out)}, headers=auth).status_code == 409
        )  # exists
        r = c.post(url, json={"preset": "3:2", "out_dir": str(out)}, headers=auth)
        assert r.status_code == 422 and r.json()["error"] == "cutdown_error"
        r = c.post(
            url, json={"preset": "1:1", "out_dir": str(out), "name": "big", "long_side": 5000}, headers=auth
        )
        assert r.status_code == 422 and "never upscale" in r.json()["detail"]
        assert c.post(url, json={"preset": "1:1", "out_dir": "rel"}, headers=auth).status_code == 422
        assert (
            c.post(url, json={"preset": "1:1", "out_dir": str(out), "name": "a:b"}, headers=auth).status_code
            == 422
        )
        assert (
            c.post(url, json={"preset": "1:1", "out_dir": str(out), "cx": 2}, headers=auth).status_code == 422
        )
        assert c.post(url, json={"preset": "1:1", "out_dir": str(out)}).status_code == 401
        assert (
            c.post(
                "/api/v0/runs/run_nope/cutdown", json={"preset": "1:1", "out_dir": str(out)}, headers=auth
            ).status_code
            == 404
        )


@needs_room_a
def test_cutdown_route_is_guarded_like_delivery(sync_settings, auth, tmp_path):
    engine = EchoEngine(shift_px=12)
    with _client(sync_settings, engine) as c:
        packet_id = _setup(c, auth)
        failed = _run(c, auth, "e")
        assert failed["state"] == "GATE_FAIL"
        out = tmp_path / "social"
        url = f"/api/v0/runs/{failed['id']}/cutdown"
        r = c.post(url, json={"preset": "1:1", "out_dir": str(out)}, headers=auth)
        assert (
            r.status_code == 409
            and r.json()["error"] == "delivery_refused"
            and r.json()["code"] == "GATE_FAIL"
        )
        assert not out.exists()
        assert (
            c.post(f"/api/v0/runs/{failed['id']}/override", json={"reason": REASON}, headers=auth).status_code
            == 200
        )
        r = c.post(url, json={"preset": "1:1", "out_dir": str(out)}, headers=auth)
        assert r.status_code == 200, r.text
        assert lineage.read(Path(r.json()["path"])).run_id == failed["id"]

        key = c.app.state.cas_for(PROJECT).put_bytes((ROOM_A / "display.tif").read_bytes(), "tif")
        previs = _forge(c, packet_id, key, node="motion", inputs={"previs": True})
        r = c.post(
            f"/api/v0/runs/{previs}/cutdown", json={"preset": "1:1", "out_dir": str(out)}, headers=auth
        )
        assert r.status_code == 409 and r.json()["code"] == "PREVIS_NOT_DELIVERABLE", r.text


@needs_room_a
@needs_x264
def test_cutdown_route_makes_a_clip_cutdown_from_a_clip_take(sync_settings, auth, tmp_path):
    with _client(sync_settings, EchoEngine()) as c:
        packet_id = _setup(c, auth)
        clip = _clip(tmp_path / "take.mp4")
        key = c.app.state.cas_for(PROJECT).put_bytes(clip.read_bytes(), "mp4")
        run_id = _forge(c, packet_id, key, node="motion")
        out = tmp_path / "social"
        r = c.post(
            f"/api/v0/runs/{run_id}/cutdown",
            json={"preset": "9:16", "out_dir": str(out), "format": "png"},  # format is for stills: ignored
            headers=auth,
        )
        assert r.status_code == 200, r.text
        path = Path(r.json()["path"])
        got = _probe_tags(path)
        assert path.suffix == ".mp4" and got["codec_name"] == "h264" and got["color_primaries"] == "bt709"
        x0, y0, w, h = r.json()["box"]
        assert (got["width"], got["height"]) == (w, h) and h == 268  # inside room_a's safe band
        assert lineage.read(path).run_id == run_id
        side = json.loads(Path(r.json()["sidecar"]).read_text(encoding="utf-8"))
        assert side["codec"] == "h264" and "format" not in side and side["source_run"] == run_id


# ---------------------------------------------------------------------------- review cover and the manual import


@needs_room_a
def test_review_bundle_renders_the_subtitle_and_author_on_the_cover(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        _setup(c, auth)
        run = _run(c, auth, "e")
        url = f"/api/v0/projects/{PROJECT}/review"
        r = c.post(
            url,
            json={
                "run_ids": [run["id"]],
                "title": "Lobby",
                "subtitle": "Round 2 <final>",
                "author": "Kabir & Co",
            },
            headers=auth,
        )
        assert r.status_code == 200, r.text
        with zipfile.ZipFile(r.json()["zip_path"]) as zf:
            page = zf.read(next(n for n in zf.namelist() if n.endswith("/index.html"))).decode("utf-8")
        assert '<p class="subtitle">Round 2 &lt;final&gt;</p>' in page
        assert '<p class="author">Kabir &amp; Co</p>' in page
        assert "{{SUBTITLE}}" not in page and "{{AUTHOR}}" not in page
        assert (
            "Round 2 <final>" not in page.replace("\\u003c", "<")[: page.index("<script")]
        )  # escaped in HTML

        r = c.post(url, json={"run_ids": [run["id"]]}, headers=auth)
        with zipfile.ZipFile(r.json()["zip_path"]) as zf:
            page = zf.read(next(n for n in zf.namelist() if n.endswith("/index.html"))).decode("utf-8")
        assert '<p class="subtitle"></p>' in page and '<p class="author"></p>' in page  # empty, hidden by CSS

        for bad in ({"subtitle": "x" * 121}, {"author": "y" * 121}):
            assert c.post(url, json={"run_ids": [run["id"]], **bad}, headers=auth).status_code == 422


def test_review_page_cover_is_empty_without_subtitle_or_author():
    page = review_bundle._render_index("Lobby", {"title": "Lobby"})
    assert '<p class="subtitle"></p>' in page and '<p class="author"></p>' in page
    page = review_bundle._render_index("Lobby", {}, "Round 2", "Kabir")
    assert '<p class="subtitle">Round 2</p>' in page and '<p class="author">Kabir</p>' in page
    with pytest.raises(ValueError):
        review_bundle._cover_text("subtitle", "z" * 121)
    assert (
        review_bundle._cover_text("author", "   ") is None
        and review_bundle._cover_text("author", None) is None
    )


@needs_room_a
def test_an_imported_manual_runs_png_has_no_marker_but_an_edit_runs_png_has_one(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        packet_id = _setup(c, auth)
        r = c.post(
            f"/api/v0/projects/{PROJECT}/import",
            json={"path": str(ROOM_A / "display.tif"), "packet_id": packet_id, "source": "manual"},
            headers=auth,
        )
        assert r.status_code == 201, r.text
        imported = r.json()
        cas = c.app.state.cas_for(PROJECT)
        png = cas.find(imported["outputs"][0])
        assert png.suffix == ".png"
        lin = lineage.read(png)
        assert lin is not None and lin.engine_id == "import:manual" and lin.ai_touched is False
        assert not lineage.ai_marker(png) and _itxt_keywords(png) == [b"plate:lineage"]

        edit = _run(c, auth, "e")
        edit_png = cas.find(edit["outputs"][0])
        assert lineage.read(edit_png).ai_touched and lineage.ai_marker(edit_png)

        # an export of the imported take is a PNG with no marker either
        r = c.post(
            _export_url(),
            json={"run_ids": [imported["id"]], "out_dir": str(cas.root.parent / "x"), "format": "png16"},
            headers=auth,
        )
        assert r.status_code == 200 and len(r.json()["exported"]) == 1, r.text
        assert not lineage.ai_marker(Path(r.json()["exported"][0]["path"]))


def test_default_pattern_is_node_and_run8():
    assert DEFAULT_PATTERN == "{node}_{run8}"
