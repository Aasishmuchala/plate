"""OCR text-lock masks and the stub / Paddle OCR backends (router plan T16)."""

from __future__ import annotations

import json
import sys

import numpy as np
import pytest

from plate_gates import textlock
from plate_gates.masks import rect
from plate_pixels import ocr as ocr_mod
from plate_pixels.ocr import OcrBackend, PaddleOcr, StubOcr

H, W = 90, 160
BOX = (40, 30, 100, 50)  # "PLATE 01" on the plate


def _plate8(seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, (H, W, 3), dtype=np.uint8)


def _pair16(seed: int = 0):
    rng = np.random.default_rng(seed)
    return (
        rng.integers(0, 65536, (H, W, 3), dtype=np.uint16),
        rng.integers(0, 65536, (H, W, 3), dtype=np.uint16),
    )


def _write_sidecar(image, boxes) -> None:
    sidecar = ocr_mod.sidecar_path(image)
    sidecar.write_text(json.dumps(boxes), encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------- StubOcr


def test_stub_ocr_reads_the_sidecar_next_to_the_image(tmp_path):
    image = tmp_path / "plate.png"
    image.write_bytes(b"not read by the stub")
    _write_sidecar(image, [{"bbox": list(BOX), "text": "PLATE 01", "conf": 0.97}])
    assert (tmp_path / "plate.png.ocr.json").is_file()
    found = StubOcr(image).detect(_plate8())
    assert found == [{"bbox": BOX, "text": "PLATE 01", "conf": 0.97}]


def test_stub_ocr_without_a_sidecar_or_image_finds_nothing(tmp_path):
    assert StubOcr(tmp_path / "plate.png").detect(_plate8()) == []
    assert StubOcr().detect(_plate8()) == []


def test_stub_ocr_accepts_boxes_directly_and_normalises_them():
    found = StubOcr(boxes=[{"box": (100.4, 50.0, 40.2, 30.7), "text": "x"}]).detect(_plate8())
    assert found == [
        {"bbox": (40, 30, 101, 50), "text": "x", "conf": 1.0}
    ]  # sorted corners, widened outwards


def test_stub_ocr_sidecar_may_wrap_the_list_and_carry_a_bom(tmp_path):
    image = tmp_path / "p.png"
    sidecar = ocr_mod.sidecar_path(image)
    sidecar.write_bytes(b"\xef\xbb\xbf" + json.dumps({"boxes": [{"bbox": [1, 2, 3, 4]}]}).encode("utf-8"))
    assert StubOcr(image).detect(_plate8())[0]["bbox"] == (1, 2, 3, 4)


def test_stub_ocr_rejects_a_broken_sidecar_and_a_non_uint8_image(tmp_path):
    image = tmp_path / "p.png"
    ocr_mod.sidecar_path(image).write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        StubOcr(image).detect(_plate8())
    _write_sidecar(image, [{"text": "no box"}])
    with pytest.raises(ValueError, match="bbox"):
        StubOcr(image).detect(_plate8())
    with pytest.raises(ValueError, match="uint8"):
        StubOcr().detect(_plate8().astype(np.float32))


def test_ocr_backend_is_abstract():
    with pytest.raises(TypeError):
        OcrBackend()  # type: ignore[abstract]


def test_paddle_ocr_without_the_package_raises_with_an_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr", None)  # makes `import paddleocr` raise ImportError
    with pytest.raises(NotImplementedError, match="gpu"):
        PaddleOcr().detect(_plate8())


# --------------------------------------------------------------------------- masks


def test_masks_is_the_ocr_box_dilated_by_six(tmp_path):
    image = tmp_path / "plate.png"
    _write_sidecar(image, [{"bbox": list(BOX), "text": "PLATE 01", "conf": 0.9}])
    lock = textlock.masks(_plate8(), legend=[], crypto_object=None, ocr=StubOcr(image))
    x0, y0, x1, y1 = BOX
    assert lock.dtype == bool and lock.shape == (H, W)
    assert np.array_equal(lock, rect((H, W), (x0 - 6, y0 - 6, x1 + 6, y1 + 6)))


def test_masks_clamps_boxes_to_the_frame_and_honours_min_conf():
    ocr = StubOcr(
        boxes=[
            {"bbox": (-10, -10, 8, 8), "conf": 0.9},  # off the top-left corner
            {"bbox": (120, 60, 400, 300), "conf": 0.2},  # low confidence, off the bottom-right
        ]
    )
    lock = textlock.masks(_plate8(), [], None, ocr, min_conf=0.5)
    assert np.array_equal(lock, rect((H, W), (0, 0, 14, 14)))
    both = textlock.masks(_plate8(), [], None, ocr, min_conf=0.0)
    assert both[H - 1, W - 1] and both[54, 114] and not lock[54, 114]


def _signage_scene():
    sign_id, wall_id = 0x0AAFB81B, 0x3D8800DF
    ranks = np.full((H, W, 2), wall_id, dtype=np.uint32)
    cov = np.zeros((H, W, 2), dtype=np.float32)
    cov[..., 0] = 1.0
    sign = rect((H, W), (10, 60, 30, 75))
    ranks[sign, 0] = sign_id
    # anti-aliased edge: 30% sign at rank 0 over the wall at rank 1 on one column
    ranks[10:20, 30, 0], cov[10:20, 30, 0] = sign_id, 0.3
    ranks[10:20, 30, 1], cov[10:20, 30, 1] = wall_id, 0.7
    legend = {
        "packet_id": "pkt_x",
        "entries": [
            {"id": "o:0aafb81b", "kind": "object", "name": "SIGN_a", "class": "signage"},
            {"id": "o:3d8800df", "kind": "object", "name": "ARCH_wall_N", "class": "architecture"},
            {"id": "m:0aafb81b", "kind": "material", "name": "SIGN_a_mat", "class": "signage"},
        ],
    }
    return legend, (ranks, cov), sign


def test_masks_adds_signage_class_ids_from_cryptomatte():
    legend, crypto, sign = _signage_scene()
    lock = textlock.masks(_plate8(), legend, crypto, ocr=None)
    assert lock[sign].all()
    assert lock[10:20, 30].all()  # a 30% edge pixel is still the sign: locked
    assert lock.sum() == sign.sum() + 10  # nothing else: the wall (architecture) is not locked


def test_masks_is_the_union_of_ocr_and_signage():
    legend, crypto, sign = _signage_scene()
    ocr = StubOcr(boxes=[{"bbox": BOX}])
    lock = textlock.masks(_plate8(), legend, crypto, ocr)
    x0, y0, x1, y1 = BOX
    ocr_only = rect((H, W), (x0 - 6, y0 - 6, x1 + 6, y1 + 6))
    assert lock[ocr_only].all() and lock[sign].all()
    assert lock.sum() == (ocr_only | sign).sum() + 10


def test_masks_ignores_signage_when_there_is_no_cryptomatte():
    legend, _, _ = _signage_scene()
    assert not textlock.masks(_plate8(), legend, None, ocr=None).any()  # Tier C: OCR only, and no OCR here


def test_masks_validates_its_inputs():
    with pytest.raises(ValueError, match="uint8"):
        textlock.masks(_plate8().astype(np.float32), [], None, None)
    ranks = np.zeros((H - 1, W, 1), np.uint32)
    with pytest.raises(ValueError, match="Cryptomatte shape"):
        textlock.masks(_plate8(), [], (ranks, ranks.astype(np.float32)), None)


# --------------------------------------------------------------------------- apply


def test_apply_restores_the_plate_inside_the_box_bit_exactly(tmp_path):
    image = tmp_path / "plate.png"
    _write_sidecar(image, [{"bbox": list(BOX), "text": "PLATE 01"}])
    plate8 = _plate8()
    plate16, out16 = _pair16()
    before = out16.copy()
    lock = textlock.masks(plate8, [], None, StubOcr(image))
    fixed = textlock.apply(plate16, out16, lock)
    x0, y0, x1, y1 = BOX
    area = (slice(y0 - 6, y1 + 6), slice(x0 - 6, x1 + 6))
    assert np.array_equal(fixed[area], plate16[area])  # the sign area is the plate, bit for bit
    assert np.array_equal(fixed[~lock], out16[~lock])  # everything else is the output, bit for bit
    assert not np.array_equal(out16[area], plate16[area])  # (the output really had rewritten it)
    assert np.array_equal(out16, before)  # inputs are not modified in place
    assert fixed is not out16 and fixed.dtype == np.uint16


def test_apply_with_an_empty_mask_changes_nothing():
    plate16, out16 = _pair16(1)
    fixed = textlock.apply(plate16, out16, np.zeros((H, W), bool))
    assert np.array_equal(fixed, out16)


def test_apply_rejects_mismatched_arrays():
    plate16, out16 = _pair16(2)
    mask = np.zeros((H, W), bool)
    with pytest.raises(ValueError, match="must agree"):
        textlock.apply(plate16, out16[:-1], mask)
    with pytest.raises(ValueError, match="must agree"):
        textlock.apply(plate16, out16.astype(np.float32), mask)
    with pytest.raises(ValueError, match="must agree"):
        textlock.apply(plate16, out16, mask[:-1])
