"""The client packet kit (plugins/max/maxscript/plate_client_kit.ms) and its daemon-side half
(plate_max.kit_ingest).

The MAXScript cannot run on this machine, so it is checked statically: ASCII, CRLF, balanced delimiters
(a scanner that knows MAXScript strings and comments), the render elements and Max calls the spec names,
no network or shell, and that the JSON keys it writes are exactly the ones kit_ingest reads and the
committed fixture shows. kit_ingest is tested end to end on a hand-written kit output
(tests/kit_fixture/) whose EXR is the committed room_a fixture packet: kit_to_packet must produce a folder
that plate_spec.validate_packet_dir accepts, byte for byte hashed. Client imagery is never used.
"""

from __future__ import annotations

import json
import re
import shutil
import string
from pathlib import Path

import pytest

import plate_spec
from plate_max import kit_ingest
from plate_max.kit_ingest import KitError

PLUGIN = Path(__file__).resolve().parent.parent
REPO = PLUGIN.parent.parent
KIT = PLUGIN / "maxscript" / "plate_client_kit.ms"
FIXTURE = Path(__file__).resolve().parent / "kit_fixture"
ROOM_A = REPO / "fixtures" / "packets" / "room_a"


# --------------------------------------------------------------------------- the MAXScript, statically


def ms_delimiter_errors(src: str) -> list[str]:
    """Unbalanced ( ) [ ] { } in MAXScript source, ignoring "strings" (backslash escapes), -- line comments
    and /* block comments */. Returns messages with line numbers; empty means balanced."""
    errors: list[str] = []
    stack: list[tuple[str, int]] = []
    closers = {")": "(", "]": "[", "}": "{"}
    i, line, n = 0, 1, len(src)
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
        elif src.startswith("--", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        elif src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end < 0:
                errors.append(f"line {line}: unterminated /* comment")
                break
            line += src.count("\n", i, end)
            i = end + 2
            continue
        elif c == '"':
            i += 1
            while i < n and src[i] != '"':
                if src[i] == "\\":
                    i += 1
                if i < n and src[i] == "\n":
                    errors.append(f"line {line}: string runs over a line break")
                    line += 1
                i += 1
            if i >= n:
                errors.append(f"line {line}: unterminated string")
                break
        elif c in "([{":
            stack.append((c, line))
        elif c in closers:
            if not stack or stack[-1][0] != closers[c]:
                errors.append(f"line {line}: unmatched {c!r}")
            else:
                stack.pop()
        i += 1
    errors.extend(f"line {ln}: unclosed {c!r}" for c, ln in stack)
    return errors


@pytest.fixture(scope="module")
def ms_bytes() -> bytes:
    return KIT.read_bytes()


@pytest.fixture(scope="module")
def ms_text(ms_bytes) -> str:
    return ms_bytes.decode("ascii")


def test_the_scanner_can_fail():
    assert ms_delimiter_errors("(a (b) [c] #(1, 2))\n") == []
    assert ms_delimiter_errors('format "(%\\n" x -- ) a comment\n/* ( */ (y)\n') == []
    assert ms_delimiter_errors('s = "a \\" ) b"\n(1)\n') == []
    assert any("unclosed" in e for e in ms_delimiter_errors("(a (b)\n"))
    assert any("unmatched" in e for e in ms_delimiter_errors("(a ]\n"))
    assert any("unmatched" in e for e in ms_delimiter_errors("a )\n"))
    assert any("unterminated string" in e for e in ms_delimiter_errors('s = "abc'))
    assert any("unterminated /*" in e for e in ms_delimiter_errors("a /* b"))


def test_the_script_is_ascii_with_crlf_line_endings(ms_bytes):
    ms_bytes.decode("ascii")
    assert ms_bytes.endswith(b"\r\n")
    assert ms_bytes.count(b"\r\n") == ms_bytes.count(b"\n") == ms_bytes.count(b"\r")
    assert b"\t" not in ms_bytes
    assert not ms_bytes.startswith(b"\xef\xbb\xbf")


def test_the_script_stays_under_400_lines(ms_text):
    assert len(ms_text.splitlines()) < 400


def test_delimiters_are_balanced(ms_text):
    assert ms_delimiter_errors(ms_text) == []


def test_it_has_the_rollout_and_the_calls_the_spec_names(ms_text):
    for needle in (
        "rollout",
        'rollout kitRollout "PLATE Packet Kit"',
        "createDialog",
        "maxOps.GetCurRenderElementMgr",
        "VRayZDepth",
        "VRayCryptomatte",
        "VRayNormals",
        "VRayWireColor",
        "VRayDiffuseFilter",
        "objecttransform",
        "maxVersion",
        "units.SystemType",
        "units.SystemScale",
        "classOf renderers.current",
        "messageBox",
        "dropdownList",
        "button btnExport",
        "getSavePath",
    ):
        assert needle in ms_text, needle


def test_it_stops_when_the_renderer_is_not_vray(ms_text):
    body = ms_text[ms_text.index("fn kitExport") :]
    check = body.index('pattern:"V_Ray*"')
    assert "messageBox" in body[check : check + 200]
    assert "return" in body[check : check + 300]
    assert check < body.index("AddRenderElement") < body.index("render camera:")


def test_it_cleans_up_after_itself(ms_text):
    # the user's own elements are disabled and re-enabled, ours removed, the raw-output settings restored
    for needle in ("RemoveRenderElement", "SetElementsActive", "oldRaw", "oldName", "e.enabled = false"):
        assert needle in ms_text, needle


def test_it_reads_nothing_from_the_network_and_runs_no_shell(ms_text):
    low = ms_text.lower()
    for forbidden in (
        "python.",
        "dotnet",
        "http",
        "socket",
        "webclient",
        "ftp",
        "curl",
        "shelllaunch",
        "doscommand",
        "hiddendoscommand",
        "systemtools",
        "callbacks.",
        "filein",
    ):
        if forbidden == "filein":
            assert not re.search(r"^\s*filein\b", low, re.M), forbidden  # the header comment mentions it
        else:
            assert forbidden not in low, forbidden
    assert not re.search(r"\bexecute\b", low)


def test_the_stem_characters_match_kit_ingest(ms_text):
    ok = re.search(r'local kitOkChars = "([^"]+)"', ms_text).group(1)
    assert ok == string.ascii_letters + string.digits + "_.-"
    assert kit_ingest.kit_stem("CAM 1:a/b\\c") == "CAM_1_a_b_c"
    assert kit_ingest.kit_stem("CAM_a-1.2") == "CAM_a-1.2"
    assert kit_ingest.kit_stem("Kamera\u00e4") == "Kamera_"


def test_the_legend_line_format_matches_the_parser(ms_text):
    assert 'format "%|%|%|%,%,%\\n"' in ms_text
    for code in ("%25", "%7C", "%0D", "%0A"):
        assert code in ms_text
    nodes = kit_ingest.parse_legend_txt("a%7Cb%25|LAY%0AER|MAT|1,2,3\r\nplain||| 4,5,6\n")
    assert nodes[0] == {"name": "a|b%", "layer": "LAY\nER", "material": "MAT", "wirecolor": [1, 2, 3]}
    assert nodes[1] == {"name": "plain", "layer": None, "material": None, "wirecolor": [4, 5, 6]}


def _json_keys_in_script(ms_text: str) -> set[str]:
    return set(re.findall(r'\\"([A-Za-z_]+)\\":', ms_text))


def _flat_keys(obj, out: set[str]) -> set[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            if k != "elements":
                _flat_keys(v, out)
    return out


def test_the_script_writes_the_keys_the_fixture_shows_and_kit_ingest_reads(ms_text):
    written = _json_keys_in_script(ms_text)
    shown = _flat_keys(json.loads((FIXTURE / "CAM_a_camera.json").read_text(encoding="utf-8")), set())
    shown |= set(json.loads((FIXTURE / "CAM_a_camera.json").read_text(encoding="utf-8"))["elements"])
    assert shown - written == set(), "the fixture shows keys the script does not write"
    assert written - shown == set(), "the script writes keys the fixture (and so the tests) never show"
    read_by_ingest = {
        "kit",
        "kit_version",
        "camera",
        "camera_class",
        "transform",
        "fov",
        "focal_length_mm",
        "film_width",
        "zoom_factor",
        "render_width",
        "render_height",
        "pixel_aspect",
        "frame",
        "fps",
        "units",
        "system_type",
        "system_scale",
        "renderer",
        "max_version",
        "colour_mode",
        "gamma",
        "f_number",
        "focus_distance",
        "dof_enabled",
        "iso",
        "shutter_speed",
        "lens_shift",
        "tilt",
        "distortion",
        "render_time_s",
        "exr",
        "legend",
        "elements",
        "warnings",
    }
    assert read_by_ingest <= written


def test_the_render_element_names_match_what_kit_ingest_expects(ms_text):
    pairs = re.findall(r'#\("(VRay\w+)", "(\w+)"\)', ms_text)
    assert {name for _, name in pairs} == set(kit_ingest.DEFAULT_LAYERS.values()) - {"RGBA"}
    assert sum(1 for cls, _ in pairs if cls == "VRayCryptomatte") == 2
    line = next(ln for ln in ms_text.splitlines() if '\\"elements\\"' in ln)
    assert dict(re.findall(r'\\"(\w+)\\": \\"(\w+)\\"', line)) == kit_ingest.DEFAULT_LAYERS


def test_the_readme_documents_the_contract():
    text = (PLUGIN / "maxscript" / "README.md").read_text(encoding="utf-8")
    for needle in (
        "_packet.exr",
        "_camera.json",
        "_legend.txt",
        "kit_to_packet",
        "client_plate_8bit",
        "unverified",
    ):
        assert needle in text, needle


# --------------------------------------------------------------------------- kit_ingest: helpers


def _b3(data) -> str:
    import blake3

    if isinstance(data, (bytes, bytearray)):
        return blake3.blake3(bytes(data)).hexdigest()
    h = blake3.blake3()
    with Path(data).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _shares(exr_path, elements) -> dict[str, float]:
    """Object id -> rank-0 coverage, the way the daemon will compute it (plate_pixels decodes the EXR)."""
    import numpy as np

    from plate_pixels import exr as pexr

    manifest = {"files": {"beauty_exr": {"elements": elements}}}
    ids, cov = pexr.read_cryptomatte(exr_path, manifest, "object")
    first, weight = ids[..., 0], cov[..., 0]
    return {
        f"o:{int(v):08x}": float(weight[first == v].sum() / first.size) for v in np.unique(first) if v != 0
    }


def _promote(src, dst) -> None:
    """The plate_to_tiff16 capability: any 8/16-bit RGB(A) image to a 16-bit TIFF (8-bit values * 257)."""
    import cv2
    import numpy as np
    import tifffile

    img = cv2.imread(str(src), cv2.IMREAD_UNCHANGED)
    assert img is not None
    img = img[..., [2, 1, 0] + ([3] if img.shape[2] == 4 else [])]
    if img.dtype == np.uint8:
        img = img.astype(np.uint16) * 257
    tifffile.imwrite(str(dst), img, photometric="rgb")


def _plate_array(width=640, height=360, dtype="uint16"):
    import numpy as np

    y, x = np.mgrid[0:height, 0:width]
    rgb = np.stack([x * 100 // width, y * 100 // height, (x + y) % 100], axis=-1).astype(np.float64) / 100.0
    top = 65535 if dtype == "uint16" else 255
    return (rgb * top).astype(dtype)


def _tiff16(path: Path, width=640, height=360, channels=3) -> Path:
    import numpy as np
    import tifffile

    arr = _plate_array(width, height)
    if channels == 4:
        arr = np.concatenate([arr, np.full((height, width, 1), 65535, np.uint16)], axis=-1)
    tifffile.imwrite(str(path), arr, photometric="rgb")
    return path


def _png8(path: Path, width=640, height=360) -> Path:
    from PIL import Image

    Image.fromarray(_plate_array(width, height, "uint8")).save(path)
    return path


@pytest.fixture
def kit_dir(tmp_path) -> Path:
    if not (ROOM_A / "beauty.exr").is_file():
        pytest.skip("fixtures/packets/room_a is not built (router plan T2)")
    folder = tmp_path / "kit"
    folder.mkdir()
    for name in ("CAM_a_camera.json", "CAM_a_legend.txt"):
        shutil.copyfile(FIXTURE / name, folder / name)
    shutil.copyfile(ROOM_A / "beauty.exr", folder / "CAM_a_packet.exr")
    return folder


def _edit_camera(folder: Path, **changes) -> None:
    path = folder / "CAM_a_camera.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is KeyError:
            doc.pop(key, None)
        else:
            doc[key] = value
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8", newline="\n")


def _flat(matrix) -> list[float]:
    return [float(v) for row in matrix for v in row]


def convert(folder: Path, plate: Path, **kw):
    options = {"hasher": _b3, "pixel_shares": _shares, "plate_to_tiff16": _promote, **kw}
    return kit_ingest.convert_kit(folder, "CAM_a", plate, **options)


def _manifest(packet_dir: Path) -> dict:
    return json.loads((packet_dir / "manifest.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- kit_ingest: the main path


def test_a_16_bit_tiff_plate_becomes_a_valid_tier_a_packet(kit_dir, tmp_path):
    plate = _tiff16(tmp_path / "plate.tif")
    result = convert(kit_dir, plate)
    root = result.packet_dir
    assert plate_spec.validate_packet_dir(root, check_files=True) == []
    m = _manifest(root)
    assert (m["tier"], m["kind"], m["packet_id"]) == ("A", "still", result.packet_id)
    assert m["host"] == {
        "app": "3dsmax",
        "version": "2026",
        "plugin": {"name": "plate_client_kit", "version": "0.1.0"},
    }
    assert m["renderer"] == {"name": "vray", "version": "7 hotfix 3"}
    assert m["units"] == {"system_unit": "meters", "meters_per_unit": 1.0, "up_axis": "Z"}
    assert m["colour"]["max_colour_mode"] == "legacy_gamma" and m["colour"]["legacy_gamma"] == 2.2
    assert m["colour"]["master"] == "display_tiff16"
    # the plate is the display master, byte for byte (a 16-bit TIFF is never re-encoded)
    assert (root / "display.tif").read_bytes() == plate.read_bytes()
    assert m["files"]["display_tiff16"]["bit_depth"] == 16
    assert "client_plate_8bit" not in result.flags
    assert sorted(p.name for p in root.iterdir()) == [
        "beauty.exr",
        "display.tif",
        "id_legend.json",
        "kit_flags.json",
        "manifest.json",
    ]
    assert all(":" not in p.name for p in root.iterdir())
    assert (root / "beauty.exr").read_bytes() == (ROOM_A / "beauty.exr").read_bytes()


def test_the_camera_block_is_the_spec_camera_math_on_the_kit_json(kit_dir, tmp_path):
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    cam = result.manifest["camera"]
    doc = json.loads((kit_dir / "CAM_a_camera.json").read_text(encoding="utf-8"))
    expect = plate_spec.world_from_camera_from_max_rows(doc["transform"])
    assert _flat(cam["world_from_camera"]) == pytest.approx(_flat(expect), abs=1e-12)
    room_a = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))["camera"]
    assert _flat(cam["world_from_camera"]) == pytest.approx(_flat(room_a["world_from_camera"]), abs=1e-9)
    assert cam["intrinsics_px"]["fx"] == pytest.approx(room_a["intrinsics_px"]["fx"])
    assert cam["resolution"] == {"width": 640, "height": 360, "pixel_aspect": 1.0}
    assert cam["safe_frame"] == {"x": 0, "y": 0, "width": 640, "height": 360}
    assert cam["frames"] == {"hero": 0, "start": 0, "end": 0, "fps": 25.0}
    point = [cam["world_from_camera"][i][3] - 5.0 * cam["world_from_camera"][i][2] for i in range(3)]
    u, v, depth = plate_spec.project_point(cam, point)
    # room_a rounds its matrix to 6 decimals, so "straight ahead" is exact only to about a thousandth of a pixel
    assert (u, v) == pytest.approx((320.0, 180.0), abs=5e-3) and depth == pytest.approx(5.0, abs=1e-4)


def test_elements_are_read_from_the_exr_not_assumed(kit_dir, tmp_path):
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    got = {e["element"]: e for e in result.manifest["files"]["beauty_exr"]["elements"]}
    room_a = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))["files"]["beauty_exr"]
    want = {e["element"]: e for e in room_a["elements"]}
    assert list(got) == [
        "beauty",
        "zdepth",
        "normals",
        "wire_colour",
        "diffuse_filter",
        "cryptomatte_object",
        "cryptomatte_material",
    ]
    for name, entry in got.items():
        for key in ("layer", "channels", "dtype", "rank_levels"):
            assert entry.get(key) == want[name].get(key), (name, key)
    assert result.manifest["files"]["beauty_exr"]["compression"] == "zip"
    assert result.manifest["files"]["beauty_exr"]["parts"] == "single"
    assert got["zdepth"]["depth_kind"] == "planar" and "depth_kind_assumed" in result.flags
    # the room_a fixture also holds raw lighting, raw GI and reflection: not part of the default set
    # (the elements the kit does not add are named in kit_flags.json, not dropped silently)
    flags = json.loads((result.packet_dir / "kit_flags.json").read_text(encoding="utf-8"))
    extras = sorted({e["layer"] for e in room_a["elements"]} - {e["layer"] for e in got.values()})
    assert extras and flags["unlisted_exr_layers"] == extras


def test_the_legend_keeps_what_the_render_covers_and_drops_the_rest(kit_dir, tmp_path):
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    legend = json.loads((result.packet_dir / "id_legend.json").read_text(encoding="utf-8"))
    room_a = json.loads((ROOM_A / "id_legend.json").read_text(encoding="utf-8"))
    got = {e["id"]: e for e in legend["entries"]}
    want = {e["id"]: e for e in room_a["entries"]}
    assert set(got) == set(want), (
        "tests/kit_fixture/CAM_a_legend.txt no longer matches fixtures/packets/room_a"
    )
    # every room_a object plus VEG_behind_camera is in the kit legend; the unseen one has no coverage
    assert "VEG_behind_camera" not in {e["name"] for e in legend["entries"]}
    for oid, entry in got.items():
        assert entry["class"] == want[oid]["class"] and entry["class_source"] == want[oid]["class_source"]
        assert entry["pixel_share"] == pytest.approx(want[oid]["pixel_share"], abs=1e-3)
    m = result.manifest
    assert m["policy"]["editable_ids"] == [
        i for i in m["policy"]["editable_ids"] if got[i]["class"] in {"vegetation", "sky", "people", "water"}
    ]
    room_a_policy = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))["policy"]
    assert set(m["policy"]["editable_ids"]) == set(room_a_policy["editable_ids"])
    assert set(m["policy"]["locked_ids"]) == set(room_a_policy["locked_ids"])
    assert m["id_legend"]["entry_count"] == len(room_a["entries"])
    assert "legend_nodes_not_in_exr" in result.flags  # VEG_behind_camera is in the legend, not in the EXR


def test_flags_travel_with_the_packet_as_an_extension_file(kit_dir, tmp_path):
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    ref = result.manifest["files"]["x_kit_flags"]
    assert ref["format"] == "json" and ref["path"] == "kit_flags.json"
    doc = json.loads((result.packet_dir / "kit_flags.json").read_text(encoding="utf-8"))
    assert doc["flags"] == result.flags
    assert doc["client_plate"]["promoted_to_tiff16"] is False
    assert doc["kit_warnings"] and "unverified" in doc["kit_warnings"][0]
    assert "vfb_corrections_unknown" in doc["flags"]


def test_the_plate_can_carry_an_alpha_channel(kit_dir, tmp_path):
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif", channels=4))
    assert result.manifest["files"]["display_tiff16"]["channels"] == "RGBA"
    assert plate_spec.validate_packet_dir(result.packet_dir) == []


# --------------------------------------------------------------------------- kit_ingest: the plate


def test_an_8_bit_plate_is_promoted_and_flagged(kit_dir, tmp_path):
    import numpy as np
    import tifffile
    from PIL import Image

    png = _png8(tmp_path / "plate.png")
    result = convert(kit_dir, png)
    assert plate_spec.validate_packet_dir(result.packet_dir, check_files=True) == []
    assert "client_plate_8bit" in result.flags and "client_plate_jpeg" not in result.flags
    assert result.manifest["files"]["display_tiff16"]["bit_depth"] == 16
    got = tifffile.imread(str(result.packet_dir / "display.tif"))
    want = np.asarray(Image.open(png).convert("RGB")).astype(np.uint16) * 257
    assert (got == want).all()  # exact promotion: v8 == v16 >> 8, nothing invented
    doc = json.loads((result.packet_dir / "kit_flags.json").read_text(encoding="utf-8"))
    assert doc["client_plate"] == {
        "format": "png",
        "bit_depth": 8,
        "channels": "RGB",
        "blake3": _b3(png),
        "promoted_to_tiff16": True,
    }


def test_a_jpeg_plate_is_flagged_lossy_and_8_bit(kit_dir, tmp_path):
    from PIL import Image

    jpg = tmp_path / "plate.jpg"
    Image.fromarray(_plate_array(dtype="uint8")).save(jpg, quality=95)
    result = convert(kit_dir, jpg)
    assert {"client_plate_8bit", "client_plate_jpeg"} <= set(result.flags)
    assert plate_spec.validate_packet_dir(result.packet_dir) == []


def test_a_16_bit_png_is_converted_without_the_8_bit_flag(kit_dir, tmp_path):
    import cv2

    png = tmp_path / "plate16.png"
    cv2.imwrite(str(png), _plate_array(dtype="uint16")[..., ::-1])
    result = convert(kit_dir, png)
    assert "client_plate_8bit" not in result.flags
    assert plate_spec.validate_packet_dir(result.packet_dir) == []


def test_a_plate_that_needs_converting_without_a_converter_is_refused_cleanly(kit_dir, tmp_path):
    with pytest.raises(KitError, match="plate_to_tiff16"):
        convert(kit_dir, _png8(tmp_path / "plate.png"), plate_to_tiff16=None)
    assert [p.name for p in kit_dir.iterdir() if p.is_dir()] == []  # no half-written packet folder


def test_a_converter_that_writes_the_wrong_thing_is_caught(kit_dir, tmp_path):
    def bad(src, dst):
        _tiff16(Path(dst), width=320, height=180)

    with pytest.raises(KitError, match="16-bit RGB"):
        convert(kit_dir, _png8(tmp_path / "plate.png"), plate_to_tiff16=bad)
    assert [p.name for p in kit_dir.iterdir() if p.is_dir()] == []


def test_a_plate_of_another_size_is_never_resampled(kit_dir, tmp_path):
    with pytest.raises(KitError, match=r"320x180.*640x360.*never resampled"):
        convert(kit_dir, _tiff16(tmp_path / "small.tif", 320, 180))


def test_inspect_plate_reads_headers_without_decoding(tmp_path):
    import tifffile
    from PIL import Image, ImageCms

    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    tif = tmp_path / "a.tif"
    tifffile.imwrite(
        str(tif), _plate_array(12, 8), photometric="rgb", extratags=[(34675, 7, len(icc), icc, True)]
    )
    info = kit_ingest.inspect_plate(tif)
    assert (info.format, info.width, info.height, info.bit_depth, info.channels, info.icc) == (
        "tiff",
        12,
        8,
        16,
        "RGB",
        True,
    )
    assert info.is_tiff16_rgb
    flt = tmp_path / "f.tif"
    tifffile.imwrite(str(flt), _plate_array(12, 8).astype("float32"), photometric="rgb")
    assert not kit_ingest.inspect_plate(flt).is_tiff16_rgb
    png = tmp_path / "a.png"
    Image.fromarray(_plate_array(12, 8, "uint8")).save(png, icc_profile=icc)
    info = kit_ingest.inspect_plate(png)
    assert (info.format, info.bit_depth, info.icc) == ("png", 8, True)
    (tmp_path / "x.bin").write_bytes(b"not an image at all")
    with pytest.raises(KitError, match="PNG, TIFF or JPEG"):
        kit_ingest.inspect_plate(tmp_path / "x.bin")
    with pytest.raises(KitError, match="not found"):
        kit_ingest.inspect_plate(tmp_path / "missing.png")
    (tmp_path / "big.tif").write_bytes(b"II+\x00\x08\x00\x00\x00" + b"\x00" * 16)
    with pytest.raises(KitError, match="BigTIFF"):
        kit_ingest.inspect_plate(tmp_path / "big.tif")


# --------------------------------------------------------------------------- kit_ingest: the EXR header


def test_the_stdlib_header_reader_agrees_with_openexr_on_room_a():
    import OpenEXR

    header = kit_ingest.read_exr_header(ROOM_A / "beauty.exr")
    exr = OpenEXR.File(str(ROOM_A / "beauty.exr"), separate_channels=True)
    part = exr.parts[0]
    assert len(header.parts) == 1
    assert header.parts[0].compression == "zip" and (header.parts[0].width, header.parts[0].height) == (
        640,
        360,
    )
    assert set(header.channels) == {c.name for c in part.header["channels"]}
    assert header.channels["R"] == "half"
    assert header.channels["VRayZDepth.Z"] == "float" and header.channels["CryptoObject00.R"] == "float"
    attrs = part.header
    for layer in ("CryptoObject", "CryptoMaterial"):
        key = next(
            k for k, v in attrs.items() if k.startswith("cryptomatte/") and k.endswith("/name") and v == layer
        )
        expected = json.loads(attrs[key.replace("/name", "/manifest")])
        assert kit_ingest._crypto_manifest(header, layer) == expected
    assert kit_ingest._crypto_manifest(header, "NoSuchLayer") is None


def _write_exr(path: Path, size=16, *, drop=(), half=(), compression="ZIP", strings=None) -> Path:
    import numpy as np
    import OpenEXR

    z = np.zeros((size, size), np.float32)
    layers = {
        "RGBA": ["R", "G", "B", "A"],
        "VRayZDepth": ["Z"],
        "VRayNormals": ["X", "Y", "Z"],
        "VRayWireColor": ["R", "G", "B"],
        "VRayDiffuseFilter": ["R", "G", "B"],
        "CryptoObject": [f"CryptoObject{n:02d}.{c}" for n in range(3) for c in "RGBA"],
        "CryptoMaterial": [f"CryptoMaterial{n:02d}.{c}" for n in range(3) for c in "RGBA"],
    }
    channels = {}
    for layer, names in layers.items():
        if layer in drop:
            continue
        for ch in names:
            full = ch if layer == "RGBA" or "." in ch else f"{layer}.{ch}"
            channels[full] = z.astype(np.float16 if layer in half else np.float32)
    header = {"compression": getattr(OpenEXR, f"{compression}_COMPRESSION"), "type": OpenEXR.scanlineimage}
    header.update(strings or {})
    OpenEXR.File(header, channels).write(str(path))
    return path


def _synthetic_kit(folder: Path, **exr_options) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE / "CAM_a_legend.txt", folder / "CAM_a_legend.txt")
    shutil.copyfile(FIXTURE / "CAM_a_camera.json", folder / "CAM_a_camera.json")
    _write_exr(folder / "CAM_a_packet.exr", **exr_options)
    _edit_camera(folder, render_width=16, render_height=16)
    return folder


@pytest.mark.parametrize(
    "options, message",
    [
        ({"drop": ("VRayZDepth",)}, "no channel 'VRayZDepth.Z'"),
        ({"drop": ("CryptoMaterial",)}, "CryptoMaterialNN"),
        ({"drop": ("VRayWireColor",)}, "VRayWireColor.R"),
        ({"half": ("CryptoObject",)}, "cryptomatte_object is stored as half"),
        ({"half": ("VRayZDepth",)}, "zdepth is stored as half"),
        ({"compression": "PXR24"}, "pxr24-compressed"),
        ({"compression": "DWAA"}, "dwaa-compressed"),
    ],
)
def test_an_exr_that_is_not_the_default_set_is_refused(tmp_path, options, message):
    folder = _synthetic_kit(tmp_path / "kit", **options)
    with pytest.raises(KitError, match=message):
        convert(folder, _tiff16(tmp_path / "p.tif", 16, 16), pixel_shares={})


def _crypto_strings(object_manifest=None, material_manifest=None) -> dict[str, str]:
    out = {}
    for key, layer, manifest in (
        ("abc1234", "CryptoObject", object_manifest),
        ("def5678", "CryptoMaterial", material_manifest),
    ):
        if manifest is not None:
            out[f"cryptomatte/{key}/name"] = layer
            out[f"cryptomatte/{key}/hash"] = "MurmurHash3_32"
            out[f"cryptomatte/{key}/manifest"] = json.dumps(manifest)
    return out


def _small_packet(tmp_path, strings, shares):
    folder = _synthetic_kit(tmp_path / "kit", strings=strings)
    return convert(folder, _tiff16(tmp_path / "p.tif", 16, 16), pixel_shares=shares)


def test_the_cryptomatte_manifest_in_the_exr_is_cross_checked(tmp_path):
    from plate_max import legend

    floor = legend.object_id("HARD_floor")
    shares = {floor: 0.5}
    right = _crypto_strings({"HARD_floor": floor[2:]}, {"HARD_floor": legend.material_id("HARD_floor")[2:]})
    ok = _small_packet(tmp_path / "ok", right, shares)
    assert plate_spec.validate_packet_dir(ok.packet_dir) == []
    assert "exr_manifest_hash_differs" not in ok.flags and "cryptomatte_manifest_missing" not in ok.flags
    assert "legend_nodes_not_in_exr" in ok.flags  # the other legend nodes are not in this tiny manifest

    wrong = _crypto_strings({"HARD_floor": "deadbeef"}, {"unrelated_material": "00000001"})
    bad = _small_packet(tmp_path / "bad", wrong, shares)
    assert {"exr_manifest_hash_differs", "material_layer_names_unmatched"} <= set(bad.flags)

    none = _small_packet(tmp_path / "none", {}, shares)
    assert "cryptomatte_manifest_missing" in none.flags
    assert plate_spec.validate_packet_dir(none.packet_dir) == []


def test_an_exr_of_the_wrong_size_or_not_an_exr_is_refused(tmp_path):
    folder = _synthetic_kit(tmp_path / "kit")
    _edit_camera(folder, render_width=32)
    with pytest.raises(KitError, match=r"16x16.*32x16"):
        convert(folder, _tiff16(tmp_path / "p.tif", 32, 16), pixel_shares={})
    (folder / "CAM_a_packet.exr").write_bytes(b"this is not an exr file")
    with pytest.raises(KitError, match="not an OpenEXR"):
        convert(folder, _tiff16(tmp_path / "q.tif", 32, 16), pixel_shares={})
    (folder / "CAM_a_packet.exr").write_bytes(b"v/1\x01\x02\x00\x00\x00chan")
    with pytest.raises(KitError, match="truncated"):
        convert(folder, _tiff16(tmp_path / "q.tif", 32, 16), pixel_shares={})


# --------------------------------------------------------------------------- kit_ingest: the camera JSON


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"fov": 60.0}, "disagrees with focal"),
        ({"lens_shift": {"horizontal": 0.1, "vertical": 0}}, "lens shift"),
        ({"tilt": {"vertical": 0, "horizontal": 0, "auto": True}}, "tilt"),
        ({"distortion": 0.2}, "distortion"),
        ({"renderer": "Default_Scanline_Renderer"}, "not V-Ray"),
        ({"units": {"system_type": "kilometers", "system_scale": 1.0}}, "kilometers"),
        ({"units": {"system_type": "meters", "system_scale": 0}}, "system_scale"),
        ({"transform": [[2, 0, 0], [0, 1, 0], [0, 0, 1], [0, 0, 0]]}, "orthonormal"),
        ({"transform": [[1, 0, 0], [0, 1, 0]]}, "4 rows of 3"),
        ({"kit_version": "9.0.0"}, "not written by plate_client_kit"),
        ({"focal_length_mm": KeyError}, "focal_length_mm"),
        ({"render_width": 0}, "positive"),
        ({"colour_mode": "unknown"}, "colour mode"),
        ({"colour_mode": "ocio"}, "OCIO"),
        ({"colour_mode": "legacy_gamma", "gamma": None}, "gamma"),
    ],
)
def test_a_bad_camera_json_is_refused_with_the_reason(kit_dir, tmp_path, changes, message):
    _edit_camera(kit_dir, **changes)
    with pytest.raises(KitError, match=message):
        convert(kit_dir, _tiff16(tmp_path / "plate.tif"))


def test_a_colour_block_can_be_supplied_when_the_kit_could_not_read_it(kit_dir, tmp_path):
    _edit_camera(kit_dir, colour_mode="ocio")
    colour = {
        "max_colour_mode": "ocio",
        "ocio": {
            "config_name": "aces_1.3",
            "config_hash": {"algo": "blake3", "hex": "0" * 64},
            "display": "sRGB",
            "view": "ACES",
        },
        "working_space": "acescg",
        "master": "display_tiff16",
        "vfb_corrections_baked": False,
        "display_encoding": {"primaries": "rec709", "transfer": "srgb", "icc": "none"},
    }
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"), colour=colour)
    assert result.manifest["colour"]["max_colour_mode"] == "ocio"
    assert plate_spec.validate_packet_dir(result.packet_dir) == []


def test_a_camera_without_dof_data_gets_nominal_values_and_says_so(kit_dir, tmp_path):
    _edit_camera(kit_dir, dof_enabled=False, f_number=None, focus_distance=None, camera_class="Freecamera")
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    assert {"f_number_nominal", "focus_distance_nominal"} <= set(result.flags)
    cam = result.manifest["camera"]
    assert (cam["dof_enabled"], cam["f_number"], cam["focus_distance"]) == (False, 8.0, 1.0)
    assert cam["exposure"] == {"mode": "none"}
    assert plate_spec.validate_packet_dir(result.packet_dir) == []


def test_dof_on_without_the_data_is_refused(kit_dir, tmp_path):
    _edit_camera(kit_dir, f_number=None)
    with pytest.raises(KitError, match="f-number"):
        convert(kit_dir, _tiff16(tmp_path / "plate.tif"))


def test_physical_exposure_is_carried_over(kit_dir, tmp_path):
    _edit_camera(kit_dir, iso=200.0, shutter_speed=100.0, f_number=2.8)
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    exposure = result.manifest["camera"]["exposure"]
    assert exposure["mode"] == "physical" and exposure["iso"] == 200.0
    assert exposure["shutter_s"] == pytest.approx(0.01) and exposure["ev100"] == pytest.approx(
        8.6147, abs=1e-3
    )
    assert plate_spec.validate_packet_dir(result.packet_dir) == []


def test_a_camera_name_with_awkward_characters_finds_its_sanitised_files(kit_dir, tmp_path):
    _edit_camera(kit_dir, camera="CAM a:1", exr="CAM_a_1_packet.exr", legend="CAM_a_1_legend.txt")
    for old, new in (
        ("CAM_a_camera.json", "CAM_a_1_camera.json"),
        ("CAM_a_packet.exr", "CAM_a_1_packet.exr"),
        ("CAM_a_legend.txt", "CAM_a_1_legend.txt"),
    ):
        (kit_dir / old).rename(kit_dir / new)
    result = kit_ingest.convert_kit(
        kit_dir,
        "CAM a:1",
        _tiff16(tmp_path / "plate.tif"),
        hasher=_b3,
        pixel_shares=_shares,
    )
    assert result.manifest["camera"]["name"] == "CAM a:1"
    assert result.manifest["shot_id"] == "cam-a-1"
    assert plate_spec.validate_packet_dir(result.packet_dir) == []


@pytest.mark.parametrize(
    "key, value", [("exr", "../escape.exr"), ("legend", "sub/dir.txt"), ("exr", "C:evil.exr")]
)
def test_file_names_in_the_camera_json_cannot_leave_the_folder(kit_dir, tmp_path, key, value):
    _edit_camera(kit_dir, **{key: value})
    with pytest.raises(KitError, match="plain file name"):
        convert(kit_dir, _tiff16(tmp_path / "plate.tif"))


def test_missing_kit_files_say_which(kit_dir, tmp_path):
    (kit_dir / "CAM_a_legend.txt").unlink()
    with pytest.raises(KitError, match="CAM_a_legend.txt"):
        convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    with pytest.raises(KitError, match="CAM_zzz_camera.json"):
        kit_ingest.convert_kit(kit_dir, "CAM zzz", tmp_path / "p.tif", hasher=_b3, pixel_shares=_shares)


# --------------------------------------------------------------------------- kit_ingest: legend and policy


def test_a_legend_without_layers_is_blocked_by_the_unlabelled_share(kit_dir, tmp_path):
    lines = (kit_dir / "CAM_a_legend.txt").read_text(encoding="utf-8").splitlines()
    plain = ["|".join([ln.split("|")[0], "0", ln.split("|")[2], ln.split("|")[3]]) for ln in lines]
    (kit_dir / "CAM_a_legend.txt").write_text("\n".join(plain) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(KitError, match="unlabelled"):
        convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    # the threshold is a parameter, but even the schema cap (0.25) does not let a mostly unlabelled frame through
    with pytest.raises(KitError, match="unlabelled"):
        convert(kit_dir, _tiff16(tmp_path / "plate.tif"), max_unlabelled_share=0.25)


def test_autotag_labels_guessable_nodes_but_never_makes_them_editable(kit_dir, tmp_path):
    rows = []
    for ln in (kit_dir / "CAM_a_legend.txt").read_text(encoding="utf-8").splitlines():
        name, layer, mat, wire = ln.split("|")
        rows.append("|".join([name, layer if name.startswith("ARCH_") else "Layer 01", mat, wire]))
    (kit_dir / "CAM_a_legend.txt").write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"), max_unlabelled_share=0.25)
    legend = json.loads((result.packet_dir / "id_legend.json").read_text(encoding="utf-8"))
    by_name = {e["name"]: e for e in legend["entries"] if e["kind"] == "object"}
    assert by_name["GLZ_pane"]["class"] == "glazing" and by_name["GLZ_pane"]["class_source"] == "autotag"
    assert by_name["SKY_dome"]["class"] == "sky" and by_name["VEG_shrub_A"]["class"] == "vegetation"
    assert by_name["ARCH_wall_N"]["class_source"] == "rule"  # a layer rule still wins over a guess
    # no editable layer prefix survives: everything stays locked
    assert result.manifest["policy"]["editable_ids"] == []
    assert plate_spec.validate_packet_dir(result.packet_dir) == []


def test_an_id_the_legend_does_not_name_is_named_from_the_exr_manifest(kit_dir, tmp_path):
    lines = [
        ln
        for ln in (kit_dir / "CAM_a_legend.txt").read_text(encoding="utf-8").splitlines()
        if "WATER_pool" not in ln
    ]
    (kit_dir / "CAM_a_legend.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    legend = json.loads((result.packet_dir / "id_legend.json").read_text(encoding="utf-8"))
    pool = next(e for e in legend["entries"] if e["id"] == "o:c8a9bf52")
    assert pool["name"] == "WATER_pool" and "layer" not in pool
    assert (pool["class"], pool["class_source"]) == ("water", "autotag")  # a guess from the name alone
    assert "o:c8a9bf52" in result.manifest["policy"]["locked_ids"]
    assert plate_spec.validate_packet_dir(result.packet_dir) == []


def test_an_id_nobody_can_name_is_refused(kit_dir, tmp_path):
    def shares(exr_path, elements):
        return {**_shares(exr_path, elements), "o:12345678": 0.01}

    with pytest.raises(KitError, match=r"o:12345678.*neither the kit legend"):
        convert(kit_dir, _tiff16(tmp_path / "plate.tif"), pixel_shares=shares)


def test_two_nodes_with_one_name_are_refused(kit_dir, tmp_path):
    with (kit_dir / "CAM_a_legend.txt").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("HARD_floor|HARD_|HARD_floor|1,2,3\n")
    with pytest.raises(KitError, match="duplicate"):
        convert(kit_dir, _tiff16(tmp_path / "plate.tif"))


def test_an_object_layer_that_does_not_hash_node_names_is_refused(kit_dir, tmp_path):
    rows = [
        "|".join(["renamed_" + ln.split("|")[0], *ln.split("|")[1:]])
        for ln in (kit_dir / "CAM_a_legend.txt").read_text(encoding="utf-8").splitlines()
    ]
    (kit_dir / "CAM_a_legend.txt").write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(KitError, match="names none of the kit legend"):
        convert(kit_dir, _tiff16(tmp_path / "plate.tif"))


@pytest.mark.parametrize(
    "text, message",
    [
        ("only|three|fields\n", "got 3 fields"),
        ("a|b|c|1,2\n", "wirecolor"),
        ("a|b|c|1,2,300\n", "wirecolor"),
        ("|b|c|1,2,3\n", "empty node name"),
    ],
)
def test_malformed_legend_lines_are_refused_with_the_line_number(text, message):
    with pytest.raises(KitError, match=rf"legend line 2.*{message}"):
        kit_ingest.parse_legend_txt("# comment\n" + text)


# --------------------------------------------------------------------------- kit_ingest: capabilities and output


def test_the_capabilities_are_required_and_checked(kit_dir, tmp_path):
    plate = _tiff16(tmp_path / "plate.tif")
    with pytest.raises(KitError, match="hasher is required"):
        kit_ingest.kit_to_packet(kit_dir, "CAM_a", plate, pixel_shares=_shares)
    with pytest.raises(KitError, match="pixel_shares is required"):
        kit_ingest.kit_to_packet(kit_dir, "CAM_a", plate, hasher=_b3)
    with pytest.raises(KitError, match="64-character"):
        convert(kit_dir, plate, hasher=lambda source: "abc")
    assert [p.name for p in kit_dir.iterdir() if p.is_dir()] == []


def test_the_hasher_may_return_the_daemons_mapping_and_may_be_given_bytes_or_paths(kit_dir, tmp_path):
    seen = []

    def hasher(source):
        seen.append(type(source))
        return {"algo": "blake3", "hex": _b3(source), "bytes": 0}

    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"), hasher=hasher)
    assert {bytes, type(tmp_path)} <= set(seen)
    assert plate_spec.validate_packet_dir(result.packet_dir, check_files=True) == []


def test_kit_to_packet_returns_the_folder_and_accepts_the_plan_signature(kit_dir, tmp_path):
    out = tmp_path / "out" / "pkt"
    packet_dir = kit_ingest.kit_to_packet(
        kit_dir,
        "CAM_a",
        _tiff16(tmp_path / "plate.tif"),
        hasher=_b3,
        pixel_shares=_shares,
        out_dir=out,
        project_id="tula-wot",
        shot_id="clubhouse-1",
        packet_id="pkt_2ZHQ7DX53BCK1J9P8GJB9Z2V5G",
        created_utc="2026-10-17T00:00:00Z",
        supersedes=None,
    )
    assert packet_dir == out
    m = _manifest(out)
    assert (m["project_id"], m["shot_id"], m["packet_id"], m["created_utc"]) == (
        "tula-wot",
        "clubhouse-1",
        "pkt_2ZHQ7DX53BCK1J9P8GJB9Z2V5G",
        "2026-10-17T00:00:00Z",
    )
    assert plate_spec.validate_packet_dir(out) == []
    with pytest.raises(KitError, match="already holds files"):
        kit_ingest.kit_to_packet(
            kit_dir, "CAM_a", _tiff16(tmp_path / "plate.tif"), hasher=_b3, pixel_shares=_shares, out_dir=out
        )
    with pytest.raises(KitError, match="packet_id"):
        convert(kit_dir, _tiff16(tmp_path / "plate.tif"), packet_id="nope")


def test_by_default_the_packet_goes_next_to_the_kit_output(kit_dir, tmp_path):
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    assert result.packet_dir.parent == kit_dir and result.packet_dir.name == result.packet_id
    again = convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    assert again.packet_dir != result.packet_dir  # a second conversion is a new packet, never an overwrite


def test_the_json_files_are_utf8_with_lf_and_no_bom(kit_dir, tmp_path):
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    for name in ("manifest.json", "id_legend.json", "kit_flags.json"):
        raw = (result.packet_dir / name).read_bytes()
        assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
        raw.decode("utf-8")


def test_a_legend_text_with_a_bom_and_crlf_is_read(kit_dir, tmp_path):
    text = (kit_dir / "CAM_a_legend.txt").read_text(encoding="utf-8").replace("\n", "\r\n")
    (kit_dir / "CAM_a_legend.txt").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    result = convert(kit_dir, _tiff16(tmp_path / "plate.tif"))
    assert plate_spec.validate_packet_dir(result.packet_dir) == []
