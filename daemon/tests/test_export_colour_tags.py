"""Export colour tags (router plan T80): a tiff16 carries the sRGB ICC profile its pixels are encoded in, an EXR
carries chromaticities, the ASWF colorInteropID and a plate:colourspace attribute that match the packet's
declared working space (and the pixels in the file), and delivery reads those tags back. Read with OpenImageIO."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import OpenImageIO as oiio
import pytest

from plate_daemon import db, export, lineage
from plate_daemon.cas import CAS
from plate_daemon.delivery import frames
from plate_daemon.index import apply_packet_entry
from plate_daemon.models import Route, Run
from plate_daemon.states import RunState
from plate_pixels import icc, imageio
from plate_pixels.ocio import srgb_eotf

PROJECT, PACKET = "colour-proj", "colour-pk"
WIDTH, HEIGHT = 24, 16


def source16() -> np.ndarray:
    """A smooth, saturated sRGB test image (all three primaries and a grey ramp), uint16 (H, W, 3)."""
    y, x = np.mgrid[0:HEIGHT, 0:WIDTH]
    rgb = np.stack([x / (WIDTH - 1), y / (HEIGHT - 1), ((x + y) % 8) / 7.0], axis=-1)
    rgb[:, :4] = [0.9, 0.1, 0.05]  # a strongly saturated stripe: where the primaries matter
    return np.rint(rgb * 65535.0).astype(np.uint16)


def png_bytes(rgb16: np.ndarray, path: Path) -> bytes:
    imageio.write_png16(path, rgb16)
    return path.read_bytes()


class Project:
    """An index, a CAS holding one result PNG, and a registered packet whose manifest declares `space`."""

    def __init__(self, tmp: Path, space: str | None):
        self.tmp = tmp
        self.conn = db.open_index(tmp / "index.sqlite")
        folder = tmp / "packet"
        folder.mkdir()
        colour = {} if space is None else {"working_space": space}
        (folder / "manifest.json").write_text(json.dumps({"colour": colour}), encoding="utf-8", newline="\n")
        apply_packet_entry(
            self.conn,
            PROJECT,
            PACKET,
            {
                "shot_id": "S1",
                "kind": "still",
                "tier": "A",
                "packet_version": "0.1.1",
                "path": str(folder),
                "manifest_blake3": "0" * 64,
            },
        )
        self.cas = CAS(tmp / "assets")
        self.src = source16()
        self.key = self.cas.put_bytes(png_bytes(self.src, tmp / "src.png"), ext="png")
        now = datetime(2026, 10, 18, tzinfo=UTC)
        self.run = Run(
            id="run-colour-1",
            project_id=PROJECT,
            packet_id=PACKET,
            state=RunState.DONE,
            priority="interactive",
            route=Route(engine_id="fake", provider="local", region="local"),
            attempt=1,
            node_id="n1",
            result_key=self.key,
            created_utc=now,
            updated_utc=now,
        )

    def export(self, fmt: str, name: str, **kw):
        return export.export(self.run, fmt, self.tmp / "out", conn=self.conn, cas=self.cas, name=name, **kw)


def make_project(tmp_path: Path, space: str | None) -> Project:
    return Project(tmp_path, space)


def exr_spec(path: Path) -> oiio.ImageSpec:
    inp = oiio.ImageInput.open(str(path))
    assert inp is not None, oiio.geterror()
    try:
        return inp.spec()
    finally:
        inp.close()


def exr_pixels(path: Path) -> np.ndarray:
    inp = oiio.ImageInput.open(str(path))
    try:
        spec = inp.spec()
        return np.asarray(inp.read_image("float"), dtype=np.float32).reshape(spec.height, spec.width, -1)
    finally:
        inp.close()


def expected_linear(rgb16: np.ndarray, matrix: np.ndarray | None = None) -> np.ndarray:
    lin = srgb_eotf(rgb16.astype(np.float64) / 65535.0)
    return lin if matrix is None else lin @ matrix.T


# ---------------------------------------------------------------------------- tiff16 and png16


def test_tiff16_carries_the_srgb_icc_profile_and_the_lineage(tmp_path):
    p = make_project(tmp_path, "lin_rec709")
    done = p.export("tiff16", "shot")
    spec = exr_spec(done.path)
    profile = spec.getattribute("ICCProfile")
    assert profile is not None and bytes(profile) == icc.SRGB_ICC
    assert "sRGB" in spec.get_string_attribute("ICCProfile:profile_description")
    assert imageio.read(done.path).icc == icc.SRGB_ICC
    assert lineage.read(done.path).run_id == p.run.id  # embedding the lineage kept the profile
    assert np.array_equal(imageio.read(done.path).rgb, p.src.astype(np.float32) / 65535.0)  # pixels untouched


def test_png16_carries_the_srgb_icc_profile(tmp_path):
    p = make_project(tmp_path, "acescg")
    done = p.export("png16", "shot")
    assert icc.read_png_icc(done.path.read_bytes()) == icc.SRGB_ICC
    assert lineage.read(done.path).run_id == p.run.id


@pytest.mark.parametrize("space", ["lin_rec709", "acescg"])
def test_the_working_space_never_changes_the_display_formats(tmp_path, space):
    p = make_project(tmp_path, space)
    tif = p.export("tiff16", "a")
    assert np.array_equal(imageio.read(tif.path).rgb, p.src.astype(np.float32) / 65535.0)


# ---------------------------------------------------------------------------- exr


def test_exr_of_a_rec709_packet_is_tagged_rec709(tmp_path):
    p = make_project(tmp_path, "lin_rec709")
    done = p.export("exr", "shot")
    spec = exr_spec(done.path)
    assert np.allclose(spec.getattribute("chromaticities"), export.REC709_CHROMATICITIES, atol=1e-6)
    assert spec.get_string_attribute("colorInteropID") == "lin_rec709_scene"
    assert spec.get_string_attribute("oiio:ColorSpace") == "lin_rec709_scene"
    assert spec.get_string_attribute("plate:colourspace") == "lin_rec709"
    assert "Rec.709" in spec.get_string_attribute(
        "plate:colour"
    ) or "lin_rec709" in spec.get_string_attribute("plate:colour")
    assert np.allclose(exr_pixels(done.path)[..., :3], expected_linear(p.src), atol=2e-3)
    assert lineage.read(done.path).run_id == p.run.id  # the lineage embed kept the tags (read back above)


def test_exr_of_an_acescg_packet_is_converted_and_tagged_acescg(tmp_path):
    p = make_project(tmp_path, "acescg")
    done = p.export("exr", "shot")
    spec = exr_spec(done.path)
    assert np.allclose(spec.getattribute("chromaticities"), export.AP1_CHROMATICITIES, atol=1e-6)
    assert spec.get_string_attribute("colorInteropID") == "lin_ap1_scene"
    assert spec.get_string_attribute("oiio:ColorSpace") == "lin_ap1_scene"
    assert spec.get_string_attribute("plate:colourspace") == "ACEScg"
    to_ap1 = export.primaries_matrix(export.REC709_CHROMATICITIES, export.AP1_CHROMATICITIES)
    got = exr_pixels(done.path)[..., :3]
    assert np.allclose(got, expected_linear(p.src, to_ap1), atol=2e-3)
    # the pixels really changed: an ACEScg file that held Rec.709 values would be mislabelled
    assert not np.allclose(got, expected_linear(p.src), atol=2e-2)


def test_the_acescg_pixels_agree_with_opencolorio(tmp_path):
    import PyOpenColorIO as OCIO

    try:
        cfg = OCIO.Config.CreateFromBuiltinConfig("studio-config-latest")
        proc = cfg.getProcessor("Linear Rec.709 (sRGB)", "ACEScg").getDefaultCPUProcessor()
    except Exception as e:  # a build of OpenColorIO without the built-in configs
        pytest.skip(f"no built-in OCIO config: {e}")
    p = make_project(tmp_path, "acescg")
    got = exr_pixels(p.export("exr", "shot").path)[..., :3]
    ref = expected_linear(p.src).astype(np.float32)
    flat = np.ascontiguousarray(ref.reshape(-1, 3))
    proc.applyRGB(flat)
    assert np.allclose(got, flat.reshape(ref.shape), atol=2e-3)


@pytest.mark.parametrize("space", [None, "display_only", "something_else"])
def test_a_packet_without_a_usable_working_space_gets_rec709_tags(tmp_path, space):
    p = make_project(tmp_path, space)
    spec = exr_spec(p.export("exr", "shot").path)
    assert spec.get_string_attribute("plate:colourspace") == "lin_rec709"
    assert np.allclose(spec.getattribute("chromaticities"), export.REC709_CHROMATICITIES, atol=1e-6)


def test_a_packet_that_is_not_in_the_index_gets_rec709_tags(tmp_path):
    p = make_project(tmp_path, "acescg")
    p.run = p.run.model_copy(update={"packet_id": "unknown-packet"})
    assert exr_spec(p.export("exr", "shot").path).get_string_attribute("plate:colourspace") == "lin_rec709"


def test_an_explicit_working_space_wins_over_the_packet(tmp_path):
    p = make_project(tmp_path, "lin_rec709")
    spec = exr_spec(p.export("exr", "shot", working_space="acescg").path)
    assert spec.get_string_attribute("plate:colourspace") == "ACEScg"


def test_an_unknown_working_space_is_refused_before_anything_is_written(tmp_path):
    p = make_project(tmp_path, "lin_rec709")
    with pytest.raises(ValueError, match="working space"):
        p.export("exr", "shot", working_space="p3d65")
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------- the helper and the reader


def test_primaries_matrix_is_the_published_srgb_to_acescg_matrix_and_invertible():
    m = export.primaries_matrix(export.REC709_CHROMATICITIES, export.AP1_CHROMATICITIES)
    published = np.array(
        [
            [0.6131324224, 0.3395380158, 0.0474166960],
            [0.0701243808, 0.9163940113, 0.0134515240],
            [0.0205876575, 0.1095745716, 0.8697854040],
        ]
    )
    assert np.allclose(m, published, atol=2e-4)
    assert np.allclose(m.sum(axis=1), 1.0, atol=1e-9)  # white stays white
    back = export.primaries_matrix(export.AP1_CHROMATICITIES, export.REC709_CHROMATICITIES)
    assert np.allclose(back @ m, np.eye(3), atol=1e-9)
    assert np.array_equal(
        export.primaries_matrix(export.REC709_CHROMATICITIES, export.REC709_CHROMATICITIES), np.eye(3)
    )


@pytest.mark.parametrize("space", ["lin_rec709", "acescg"])
def test_delivery_reads_the_tags_back_to_the_same_rec709_pixels(tmp_path, space):
    p = make_project(tmp_path, space)
    done = p.export("exr", "shot")
    frame = next(iter(frames.open_frames(done.path)))
    assert frame.encoding == "linear"
    assert np.allclose(frame.rgb, expected_linear(p.src), atol=3e-3)


def write_exr(path: Path, rgb: np.ndarray, **attrs) -> None:
    spec = oiio.ImageSpec(rgb.shape[1], rgb.shape[0], 3, "half")
    for name, value in attrs.items():
        if name == "chromaticities":
            spec.attribute(name, oiio.TypeDesc("float[8]"), value)
        else:
            spec.attribute(name, value)
    out = oiio.ImageOutput.create(str(path))
    assert out.open(str(path), spec), out.geterror()
    assert out.write_image(np.ascontiguousarray(rgb, dtype=np.float32)), out.geterror()
    out.close()


def test_delivery_leaves_an_exr_with_no_tags_alone(tmp_path):
    rgb = np.random.default_rng(0).random((6, 7, 3)).astype(np.float32)
    write_exr(tmp_path / "plain.exr", rgb)
    frame = next(iter(frames.open_frames(tmp_path / "plain.exr")))
    assert np.allclose(frame.rgb, rgb, atol=2e-3)


def test_delivery_understands_an_interop_id_without_chromaticities(tmp_path):
    rgb = np.random.default_rng(1).random((6, 7, 3)).astype(np.float32)
    write_exr(tmp_path / "ap1.exr", rgb, colorInteropID="lin_ap1_scene")
    frame = next(iter(frames.open_frames(tmp_path / "ap1.exr")))
    to_709 = export.primaries_matrix(export.AP1_CHROMATICITIES, export.REC709_CHROMATICITIES)
    assert np.allclose(frame.rgb, rgb @ to_709.T, atol=3e-3)


def test_delivery_converts_other_declared_primaries_too(tmp_path):
    rgb = np.random.default_rng(2).random((6, 7, 3)).astype(np.float32)
    p3 = (0.68, 0.32, 0.265, 0.69, 0.15, 0.06, 0.3127, 0.3290)  # P3-D65
    write_exr(tmp_path / "p3.exr", rgb, chromaticities=p3)
    frame = next(iter(frames.open_frames(tmp_path / "p3.exr")))
    assert np.allclose(
        frame.rgb, rgb @ export.primaries_matrix(p3, export.REC709_CHROMATICITIES).T, atol=3e-3
    )
