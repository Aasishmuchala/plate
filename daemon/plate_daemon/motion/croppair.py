"""CropPair generator: the start and end frames of a move, cut from ONE approved still (router plan T43, T94).

`make_pair(approved_still16, cx, cy, z0, z1, engine_size)` cuts the two crops with `plate_gates.croppair.cut_crops`
(amphi-plaza geometry: even sizes, centred on (cx, cy), clamped inside the frame, native pixels), brings both to the
SAME frame, encodes them as house 8-bit sRGB PNGs (`plate_pixels.egress.encode_png8`: ICC embedded, metadata
stripped) and returns

    {start_png, end_png, boxes, sizemap, push_ratio, direction, mode, ...}

Rules this module enforces (they are doctrine, not options):

- Source view only. Both frames are crops of the same still, so the engine only interpolates between two real
  crops; no camera is invented and `1.0 <= z0, z1 <= 3.0`. The move is recorded as `direction`: `in` (a push-in,
  z0 < z1, the default), `out` (a pull-back, z0 > z1: the caller asks for it with `allow_out`, so a swapped pair is
  never silently a reveal) or `locked` (z0 == z1 with `allow_equal`: a locked tripod, or a slider drift when an end
  centre (cx1, cy1) slides the end crop).
- The plate is never upscaled. The shared frame is the SMALLER crop's native size (the end crop of a push-in, the
  start crop of a pull-back), shrunk further only when `engine_size` is a smaller envelope; the larger crop is
  brought down to it with `cv2.INTER_AREA`. When the engine's frame is larger than the crop the frame is sent at
  native size and the engine's own output is bigger (`note: native_below_engine_size`). `engine_size` of 0 x 0 (the
  "auto" size Kling reports) means no envelope.
- "The SAME scale factor" of the plan reads as the same target frame for both crops: start and end land at one
  size, so the push the engine sees is exactly `z1 / z0` (`push_ratio`; `measured_push_ratio` is `w0 / w1` of the
  even-rounded boxes). The per-crop scale (`scale_start`, `scale_end`) is recorded next to it.
- No `ffmpeg zoompan`. The clip is delivered as generated and the boxes are recorded in `reconform` for the
  Resolve reconform (the crop's place in the full frame at every end of the move).

`sizemap` is `{engine_size, start, end}` with `start`/`end` the egress `SizeMap` dicts (region = the crop's box in
plate pixels, `pad_mode` overscan), so the same ingress maths that restores stills describes where each frame sits.
`store_pair` puts the two PNGs in the project CAS and `node_record` is what the Motion/CropPair node carries.

`make_two_stills(start16, end16, engine_size)` is the same record for two approved stills of one camera (a time of
day change, decision 49): whole-frame crops at z = 1 of each, the same envelope rule, `mode: two_stills`. The two
stills must be the same size.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import cv2
import numpy as np

from plate_gates.croppair import Z_MAX, Z_MIN, box_for, cut_crops
from plate_gates.croppair import direction as zoom_direction
from plate_pixels.egress import QUANTISERS, SizeMap, encode_png8
from plate_pixels.sizes import EngineSize, same_aspect

__all__ = [
    "CropPairError",
    "Z_MAX",
    "Z_MIN",
    "centre_px",
    "make_pair",
    "make_two_stills",
    "node_record",
    "store_pair",
]

Box = tuple[int, int, int, int]  # (x0, y0, w, h) in source pixels


class CropPairError(ValueError):
    """The pair cannot be cut as asked (bad still, bad engine size, or it would upscale the plate)."""


def centre_px(width: int, height: int, centre: Sequence[float]) -> tuple[float, float]:
    """A recipe centre to source pixels. Values inside [0, 1] are fractions of the frame (`[0.5, 0.5]` is the middle);
    anything larger is already pixels."""
    if len(centre) != 2:
        raise CropPairError(f"centre must be [x, y], got {list(centre)!r}")
    cx, cy = float(centre[0]), float(centre[1])
    if not (np.isfinite(cx) and np.isfinite(cy)) or cx < 0 or cy < 0:
        raise CropPairError(f"centre must be finite and non-negative, got {list(centre)!r}")
    return (cx * width if cx <= 1.0 else cx), (cy * height if cy <= 1.0 else cy)


def _engine_wh(engine_size: Any) -> tuple[int, int]:
    """(w, h) from (w, h), {width|w, height|h}, or an object with width/height or w/h. 0 means no limit."""
    if engine_size is None:
        return 0, 0
    if isinstance(engine_size, Mapping):
        w = engine_size.get("width", engine_size.get("w"))
        h = engine_size.get("height", engine_size.get("h"))
    elif isinstance(engine_size, Sequence) and not isinstance(engine_size, str | bytes):
        if len(engine_size) != 2:
            raise CropPairError(f"engine_size must be (w, h), got {engine_size!r}")
        w, h = engine_size
    else:
        w = getattr(engine_size, "width", getattr(engine_size, "w", None))
        h = getattr(engine_size, "height", getattr(engine_size, "h", None))
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        raise CropPairError(f"engine_size has no usable width and height: {engine_size!r}") from None
    if w < 0 or h < 0 or (w == 0) != (h == 0):
        raise CropPairError(f"engine_size must be positive on both sides (or 0 x 0 for none), got {w} x {h}")
    return w, h


def _target_size(small_box: Box, env: tuple[int, int]) -> tuple[int, int]:
    """The shared frame: the smaller crop's native size; when the engine envelope is smaller, the envelope itself if
    its aspect agrees with the crop's (the egress "exact" mode, within 0.5 %), else the crop shrunk uniformly to fit
    it."""
    _, _, w1, h1 = small_box
    ew, eh = env
    if ew == 0 or (ew >= w1 and eh >= h1):
        return w1, h1
    if ew <= w1 and eh <= h1 and same_aspect(w1 / h1, ew / eh):
        return ew, eh
    if (
        ew * h1 <= eh * w1
    ):  # width is the tighter limit; integer maths so 426 x (320 / 426) cannot land on 319
        tw, th = ew, h1 * ew // w1
    else:
        tw, th = w1 * eh // h1, eh
    return max(2, tw // 2 * 2), max(2, th // 2 * 2)


def _resample(crop: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    h, w = crop.shape[:2]
    tw, th = size
    if tw > w or th > h:  # unreachable by construction; the doctrine check stays explicit
        raise CropPairError(f"resampling a {w}x{h} crop to {tw}x{th} would upscale the plate")
    if (tw, th) == (w, h):
        return crop
    return cv2.resize(crop, (tw, th), interpolation=cv2.INTER_AREA)


def _still_array(still: Any, what: str) -> np.ndarray:
    arr = np.asarray(still)
    if arr.ndim != 3 or arr.shape[2] != 3 or arr.shape[0] < 2 or arr.shape[1] < 2:
        raise CropPairError(f"{what} must be H x W x 3 RGB, got shape {arr.shape}")
    if arr.dtype == np.float64:
        arr = arr.astype(np.float32)
    if arr.dtype not in (np.uint8, np.uint16, np.float32):
        raise CropPairError(f"{what} must be uint8, uint16 or float, got {arr.dtype}")
    return arr


def make_pair(
    approved_still16: np.ndarray,
    cx: float,
    cy: float,
    z0: float,
    z1: float,
    engine_size: Any = None,
    *,
    quantise: str = "de00",
    cx1: float | None = None,
    cy1: float | None = None,
    allow_equal: bool = False,
    allow_out: bool = False,
) -> dict[str, Any]:
    """The crop pair from an approved still (display-referred RGB, H x W x 3: uint16 from the 16-bit master, or
    uint8 / float in [0, 1]). `cx`, `cy` (and the end centre `cx1`, `cy1`, default the start's) are source pixels
    (see `centre_px` for recipe fractions). Raises ValueError for a zoom pair outside [1.0, 3.0], an equal pair
    without `allow_equal` or a pull-back (z0 > z1) without `allow_out`, and CropPairError for an unusable still or
    size."""
    arr = _still_array(approved_still16, "the approved still")
    if quantise not in QUANTISERS:
        raise CropPairError(f"quantise must be one of {QUANTISERS}, got {quantise!r}")
    if z0 > z1 and not allow_out:
        raise ValueError(
            f"zoom pair {z0} -> {z1} is a pull-back: a push-in goes in unless the move asks for out (allow_out)"
        )
    H, W = arr.shape[:2]
    start_crop, end_crop, boxes = cut_crops(arr, cx, cy, z0, z1, allow_equal=allow_equal, cx1=cx1, cy1=cy1)
    env = _engine_wh(engine_size)
    small = boxes[1] if boxes[1][2] <= boxes[0][2] else boxes[0]  # the tighter crop sets the frame
    tw, th = _target_size(small, env)
    start = _resample(np.asarray(start_crop), (tw, th))
    end = _resample(np.asarray(end_crop), (tw, th))
    start_png = encode_png8(start, quantise)
    end_png = encode_png8(end, quantise)

    native = (tw, th) == (small[2], small[3])
    note = "native_below_engine_size" if native and env != (0, 0) and (env[0] > tw or env[1] > th) else ""
    if env != (0, 0) and (tw, th) == env:
        mode = "exact"
    else:
        mode = "arbitrary"
    maps: dict[str, dict[str, Any]] = {}
    scales: dict[str, float] = {}
    for name, (x0, y0, w, h) in zip(("start", "end"), boxes, strict=True):
        scales[name] = tw / w
        maps[name] = SizeMap(
            src=(W, H),
            pad=(0, 0, 0, 0),
            scale=tw / w,
            dst=(tw, th),
            region=(x0, y0, x0 + w, y0 + h),
            pad_mode="overscan",
            mode=mode,
            engine_size=EngineSize(tw, th).to_dict(),
            synthetic=(0, 0, 0, 0),
            note=note,
            quantise=quantise,
        ).to_dict()

    zoom = {"z0": float(z0), "z1": float(z1), "cx": float(cx), "cy": float(cy)}
    if cx1 is not None or cy1 is not None:  # a slider drift: the end crop has its own centre
        zoom.update(cx1=float(cx if cx1 is None else cx1), cy1=float(cy if cy1 is None else cy1))
    return _record(
        start_png,
        end_png,
        boxes,
        maps,
        scales,
        size=(tw, th),
        source=(W, H),
        zoom=zoom,
        note=note,
        mode="croppair",
        direction=zoom_direction(z0, z1),
    )


def _record(
    start_png: bytes,
    end_png: bytes,
    boxes: Sequence[Box],
    maps: Mapping[str, dict[str, Any]],
    scales: Mapping[str, float],
    *,
    size: tuple[int, int],
    source: tuple[int, int],
    zoom: Mapping[str, float],
    note: str,
    mode: str,
    direction: str,
) -> dict[str, Any]:
    boxes_t: list[Box] = [tuple(int(v) for v in b) for b in boxes]  # type: ignore[misc]
    W, H = source
    return {
        "start_png": start_png,
        "end_png": end_png,
        "boxes": boxes_t,
        "sizemap": {"engine_size": EngineSize(*size).to_dict(), "start": maps["start"], "end": maps["end"]},
        "push_ratio": float(zoom["z1"]) / float(zoom["z0"]),
        "measured_push_ratio": boxes[0][2] / boxes[1][2],
        "scale_start": scales["start"],
        "scale_end": scales["end"],
        "size": size,
        "source_size": (W, H),
        "zoom": dict(zoom),
        "mode": mode,
        "direction": direction,
        "reconform": {
            "method": "none",
            "zoompan": False,
            "note": "the clip is delivered as generated; the boxes place both ends of the move in the full "
            "frame for the Resolve reconform",
            "source_size": [W, H],
            "boxes": [list(b) for b in boxes_t],
            "zoom": dict(zoom),
        },
        "note": note,
    }


def make_two_stills(
    start16: np.ndarray, end16: np.ndarray, engine_size: Any = None, *, quantise: str = "de00"
) -> dict[str, Any]:
    """The start/end record for two approved stills of one camera: each still's whole frame (the z = 1 crop, even
    sizes) brought to one frame by the same envelope rule as `make_pair` (never upscaled). Raises CropPairError when
    the stills differ in size (`end_size`) or one is unusable."""
    a = _still_array(start16, "the start still")
    b = _still_array(end16, "the end still")
    if a.shape[:2] != b.shape[:2]:
        raise CropPairError(
            f"the end still is {b.shape[1]} x {b.shape[0]}, the start {a.shape[1]} x {a.shape[0]}: two stills of "
            "one camera are the same size"
        )
    if quantise not in QUANTISERS:
        raise CropPairError(f"quantise must be one of {QUANTISERS}, got {quantise!r}")
    H, W = a.shape[:2]
    box = box_for(W, H, W / 2.0, H / 2.0, 1.0)
    env = _engine_wh(engine_size)
    tw, th = _target_size(box, env)
    x0, y0, w, h = box
    frames = [_resample(np.ascontiguousarray(img[y0 : y0 + h, x0 : x0 + w]), (tw, th)) for img in (a, b)]
    native = (tw, th) == (w, h)
    note = "native_below_engine_size" if native and env != (0, 0) and (env[0] > tw or env[1] > th) else ""
    mode = "exact" if env != (0, 0) and (tw, th) == env else "arbitrary"
    sm = SizeMap(
        src=(W, H),
        pad=(0, 0, 0, 0),
        scale=tw / w,
        dst=(tw, th),
        region=(x0, y0, x0 + w, y0 + h),
        pad_mode="overscan",
        mode=mode,
        engine_size=EngineSize(tw, th).to_dict(),
        synthetic=(0, 0, 0, 0),
        note=note,
        quantise=quantise,
    ).to_dict()
    zoom = {"z0": 1.0, "z1": 1.0, "cx": W / 2.0, "cy": H / 2.0}
    return _record(
        encode_png8(frames[0], quantise),
        encode_png8(frames[1], quantise),
        (box, box),
        {"start": sm, "end": dict(sm)},
        {"start": tw / w, "end": tw / w},
        size=(tw, th),
        source=(W, H),
        zoom=zoom,
        note=note,
        mode="two_stills",
        direction="locked",
    )


def store_pair(cas: Any, pair: Mapping[str, Any]) -> dict[str, str]:
    """Put both PNGs in the project CAS; returns {start, end} as BLAKE3 keys."""
    return {
        "start": cas.put_bytes(pair["start_png"], "png"),
        "end": cas.put_bytes(pair["end_png"], "png"),
    }


def node_record(pair: Mapping[str, Any], keys: Mapping[str, str]) -> dict[str, Any]:
    """What the CropPair node carries after generation: the CAS keys, the boxes, the size map and the reconform
    record. JSON-safe (tuples become lists)."""
    return {
        "start": keys["start"],
        "end": keys["end"],
        "boxes": [list(b) for b in pair["boxes"]],
        "push_ratio": pair["push_ratio"],
        "measured_push_ratio": pair["measured_push_ratio"],
        "mode": pair.get("mode", "croppair"),
        "direction": pair.get("direction", "in"),
        "size": list(pair["size"]),
        "source_size": list(pair["source_size"]),
        "zoom": dict(pair["zoom"]),
        "sizemap": pair["sizemap"],
        "reconform": pair["reconform"],
    }
