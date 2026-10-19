"""Social cutdowns: downsample-only crops of an approved master (BUILD_PLAN decision 56).

A cutdown is a crop of a finished take to a platform aspect (9:16, 4:5, 1:1, 16:9) inside the packet's safe
frame. It is never generated and never enlarged: `crop_box` picks the largest even-sized box of the aspect
that fits, `long_side` can only shrink it (INTER_AREA), and a request above the box raises
`CutdownError("cutdowns never upscale")`.

- `crop_box(width, height, preset, cx=, cy=, safe_frame=)` -> `(x0, y0, w, h)`. The box is centred on (cx, cy),
  given as fractions of the frame, then clamped inside the safe frame. Width, height and the offsets are all
  even, so the box lands on whole chroma samples of a 4:2:0 video. Sizes are floored, never rounded up.
- `still_cutdown(rgb16, box, out_path, fmt="png"|"jpeg", quality=92, long_side=None, lineage=)` writes an 8-bit
  sRGB PNG (iCCP) or JPEG (APP2 ICC profile). The 16-bit crop is resampled in float and quantised once.
- `clip_cutdown(mp4_in, box, out_path, codec="h264"|"hevc", long_side=None, lineage=)` runs ffmpeg
  `crop=w:h:x0:y0[,scale]` and encodes 8-bit 4:2:0 with the BT.709 tags `delivery.video` uses (matrix, range,
  primaries and transfer on the frames, the encoder and the codec VUI), probes the result for them, and drops
  the audio like every other delivery. A source tagged with another matrix is converted, not relabelled.
- Both embed the source run's lineage (decision 26, with the IPTC AI marker when the engine was generative) and,
  when a lineage is given, write `<name>.cutdown.json = {source_run, box, preset, format|codec,
  gate_report_ref}` beside the file. The file is staged under a hidden sibling name and moved into place, and
  an existing file is never overwritten (FileExistsError). The name is claimed, not checked (`export.claim`,
  `O_CREAT | O_EXCL`): of any number of cutdowns racing to one name exactly one is written. The staged file sits
  in the client's folder under `.<name>.<tag><suffix>` while an in-flight marker `.<name>.<tag>.lock` is held
  (router plan T153, decision 87: the shape `export.sweep_stale` reads), and the cutdown sweeps the leftovers of
  its own name first, so a kill -9 mid-encode is cleaned up by the next cutdown or export of that name.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import lineage as lineage_mod
from .delivery.guard import DeliveryError
from .delivery.tools import probe_video, run_ffmpeg
from .delivery.video import BT709
from .export import claim, sweep_stale
from .fsutil import in_flight, replace_with_retry, write_json_atomic
from .lineage import Lineage

__all__ = [
    "CLIP_CODECS",
    "PRESETS",
    "STILL_FORMATS",
    "Box",
    "CutdownError",
    "clip_cutdown",
    "crop_box",
    "sidecar_path",
    "still_cutdown",
    "write_sidecar",
]

Box = tuple[int, int, int, int]

PRESETS: dict[str, float] = {"9:16": 9 / 16, "4:5": 4 / 5, "1:1": 1.0, "16:9": 16 / 9}
STILL_FORMATS: dict[str, tuple[str, ...]] = {"png": (".png",), "jpeg": (".jpg", ".jpeg")}
CLIP_CODECS = ("h264", "hevc")
SIDECAR_SUFFIX = ".cutdown.json"
SIDECAR_SCHEMA = 1
# ffprobe colour_space -> the matrix ffmpeg's scale filter decodes with (an untagged clip is BT.709).
_DECODE_MATRICES = {
    "bt709": "bt709",
    "smpte170m": "smpte170m",
    "bt470bg": "bt470",
    "fcc": "fcc",
    "smpte240m": "smpte240m",
    "bt2020nc": "bt2020",
    "bt2020c": "bt2020",
}
_APP2_ICC_HEAD = b"ICC_PROFILE\x00\x01\x01"


class CutdownError(ValueError):
    """A cutdown that cannot be made: unknown preset, a box outside the frame, an upscale, a bad file."""


# ---------------------------------------------------------------------------- the crop box


def _even_floor(value: float) -> int:
    return int(math.floor(value)) & ~1


def _region(width: int, height: int, safe_frame: Any) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) of the safe frame, snapped inward to even coordinates, inside the frame."""
    if safe_frame is None:
        x, y, w, h = 0, 0, width, height
    elif isinstance(safe_frame, Mapping):
        try:
            x, y, w, h = (int(safe_frame[k]) for k in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError) as e:
            raise CutdownError("safe_frame needs x, y, width and height") from e
    else:
        try:
            x, y, w, h = (int(v) for v in safe_frame)
        except (TypeError, ValueError) as e:
            raise CutdownError("safe_frame is (x, y, width, height)") from e
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
        raise CutdownError(f"safe_frame {(x, y, w, h)} does not fit the {width} x {height} frame")
    x0, y0 = x + (x & 1), y + (y & 1)
    x1, y1 = (x + w) & ~1, (y + h) & ~1
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise CutdownError("safe_frame is too small to crop")
    return x0, y0, x1, y1


def crop_box(
    width: int,
    height: int,
    preset: str,
    *,
    cx: float = 0.5,
    cy: float = 0.5,
    safe_frame: Any = None,
) -> Box:
    """The largest even-sized box of `preset`'s aspect inside `safe_frame` (default the frame; a mapping
    `{x, y, width, height}` or an `(x, y, width, height)` tuple), centred on (`cx`, `cy`) in frame fractions
    and clamped to the safe frame. Returns `(x0, y0, w, h)`; every number is even."""
    if preset not in PRESETS:
        raise CutdownError(f"preset {preset!r}: use one of {', '.join(PRESETS)}")
    if not (isinstance(width, int) and isinstance(height, int)) or width < 2 or height < 2:
        raise CutdownError(f"frame {width} x {height}: sizes are whole numbers of at least 2")
    for name, v in (("cx", cx), ("cy", cy)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:
            raise CutdownError(f"{name} is a fraction of the frame from 0 to 1, got {v!r}")
    rx0, ry0, rx1, ry1 = _region(width, height, safe_frame)
    rw, rh = rx1 - rx0, ry1 - ry0
    num, den = (int(v) for v in preset.split(":"))  # whole-number arithmetic: no float floor surprises
    if rw * den >= rh * num:  # the region is wider than the aspect: the height limits the box
        h, w = rh, (rh * num) // den
    else:
        w, h = rw, (rw * den) // num
    w, h = max(2, _even_floor(w)), max(2, _even_floor(h))
    x0 = _even_floor(min(max(cx * width - w / 2.0, rx0), rx1 - w))
    y0 = _even_floor(min(max(cy * height - h / 2.0, ry0), ry1 - h))
    return max(x0, rx0), max(y0, ry0), w, h


def _check_box(box: Sequence[int], width: int, height: int) -> Box:
    try:
        x0, y0, w, h = (int(v) for v in box)
    except (TypeError, ValueError) as e:
        raise CutdownError("box is (x0, y0, w, h)") from e
    if w < 2 or h < 2 or x0 < 0 or y0 < 0 or x0 + w > width or y0 + h > height:
        raise CutdownError(f"box {(x0, y0, w, h)} is not inside the {width} x {height} frame")
    return x0, y0, w, h


def _target_size(w: int, h: int, long_side: int | None, *, even: bool) -> tuple[int, int]:
    """(W, H) of the output: the box itself, or shrunk so the long side is `long_side` (rounded down to even
    when `even`). Never larger than the box."""
    if long_side is None:
        return w, h
    if isinstance(long_side, bool) or not isinstance(long_side, int) or long_side < 2:
        raise CutdownError(f"long_side is a whole number of pixels from 2, got {long_side!r}")
    long = max(w, h)
    if long_side > long:
        raise CutdownError("cutdowns never upscale")
    target = (long_side & ~1) if even else long_side
    if target == long:
        return w, h
    scale = target / long

    def fit(v: int) -> int:
        return max(2, 2 * round(v * scale / 2)) if even else max(1, round(v * scale))

    tw, th = fit(w), fit(h)
    return (target, th) if w >= h else (tw, target)


# ---------------------------------------------------------------------------- sidecar and staging


def sidecar_path(out: str | Path) -> Path:
    """`shot_9x16.png` -> `shot_9x16.cutdown.json`."""
    out = Path(out)
    return out.with_name(out.stem + SIDECAR_SUFFIX)


def write_sidecar(
    out: str | Path,
    *,
    source_run: str,
    box: Sequence[int],
    preset: str | None,
    gate_report_ref: str | None,
    fmt: str | None = None,
    codec: str | None = None,
) -> Path:
    """`<name>.cutdown.json = {source_run, box, preset, format | codec, gate_report_ref}`."""
    body: dict[str, Any] = {
        "source_run": source_run,
        "box": [int(v) for v in box],
        "preset": preset,
    }
    if codec is not None:
        body["codec"] = codec
    else:
        body["format"] = fmt
    body["gate_report_ref"] = gate_report_ref
    path = sidecar_path(out)
    write_json_atomic(path, body, indent=1)
    return path


def _report_ref(lineage: Lineage) -> str | None:
    ref = lineage.gate_report
    return ref if isinstance(ref, str) else None


def _place(staged: Path, final: Path, lineage: Lineage | None) -> None:
    """Embed the lineage, claim `final` (`O_CREAT | O_EXCL`: of any number of cutdowns racing to one name
    exactly one wins, the others raise FileExistsError) and move the staged file over the empty claim; a
    failure after the claim gives the name back."""
    if lineage is not None:
        lineage_mod.embed(staged, lineage)
    if not claim(final):
        raise FileExistsError(f"{final} already exists; cutdowns never overwrite")
    try:
        replace_with_retry(staged, final)
    except BaseException:
        final.unlink(missing_ok=True)
        raise


@contextmanager
def _staged(out: Path) -> Iterator[Path]:
    """The hidden path `.<name>.<tag><suffix>` next to `out` to write the cutdown to, with the in-flight marker
    held for the block (and the staged file removed on the way out): a process that dies in it leaves a pair
    `export.sweep_stale` recognises. The leftovers of this name and its sidecar are swept first."""
    tag = uuid.uuid4().hex[:12]
    staged = out.with_name(f".{out.name}.{tag}{out.suffix}")
    with in_flight(out.with_name(f".{out.name}.{tag}.lock")):
        sweep_stale(out.parent, (out.name, sidecar_path(out).name))
        try:
            yield staged
        finally:
            staged.unlink(missing_ok=True)


def _check_destination(out: Path, suffixes: Sequence[str], what: str) -> None:
    if out.suffix.lower() not in suffixes:
        raise CutdownError(f"{out.name}: a {what} is written as {' or '.join(suffixes)}")
    if out.exists():
        raise FileExistsError(f"{out} already exists; cutdowns never overwrite")
    out.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------- stills


def _icc_segment(profile: bytes) -> bytes:
    """A JPEG APP2 segment carrying the profile (one chunk: an sRGB matrix profile is a few KB)."""
    length = 2 + len(_APP2_ICC_HEAD) + len(profile)
    if length > 0xFFFF:
        raise CutdownError("ICC profile too large for one JPEG segment")
    return b"\xff\xe2" + length.to_bytes(2, "big") + _APP2_ICC_HEAD + profile


def _jpeg_bytes(rgb8: np.ndarray, quality: int) -> bytes:
    from plate_pixels.icc import SRGB_ICC

    ok, buf = cv2.imencode(
        ".jpg", np.ascontiguousarray(rgb8[..., ::-1]), [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    )
    if not ok:
        raise CutdownError("cv2 could not encode the JPEG")
    data = bytes(buf)
    pos = 2  # after SOI: behind a JFIF APP0, where an ICC profile belongs
    if data[2:4] == b"\xff\xe0":
        pos = 4 + int.from_bytes(data[4:6], "big")
    return data[:pos] + _icc_segment(SRGB_ICC) + data[pos:]


def still_cutdown(
    rgb16: np.ndarray,
    box: Sequence[int],
    out_path: str | Path,
    fmt: str = "png",
    quality: int = 92,
    long_side: int | None = None,
    *,
    lineage: Lineage | None = None,
    preset: str | None = None,
) -> Path:
    """Crop `box` out of the uint16 (or uint8) RGB image and write it as an 8-bit sRGB PNG or JPEG, never
    larger than the box (`long_side` shrinks it with INTER_AREA). With `lineage` the record is embedded in the
    file and `<name>.cutdown.json` is written beside it. Returns the path."""
    if fmt not in STILL_FORMATS:
        raise CutdownError(f"format {fmt!r}: use one of {', '.join(STILL_FORMATS)}")
    if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
        raise CutdownError(f"quality is a whole number from 1 to 100, got {quality!r}")
    img = np.asarray(rgb16)
    if img.ndim != 3 or img.shape[2] < 3 or img.dtype not in (np.uint16, np.uint8):
        raise CutdownError("rgb16 is an (H, W, 3) uint16 (or uint8) RGB image")
    height, width = img.shape[:2]
    x0, y0, w, h = _check_box(box, width, height)
    out = Path(out_path)
    _check_destination(out, STILL_FORMATS[fmt], fmt.upper())
    tw, th = _target_size(w, h, long_side, even=False)
    peak = 65535.0 if img.dtype == np.uint16 else 255.0
    crop = np.ascontiguousarray(img[y0 : y0 + h, x0 : x0 + w, :3]).astype(np.float32) / peak
    if (tw, th) != (w, h):
        crop = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_AREA)
    rgb8 = np.rint(np.clip(crop, 0.0, 1.0) * 255.0).astype(np.uint8)  # quantised once
    with _staged(out) as staged:
        if fmt == "png":
            from plate_pixels import imageio

            imageio.write_png8(staged, rgb8)
        else:
            staged.write_bytes(_jpeg_bytes(rgb8, quality))
        _place(staged, out, lineage)
    if lineage is not None:
        write_sidecar(
            out,
            source_run=lineage.run_id,
            box=(x0, y0, w, h),
            preset=preset,
            gate_report_ref=_report_ref(lineage),
            fmt=fmt,
        )
    return out


# ---------------------------------------------------------------------------- clips


def _codec_args(codec: str) -> list[str]:
    vui = f"colorprim={BT709.primaries}:transfer={BT709.transfer}:colormatrix={BT709.matrix}"
    if codec == "h264":
        return [
            "-c:v",
            "libx264",
            "-crf",
            "17",
            "-preset",
            "medium",
            "-profile:v",
            "high",
            "-x264-params",
            f"{vui}:fullrange=off",
        ]
    return [
        "-c:v",
        "libx265",
        "-crf",
        "18",
        "-preset",
        "medium",
        "-tag:v",
        "hvc1",
        "-x265-params",
        f"{vui}:range=limited:log-level=error",
    ]


def clip_cutdown(
    mp4_in: str | Path,
    box: Sequence[int],
    out_path: str | Path,
    codec: str = "h264",
    long_side: int | None = None,
    *,
    lineage: Lineage | None = None,
    preset: str | None = None,
) -> Path:
    """Crop `box` out of a clip with ffmpeg and encode 8-bit 4:2:0 H.264 or HEVC in an .mp4 with the BT.709
    tags (see the module docstring). `long_side` shrinks, never enlarges. With `lineage` the record goes into
    the container and `<name>.cutdown.json` beside it. Returns the path."""
    if codec not in CLIP_CODECS:
        raise CutdownError(f"codec {codec!r}: use one of {', '.join(CLIP_CODECS)}")
    src = Path(mp4_in)
    stream = probe_video(src)
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    x0, y0, w, h = _check_box(box, width, height)
    out = Path(out_path)
    _check_destination(out, (".mp4",), "clip cutdown")
    if w % 2 or h % 2 or x0 % 2 or y0 % 2:
        raise CutdownError(f"box {(x0, y0, w, h)}: a 4:2:0 clip is cropped on even coordinates")
    tw, th = _target_size(w, h, long_side, even=True)
    matrix = _DECODE_MATRICES.get(str(stream.get("color_space") or ""), "bt709")
    rng = "pc" if stream.get("color_range") == "pc" else "tv"
    scale = f"scale={tw}:{th}:" if (tw, th) != (w, h) else "scale="
    vf = (
        f"crop={w}:{h}:{x0}:{y0},"
        f"{scale}in_color_matrix={matrix}:in_range={rng}:out_color_matrix={BT709.matrix}"
        f":out_range={BT709.range}:flags=area+accurate_rnd+full_chroma_int,"
        f"format=yuv420p,{BT709.setparams()}"
    )
    placed = False
    with _staged(out) as staged:
        try:
            run_ffmpeg(
                [
                    "-i",
                    str(src),
                    "-map",
                    "0:v:0",
                    "-vf",
                    vf,
                    *_codec_args(codec),
                    "-pix_fmt",
                    "yuv420p",
                    *BT709.ffmpeg_args(),
                    "-an",
                    "-movflags",
                    "+faststart",
                    "-f",
                    "mp4",
                    str(staged),
                ]
            )
            _place(staged, out, lineage)
            placed = True
            got = probe_video(out)
            wrong = {k: (got.get(k), v) for k, v in BT709.expected().items() if got.get(k) != v}
            if wrong:
                raise DeliveryError(f"{out.name}: colour tags did not land (got, wanted): {wrong}")
            if (int(got.get("width") or 0), int(got.get("height") or 0)) != (tw, th):
                raise DeliveryError(
                    f"{out.name}: size is {got.get('width')} x {got.get('height')}, wanted {tw} x {th}"
                )
        except BaseException:
            if placed:  # a cutdown that failed its own check is not left behind
                out.unlink(missing_ok=True)
            raise
    if lineage is not None:
        write_sidecar(
            out,
            source_run=lineage.run_id,
            box=(x0, y0, w, h),
            preset=preset,
            gate_report_ref=_report_ref(lineage),
            codec=codec,
        )
    return out
