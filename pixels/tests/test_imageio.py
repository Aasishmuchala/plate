"""Display-image IO: PNG and TIFF round trips, ICC embedding, metadata stripping, atomic writes."""

from __future__ import annotations

import hashlib
import io
import struct
import zlib

import cv2
import numpy as np
import pytest
import tifffile
from PIL import ImageCms

from plate_pixels import icc, imageio

H, W = 24, 40


def _u16(seed=1, channels=3):
    return np.random.default_rng(seed).integers(0, 65536, size=(H, W, channels), dtype=np.uint16)


def _png_with_junk(rgb8: np.ndarray) -> bytes:
    """A PNG carrying every kind of metadata chunk the egress strip must remove, plus an iCCP and a gAMA."""
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(rgb8[..., ::-1]))
    assert ok
    chunks = list(icc.iter_png_chunks(bytes(buf)))
    ihdr = chunks[0]
    rest = chunks[1:]
    junk = [
        (b"tEXt", b"Author\x00Aparna Studio"),
        (b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00<x:xmpmeta/>"),
        (b"zTXt", b"Comment\x00\x00" + zlib.compress(b"client NDA")),
        (b"eXIf", b"MM\x00*\x00\x00\x00\x08\x00\x00"),
        (b"tIME", struct.pack(">HBBBBB", 2026, 10, 17, 9, 41, 7)),
        (b"pHYs", struct.pack(">IIB", 2835, 2835, 1)),
    ]
    out = [icc.PNG_SIGNATURE, icc._chunk(*ihdr), icc._chunk(b"gAMA", struct.pack(">I", 45455))]
    out.append(icc._chunk(b"iCCP", b"sRGB\x00\x00" + zlib.compress(icc.SRGB_ICC)))
    out += [icc._chunk(*j) for j in junk]
    out += [icc._chunk(*c) for c in rest]
    return b"".join(out)


# --------------------------------------------------------------------------- TIFF


def test_tiff16_round_trip_is_exact_and_carries_the_profile(tmp_path):
    a = _u16()
    path = tmp_path / "master.tif"
    imageio.write_tiff16(path, a)
    im = imageio.read(path)
    assert im.bit_depth == 16 and im.encoding == "srgb" and im.alpha is None
    assert im.rgb.dtype == np.float32 and im.rgb.shape == (H, W, 3)
    assert np.array_equal(np.rint(im.rgb * 65535).astype(np.uint16), a)
    assert im.icc == icc.SRGB_ICC
    assert not (tmp_path / "master.tif.part").exists()


def test_tiff16_float_input_quantises_to_nearest_level(tmp_path):
    a = _u16(2)
    path = tmp_path / "f.tif"
    imageio.write_tiff16(path, a.astype(np.float32) / 65535.0, icc=None)
    im = imageio.read(path)
    assert np.array_equal(np.rint(im.rgb * 65535).astype(np.uint16), a)
    assert im.icc is None


def test_tiff16_alpha_and_no_identifying_tags(tmp_path):
    a = _u16(3)
    alpha = _u16(4, 1)[..., 0]
    path = tmp_path / "rgba.tif"
    imageio.write_tiff16(path, a, alpha=alpha)
    im = imageio.read(path)
    assert im.alpha is not None
    assert np.array_equal(np.rint(im.alpha * 65535).astype(np.uint16), alpha)
    with tifffile.TiffFile(path) as tf:
        tags = {t.name for t in tf.pages[0].tags.values()}
    assert "Software" not in tags and "ImageDescription" not in tags
    assert "InterColorProfile" in tags


def test_tiff_float32_reads_as_linear(tmp_path):
    a = np.random.default_rng(5).random((H, W, 3), dtype=np.float32) * 4.0
    path = tmp_path / "lin.tiff"
    with open(path, "wb") as fh:
        tifffile.imwrite(fh, a, photometric="rgb")
    im = imageio.read(path)
    assert im.encoding == "linear" and im.bit_depth == 32
    assert np.array_equal(im.rgb, a)


def test_overwrite_replaces_the_previous_file(tmp_path):
    path = tmp_path / "x.tif"
    imageio.write_tiff16(path, _u16(1))
    imageio.write_tiff16(path, _u16(2))
    assert np.array_equal(np.rint(imageio.read(path).rgb * 65535).astype(np.uint16), _u16(2))


# --------------------------------------------------------------------------- PNG


def test_png16_round_trip_is_exact_and_embeds_icc(tmp_path):
    a = _u16(6)
    path = tmp_path / "plate16.png"
    imageio.write_png16(path, a)
    im = imageio.read(path)
    assert im.bit_depth == 16 and im.encoding == "srgb"
    assert np.array_equal(np.rint(im.rgb * 65535).astype(np.uint16), a)
    assert im.icc == icc.SRGB_ICC
    names = icc.png_chunk_types(path.read_bytes())
    assert names[0] == "IHDR" and names[1] == "iCCP" and names[-1] == "IEND"
    assert "sRGB" not in names  # iCCP and sRGB chunks must not coexist


def test_png8_round_trip_and_icc_opt_out(tmp_path):
    a8 = (_u16(7) >> 8).astype(np.uint8)
    path = tmp_path / "plate8.png"
    imageio.write_png8(path, a8)
    im = imageio.read(path)
    assert im.bit_depth == 8 and im.icc == icc.SRGB_ICC
    assert np.array_equal(np.rint(im.rgb * 255).astype(np.uint8), a8)
    bare = tmp_path / "bare.png"
    imageio.write_png8(bare, a8, icc=None)
    assert imageio.read(bare).icc is None and "iCCP" not in icc.png_chunk_types(bare.read_bytes())


def test_png_rgba_and_gray_inputs(tmp_path):
    a = _u16(8)
    alpha = _u16(9, 1)[..., 0]
    path = tmp_path / "rgba.png"
    imageio.write_png16(path, a, alpha=alpha)
    im = imageio.read(path)
    assert np.array_equal(np.rint(im.alpha * 65535).astype(np.uint16), alpha)
    gray = tmp_path / "gray.png"
    ok, buf = cv2.imencode(".png", (np.arange(H * W, dtype=np.uint16).reshape(H, W) % 256).astype(np.uint8))
    gray.write_bytes(bytes(buf))
    g = imageio.read(gray)
    assert g.rgb.shape == (H, W, 3) and np.array_equal(g.rgb[..., 0], g.rgb[..., 2])


def test_non_ascii_directory_round_trip(tmp_path):
    folder = tmp_path / "prøject_客户"
    path = folder / "plate.png"
    imageio.write_png16(path, _u16(10))
    assert imageio.read(path).bit_depth == 16


def test_float_input_is_clipped_not_wrapped(tmp_path):
    a = np.full((4, 4, 3), 1.7, dtype=np.float32)
    a[0, 0] = -0.3
    path = tmp_path / "clip.png"
    imageio.write_png8(path, a)
    im = imageio.read(path)
    assert im.rgb[1, 1, 0] == 1.0 and im.rgb[0, 0, 0] == 0.0


def test_strip_metadata_keeps_the_profile_and_the_pixels():
    a8 = (_u16(11) >> 8).astype(np.uint8)
    dirty = _png_with_junk(a8)
    before = icc.png_chunk_types(dirty)
    for junk in ("tEXt", "iTXt", "zTXt", "eXIf", "tIME", "pHYs"):
        assert junk in before
    clean = icc.strip_metadata_png(dirty)
    after = icc.png_chunk_types(clean)
    assert after[0] == "IHDR" and after[-1] == "IEND"
    assert set(after) <= {"IHDR", "PLTE", "IDAT", "IEND", "iCCP", "sRGB", "gAMA", "cHRM", "tRNS"}
    assert "iCCP" in after and "gAMA" in after and "IDAT" in after
    for junk in ("tEXt", "iTXt", "zTXt", "eXIf", "tIME", "pHYs"):
        assert junk not in after
    assert icc.read_png_icc(clean) == icc.SRGB_ICC
    got = cv2.imdecode(np.frombuffer(clean, np.uint8), cv2.IMREAD_UNCHANGED)
    assert np.array_equal(got[..., ::-1], a8)
    assert b"Aparna" not in clean and b"NDA" not in clean


def test_strip_rejects_non_png():
    with pytest.raises(ValueError):
        icc.strip_metadata_png(b"GIF89a....")
    with pytest.raises(ValueError):
        icc.strip_metadata_png(icc.PNG_SIGNATURE + b"\x00\x00")


# --------------------------------------------------------------------------- the generated profile


def test_srgb_icc_is_a_valid_profile():
    data = icc.SRGB_ICC
    assert struct.unpack(">I", data[:4])[0] == len(data)
    assert data[36:40] == b"acsp" and data[12:16] == b"mntr" and data[16:20] == b"RGB "
    zeroed = data[:84] + b"\x00" * 16 + data[100:]
    assert hashlib.md5(zeroed).digest() == data[84:100]
    assert icc.build_srgb_profile() == data  # deterministic
    profile = ImageCms.ImageCmsProfile(io.BytesIO(data))
    assert "sRGB" in ImageCms.getProfileDescription(profile)


def test_srgb_icc_agrees_with_littlecms_builtin_srgb():
    from PIL import Image as PILImage

    ramp = np.zeros((1, 256, 3), dtype=np.uint8)
    ramp[0, :, 0] = np.arange(256)
    ramp[0, :, 1] = np.arange(256)[::-1]
    ramp[0, :, 2] = (np.arange(256) * 7) % 256
    ours = ImageCms.ImageCmsProfile(io.BytesIO(icc.SRGB_ICC))
    ref = ImageCms.createProfile("sRGB")
    transform = ImageCms.buildTransform(ours, ref, "RGB", "RGB", renderingIntent=1)
    out = np.asarray(ImageCms.applyTransform(PILImage.fromarray(ramp), transform))
    assert np.abs(out.astype(int) - ramp.astype(int)).max() <= 2


# --------------------------------------------------------------------------- atomic replace


def test_replace_with_retry_survives_transient_locks(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.part", tmp_path / "a"
    src.write_bytes(b"x")
    calls = {"n": 0}
    real = imageio.os.replace

    def flaky(s, d):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError("sharing violation")
        return real(s, d)

    monkeypatch.setattr(imageio.os, "replace", flaky)
    monkeypatch.setattr(imageio.time, "sleep", lambda s: None)
    imageio.replace_with_retry(src, dst)
    assert calls["n"] == 4 and dst.read_bytes() == b"x"


def test_replace_with_retry_gives_up_after_five_retries_and_cleans_the_part(tmp_path, monkeypatch):
    calls = {"n": 0}

    def locked(s, d):
        calls["n"] += 1
        raise PermissionError("locked")

    monkeypatch.setattr(imageio.os, "replace", locked)
    monkeypatch.setattr(imageio.time, "sleep", lambda s: None)
    target = tmp_path / "out.png"
    with pytest.raises(PermissionError):
        imageio.write_png8(target, np.zeros((2, 2, 3), np.uint8))
    assert calls["n"] == 6  # first try plus five retries
    assert not target.exists() and not (tmp_path / "out.png.part").exists()


def test_part_path_keeps_the_extension_visible():
    assert imageio.part_path("a/b.tif").name == "b.tif.part"
