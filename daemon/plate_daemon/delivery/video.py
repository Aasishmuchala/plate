"""Delivery encodes: ProRes 4444, HEVC with colour tags, linear EXR sequences, and the Vantage AV1 re-tag.

Every function is keyword-gated by a `guard.Provenance`: `authorize` runs first (previs and unresolved
GATE_FAIL are refused before a byte is produced), the output is staged under a hidden sibling name, gets its
lineage embedded, is moved into place, is re-probed for the colour tags it was supposed to land with, and
gets its `<file>.gate.json` sidecar. An existing destination is never overwritten.

Colour. Display frames are sRGB-encoded RGB and are converted to YUV with an explicit BT.709 matrix, limited
range (`scale=out_color_matrix=bt709:out_range=tv`). swscale's default for RGB input is BT.601, so leaving the
matrix out would tag the file BT.709 and encode it BT.601. The tags (`color_primaries`, `color_trc`,
`colorspace`, `color_range`) are set on the frames (`setparams`), on the encoder, and for HEVC inside the
bitstream (`-x265-params colorprim/transfer/colormatrix/range`), so they survive a player that trusts any one
of the three. Frames reach ffmpeg as raw `rgb48le` / `rgba64le` on stdin, so 16-bit sources lose nothing and
Windows needs no temp copies.

EXR sequences are written with OpenImageIO, not ffmpeg: scene-linear (the sRGB EOTF applied to display
frames), half float, ZIP, with the Rec.709 `chromaticities` header attribute. ffmpeg's gbrpf32le upconversion
neither linearises nor writes chromaticities.

The AV1 re-tag is a stream copy: the pixels are the same bytes. It rewrites the sequence-header colour fields
with the `av1_metadata` bitstream filter AND the container tags (the MP4 `colr` box), because a Vantage 3.3
file carries its wrong GBR / full-range / sRGB claim in the box, and a bitstream-filter-only copy leaves the
box untouched (measured on this ffmpeg: the box still reads matrix 0, range pc after the bsf alone).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from .. import lineage as lineage_mod
from ..fsutil import replace_with_retry
from .frames import Frames, alpha_is_opaque, is_video_file, open_frames, to_display16
from .guard import (
    DeliveryError,
    Provenance,
    ToolMissing,
    authorize,
    embed_and_place,
    sequence_stem,
    stage_path,
    write_sidecar,
)
from .tools import DEFAULT_TIMEOUT_S, probe_video, run_ffmpeg, to_fraction

__all__ = [
    "BT709",
    "COLOR_TAGS",
    "RETAG_PROFILES",
    "REC709_CHROMATICITIES",
    "ColorTags",
    "Delivered",
    "av1_retag",
    "exr_sequence",
    "hevc",
    "prores4444",
]


@dataclass(frozen=True)
class ColorTags:
    """ffmpeg names for the four colour tags. `*_code` are the H.273 numbers the AV1 filter takes."""

    primaries: str
    transfer: str
    matrix: str
    range: str  # 'tv' (limited) or 'pc' (full)
    primaries_code: int = 1
    transfer_code: int = 1
    matrix_code: int = 1

    def ffmpeg_args(self) -> list[str]:
        return [
            "-color_primaries",
            self.primaries,
            "-color_trc",
            self.transfer,
            "-colorspace",
            self.matrix,
            "-color_range",
            self.range,
        ]

    def setparams(self) -> str:
        return (
            f"setparams=color_primaries={self.primaries}:color_trc={self.transfer}"
            f":colorspace={self.matrix}:range={self.range}"
        )

    def expected(self) -> dict[str, str]:
        return {
            "color_primaries": self.primaries,
            "color_transfer": self.transfer,
            "color_space": self.matrix,
            "color_range": self.range,
        }


BT709 = ColorTags("bt709", "bt709", "bt709", "tv")
COLOR_TAGS: dict[str, ColorTags] = {"bt709": BT709}

# 'bt709': the plan's tags. 'vantage33': what a Vantage 3.3 AV1 export measured as (2026-10-12, Brochure
# Satish 02): full-range pixels, JFIF-style BT.601 matrix, sRGB transfer, BT.709 primaries.
RETAG_PROFILES: dict[str, ColorTags] = {
    "bt709": BT709,
    "vantage33": ColorTags("bt709", "iec61966-2-1", "smpte170m", "pc", 1, 13, 6),
}

REC709_CHROMATICITIES = (0.64, 0.33, 0.30, 0.60, 0.15, 0.06, 0.3127, 0.3290)
_HALF_MAX = 65504.0
_PRORES_PIX_FMTS = {"yuva444p10le", "yuva444p12le"}


@dataclass(frozen=True)
class Delivered:
    """`path` is the file (the first frame of a sequence); `files` lists every file written."""

    path: Path
    sidecar: Path
    format: str
    files: tuple[Path, ...]
    override: dict[str, str] | None = None


# ---------------------------------------------------------------------------- shared steps


def _resolve_tags(tags: ColorTags | str) -> ColorTags:
    if isinstance(tags, ColorTags):
        return tags
    try:
        return COLOR_TAGS[tags]
    except KeyError:
        raise DeliveryError(f"unknown colour tags {tags!r}; known: {sorted(COLOR_TAGS)}") from None


def _check_destination(out: Path, suffixes: tuple[str, ...]) -> None:
    if out.suffix.lower() not in suffixes:
        raise DeliveryError(f"{out.name}: this format is written as {' or '.join(suffixes)}")
    if out.exists():
        raise FileExistsError(f"{out} already exists; delivery never overwrites")
    out.parent.mkdir(parents=True, exist_ok=True)


def _probe_or_none(path: Path) -> dict[str, Any] | None:
    try:
        return probe_video(path)
    except ToolMissing:
        return None


def _verify_tags(path: Path, tags: ColorTags, *, codec: str, pix_fmts: set[str] | None) -> bool:
    """Probe the finished file; raise when the tags or the codec did not land. False when ffprobe is absent."""
    stream = _probe_or_none(path)
    if stream is None:
        return False
    wrong = {k: (stream.get(k), v) for k, v in tags.expected().items() if stream.get(k) != v}
    if wrong:
        raise DeliveryError(f"{path.name}: colour tags did not land (got, wanted): {wrong}")
    if stream.get("codec_name") != codec:
        raise DeliveryError(f"{path.name}: codec is {stream.get('codec_name')}, wanted {codec}")
    if pix_fmts is not None and stream.get("pix_fmt") not in pix_fmts:
        raise DeliveryError(
            f"{path.name}: pixel format is {stream.get('pix_fmt')}, wanted one of {sorted(pix_fmts)}"
        )
    return True


def _rate(fps: Any, probed: Fraction | None) -> Fraction:
    if fps is None:
        if probed is None or probed <= 0:
            raise DeliveryError("fps is required for image frames")
        return probed
    want = to_fraction(fps)
    if probed is not None and probed > 0 and abs(float(want - probed)) > 0.001:
        raise DeliveryError(
            f"the source runs at {float(probed):g} fps and {float(want):g} was asked; delivery never retimes"
        )
    return want


def _raw_chunks(frames: Frames, *, with_alpha: bool, flatten_alpha: bool) -> Iterator[bytes]:
    for frame in frames:
        if flatten_alpha and not alpha_is_opaque(frame):
            raise DeliveryError(
                "this format has no alpha channel and a frame is not opaque; flatten it first"
            )
        yield to_display16(frame, with_alpha=with_alpha)


def _scale_filter(tags: ColorTags, pix_fmt: str, in_opts: str = "") -> str:
    return (
        f"scale={in_opts}out_color_matrix={tags.matrix}:out_range={tags.range}"
        f":flags=accurate_rnd+full_chroma_int,format={pix_fmt},{tags.setparams()}"
    )


def _encode(
    source: Any,
    out: Path,
    fps: Any,
    prov: Provenance,
    *,
    fmt: str,
    suffixes: tuple[str, ...],
    container: list[str],
    codec_args: list[str],
    pix_fmt: str,
    tags: ColorTags,
    codec: str,
    verify_pix_fmts: set[str] | None,
    keep_alpha: bool,
    start_number: int | None,
    timeout_s: float,
) -> Delivered:
    override = authorize(prov)
    out = Path(out)
    _check_destination(out, suffixes)
    extra: dict[str, Any] = {}
    frames: Frames | None = None
    if is_video_file(source):
        src = Path(source)
        probed = _probe_or_none(src)
        rate = _rate(fps, probed["fps"] if probed else None)
        in_opts = ""
        if probed and probed.get("color_space") in (None, "unknown", "unspecified"):
            # an untagged file would be read with swscale's BT.601 default; every PLATE render is BT.709
            in_opts = "in_color_matrix=bt709:"
            extra["assumed_input_matrix"] = "bt709"
        input_args = ["-i", str(src)]
        feed = None
    else:
        frames = open_frames(source, start_number=start_number)
        rate = _rate(fps, None)
        with_alpha = frames.has_alpha and keep_alpha
        input_args = [
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba64le" if with_alpha else "rgb48le",
            "-video_size",
            f"{frames.width}x{frames.height}",
            "-framerate",
            f"{rate.numerator}/{rate.denominator}",
            "-i",
            "pipe:0",
        ]
        in_opts = ""
        feed = _raw_chunks(frames, with_alpha=with_alpha, flatten_alpha=not keep_alpha)

    cmd = [
        *input_args,
        "-vf",
        _scale_filter(tags, pix_fmt, in_opts),
        "-r",
        f"{rate.numerator}/{rate.denominator}",
        *codec_args,
        "-pix_fmt",
        pix_fmt,
        *tags.ffmpeg_args(),
        "-an",
        *container,
    ]
    staged = stage_path(out)
    try:
        run_ffmpeg([*cmd, str(staged)], frames=feed, timeout_s=timeout_s)
        embed_and_place(staged, out, prov.lineage)
    finally:
        staged.unlink(missing_ok=True)
    try:
        verified = _verify_tags(out, tags, codec=codec, pix_fmts=verify_pix_fmts)
        side = write_sidecar(
            out,
            prov,
            fmt=fmt,
            files=[out],
            override=override,
            color={**tags.expected(), "matrix_applied": tags.matrix, "verified": verified},
            extra={"fps": f"{rate.numerator}/{rate.denominator}", **extra},
        )
    except Exception:
        out.unlink(missing_ok=True)
        raise
    return Delivered(path=out, sidecar=side, format=fmt, files=(out,), override=override)


# ---------------------------------------------------------------------------- ProRes 4444


def prores4444(
    frames_or_mp4: Any,
    out: str | Path,
    fps: Any = None,
    *,
    provenance: Provenance,
    start_number: int | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Delivered:
    """ProRes 4444 in a .mov, `yuva444p10le`, BT.709 limited range, alpha kept when the frames have it.
    `frames_or_mp4`: a video file, or anything `frames.open_frames` takes. `fps` is required for image
    frames; for a video file it is optional and must match the file (no silent retiming)."""
    return _encode(
        frames_or_mp4,
        Path(out),
        fps,
        provenance,
        fmt="prores4444",
        suffixes=(".mov",),
        container=["-f", "mov"],
        codec_args=["-c:v", "prores_ks", "-profile:v", "4"],
        pix_fmt="yuva444p10le",
        tags=BT709,
        codec="prores",
        verify_pix_fmts=_PRORES_PIX_FMTS,  # ffmpeg's decoder reports a 4444 stream as 12-bit
        keep_alpha=True,
        start_number=start_number,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------- HEVC


def hevc(
    frames_or_mp4: Any,
    out: str | Path,
    fps: Any = None,
    crf: int = 16,
    tags: ColorTags | str = "bt709",
    *,
    provenance: Provenance,
    preset: str = "medium",
    start_number: int | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Delivered:
    """HEVC Main 10 (`yuv420p10le`, `hvc1` tag) in an .mp4 with `+faststart`; the colour tags go on the
    frames, the encoder and the x265 VUI. HEVC carries no alpha: frames that are not opaque are refused."""
    t = _resolve_tags(tags)
    x265 = (
        f"colorprim={t.primaries}:transfer={t.transfer}:colormatrix={t.matrix}"
        f":range={'limited' if t.range == 'tv' else 'full'}:log-level=error"
    )
    return _encode(
        frames_or_mp4,
        Path(out),
        fps,
        provenance,
        fmt="hevc",
        suffixes=(".mp4", ".m4v"),
        container=["-movflags", "+faststart", "-f", "mp4"],
        codec_args=[
            "-c:v",
            "libx265",
            "-crf",
            str(int(crf)),
            "-preset",
            preset,
            "-tag:v",
            "hvc1",
            "-x265-params",
            x265,
        ],
        pix_fmt="yuv420p10le",
        tags=t,
        codec="hevc",
        verify_pix_fmts={"yuv420p10le"},
        keep_alpha=False,
        start_number=start_number,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------- EXR sequence


def _write_exr(path: Path, linear: np.ndarray) -> None:
    import OpenImageIO as oiio

    h, w = linear.shape[:2]
    spec = oiio.ImageSpec(w, h, 3, "half")
    spec.attribute("compression", "zip")
    spec.attribute("chromaticities", oiio.TypeDesc("float[8]"), REC709_CHROMATICITIES)
    out = oiio.ImageOutput.create(str(path))
    if out is None:
        raise DeliveryError(f"OpenImageIO cannot write {path.name}: {oiio.geterror()}")
    try:
        if not out.open(str(path), spec):
            raise DeliveryError(f"cannot open {path.name} for writing: {out.geterror()}")
        if not out.write_image(np.ascontiguousarray(linear, dtype=np.float32)):
            raise DeliveryError(f"cannot write {path.name}: {out.geterror()}")
    finally:
        out.close()


def exr_sequence(
    rgb16_frames: Any,
    out_pattern: str | Path,
    *,
    provenance: Provenance,
    encoding: str = "srgb",
    first_frame: int = 1,
    start_number: int | None = None,
) -> Delivered:
    """Linear Rec.709 half-float EXR frames, ZIP, with the `chromaticities` attribute.

    `rgb16_frames`: whatever `frames.open_frames` takes (16-bit PNG/TIFF paths, a pattern, a folder, arrays);
    display frames are sRGB-decoded, float TIFF / EXR / `encoding='linear'` arrays are taken as scene-linear.
    `out_pattern`: `shot.%04d.exr` for a sequence (numbered from `first_frame`), or a plain `.exr` path for a
    single still. Frames are staged, tagged with lineage, then all moved into place together or not at all.
    One sidecar covers the sequence."""
    override = authorize(provenance)
    pattern = Path(out_pattern)
    if pattern.suffix.lower() != ".exr":
        raise DeliveryError(f"{pattern.name}: EXR frames are written as .exr")
    numbered = sequence_stem(pattern.name) is not None
    frames = open_frames(rgb16_frames, start_number=start_number, encoding=encoding)
    if not numbered and frames.count != 1:
        raise DeliveryError(f"{pattern.name} has no frame field (%04d) but there is more than one frame")
    pattern.parent.mkdir(parents=True, exist_ok=True)

    staged: list[tuple[Path, Path]] = []
    try:
        for i, frame in enumerate(frames):
            if not alpha_is_opaque(frame):
                raise DeliveryError("EXR delivery takes RGB; a frame has non-opaque alpha")
            name = pattern.name % (first_frame + i) if numbered else pattern.name
            final = pattern.with_name(name)
            linear = frame.rgb
            if frame.encoding == "srgb":
                from plate_pixels.ocio import srgb_eotf

                linear = srgb_eotf(linear).astype(np.float32)
            linear = np.clip(np.nan_to_num(linear, nan=0.0), -_HALF_MAX, _HALF_MAX)
            tmp = stage_path(final)
            staged.append((tmp, final))
            _write_exr(tmp, linear)
            lineage_mod.embed(tmp, provenance.lineage)
        clash = [f.name for _, f in staged if f.exists()]
        if clash:
            raise FileExistsError(f"{clash[0]} already exists; delivery never overwrites")
        done: list[Path] = []
        try:
            for tmp, final in staged:
                replace_with_retry(tmp, final)
                done.append(final)
        except Exception:
            for f in done:
                f.unlink(missing_ok=True)
            raise
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
    files = tuple(f for _, f in staged)
    try:
        side = write_sidecar(
            pattern,
            provenance,
            fmt="exr_sequence",
            files=files,
            override=override,
            color={
                "encoding": "linear",
                "chromaticities": list(REC709_CHROMATICITIES),
                "pixel_type": "half",
                "compression": "zip",
                "source_encoding": "srgb" if encoding == "srgb" else "linear",
            },
            extra={"frames": len(files), "first_frame": first_frame if numbered else None},
        )
    except Exception:
        for f in files:
            f.unlink(missing_ok=True)
        raise
    return Delivered(path=files[0], sidecar=side, format="exr_sequence", files=files, override=override)


# ---------------------------------------------------------------------------- AV1 Vantage re-tag


def _resolve_profile(profile: str, stream: dict[str, Any]) -> tuple[str, ColorTags]:
    if profile == "auto":
        # A 4:2:0 stream that claims an identity (GBR) matrix is invalid by construction: that is the
        # Vantage 3.3 export. Anything else gets the plan's BT.709 limited-range tags.
        broken = stream.get("color_space") in ("gbr", "rgb") and str(stream.get("pix_fmt", "")).startswith(
            "yuv"
        )
        profile = "vantage33" if broken else "bt709"
    try:
        return profile, RETAG_PROFILES[profile]
    except KeyError:
        raise DeliveryError(
            f"unknown re-tag profile {profile!r}; known: auto, {sorted(RETAG_PROFILES)}"
        ) from None


def av1_retag(
    source: str | Path,
    out: str | Path,
    *,
    provenance: Provenance,
    profile: str = "auto",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Delivered:
    """Lossless AV1 colour re-tag (the Vantage 3.3 purple-tint fix): `-c copy`, sequence-header colour fields
    rewritten by `av1_metadata`, container tags rewritten to match. Profiles: 'bt709' (BT.709 limited, the
    plan's tags), 'vantage33' (BT.709 primaries, sRGB transfer, BT.601 matrix, full range: the measured
    Vantage 3.3 signature), 'auto' (vantage33 when the input claims an identity matrix on a YUV stream, else
    bt709). No gate report is needed (no pixel changes), but previs and GATE_FAIL are still refused."""
    override = authorize(provenance, require_report=False)
    src, dest = Path(source), Path(out)
    if not src.is_file():
        raise DeliveryError(f"{src.name}: no such file")
    if src.resolve() == dest.resolve():
        raise DeliveryError("the re-tagged copy must be a new file")
    _check_destination(dest, (".mp4", ".mkv", ".mov"))
    stream = probe_video(src)
    if stream.get("codec_name") != "av1":
        raise DeliveryError(f"{src.name}: codec is {stream.get('codec_name')}, the AV1 re-tag needs av1")
    used, t = _resolve_profile(profile, stream)
    bsf = (
        f"av1_metadata=color_primaries={t.primaries_code}:transfer_characteristics={t.transfer_code}"
        f":matrix_coefficients={t.matrix_code}:color_range={t.range}"
    )
    fmt_flag = {".mp4": "mp4", ".mov": "mov", ".mkv": "matroska"}[dest.suffix.lower()]
    staged = stage_path(dest)
    try:
        run_ffmpeg(
            [
                "-i",
                str(src),
                "-map",
                "0:v",
                "-map",
                "0:a?",
                "-c",
                "copy",
                "-bsf:v",
                bsf,
                *t.ffmpeg_args(),
                *(["-movflags", "+faststart"] if fmt_flag in ("mp4", "mov") else []),
                "-f",
                fmt_flag,
                str(staged),
            ],
            timeout_s=timeout_s,
        )
        embed_and_place(staged, dest, provenance.lineage)
    finally:
        staged.unlink(missing_ok=True)
    try:
        verified = _verify_tags(dest, t, codec="av1", pix_fmts=None)
        side = write_sidecar(
            dest,
            provenance,
            fmt="av1_retag",
            files=[dest],
            override=override,
            color={
                **t.expected(),
                "profile": used,
                "was": {
                    k: stream.get(k)
                    for k in ("color_primaries", "color_transfer", "color_space", "color_range")
                },
                "lossless": True,
                "verified": verified,
            },
        )
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return Delivered(path=dest, sidecar=side, format="av1_retag", files=(dest,), override=override)
