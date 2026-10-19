"""The engine result's format is recorded, not judged (router plan T152, decision 86).

`ingress.result_info` names the container, the decoded bit depth and channel count, a dropped alpha, a fully
transparent one and a lossy container; `decode_result_with_info` returns it beside the pixels and `restore` merges
it into its report. A float result with any non-finite value is refused before it is clipped: the attacker's float
TIFF with NaNs inside the mask was cast to uint16 unchecked and ended DONE (`result-format-unrecorded`).
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from plate_pixels import egress, ingress
from plate_pixels.ingress import IngressError

H, W = 36, 64
FREE = {"sizes": {"auto": True}}  # any size: the size map is the identity


def _img(dtype=np.uint8, channels: int = 3) -> np.ndarray:
    rng = np.random.default_rng(7)
    top = {np.uint8: 255, np.uint16: 65535}.get(dtype, 1.0)
    a = rng.random((H, W, channels)) * top
    return a.astype(dtype)


def _enc(ext: str, img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(ext, img)
    assert ok, ext
    return bytes(buf)


def _rgba_clear() -> np.ndarray:
    a = _img(np.uint8, 4)
    a[..., 3] = 0
    return a


@pytest.mark.parametrize(
    ("data", "fmt", "depth", "channels", "dropped", "clear", "lossy"),
    [
        (_enc(".png", _img()), "png", 8, 3, False, False, False),
        (_enc(".jpg", _img()), "jpeg", 8, 3, False, False, True),
        (_enc(".webp", _img()), "webp", 8, 3, False, False, False),
        (_enc(".bmp", _img()), "bmp", 8, 3, False, False, False),
        (_enc(".png", _img(np.uint16)), "png", 16, 3, False, False, False),
        (_enc(".png", _img(np.uint8, 4)), "png", 8, 4, True, False, False),
        (_enc(".png", _rgba_clear()), "png", 8, 4, True, True, False),
        (_enc(".png", _img()[..., 0]), "png", 8, 1, False, False, False),
        (_enc(".tiff", _img(np.float32)), "tiff", 32, 3, False, False, False),
    ],
    ids=["png8", "jpeg", "webp", "bmp", "png16", "rgba", "rgba-clear", "grey", "tiff32"],
)
def test_result_info_names_the_container_depth_channels_and_alpha(
    data, fmt, depth, channels, dropped, clear, lossy
):
    rgb, info = ingress.decode_result_with_info(data)
    assert info == {
        "raw_format": fmt,
        "raw_bit_depth": depth,
        "raw_channels": channels,
        "alpha_dropped": dropped,
        "alpha_transparent": clear,
        "lossy": lossy,
    }
    assert rgb.shape == (H, W, 3) and rgb.dtype == np.float32  # every result decodes to the same RGB float
    assert np.array_equal(ingress.decode_result(data), rgb)


def test_result_info_on_the_raw_array_before_the_channel_drop():
    decoded = cv2.imdecode(np.frombuffer(_enc(".png", _rgba_clear()), np.uint8), cv2.IMREAD_UNCHANGED)
    assert ingress.result_info(_enc(".png", _rgba_clear()), decoded)["alpha_transparent"] is True
    assert ingress.result_info(b"not an image", _img())["raw_format"] == "unknown"
    info = ingress.result_info(None, _img(np.float32, 4))  # an in-memory array
    assert (info["raw_format"], info["raw_bit_depth"], info["raw_channels"], info["alpha_dropped"]) == (
        "array",
        32,
        4,
        True,
    )


def test_an_opaque_alpha_is_dropped_but_not_transparent():
    a = _img(np.uint8, 4)
    a[..., 3] = 255
    a[0, 0, 3] = 0  # one clear pixel is not a fully transparent result
    info = ingress.decode_result_with_info(_enc(".png", a))[1]
    assert info["alpha_dropped"] is True and info["alpha_transparent"] is False


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf], ids=["nan", "inf", "-inf"])
def test_a_float_result_with_a_non_finite_pixel_is_refused_before_the_clip(bad):
    a = _img(np.float32)
    a[5, 7, 1] = bad
    a[20, 40, :] = bad  # one pixel, three bad values: counted once
    with pytest.raises(IngressError, match=r"engine result has 2 non-finite pixels of 2304"):
        ingress.decode_result(_enc(".tiff", a))
    with pytest.raises(IngressError, match="non-finite"):
        ingress.decode_result(a)  # an in-memory float array too


def test_a_finite_float_result_out_of_range_is_still_clipped():
    a = _img(np.float32) * 3.0 - 1.0
    rgb = ingress.decode_result(_enc(".tiff", a))
    assert rgb.min() >= 0.0 and rgb.max() <= 1.0


def test_restore_reports_the_result_format():
    plate = (_img(np.uint16) // 4 + 20000).astype(np.uint16)
    png, sm = egress.prepare(plate, None, FREE)
    sent = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
    jpeg = _enc(".jpg", sent)  # what was sent, as JPEG
    out, rep = ingress.restore(jpeg, sm, plate)
    assert out.shape == plate.shape
    assert {k: rep[k] for k in ("raw_format", "raw_bit_depth", "raw_channels", "lossy")} == {
        "raw_format": "jpeg",
        "raw_bit_depth": 8,
        "raw_channels": 3,
        "lossy": True,
    }
    assert rep["alpha_dropped"] is False and rep["alpha_transparent"] is False
