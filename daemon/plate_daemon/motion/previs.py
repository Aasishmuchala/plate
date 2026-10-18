"""Seedance 2.5 reference-to-video previs: clay motion from the real camera path, dressed by the approved stills,
watermarked PREVIS (router plan T44, BUILD_PLAN P3 and the "Seedance drifts or invents" risk).

What goes to the engine (`seedance-2.5-r2v`, Atlas, custody D):

- `reference_images`: the approved still upstream of the Motion node as `role: first_frame` (`@img1`), then one
  approved still per room along the path (`occupancy.choose_refs`: the still whose camera sees the room's dominant
  architecture ids best), each sent once, 30 at most. The stills are the CAS outputs of approved, gate-passed nodes
  of the same scene (equal `scene_hash`), as house 8-bit sRGB PNGs (ICC only, metadata stripped) under opaque names,
  at native size: never upscaled; a side over 6000 px is brought down, a side under 300 px is sent as is and noted.
- `reference_videos[0]`: the packet's clay sequence (`files.clay_seq`) as an H.264 mp4, the motion reference.
- `prompt`: the composer's (`llm.composer.compose` on PREVIS_RECIPE; template only when no client is given or the
  custody gate keeps the model out) plus `timing_hints(rooms, timemap, refs)`, integer-second timestamps per the
  Seedance 2.5 docs: `0-3s: room 1 (ref @img1); 3s: threshold to room 2 (ref @img2)`.
- params: `camera_fixed: false`, `generate_audio: false`, `ratio: adaptive` (it follows the first frame),
  `duration = round(duration_s)` clamped 4 to 30, `resolution` 480P for a draft and 1080P for a complete take
  (Seedance writes 1080p in 10 bits; the take keeps 10 bits through the watermark pass).

The time map. Seedance delivers 24 fps and the clip length is whole seconds, so the packet's path (Max 25 or 30 fps)
is PLAYED at `playback_fps = n_frames / duration` to fill the clip: equal to the packet's fps whenever the path
already lasts a whole number of seconds, a small retime otherwise (`retime` is recorded; a 12-frame fixture path
clamped to the 4 s minimum plays at 3 fps). The clay reference video is encoded at that rate, the rooms are timed
on it, and `TimeMap(playback_fps, 24, (first, last))` is what the run records, so the drift gate maps each engine
frame to the packet frame the engine was shown.

The output is PREVIS, always: `PREVIS` tiled over the frame at 20 % opacity, burned in with ffmpeg `drawtext`,
the font given as `fontfile=` from the bundled `fonts/DejaVuSans.ttf` (ffmpeg runs with the fonts folder as its
working directory, so the path needs no escaping and no system font is ever used). An ffmpeg built without
libfreetype has no drawtext (Homebrew's 8.1 on the dev Mac); there the same tiles, drawn with the same bundled font
by Pillow, are burned in with ffmpeg `overlay`, and the run says which (`watermark.backend`). The engine's clean
bytes are never kept: only the watermarked mp4 enters the CAS. The run's ledger records carry `previs: true`, the
Motion node gets `previs: true`, and delivery refuses it (`check_deliverable`, canvas rule PREVIS_NOT_DELIVERABLE).

`run(project, motion_node, quality)` in order: the video lock (the upstream still must have passed its gates and
been approved: `PrevisLocked`, 409 VIDEO_LOCKED, before any route or price exists), custody (`CustodyViolation`
before a byte is staged: Atlas is grade D, refused on NDA projects, where previs means local Wan 2.2), the plan
(rooms, refs, hints, prompt, clay video), the quote (ledger `quoted`), approval (`approved_by` is required for any
route that leaves the box), then submit, poll, fetch, watermark, CAS, ledger `done`, the node update.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from blake3 import blake3

from plate_gates import io as pio
from plate_gates.timemap import TimeMap, fps_text

from .. import canvas_rules, custody, idempotency
from ..cas import CAS
from ..config import app_home
from ..fsutil import replace_with_retry
from ..ledger import Ledger
from ..llm.composer import Composed, compose, facts_from_packet
from ..lock import ProjectLocked
from ..models import QUOTE_TTL, Quote
from ..pipeline import FAKE_ROUTE, AdapterBridge, PipelineError, adapter_class, decode_png16
from ..projects import Project
from ..quotes import DEFAULT_ETA_S, build_quote
from ..recipes import DEFAULT_NEGATIVE_PROMPT, ProjectSheet, Recipe, assemble, unfilled_count
from ..timeutil import utcnow
from . import occupancy

__all__ = [
    "ENGINE_FPS",
    "ENGINE_ID",
    "FONT_NAME",
    "FONTS_DIR",
    "MAX_DURATION_S",
    "MIN_DURATION_S",
    "PREVIS_RECIPE",
    "QUALITY_RESOLUTION",
    "WATERMARK_OPACITY",
    "WATERMARK_TEXT",
    "PrevisError",
    "PrevisJobFailed",
    "PrevisLocked",
    "PrevisNotDeliverable",
    "PrevisPlan",
    "PrevisResult",
    "burn_watermark",
    "check_deliverable",
    "check_unlocked",
    "clay_reference",
    "clip_seconds",
    "drawtext_filter",
    "font_path",
    "has_drawtext",
    "is_previs",
    "plan",
    "previs_timemap",
    "quote",
    "run",
    "seedance_engine",
    "timing_hints",
    "watermark_layout",
    "watermark_png",
]

ENGINE_ID = "seedance-2.5-r2v"
ENGINE_FPS = Fraction(24)  # Seedance 2.5 delivers 24 fps
MIN_DURATION_S, MAX_DURATION_S = 4, 30  # manifest seedance-2.5-r2v, param duration
QUALITY_RESOLUTION = {"draft": "480P", "complete": "1080P"}
TASK = "previs"  # the manifests' allowed_tasks entry
SHOT_CLASS = "motion"
REF_MIN_SIDE, REF_MAX_SIDE = 300, 6000  # Seedance 2.5 input sides (manifest sizes.arbitrary)
WATERMARK_TEXT = "PREVIS"
WATERMARK_OPACITY = 0.20
FONTS_DIR = Path(__file__).resolve().parent / "fonts"
# DejaVu / Bitstream Vera licence (fonts/LICENSE_DEJAVU): free to bundle and ship.
FONT_NAME = "DejaVuSans.ttf"
POLL_INTERVAL_S = 5.0
POLL_TIMEOUT_S = 12 * 60.0
STILL_KINDS = frozenset({"Edit", "Comp", "Colour", "LightMix", "Recolour", "People", "Light"})
DEFAULT_INTENT = "Clay-motion previs of the approved stills along the packet's real camera path."
_NAME_UNSAFE = re.compile(r"[;\r\n\t]+")

# The template the composer tailors. No [TOKENS]: a template-only run (NDA, no client) sends it as is. The @video1 /
# @img<n> reference syntax follows the Seedance 2.5 docs as scouted; it is unverified until the P0 smoke test.
PREVIS_RECIPE = Recipe(
    id="previs_clay_walk",
    num="",
    title="Clay-motion previs",
    use="Seedance 2.5 reference-to-video previs from the packet's clay path; always watermarked PREVIS.",
    prompt=(
        "Previs walk-through. The camera follows the clay reference video (@video1) exactly: the same path, the "
        "same speed and the same framing at every moment; it never moves on its own. The first frame is the "
        "approved still (@img1). Each space along the path looks the way its reference still shows it: the same "
        "materials, the same light, the same proportions.\n"
        "SCENE LOCKS: architecture, materials and proportions as in the reference stills; straight lines stay "
        "straight; no camera move the clay video does not make; no people, no text, no logos, no captions."
    ),
    negative_prompt=DEFAULT_NEGATIVE_PROMPT,
)


class PrevisError(PipelineError):
    """A previs request refused before any spend. `status` and `code` become the HTTP answer."""


class PrevisLocked(PrevisError):
    """The Motion node's upstream still has not passed its gates and been approved (canvas rule VIDEO_LOCKED)."""

    def __init__(self, message: str, **extra: Any):
        super().__init__(message, code="VIDEO_LOCKED", status=409, **extra)


class PrevisNotDeliverable(PrevisError):
    """A previs output was offered to delivery (canvas rule PREVIS_NOT_DELIVERABLE)."""

    def __init__(self, message: str = "previs output is not deliverable (PREVIS)", **extra: Any):
        super().__init__(message, code="PREVIS_NOT_DELIVERABLE", status=409, **extra)


class PrevisJobFailed(RuntimeError):
    """The engine refused or failed the job, or it did not finish in time. `state` is refused, failed or timeout."""

    def __init__(self, state: str, detail: str | None = None):
        super().__init__(f"previs job {state}" + (f": {detail}" if detail else ""))
        self.state = state
        self.detail = detail


# ---------------------------------------------------------------------------- previs is never delivered


def is_previs(obj: Any) -> bool:
    """True for a previs run record, result or node: `previs: true` on it, on its `data` or its `params`."""
    if obj is None:
        return False
    if getattr(obj, "previs", None) is True:
        return True
    if isinstance(obj, Mapping):
        if obj.get("previs") is True:
            return True
        for key in ("data", "params", "record"):
            inner = obj.get(key)
            if isinstance(inner, Mapping) and inner.get("previs") is True:
                return True
    return False


def check_deliverable(obj: Any) -> None:
    """Raise PrevisNotDeliverable for previs output; Delivery and Export call this before writing anything."""
    if is_previs(obj):
        raise PrevisNotDeliverable()


# ---------------------------------------------------------------------------- the clip's clock


def _round_half_up(x: float | Fraction) -> int:
    return int(Fraction(x) + Fraction(1, 2)) if x >= 0 else -int(-Fraction(x) + Fraction(1, 2))


def clip_seconds(duration_s: float | Fraction) -> int:
    """Seedance's integer duration for a path of `duration_s`: round (half up), clamped to 4..30."""
    return min(max(_round_half_up(duration_s), MIN_DURATION_S), MAX_DURATION_S)


def previs_timemap(
    packet_fps: Any, frames: tuple[int, int], engine_fps: Any = ENGINE_FPS
) -> tuple[TimeMap, dict[str, Any]]:
    """(TimeMap(playback_fps, engine_fps, frames), info): the path played to fill a whole-second clip (module
    docstring). info = {packet_fps, playback_fps, retime, duration_s (the clip's), packet_duration_s}."""
    native = TimeMap(packet_fps, engine_fps, frames)
    n = native.n_packet_frames
    seconds = clip_seconds(native.duration_s)
    playback = Fraction(n, seconds)
    tm = TimeMap(playback, engine_fps, frames)
    info = {
        "packet_fps": fps_text(native.packet_fps),
        "playback_fps": fps_text(playback),
        "retime": float(playback / native.packet_fps),
        "duration_s": seconds,
        "packet_duration_s": float(native.duration_s),
    }
    return tm, info


# ---------------------------------------------------------------------------- timing hints


def _room_names(rooms: Sequence[Mapping[str, Any]], names: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, room in enumerate(rooms):
        rid = str(room["room_id"])
        given = None
        if isinstance(names, Mapping):
            given = names.get(rid)
        elif isinstance(names, Sequence) and not isinstance(names, str) and i < len(names):
            given = names[i]
        text = " ".join(_NAME_UNSAFE.sub(" ", str(given)).split())[:40] if given else ""
        out[rid] = text or f"room {i + 1}"
    return out


def _ref_index(refs: Sequence[Mapping[str, Any]] | None, room_id: str) -> int | None:
    for ref in refs or ():
        if room_id in (ref.get("room_ids") or ()):
            return int(ref["index"])
    return None


def _arrival(boundary: str, name: str) -> str:
    if boundary == "threshold":
        return f"threshold to {name}"
    if boundary == "occluder":
        return f"past the occluder into {name}"
    return f"into {name}"


def timing_hints(
    rooms: Sequence[Mapping[str, Any]],
    timemap: TimeMap,
    refs: Sequence[Mapping[str, Any]] | None = None,
    *,
    names: Any = None,
) -> str:
    """Integer-second timing hints over the clip, one per room:
    `0-3s: room 1 (ref @img2); 3-7s: room 2 (ref @img3); 7s: threshold to room 3 (ref @img4)` (the last room is
    named at the moment the camera arrives), or `0-5s: room 1 (ref @img1)` for one room. Room starts are rounded
    half up; a room shorter than the rounding (its start lands on the previous room's) is folded into the room
    that follows it, and a room that would start at the clip's end is dropped. `names` ({room_id: text} or a list)
    replaces "room N" with an artist's words; the legend's names never appear (the legend is C0)."""
    if not rooms:
        raise ValueError("timing hints need at least one room")
    duration = max(_round_half_up(timemap.duration_s or 0), 1)
    labels = _room_names(rooms, names)
    segments: list[tuple[int, Mapping[str, Any]]] = []
    for i, room in enumerate(rooms):
        start = 0 if i == 0 else _round_half_up(Fraction(room["engine_seconds"][0]).limit_denominator(10**6))
        if segments and start <= segments[-1][0]:
            segments[-1] = (segments[-1][0], room)
            continue
        if segments and start >= duration:
            continue
        segments.append((start, room))

    def label(room: Mapping[str, Any]) -> str:
        idx = _ref_index(refs, str(room["room_id"]))
        return labels[str(room["room_id"])] + (f" (ref @img{idx})" if idx is not None else "")

    if len(segments) == 1:
        return f"0-{duration}s: {label(segments[0][1])}"
    parts = [f"{s}-{segments[j + 1][0]}s: {label(room)}" for j, (s, room) in enumerate(segments[:-1])]
    s_last, last = segments[-1]
    idx = _ref_index(refs, str(last["room_id"]))
    arrival = _arrival(str(last.get("boundary") or "ids"), labels[str(last["room_id"])])
    parts.append(f"{s_last}s: {arrival}" + (f" (ref @img{idx})" if idx is not None else ""))
    return "; ".join(parts)


# ---------------------------------------------------------------------------- the watermark


def font_path() -> Path:
    """The bundled DejaVuSans.ttf. Never a system font: the watermark must look the same on every box."""
    p = FONTS_DIR / FONT_NAME
    if not p.is_file():
        raise FileNotFoundError(f"the bundled watermark font is missing: {p}")
    return p


@dataclass(frozen=True)
class WatermarkLayout:
    """Where the PREVIS tiles go: `origins` are Pillow text origins (anchor 'la'), `boxes` the ink boxes
    (x0, y0, x1, y1, half-open) and `drawtext_xy` the matching drawtext x/y (the top-left of the ink). Each tile
    is plain white text at WATERMARK_OPACITY. No outline or shadow: on mid-tones a dark edge at the same opacity
    cancels the fill (the tile's mean luma barely moves), and the mark must read as a lift everywhere but
    near-white, where PREVIS is still in the file's metadata, the run record and the node."""

    width: int
    height: int
    fontsize: int
    origins: tuple[tuple[int, int], ...]
    boxes: tuple[tuple[int, int, int, int], ...]

    @property
    def drawtext_xy(self) -> tuple[tuple[int, int], ...]:
        return tuple((b[0], b[1]) for b in self.boxes)

    def inside(self) -> list[tuple[int, int, int, int]]:
        """The boxes that lie wholly inside the frame (what a test measures)."""
        return [
            b for b in self.boxes if b[0] >= 0 and b[1] >= 0 and b[2] <= self.width and b[3] <= self.height
        ]


@lru_cache(maxsize=32)
def watermark_layout(width: int, height: int) -> WatermarkLayout:
    """A staggered grid of PREVIS tiles: font size 1/8 of the short side (at least 12 px), rows 2.6 font sizes
    apart, columns 1.8 text widths apart, every other row shifted half a column."""
    from PIL import ImageFont

    w, h = int(width), int(height)
    size = max(12, _round_half_up(Fraction(min(w, h), 8)))
    font = ImageFont.truetype(str(font_path()), size)
    left, top, right, bottom = font.getbbox(WATERMARK_TEXT)
    text_w = right - left
    step_x = max(int(text_w * 1.8), text_w + size)
    step_y = max(int(size * 2.6), (bottom - top) + size)
    margin = max(size // 2, 4)
    origins: list[tuple[int, int]] = []
    boxes: list[tuple[int, int, int, int]] = []
    y, row = margin - top, 0
    while y + top < h:
        x = margin - left - (step_x // 2 if row % 2 else 0)
        while x + left < w:
            if x + right > 0:
                origins.append((x, y))
                boxes.append((x + left, y + top, x + right, y + bottom))
            x += step_x
        y += step_y
        row += 1
    return WatermarkLayout(w, h, size, tuple(origins), tuple(boxes))


def _alpha() -> int:
    return _round_half_up(Fraction(WATERMARK_OPACITY).limit_denominator(1000) * 255)


def watermark_png(width: int, height: int) -> bytes:
    """The tiles as an RGBA PNG (white text at 20 % opacity) in the bundled font, the overlay backend's mark."""
    import io

    from PIL import Image, ImageDraw, ImageFont

    lay = watermark_layout(width, height)
    font = ImageFont.truetype(str(font_path()), lay.fontsize)
    img = Image.new("RGBA", (lay.width, lay.height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    for xy in lay.origins:
        draw.text(xy, WATERMARK_TEXT, font=font, fill=(255, 255, 255, _alpha()))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def drawtext_filter(width: int, height: int) -> str:
    """The ffmpeg filter chain: one drawtext per tile. `fontfile=` names the bundled font by its bare file name;
    the caller runs ffmpeg in FONTS_DIR so it resolves there (no system font, no path escaping on Windows)."""
    lay = watermark_layout(width, height)
    op = f"{WATERMARK_OPACITY:.2f}"
    common = [
        f"fontfile={FONT_NAME}",
        f"text={WATERMARK_TEXT}",
        f"fontsize={lay.fontsize}",
        f"fontcolor=white@{op}",
    ]
    return ",".join("drawtext=" + ":".join([*common, f"x={x}", f"y={y}"]) for x, y in lay.drawtext_xy)


@lru_cache(maxsize=8)
def has_drawtext(tool: str) -> bool:
    """Whether this ffmpeg has the drawtext filter (it needs libfreetype)."""
    try:
        res = subprocess.run([tool, "-hide_banner", "-filters"], capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return re.search(rb"\sdrawtext\s", res.stdout) is not None


def _ffmpeg(tool: str | Path | None) -> str:
    found = Path(tool) if tool is not None else pio.ffmpeg()
    if found is None:
        raise FileNotFoundError("ffmpeg not found: install it or set PLATE_FFMPEG")
    return str(found)


def _run_ffmpeg(cmd: list[str], *, cwd: Path | None = None) -> None:
    res = subprocess.run(cmd, capture_output=True, check=False, cwd=str(cwd) if cwd else None)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr.decode('utf-8', 'replace').strip()[-800:]}")


def _encode_args(pix_fmt: str) -> list[str]:
    return [
        "-an",
        "-map_metadata",
        "-1",
        "-metadata",
        f"title={WATERMARK_TEXT}",
        "-metadata",
        f"comment={WATERMARK_TEXT}: not deliverable",
        "-c:v",
        "libx264",
        "-crf",
        "16",
        "-preset",
        "medium",
        "-pix_fmt",
        pix_fmt,
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-fflags",
        "+bitexact",
        "-movflags",
        "+faststart",
    ]


def burn_watermark(
    src: str | Path, dst: str | Path, *, backend: str = "auto", ffmpeg: str | Path | None = None
) -> dict[str, Any]:
    """Burn the PREVIS tiles into `src`, writing `dst` (H.264, 10-bit kept when the source is 10-bit). `backend`
    is `drawtext`, `overlay` (the bundled font rendered by Pillow, burned with ffmpeg overlay) or `auto` (drawtext
    when this ffmpeg has it). Returns what the run records about the watermark."""
    if backend not in ("auto", "drawtext", "overlay"):
        raise ValueError(f"backend must be auto, drawtext or overlay, not {backend!r}")
    tool = _ffmpeg(ffmpeg)
    src, dst = Path(src).resolve(), Path(dst).resolve()
    info = pio.probe(src)
    w, h = int(info["width"]), int(info["height"])
    ten_bit = "10" in str(info.get("pix_fmt") or "")
    pix_fmt = "yuv420p10le" if ten_bit else "yuv420p"
    lay = watermark_layout(w, h)
    use = backend
    if use == "auto":
        use = "drawtext" if has_drawtext(tool) else "overlay"
    elif use == "drawtext" and not has_drawtext(tool):
        raise RuntimeError(
            "this ffmpeg has no drawtext filter (built without libfreetype); use backend overlay"
        )
    font_path()
    if use == "drawtext":
        cmd = [tool, "-v", "error", "-nostdin", "-y", "-i", str(src), "-vf", drawtext_filter(w, h)]
        _run_ffmpeg([*cmd, *_encode_args(pix_fmt), str(dst)], cwd=FONTS_DIR)
    else:
        png = dst.with_name(dst.stem + ".wm.png")
        png.write_bytes(watermark_png(w, h))
        try:
            cmd = [tool, "-v", "error", "-nostdin", "-y", "-i", str(src), "-loop", "1", "-i", str(png)]
            cmd += ["-filter_complex", "[0:v][1:v]overlay=0:0:shortest=1:format=auto[v]", "-map", "[v]"]
            _run_ffmpeg([*cmd, *_encode_args(pix_fmt), str(dst)])
        finally:
            png.unlink(missing_ok=True)
    return {
        "text": WATERMARK_TEXT,
        "opacity": WATERMARK_OPACITY,
        "tiled": True,
        "backend": use,
        "font": FONT_NAME,
        "fontsize": lay.fontsize,
        "tiles": len(lay.boxes),
        "pix_fmt": pix_fmt,
        "size": [w, h],
    }


# ---------------------------------------------------------------------------- the clay reference


def clay_reference(
    root: Path,
    manifest: Mapping[str, Any],
    out: str | Path,
    playback_fps: Any,
    *,
    ffmpeg: str | Path | None = None,
) -> Path:
    """The packet's clay sequence as an H.264 mp4 at `playback_fps` (no audio, no metadata, even-padded, never
    resampled): Seedance's motion reference, `reference_videos[0]`."""
    ref = (manifest.get("files") or {}).get("clay_seq")
    if not isinstance(ref, Mapping) or ref.get("kind") != "sequence":
        raise PrevisError("the motion packet has no clay sequence (files.clay_seq)", code="no_clay")
    first, last = int(ref["first"]), int(ref["last"])
    pattern = Path(root).joinpath(*str(ref["pattern"]).split("/"))
    fps = playback_fps if isinstance(playback_fps, Fraction) else Fraction(playback_fps)
    out = Path(out)
    cmd = [_ffmpeg(ffmpeg), "-v", "error", "-nostdin", "-y", "-framerate", fps_text(fps)]
    cmd += ["-start_number", str(first), "-i", str(pattern), "-frames:v", str(last - first + 1)]
    cmd += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-an", "-map_metadata", "-1", "-c:v", "libx264"]
    cmd += ["-crf", "12", "-preset", "medium", "-pix_fmt", "yuv420p", "-fflags", "+bitexact"]
    cmd += ["-movflags", "+faststart", str(out)]
    _run_ffmpeg(cmd)
    return out


# ---------------------------------------------------------------------------- the graph


def _node_id(motion_node: Any) -> str:
    if isinstance(motion_node, str):
        return motion_node
    if isinstance(motion_node, Mapping) and isinstance(motion_node.get("id"), str):
        return motion_node["id"]
    raise PrevisError("motion_node must be a node id or a node with an id")


def check_unlocked(graph: Any, node_id: str) -> str:
    """The server-side video lock; returns the id of the approved still feeding the Motion node. Raises
    PrevisLocked (409 VIDEO_LOCKED) when that still has not passed its gates and been approved, or when nothing
    feeds the node; any other canvas rule broken upstream is a 422 with its own code."""
    g = canvas_rules.as_graph(graph)
    node = next((n for n in g.nodes if n.get("id") == node_id), None)
    if node is None:
        raise PrevisError(f"the graph has no node {node_id}", code="not_found", status=404)
    if node.get("kind") != "Motion":
        raise PrevisError(f"{node.get('kind')} node {node_id} is not a Motion node", code="node_kind")
    incoming = [e for e in g.edges if e["target"] == node_id]
    if not incoming:
        raise PrevisLocked(f"motion node {node_id} has no approved still upstream")
    for e in incoming:
        res = canvas_rules.validate_edge(g, e)
        if not res.ok:
            if res.code == "VIDEO_LOCKED":
                raise PrevisLocked(res.message or "motion is locked", edge=e)
            raise PrevisError(res.message or "canvas rule", code=res.code or "rule", edge=e)
    upstream = canvas_rules.ancestors(g, node_id)
    broken = [v for v in canvas_rules.validate_graph(g) if v["edge"]["target"] in upstream]
    if broken:
        raise PrevisError(broken[0]["message"], code=broken[0]["code"], violations=broken)
    # The approved still is the source on the Motion node's image port. Since T81 a packet edge (Plate) or a
    # reference edge (elements) may also feed a Motion node, so do not take whichever edge comes first.
    stills = [e for e in incoming if (e.get("targetHandle") or e.get("target_handle") or "image") == "image"]
    return (stills or incoming)[0]["source"]


def _params(node: Mapping[str, Any]) -> dict[str, Any]:
    return dict(node.get("params")) if isinstance(node.get("params"), Mapping) else {}


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    return next((mapping[k] for k in keys if mapping.get(k) is not None), None)


@dataclass
class _Packet:
    packet_id: str
    root: Path
    manifest: dict[str, Any]
    _legend: dict[str, Any] | None = None

    @property
    def legend(self) -> dict[str, Any]:
        if self._legend is None:
            ref = (self.manifest.get("files") or {}).get("id_legend") or {"path": "id_legend.json"}
            path = self.root / ref["path"]
            self._legend = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"entries": []}
        return self._legend

    @property
    def scene(self) -> str | None:
        return (self.manifest.get("scene_hash") or {}).get("hex")

    @property
    def custody(self) -> Any:
        return (self.manifest.get("policy") or {}).get("custody")


class _Packets:
    """The project's packets.json, loaded on demand (by reference: each entry's folder and manifest)."""

    def __init__(self, project: Project):
        self.entries = project.packets()
        self._cache: dict[str, _Packet] = {}

    def get(self, packet_id: str) -> _Packet:
        hit = self._cache.get(packet_id)
        if hit is not None:
            return hit
        entry = self.entries.get(packet_id)
        if entry is None or not entry.get("path"):
            raise PrevisError(f"packet {packet_id} is not in this project", code="unknown_packet")
        root = Path(entry["path"])
        mp = root / "manifest.json"
        if not mp.is_file():
            raise PrevisError(f"packet {packet_id}: no manifest.json at its path", code="unknown_packet")
        pk = _Packet(packet_id, root, json.loads(mp.read_text(encoding="utf-8")))
        self._cache[packet_id] = pk
        return pk

    def motion_for(self, hero_packet_id: str) -> _Packet:
        """The one motion packet whose hero still is `hero_packet_id`."""
        found = []
        for pid, entry in sorted(self.entries.items()):
            if entry.get("kind") not in (None, "motion"):
                continue
            try:
                pk = self.get(pid)
            except PrevisError:
                continue
            if (pk.manifest.get("motion") or {}).get("hero_still_packet_id") == hero_packet_id:
                found.append(pk)
        if not found:
            raise PrevisError(
                f"no motion packet has {hero_packet_id} as its hero still", code="no_motion_packet"
            )
        if len(found) > 1:
            raise PrevisError(
                f"{len(found)} motion packets start at {hero_packet_id}: name one in params.motion_packet_id",
                code="ambiguous_motion_packet",
            )
        return found[0]


# ---------------------------------------------------------------------------- the plan


@dataclass
class PrevisPlan:
    """Everything the engine is sent and the run records. `payload` is the worker-protocol payload
    ({engine_id, inputs, params}); `record` is the JSON summary the ledger and the result carry."""

    project_id: str
    node_id: str
    quality: str
    engine_id: str
    route: dict[str, Any]
    motion_packet_id: str
    hero_packet_id: str
    hero_node_id: str
    timemap: TimeMap
    time_info: dict[str, Any]
    rooms: list[dict[str, Any]]
    refs: list[dict[str, Any]]
    hints: str
    prompt: str
    composer: dict[str, Any]
    payload: dict[str, Any]
    input_keys: dict[str, Any]
    work_dir: Path
    warnings: list[str] = field(default_factory=list)

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.payload["params"])

    @property
    def duration_s(self) -> int:
        return int(self.payload["params"]["duration"])

    @property
    def resolution(self) -> str:
        return str(self.payload["params"]["resolution"])

    def input_hashes(self) -> dict[str, str]:
        return idempotency.input_hashes(
            {
                "reference_images": self.input_keys["reference_images"],
                "reference_videos": self.input_keys["reference_videos"],
                "prompt": self.prompt,
            }
        )

    def idempotency_key(self, attempt: int = 1) -> str:
        return idempotency.idempotency_key(
            self.input_hashes(), self.params, idempotency.route_id(self.route), attempt
        )

    def record(self) -> dict[str, Any]:
        return {
            "previs": True,
            "quality": self.quality,
            "resolution": self.resolution,
            "output_bits": 10 if self.resolution == "1080P" else 8,
            "duration_s": self.duration_s,
            "motion_packet_id": self.motion_packet_id,
            "hero_packet_id": self.hero_packet_id,
            "hero_node_id": self.hero_node_id,
            "timemap": self.timemap.to_json(),
            "time": dict(self.time_info),
            "rooms": [
                {
                    "room_id": r["room_id"],
                    "frames": list(r["frames"]),
                    "engine_seconds": list(r["engine_seconds"]),
                    "dominant_ids": list(r["dominant_ids"]),
                    "boundary": r["boundary"],
                }
                for r in self.rooms
            ],
            "refs": [
                {k: ref[k] for k in ("index", "role", "packet_id", "node_id", "key", "room_ids", "size")}
                for ref in self.refs
            ],
            "hints": self.hints,
            "prompt": self.prompt,
            "composer": dict(self.composer),
            "inputs": dict(self.input_keys),
            "params": self.params,
            "warnings": list(self.warnings),
        }


def _sheet(project: Project) -> ProjectSheet:
    settings = project.doc.get("settings") or {}
    data = settings.get("project_sheet") if isinstance(settings, Mapping) else None
    return ProjectSheet.from_dict(data if isinstance(data, Mapping) else None)


def _compose(
    manifest: Mapping[str, Any], legend: Mapping[str, Any], sheet: ProjectSheet, intent: str, client: Any
) -> Composed:
    if client is None:
        template = assemble(PREVIS_RECIPE, sheet)
        return Composed(
            prompt=template,
            negative_prompt=PREVIS_RECIPE.negative_prompt or DEFAULT_NEGATIVE_PROMPT,
            tokens_unfilled=unfilled_count(template),
            llm_used=False,
            reason="no_client",
        )
    return compose(PREVIS_RECIPE, sheet, facts_from_packet(manifest, legend), intent, client=client)


def _still_png(rgb16: np.ndarray) -> tuple[bytes, list[str]]:
    """House 8-bit PNG of an approved still for Seedance: native size, a side over 6000 px brought down (area
    filter); never upscaled, so a side under 300 px is noted, not grown."""
    from plate_pixels.egress import encode_png8

    notes: list[str] = []
    h, w = rgb16.shape[:2]
    if max(w, h) > REF_MAX_SIDE:
        s = REF_MAX_SIDE / max(w, h)
        rgb16 = cv2.resize(rgb16, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
        notes.append(
            f"a {w}x{h} still was brought down to {rgb16.shape[1]}x{rgb16.shape[0]} (Seedance max side)"
        )
    if min(rgb16.shape[:2]) < REF_MIN_SIDE:
        notes.append(
            f"a {rgb16.shape[1]}x{rgb16.shape[0]} still is under Seedance's {REF_MIN_SIDE} px minimum side; "
            "sent at native size (stills are never upscaled)"
        )
    return encode_png8(rgb16), notes


def _work_dir(work_dir: str | Path | None, project_id: str, node_id: str) -> Path:
    if work_dir is not None:
        d = Path(work_dir)
    else:
        d = app_home() / "cache" / "previs" / project_id / blake3(node_id.encode("utf-8")).hexdigest()[:16]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _context(project: Project, motion_node: Any) -> dict[str, Any]:
    """Graph and packets for a Motion node, the lock checked first. Reads; writes nothing."""
    nid = _node_id(motion_node)
    hero_id = check_unlocked(project.graph, nid)
    g = canvas_rules.as_graph(project.graph)
    nodes = {n["id"]: n for n in g.nodes}
    node, hero = nodes[nid], nodes[hero_id]
    if not isinstance(hero.get("outputHash"), str):
        raise PrevisLocked(f"the still {hero_id} feeding motion node {nid} has no output to start from")
    plates = canvas_rules.upstream_plates(g, hero_id)
    if not plates or not isinstance(plates[0].get("packetId"), str):
        raise PrevisError(
            "generation must be anchored to a plate camera (source-view-only)", code="NO_PLATE_ANCHOR"
        )
    hero_pid = plates[0]["packetId"]
    packets = _Packets(project)
    hero_pk = packets.get(hero_pid)
    params = _params(node)
    named = _first(params, "motion_packet_id", "motionPacketId", "packet_id", "packetId")
    motion = packets.get(str(named)) if named else packets.motion_for(hero_pid)
    if motion.manifest.get("kind") != "motion":
        raise PrevisError(f"packet {motion.packet_id} is not a motion packet", code="no_motion_packet")
    if (motion.manifest.get("motion") or {}).get("hero_still_packet_id") != hero_pid:
        raise PrevisError(
            f"motion packet {motion.packet_id} does not start at {hero_pid}, the still feeding the node: the "
            "first frame would not be the path's first frame",
            code="hero_mismatch",
        )
    allowed = custody.project_allowed(project.custody, motion.custody) & custody.project_allowed(
        project.custody, hero_pk.custody
    )
    return {
        "graph": g,
        "nodes": nodes,
        "node": node,
        "node_id": nid,
        "hero": hero,
        "hero_id": hero_id,
        "hero_pk": hero_pk,
        "motion": motion,
        "packets": packets,
        "allowed": allowed,
        "params": params,
    }


def _approved_stills(ctx: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The hero first, then every approved, gate-passed still of the same scene (by graph order)."""
    g, packets, motion = ctx["graph"], ctx["packets"], ctx["motion"]
    out: list[dict[str, Any]] = []
    for n in g.nodes:
        nid = n["id"]
        is_hero = nid == ctx["hero_id"]
        if not is_hero:
            if n.get("kind") not in STILL_KINDS or is_previs(n):
                continue
            if not (n.get("gateStatus") == "pass" and n.get("approved") is True):
                continue
            if n.get("outputTier") != "measured" or not isinstance(n.get("outputHash"), str):
                continue
        plates = canvas_rules.upstream_plates(g, nid)
        pid = plates[0].get("packetId") if plates else None
        if not isinstance(pid, str):
            continue
        try:
            pk = packets.get(pid)
        except PrevisError:
            if is_hero:
                raise
            continue
        if not is_hero and pk.scene != motion.scene:
            continue
        still = {
            "packet_id": pid,
            "node_id": nid,
            "key": n["outputHash"],
            "camera": pk.manifest.get("camera") or {},
            "legend": pk.legend,
            "hero": is_hero,
        }
        if is_hero:
            out.insert(0, still)
        else:
            out.append(still)
    return out


def plan(
    project: Project,
    motion_node: Any,
    quality: str = "draft",
    *,
    route: Mapping[str, Any] | None = None,
    engine_id: str = ENGINE_ID,
    client: Any = None,
    id_seq: Any = None,
    work_dir: str | Path | None = None,
    cas: CAS | None = None,
    ffmpeg: str | Path | None = None,
    _ctx: Mapping[str, Any] | None = None,
) -> PrevisPlan:
    """Rooms, refs, timing hints, prompt and the staged inputs for a previs of `motion_node`. Stages the refs and
    the clay video in the project CAS and in `work_dir` (default `<PLATE_HOME>/cache/previs/...`); talks to no
    vendor. `id_seq` overrides the packet's id sequence (tests synthesise rooms with it)."""
    if quality not in QUALITY_RESOLUTION:
        raise PrevisError(
            f"quality must be one of {sorted(QUALITY_RESOLUTION)}, not {quality!r}", code="quality"
        )
    ctx = _ctx or _context(project, motion_node)
    cas = cas or project.cas()
    motion: _Packet = ctx["motion"]
    nid = ctx["node_id"]
    warnings: list[str] = []

    rng = occupancy.sequence_range(motion.manifest)
    if rng is None:
        raise PrevisError(f"motion packet {motion.packet_id} has no frame range", code="no_motion_packet")
    tm, time_info = previs_timemap(occupancy.packet_fps(motion.root, motion.manifest), rng, ENGINE_FPS)
    if time_info["retime"] != 1.0:
        warnings.append(
            f"the {time_info['packet_duration_s']:.3f} s path plays at {time_info['playback_fps']} fps to fill the "
            f"{time_info['duration_s']} s clip (retime {time_info['retime']:.4f})"
        )

    stills = _approved_stills(ctx)
    legend = occupancy.merge_legends(motion.legend, *(s["legend"] for s in stills))
    rooms = occupancy.rooms_along_path((motion.root, motion.manifest), id_seq, legend, tm)
    refs = occupancy.choose_refs(rooms, stills, hero_packet_id=ctx["hero_pk"].packet_id)
    hints = timing_hints(rooms, tm, refs, names=_first(ctx["params"], "room_names", "roomNames"))

    intent = _first(ctx["params"], "intent", "prompt")
    composed = _compose(
        motion.manifest,
        motion.legend,
        _sheet(project),
        intent if isinstance(intent, str) else DEFAULT_INTENT,
        client,
    )
    prompt = f"{composed.prompt.rstrip()}\nTiming: {hints}"

    wd = _work_dir(work_dir, project.project_id, nid)
    images: list[dict[str, Any]] = []
    image_keys: list[dict[str, Any]] = []
    for ref in refs:
        still = ref["still"]
        png, notes = _still_png(decode_png16(cas.read_bytes(still["key"])))
        warnings.extend(f"{still['packet_id']}: {n}" for n in notes)
        key = cas.put_bytes(png, "png")
        path = wd / f"{key[:26]}.png"
        if not path.is_file():
            path.write_bytes(png)
        h, w = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED).shape[:2]
        ref.update({"packet_id": still["packet_id"], "node_id": still["node_id"], "key": key, "size": [w, h]})
        images.append({"path": str(path), "role": ref["role"], "w": w, "h": h})
        image_keys.append({"key": key, "role": ref["role"]})

    clay_tmp = wd / f"clay-{blake3(motion.packet_id.encode('utf-8')).hexdigest()[:12]}.part.mp4"
    clay_reference(motion.root, motion.manifest, clay_tmp, Fraction(time_info["playback_fps"]), ffmpeg=ffmpeg)
    clay_key = cas.put_file(clay_tmp, "mp4")
    clay_path = wd / f"{clay_key[:26]}.mp4"
    if clay_path.is_file():
        clay_tmp.unlink(missing_ok=True)
    else:
        replace_with_retry(clay_tmp, clay_path)
    clay_w, clay_h = (
        int((motion.manifest["files"]["clay_seq"]).get("width") or 0),
        int((motion.manifest["files"]["clay_seq"]).get("height") or 0),
    )
    if clay_w and clay_h and min(clay_w, clay_h) < REF_MIN_SIDE:
        warnings.append(
            f"the clay reference is {clay_w}x{clay_h}, under Seedance's {REF_MIN_SIDE} px minimum side; "
            "sent as rendered"
        )

    params = {
        "duration": int(time_info["duration_s"]),
        "resolution": QUALITY_RESOLUTION[quality],
        "ratio": "adaptive",
        "camera_fixed": False,
        "generate_audio": False,
    }
    payload = {
        "engine_id": engine_id,
        "inputs": {
            "reference_images": images,
            "reference_videos": [{"path": str(clay_path)}],
            "prompt": prompt,
        },
        "params": params,
    }
    return PrevisPlan(
        project_id=project.project_id,
        node_id=nid,
        quality=quality,
        engine_id=engine_id,
        route=dict(route or {}),
        motion_packet_id=motion.packet_id,
        hero_packet_id=ctx["hero_pk"].packet_id,
        hero_node_id=ctx["hero_id"],
        timemap=tm,
        time_info=time_info,
        rooms=rooms,
        refs=refs,
        hints=hints,
        prompt=prompt,
        composer=composed.as_dict(),
        payload=payload,
        input_keys={"reference_images": image_keys, "reference_videos": [{"key": clay_key}]},
        work_dir=wd,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------- engines, routes, quotes


def _manifests(manifests: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if manifests is not None:
        return manifests
    from plate_engines.loader import load_manifests

    return load_manifests()


def _route_record(manifest: Any, r: Any) -> dict[str, Any]:
    return {
        "engine_id": manifest.id,
        "provider": r.provider,
        "region": r.region or "unknown",
        "route_id": r.route_id,
        "custody_grade": r.custody_grade,
        "custody_status": r.custody_status,
        "upload_visibility": r.upload_visibility,
        "model_version": r.model_slug or manifest.model_id or manifest.version,
    }


def _pick_route(manifest: Any, allowed: Any, route_id: str | None) -> tuple[Any, dict[str, Any]]:
    candidates = [r for r in manifest.routes if r.status == "active"]
    if route_id is not None:
        candidates = [r for r in candidates if r.route_id == route_id]
    if not candidates:
        raise PrevisError(f"{manifest.id} has no active route {route_id or ''}".strip(), code="unknown_route")
    ok = [r for r in candidates if custody.route_allowed(r, allowed)]
    if not ok:
        custody.require_route(candidates[0], allowed, what=manifest.id)  # raises CustodyViolation
    return ok[0], _route_record(manifest, ok[0])


def _engine_route(engine: Any, engine_id: str) -> dict[str, Any]:
    """An injected engine's route (its `route` attribute, else the in-process fake's: local, C0)."""
    route = getattr(engine, "route", None)
    if isinstance(route, Mapping):
        return {**dict(route), "engine_id": route.get("engine_id") or engine_id}
    return {**FAKE_ROUTE, "engine_id": engine_id}


def seedance_engine(
    project: Project,
    run_id: str,
    route: Any,
    manifest: Any,
    motion: Any,
    *,
    settings: Any = None,
    legend: Mapping[str, Any] | None = None,
    staging_dir: Path | None = None,
) -> AdapterBridge:
    """The real engine: the plate_engines SeedanceR2V adapter on the project's Egress (host allowlist, metadata
    strip, leak scan with the packet's names, outbound log) behind the worker protocol."""
    from ..egress import Egress, EgressRun
    from ..outbound_log import OutboundLog

    cls = adapter_class(manifest, route)
    if cls is None:
        raise PrevisError(f"no adapter speaks {manifest.id} via {route.route_id}", code="no_adapter")
    transport = None
    if route.upload_visibility != "local":
        erun = EgressRun.for_project(
            project, run_id, packet=motion.manifest, legend=legend or motion.legend, packet_dir=motion.root
        )
        transport = Egress(settings, OutboundLog.for_project(project.root)).transport(erun, route)
    kwargs: dict[str, Any] = {}
    if staging_dir is not None and "staging_dir" in inspect.signature(cls).parameters:
        kwargs["staging_dir"] = staging_dir
    return AdapterBridge(cls(manifest, route, transport=transport, **kwargs))


def quote(p: PrevisPlan, manifests: Mapping[str, Any] | None = None, *, now: datetime | None = None) -> Quote:
    """The plan priced on its route: the manifest's per-second price for a manifest engine, zero for the local
    in-process fake (it is not a vendor)."""
    now = now or utcnow()
    eid = str(p.route.get("engine_id") or p.engine_id)
    m = None
    if p.route.get("provider") != "fake":
        m = _manifests(manifests).get(eid)
    if m is not None:
        return build_quote(m, p.route, p.input_hashes(), p.params, now=now)
    return Quote(
        provider=str(p.route.get("provider") or "fake"),
        model=str(eid),
        model_version=str(p.route.get("model_version") or "fake"),
        region=str(p.route.get("region") or "local"),
        custody_grade=custody.effective_grade(p.route).value,
        price_usd=0.0,
        eta_s=DEFAULT_ETA_S.get("video_r2v", 300.0),
        created_utc=now,
        expires_utc=now + QUOTE_TTL,
        size=None,
        route_id=p.route.get("route_id"),
        price_status="confirmed",
    )


# ---------------------------------------------------------------------------- the run


@dataclass
class PrevisResult:
    """A finished previs take: the watermarked mp4 in the CAS (`output_key`) and its run record."""

    run_id: str
    output_key: str
    output_path: Path
    plan: PrevisPlan
    quote: Quote
    watermark: dict[str, Any]
    record: dict[str, Any]
    previs: bool = True


def _leaves_box(route: Mapping[str, Any]) -> bool:
    return route.get("upload_visibility") != "local" or custody.effective_grade(route).value != "C0"


def _ledger(
    project: Project, kind: str, run_id: str, p: PrevisPlan, clock: Callable[[], datetime], **extra: Any
):
    data = {
        "state": extra.pop("state"),
        "packet_id": p.motion_packet_id,
        "previs": True,
        "task": TASK,
        "shot_class": SHOT_CLASS,
        "model": p.route.get("model_version") or p.engine_id,
        "route": {k: p.route.get(k) for k in ("engine_id", "provider", "region", "route_id")},
        "quality": p.quality,
        **extra.pop("data", {}),
    }
    rec = {
        "kind": kind,
        "run_id": run_id,
        "node_id": p.node_id,
        "engine_id": p.route.get("engine_id") or p.engine_id,
        "route_id": p.route.get("route_id") or p.route.get("provider"),
        "custody_grade": custody.effective_grade(p.route).value,
        **extra,
        "data": data,
    }
    rec = {k: v for k, v in rec.items() if v is not None and v != ""}
    return Ledger(project.root, clock=clock).append(rec)


def _mark_node(project: Project, node_id: str, run_id: str, output_key: str, quality: str) -> None:
    """Motion node: previs true, the take as its output. Under the project lock, on a fresh read of project.json."""
    deadline = time.monotonic() + 5.0
    while True:
        try:
            with project.lock():
                fresh = Project.load(project.root)
                graph = fresh.graph
                for n in graph.get("nodes") or []:
                    if n.get("id") != node_id:
                        continue
                    target = n["data"] if isinstance(n.get("data"), dict) else n
                    target["previs"] = True
                    target["outputHash"] = output_key
                    target["previsRunId"] = run_id
                    target["previsQuality"] = quality
                fresh.save_graph(graph)
                project.doc = fresh.doc
                return
        except ProjectLocked:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _wait(
    engine: Any, job: str, *, sleep: Callable[[float], None], poll_interval_s: float, timeout_s: float
) -> str:
    """Poll until the job succeeds (its result URL) or ends; PrevisJobFailed for refused, failed, timeout or a
    success without a result. The poll count bounds the loop too: an injected sleep does not move the clock."""
    deadline = time.monotonic() + timeout_s
    max_polls = max(1, int(timeout_s / max(poll_interval_s, 1e-9)) + 1)
    for i in range(max_polls):
        if i:
            if time.monotonic() >= deadline:
                break
            sleep(poll_interval_s)
        st = engine.poll(job)
        if st.status == "succeeded":
            if not st.result_url:
                raise PrevisJobFailed("failed", "the engine finished without a result")
            return str(st.result_url)
        if st.status in ("failed", "refused"):
            raise PrevisJobFailed(st.status, st.detail)
    raise PrevisJobFailed("timeout", f"no result after {timeout_s:.0f} s")


def run(
    project: Project,
    motion_node: Any,
    quality: str = "draft",
    *,
    engine: Any = None,
    approved_by: str | None = None,
    client: Any = None,
    manifests: Mapping[str, Any] | None = None,
    route_id: str | None = None,
    settings: Any = None,
    id_seq: Any = None,
    work_dir: str | Path | None = None,
    watermark_backend: str = "auto",
    ffmpeg: str | Path | None = None,
    update_graph: bool = True,
    clock: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_s: float = POLL_INTERVAL_S,
    timeout_s: float = POLL_TIMEOUT_S,
) -> PrevisResult:
    """A previs take for `motion_node`: 480P for `draft`, 1080P for `complete` (module docstring for the order).

    `engine` speaks the worker protocol (submit(payload, key) / poll(id) -> PollResult / fetch(url)); without one
    the Seedance 2.5 adapter is built on the project's egress. A route that leaves the box needs `approved_by`
    (quote before spend); the in-process fake does not."""
    ctx = _context(project, motion_node)  # the video lock first: 409 before any route or price
    run_id = idempotency.new_id("prv", clock())
    if engine is None:
        mf = _manifests(manifests)
        m = mf.get(ENGINE_ID)
        if m is None:
            raise PrevisError(f"no {ENGINE_ID} manifest", code="unknown_engine")
        r, route = _pick_route(m, ctx["allowed"], route_id)  # CustodyViolation before a byte is staged
        engine = seedance_engine(
            project,
            run_id,
            r,
            m,
            ctx["motion"],
            settings=settings,
            staging_dir=app_home() / "cache" / "adapters" / run_id,
        )
        manifests = mf
    else:
        route = _engine_route(engine, getattr(engine, "engine_id", None) or "fake")
        custody.require_route(route, ctx["allowed"], what=str(route["engine_id"]))

    p = plan(
        project,
        motion_node,
        quality,
        route=route,
        engine_id=str(route["engine_id"]),
        client=client,
        id_seq=id_seq,
        work_dir=work_dir,
        ffmpeg=ffmpeg,
        _ctx=ctx,
    )
    q = quote(p, manifests, now=clock())
    _ledger(
        project,
        "quoted",
        run_id,
        p,
        clock,
        state="QUOTED",
        cost_usd_quoted=q.price_usd,
        data={
            "quote": q.terms_json(),
            "params": p.params,
            "inputs": p.input_keys,
            "prompt": p.prompt,
            "hints": p.hints,
        },
    )
    if _leaves_box(route):
        if not (isinstance(approved_by, str) and approved_by.strip()):
            raise PrevisError(
                f"{route['engine_id']} via {route.get('route_id') or route.get('provider')} leaves the box: the "
                "quote needs a human approval (approved_by)",
                code="approval_required",
                status=409,
                quote=q.terms_json(),
                run_id=run_id,
            )
        if q.expired(clock()):
            raise PrevisError("the quote expired before it was approved", code="quote_expired", status=409)
    _ledger(
        project,
        "approved",
        run_id,
        p,
        clock,
        state="APPROVED",
        by=approved_by or "system:local",
        cost_usd_quoted=q.price_usd,
    )

    key = p.idempotency_key()
    job: str | None = None
    try:
        job = str(engine.submit(dict(p.payload), key))
        _ledger(
            project,
            "submitted",
            run_id,
            p,
            clock,
            state="SUBMITTED",
            provider_job_id=job,
            data={"idempotency_key": key},
        )
        result_url = _wait(engine, job, sleep=sleep, poll_interval_s=poll_interval_s, timeout_s=timeout_s)
        raw_bytes = engine.fetch(result_url)
        raw_hash = blake3(raw_bytes).hexdigest()
        _ledger(
            project,
            "fetched",
            run_id,
            p,
            clock,
            state="NORMALIZING",
            provider_job_id=job,
            data={"result_blake3": raw_hash, "result_kept": False},
        )
        raw = p.work_dir / f"{run_id}.raw.mp4"
        out = p.work_dir / f"{run_id}.mp4"
        raw.write_bytes(raw_bytes)
        try:
            wm = burn_watermark(raw, out, backend=watermark_backend, ffmpeg=ffmpeg)
        finally:
            raw.unlink(missing_ok=True)  # the clean take is never kept
        cas = project.cas()
        output_key = cas.put_file(out, "mp4")
        out.unlink(missing_ok=True)
    except PrevisJobFailed as e:
        kind = "refused" if e.state == "refused" else "failed"
        _ledger(
            project,
            kind,
            run_id,
            p,
            clock,
            state=kind.upper(),
            provider_job_id=job,
            reason=(e.detail or e.state)[:500],
        )
        raise
    except Exception as e:
        _ledger(
            project,
            "failed",
            run_id,
            p,
            clock,
            state="FAILED",
            provider_job_id=job,
            reason=f"{type(e).__name__}: {e}"[:500],
        )
        raise
    output_path = cas.find(output_key) or cas.path_for(output_key, "mp4")
    record = {
        **p.record(),
        "run_id": run_id,
        "output_key": output_key,
        "watermark": wm,
        "result_blake3": raw_hash,
        "provider_job_id": job,
        "engine_fps": float(ENGINE_FPS),
    }
    _ledger(
        project,
        "done",
        run_id,
        p,
        clock,
        state="DONE",
        provider_job_id=job,
        data={
            "outputs": [output_key],
            "result_key": output_key,
            "watermark": wm,
            "timemap": p.timemap.to_json(),
            "rooms": record["rooms"],
            "refs": record["refs"],
            "verdict": "previs",
        },
    )
    if update_graph:
        _mark_node(project, p.node_id, run_id, output_key, quality)
    return PrevisResult(run_id, output_key, Path(output_path), p, q, wm, record)
