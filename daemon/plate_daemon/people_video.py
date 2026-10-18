"""People in motion: a STAFFAGE video take composited frame by frame over an approved locked or crop-pair clip
(router plan T105, Wave 9).

    install(pipeline) -> PeopleVideoRunner      idempotent; the runner is kept at `pipeline.people_video`

A People node with `params.video` true is quoted and driven here; every other node and run falls through to what
was installed before (T107 installs this AFTER `motion_runs.install`, so these wrappers are consulted first and a
still People node still reaches `motion_runs`). The life of one run:

    quote      The clip first: the node on the People node's `clip` input (else its one Motion or CropPair input)
               must have passed its gates and been approved, `kling_job.check_unlocked` semantics on that node:
               409 VIDEO_LOCKED naming it. Then the canvas rules upstream (422 with the rule's code), custody
               (STAFFAGE is grade D unless configured `local_only`: an NDA project gets `CustodyViolation`, 403,
               before anything is staged or read), the node's parameters (`people.parse_params`, 422) and the take:
               the clip node's accepted DONE run whose clip is in the CAS (its `outputHash` picks among several;
               409 VIDEO_LOCKED without one; 422 `previs_clip` for a previs take, which is never delivered, and
               `no_boxes` for a take that records no crop boxes). Priced as a still People take is, USD 0.10 per
               second, over the clip's length. `inputs` carries the clip (`clip`, `clip_run`), the plate, the still
               the clip was cut from, the ground point, the behaviour and the rest of the People parameters.
    submit     `people.plan_take` + `people.start_take` on the still the clip was cut from (custody, the leak scan,
               the egress PNG under an opaque name, the STAFFAGE job polled to its end), after the clip lock is
               checked again on the graph as it is now. The person layer STAFFAGE returns (plate and matte, video or
               stills) is copied into the CAS at once; that record is the provider job, so a daemon that dies
               afterwards never pays for a second take.
    normalize  For every frame t of the base clip: the person frame at the same time (the clip's time map; the
               last frame is held when the take is shorter); restored onto the still's grid (the size map, and the
               registration measured once on the first frame) and tone-matched on the ring around her; her alpha
               behind the packet's Z (`occluder_mask` at her depth) and the shadow measured on the first frame and
               reused; the crop box at t, linear between the pair's start and end boxes (constant for a locked
               pair); all of it mapped from the still into the clip through that box (a sub-pixel affine, so a
               push-in scales her by w0 / w_t, anti-aliased when the box shrinks the still) and composited over the
               base frame, whose every pixel outside her footprint is copied. Frames are piped as rawvideo into
               ffmpeg at the clip's fps, H.264 RGB at CRF 0 (`libx264rgb`, lossless; the first frame is read back and
               checked): the delivery codecs come later. Her matte goes along as a lossless clip of its own.
               `outputs = [clip]`, `normalized.timemap` is the base clip's, `normalized.people = {frames,
               matte_px_mean, ...}`.
    gate       The video gates on the composite with her footprint (alpha or shadow) dilated by 8 px masked OUT:
               landing against the base clip's own first and last frames (`video.first_frame`,
               `video.last_frame`), camera lock (`people.lock_delta_px`: the composite may not move the masked lock
               score by more than 0.5 px against the base clip, hard; on a locked clip the masked score is also
               `video.camera_lock_median_flow_px`, hard on RAFT, advisory on DIS), flicker (reported, with the base
               clip's), duration, output tags, `person.coverage`. Measured while compositing on the frames that were
               encoded, which the lossless intermediate makes the frames of the file.

Limits stated rather than hidden: the person stands at ONE depth (the node's, as for a still), so a take in which
she walks far toward or away from the camera is occluded at the placement's depth; her shadow is the first frame's.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from plate_gates import io as pio
from plate_gates import reprojection, video_gates
from plate_gates.composite import match_to_ring
from plate_gates.report import Check, GateReport
from plate_gates.timemap import TimeMap, as_fps, fps_text
from plate_pixels import ingress
from plate_pixels.register import warp_to_plate

from . import canvas_rules, custody, motion_runs
from .adapter import PollResult
from .cas import CAS
from .custody import CustodyViolation
from .db import tx
from .idempotency import canonical_json
from .machine import create_run, get_run, transition
from .models import QUOTE_TTL, Quote, Run, RunCreate
from .motion import kling_job, people
from .nodes.common import cas_has, plain
from .pipeline import LEDGER_KINDS, STAGING_KEY, PipelineError, decode_png16
from .quotes import insert_quote
from .states import TERMINAL, WORKER_STATES, Priority, RunState
from .worker import GateOutcome, cancel_run
from .worker import step as worker_step

__all__ = [
    "CLIP_HANDLE",
    "FLAVOUR",
    "LOCK_DELTA_ID",
    "LOCK_DELTA_MAX_PX",
    "MATTE_DILATE_PX",
    "Composited",
    "PeopleVideoRunner",
    "box_affine",
    "clip_source",
    "composite_clip",
    "gate_report",
    "install",
    "is_people_video_run",
    "wants_video",
]

log = logging.getLogger(__name__)
S = RunState

KIND = "People"
FLAVOUR = "people_video"
SPEC_KEY = motion_runs.SPEC_KEY  # runs.inputs[SPEC_KEY]: how to rebuild the job at execution
CLIP_HANDLE = "clip"
ENGINE_ID = motion_runs.PEOPLE_ENGINE_ID
USD_PER_S = motion_runs.PEOPLE_USD_PER_S
ETA_S = motion_runs.PEOPLE_ETA_S
TASK = motion_runs.PEOPLE_TASK
SHOT_CLASS = "people_video"
JOB_PREFIX = "pplv_"
RESULT_URL = "plate-people-video://"
MATTE_DILATE_PX = 8  # the person's footprint grown by this much is masked out of every video measure
LOCK_DELTA_ID = "people.lock_delta_px"
LOCK_DELTA_MAX_PX = 0.5
LOCK_SAMPLES = 8
LOCK_MIN_SHARE = 0.05  # a lock pair with less than 5% of the frame outside the footprint is not measured
LAYER_EPS = (
    1.0 / 1024.0
)  # alpha or shadow below this cannot move an 8-bit level: dropped, so the copy is exact
ENCODE = ("-c:v", "libx264rgb", "-crf", "0", "-preset", "veryfast", "-pix_fmt", "rgb24")
CLIP_EXT = "mp4"
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)
RESERVED_INPUTS = frozenset(
    {"clip", "clip_run", "clip_node", "plate", "still", "ground_point", "behaviour", "motion", "video"}
)


class _Locked(kling_job.MotionLocked):
    """The clip is not an approved take (409 VIDEO_LOCKED)."""


# ---------------------------------------------------------------------------- the graph


def wants_video(node: Mapping[str, Any], body: Mapping[str, Any] | None = None) -> bool:
    """A People node asked for a moving take: `params.video` is true on the node or in the quote body's params."""
    if node.get("kind") != KIND:
        return False
    params = node.get("params") if isinstance(node.get("params"), Mapping) else {}
    overlay = (body or {}).get("params") if isinstance((body or {}).get("params"), Mapping) else {}
    value = overlay.get("video", params.get("video"))
    return value is True


def is_people_video_run(run: Run) -> bool:
    spec = run.inputs.get(SPEC_KEY)
    staging = run.inputs.get(STAGING_KEY)
    return (
        isinstance(spec, Mapping)
        and spec.get("flavour") == FLAVOUR
        and isinstance(staging, Mapping)
        and staging.get("node_kind") == KIND
    )


def clip_source(g: Any, node_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(edge, source node) of the People node's clip: the edge on its `clip` input, else its one incoming edge from
    a Motion or CropPair node (a CropPair's port is typed as a still, so an unnamed edge from one lands on `image`).
    None when nothing is wired; 422 for two clips."""
    by_id = {n["id"]: n for n in g.nodes}
    edges = motion_runs.edges_by_input(g, node_id).get(CLIP_HANDLE) or [
        e
        for e in g.edges
        if e["target"] == node_id and (by_id.get(e["source"]) or {}).get("kind") in canvas_rules.VIDEO_KINDS
    ]
    if not edges:
        return None
    if len(edges) > 1:
        raise PipelineError(f"{node_id} has {len(edges)} clips; People in motion takes one", code="params")
    return edges[0], by_id[edges[0]["source"]]


def check_clip_unlocked(g: Any, node_id: str) -> dict[str, Any]:
    """The clip's node, or MotionLocked (409 VIDEO_LOCKED naming it) unless it has passed its gates and been
    approved: the lock `kling_job.check_unlocked` applies to a still, applied to the clip."""
    found = clip_source(g, node_id)
    if found is None:
        raise _Locked(
            f"People {node_id} has no clip: People in motion composites over an approved Motion or CropPair "
            "take wired to its clip input",
            node=node_id,
        )
    edge, src = found
    if src.get("kind") not in canvas_rules.VIDEO_KINDS:
        raise PipelineError(
            f"{src['id']} ({src.get('kind')}) is not a clip: the clip input takes a Motion or CropPair take",
            code="bad_clip",
            node=node_id,
            clip=src["id"],
        )
    if not (src.get("gateStatus") == "pass" and src.get("approved") is True):
        raise _Locked(
            f"people in motion is locked until the clip {src['id']} passes gates and is approved",
            node=node_id,
            clip=str(src["id"]),
            edge=plain(edge),
        )
    return src


def _has_note(conn: Any, run_id: str, kind: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM run_events WHERE run_id = ? AND kind = ? LIMIT 1", (run_id, kind)
    ).fetchone()
    return row is not None


def motion_record(run: Run) -> dict[str, Any]:
    """A start/end take's motion record: what NORMALIZING stored (`normalized.motion`) over what the quote stored
    (`inputs.motion`); both carry the crop boxes, the move and the time map."""
    quoted = run.inputs.get("motion") if isinstance(run.inputs.get("motion"), Mapping) else {}
    stored = (run.normalized or {}).get("motion")
    return {**quoted, **(stored if isinstance(stored, Mapping) else {})}


def _move_kind(record: Mapping[str, Any]) -> Any:
    move = record.get("move") if isinstance(record.get("move"), Mapping) else {}
    return move.get("move_kind") or record.get("move_kind")


def clip_take(conn: Any, src: Mapping[str, Any], cas: CAS) -> Run:
    """The clip node's accepted take: its newest DONE run (or GATE_FAIL with an override) that a human accepted
    and whose clip is an mp4 in this project's CAS; the node's `outputHash`, when it names one, picks it. A start/end
    (Kling) take only: 422 `previs_clip` for a previs take, `no_boxes` when it records no crop boxes."""
    sid = str(src["id"])
    want = src.get("outputHash") if isinstance(src.get("outputHash"), str) else None
    rows = conn.execute(
        "SELECT id FROM runs WHERE node_id = ? AND state IN (?, ?) ORDER BY created_utc DESC, rowid DESC",
        (sid, S.DONE.value, S.GATE_FAIL.value),
    ).fetchall()
    for (rid,) in rows:
        run = get_run(conn, rid)
        if run.state is S.GATE_FAIL and not _has_note(conn, rid, "override"):
            continue
        clip = run.outputs[0] if run.outputs else None
        path = cas.find(clip) if cas_has(cas, clip) else None
        if path is None or path.suffix.lower() != f".{CLIP_EXT}":
            continue
        if want and clip != want:
            continue
        if not _has_note(conn, rid, "accept"):
            continue
        if not motion_runs.is_motion_run(run):
            continue
        if motion_runs.flavour_of(run) == "previs":
            raise PipelineError(
                f"the clip {sid} is a previs take: PREVIS is watermarked and never delivered, so nobody is "
                "composited over it",
                code="previs_clip",
                clip=sid,
            )
        boxes = motion_record(run).get("boxes")
        if not (
            isinstance(boxes, list) and boxes and all(isinstance(b, list) and len(b) == 4 for b in boxes)
        ):
            raise PipelineError(
                f"the clip take {rid} of {sid} records no crop boxes", code="no_boxes", clip=sid
            )
        return run
    raise _Locked(
        f"the clip {sid} has no accepted take in this project's CAS: make it, scrub it and accept it first",
        clip=sid,
    )


# ---------------------------------------------------------------------------- geometry


def box_at(boxes: Sequence[Sequence[float]], s: float) -> tuple[float, float, float, float]:
    """The crop box (x0, y0, w, h) at the fraction `s` of the clip: linear between the start and end boxes."""
    b0 = [float(v) for v in boxes[0]]
    b1 = [float(v) for v in boxes[-1]]
    s = min(max(float(s), 0.0), 1.0)
    x, y, w, h = (a + (b - a) * s for a, b in zip(b0, b1, strict=True))
    return x, y, w, h


def box_affine(box: Sequence[float], width: int, height: int) -> np.ndarray:
    """2x3 float64: still pixel centres -> clip pixel centres, for a clip frame of `width` x `height` showing `box`
    (x0, y0, w, h in still pixels). The box is centre-cropped to the clip's aspect first, as `video.landing` reads
    a still against a clip, so an engine frame of a slightly different aspect lands on the same pixels."""
    x0, y0, bw, bh = (float(v) for v in box)
    if bw <= 0 or bh <= 0:
        raise PipelineError(f"crop box {list(box)} has no size", code="no_boxes")
    want = width / height
    if bw / bh > want:
        cw, ch = bh * want, bh
    else:
        cw, ch = bw, bw / want
    bx, by = x0 + (bw - cw) / 2.0, y0 + (bh - ch) / 2.0
    sx, sy = width / cw, height / ch
    return np.array([[sx, 0.0, (0.5 - bx) * sx - 0.5], [0.0, sy, (0.5 - by) * sy - 0.5]], np.float64)


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) exclusive of a bool mask, or None when it is empty."""
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return None
    cols = np.flatnonzero(mask.any(axis=0))
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


@dataclass
class _Person:
    """One person frame in still coordinates: RGB in [0, 1], alpha after occlusion, and its footprint's box."""

    rgb: np.ndarray
    alpha: np.ndarray
    box: tuple[int, int, int, int] | None


@dataclass
class _Placed:
    window: tuple[int, int, int, int] | None  # (u0, v0, u1, v1) in clip pixels
    footprint: np.ndarray | None  # bool, the window's pixels the composite changed or may change
    alpha: np.ndarray | None  # float32, the window's alpha


def place(frame: np.ndarray, person: _Person, shadow: np.ndarray, A: Any) -> _Placed:
    """Composite `person` (and the first frame's `shadow`) into the clip frame `frame` (uint8 RGB, in place) through
    the still -> clip affine `A`. Only the window her footprint maps to is touched; outside it, and outside her
    footprint inside it, the frame is not written."""
    if person.box is None:
        return _Placed(None, None, None)
    H, W = frame.shape[:2]
    h, w = person.alpha.shape
    sx, sy = float(A[0, 0]), float(A[1, 1])
    scale = min(sx, sy)
    sigma = (1.0 / scale - 1.0) / 2.0 if scale < 1.0 else 0.0
    sigma = sigma if sigma >= 0.3 else 0.0
    m = int(math.ceil(3.0 * sigma)) + 2
    x0, y0, x1, y1 = person.box
    xs0, ys0, xs1, ys1 = max(0, x0 - m), max(0, y0 - m), min(w, x1 + m), min(h, y1 + m)
    # the window: the source box's edges mapped into the clip, one pixel of slack, clipped to the frame
    ex0, ey0 = sx * (xs0 - 0.5) + A[0, 2] + 0.5, sy * (ys0 - 0.5) + A[1, 2] + 0.5
    ex1, ey1 = sx * (xs1 - 0.5) + A[0, 2] + 0.5, sy * (ys1 - 0.5) + A[1, 2] + 0.5
    u0, v0 = max(0, int(math.floor(ex0)) - 1), max(0, int(math.floor(ey0)) - 1)
    u1, v1 = min(W, int(math.ceil(ex1)) + 1), min(H, int(math.ceil(ey1)) + 1)
    if u1 <= u0 or v1 <= v0:
        return _Placed(None, None, None)
    rgb = person.rgb[ys0:ys1, xs0:xs1]
    layers = np.dstack([person.alpha[ys0:ys1, xs0:xs1], shadow[ys0:ys1, xs0:xs1]])
    if sigma > 0.0:  # the box shrinks the still: low-pass before the affine samples it
        rgb = cv2.GaussianBlur(rgb, (0, 0), sigma)
        layers = cv2.GaussianBlur(layers, (0, 0), sigma)
    M = np.array([[sx, 0.0, sx * xs0 + A[0, 2] - u0], [0.0, sy, sy * ys0 + A[1, 2] - v0]], np.float64)
    size = (u1 - u0, v1 - v0)
    warp = {"flags": cv2.INTER_LINEAR, "borderMode": cv2.BORDER_CONSTANT, "borderValue": 0}
    p = cv2.warpAffine(np.ascontiguousarray(rgb), M, size, **warp)
    ls = cv2.warpAffine(np.ascontiguousarray(layers), M, size, **warp)
    a = np.clip(ls[..., 0], 0.0, 1.0)
    s = np.clip(ls[..., 1], 0.0, people.SHADOW_CAP)
    a[a < LAYER_EPS] = 0.0
    s[s < LAYER_EPS] = 0.0
    fp = (a > 0.0) | (s > 0.0)
    if fp.any():
        win = frame[v0:v1, u0:u1]
        b = win.astype(np.float32) / 255.0
        out = b * (1.0 - s[..., None])
        out = out * (1.0 - a[..., None]) + np.clip(p, 0.0, 1.0) * a[..., None]
        win[fp] = np.clip(np.rint(out[fp] * 255.0), 0, 255).astype(np.uint8)
    return _Placed((u0, v0, u1, v1), fp, a)


# ---------------------------------------------------------------------------- clip IO


def _ffmpeg(tool: Any = None) -> Path:
    found = Path(tool) if tool else pio.ffmpeg()
    if found is None:
        raise PipelineError("ffmpeg is needed for People in motion (PLATE_FFMPEG)", code="no_ffmpeg")
    return found


def iter_rgb(path: Path, width: int, height: int, *, ffmpeg: Any = None) -> Iterator[np.ndarray]:
    """Every frame of a clip as uint8 RGB (H, W, 3), one at a time, through an ffmpeg rawvideo pipe."""
    cmd = [str(_ffmpeg(ffmpeg)), "-v", "error", "-nostdin", "-noautorotate", "-i", str(path)]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    size = width * height * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3).copy()
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


class ClipWriter:
    """uint8 RGB frames piped as rawvideo into ffmpeg: H.264 RGB at CRF 0 (lossless), +faststart. `close` raises
    with ffmpeg's own message when it failed."""

    def __init__(self, path: Path, width: int, height: int, fps: Any, *, ffmpeg: Any = None):
        self.path = Path(path)
        self.err = tempfile.TemporaryFile()
        cmd = [str(_ffmpeg(ffmpeg)), "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24"]
        cmd += ["-s", f"{width}x{height}", "-r", fps_text(as_fps(fps)), "-i", "-", *ENCODE]
        cmd += ["-movflags", "+faststart", str(self.path)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.err)
        self.frames = 0

    def write(self, rgb8: np.ndarray) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(np.ascontiguousarray(rgb8, np.uint8).tobytes())
        except (BrokenPipeError, OSError) as e:
            raise PipelineError(f"ffmpeg stopped taking frames: {self._message() or e}", code="encode") from e
        self.frames += 1

    def _message(self) -> str:
        self.err.seek(0)
        return self.err.read().decode("utf-8", "replace").strip()[:500]

    def close(self) -> Path:
        try:
            if self.proc.stdin is not None and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except OSError:
            pass
        code = self.proc.wait()
        message = self._message()
        self.err.close()
        if code != 0:
            raise PipelineError(f"ffmpeg failed writing {self.path.name}: {message}", code="encode")
        return self.path

    def abort(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()
        finally:
            self.err.close()


# ---------------------------------------------------------------------------- the composite


def _restorer(plan: people.TakePlan, reach: int) -> Any:
    """A function (plate layer, matte layer, warp) -> (RGB16, matte) onto the still's grid: the size map, the
    first frame's registration warp, the tone match on the ring around her (as a still People take)."""
    sm, base16 = plan.sizemap, plan.base16

    def restore(plate_layer: np.ndarray, matte_layer: np.ndarray, warp: np.ndarray | None) -> Any:
        matte = people.undo_sizemap(people.to_matte(matte_layer), sm)
        rgb = people.undo_sizemap(people.to_rgb(plate_layer), sm)
        rgb16 = rgb.astype(np.uint16) * np.uint16(257) if rgb.dtype == np.uint8 else rgb.astype(np.uint16)
        if warp is not None:
            rgb16 = warp_to_plate(np.ascontiguousarray(rgb16), warp)
            matte = warp_to_plate(np.ascontiguousarray(matte), warp, interpolation=cv2.INTER_LINEAR)
        matte = np.where(matte < people.MATTE_EPS, 0.0, np.clip(matte, 0.0, 1.0)).astype(np.float32)
        solid = matte > people.MATTE_EPS
        if solid.any():
            dist = cv2.distanceTransform((~solid).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
            rgb16 = match_to_ring(base16, rgb16, dist <= reach, ring_px=24)
        return rgb16, matte

    return restore


def _registration(plan: people.TakePlan, plate_layer: np.ndarray, matte_layer: np.ndarray, reach: int) -> Any:
    """The first person frame registered against the still on the ring outside her (`ingress.restore`): the
    report, and the warp every frame then gets (None when no warp is needed)."""
    matte0 = people.undo_sizemap(people.to_matte(matte_layer), plan.sizemap)
    solid = matte0 > people.MATTE_EPS
    if not solid.any():
        raise people.PeopleError(
            "STAFFAGE's matte is empty on the first frame: there is no person to place", code="empty_matte"
        )
    dist = cv2.distanceTransform((~solid).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    _, rep = ingress.restore(
        people.to_rgb(plate_layer), plan.sizemap, plan.base16, ring_mask=dist > reach + 8
    )
    warp = np.asarray(rep["registration"]["warp"], np.float32) if rep.get("warp_applied") else None
    return rep, warp


def _dilated(footprint: np.ndarray, window: tuple[int, int, int, int], shape: tuple[int, int]) -> np.ndarray:
    """The frame-sized footprint grown by MATTE_DILATE_PX (a disc), computed on the window plus the growth only."""
    H, W = shape
    out = np.zeros((H, W), bool)
    u0, v0, u1, v1 = window
    r = MATTE_DILATE_PX
    a0, b0, a1, b1 = max(0, u0 - r), max(0, v0 - r), min(W, u1 + r), min(H, v1 + r)
    sub = np.zeros((b1 - b0, a1 - a0), np.uint8)
    sub[v0 - b0 : v1 - b0, u0 - a0 : u1 - a0] = footprint
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    out[b0:b1, a0:a1] = cv2.dilate(sub, kernel) > 0
    return out


def _luma(rgb8: np.ndarray) -> np.ndarray:
    return rgb8.astype(np.float32) @ LUMA


def _mad(a: np.ndarray, b: np.ndarray, keep: np.ndarray) -> float | None:
    """Mean absolute difference (levels of 255, RGB) over the `keep` pixels, None when there are none."""
    if not keep.any():
        return None
    return float(np.abs(a[keep].astype(np.float32) - b[keep].astype(np.float32)).mean())


def _sample_frames(n: int, samples: int) -> list[int]:
    if n <= 0:
        return []
    if samples >= n:
        return list(range(n))
    return sorted({int(j) for j in np.round(np.linspace(0, n - 1, int(samples))).astype(int)})


def _work_size(width: int, height: int, limit: int) -> tuple[int, int]:
    w = min(int(limit), int(width))
    return w, max(int(round(height * w / width)), 1)


@dataclass
class Composited:
    """What `composite_clip` made and measured. Clip pixels throughout; `lock` holds the sampled frames' flow
    medians outside the dilated footprint, for the base clip and the composite."""

    clip: Path
    matte_clip: Path | None
    width: int
    height: int
    fps: Any
    frames: int
    person_frames: int
    person_fps: float | None
    held_frames: int
    matte_px: list[int]
    landing_first: float | None
    landing_last: float | None
    flicker: dict[str, Any]
    lock: dict[str, Any]
    outside_changed_max: float
    registration: dict[str, Any]
    shadow_px: int
    timemap: dict[str, Any] | None
    notes: list[str] = field(default_factory=list)

    @property
    def matte_px_mean(self) -> float:
        return float(np.mean(self.matte_px)) if self.matte_px else 0.0

    @property
    def coverage(self) -> float:
        return self.matte_px_mean / float(self.width * self.height) if self.matte_px else 0.0

    def people_record(self) -> dict[str, Any]:
        area = float(self.width * self.height)
        return {
            "frames": int(self.frames),
            "matte_px_mean": round(self.matte_px_mean, 3),
            "coverage": round(self.coverage, 6),
            "coverage_max": round(max(self.matte_px, default=0) / area, 6),
            "frames_with_person": int(sum(1 for n in self.matte_px if n > 0)),
            "person_frames": int(self.person_frames),
            "person_fps": self.person_fps,
            "held_frames": int(self.held_frames),
            "shadow_px": int(self.shadow_px),
        }


def _flicker(deltas: list[float]) -> dict[str, Any]:
    if not deltas:
        return {"mean_abs_luma_delta": 0.0, "p95": 0.0, "frames": len(deltas) + 1}
    d = np.asarray(deltas, np.float64)
    return {
        "mean_abs_luma_delta": float(d.mean()),
        "p95": float(np.percentile(d, 95)),
        "frames": int(d.size + 1),
    }


def _lock(
    samples: list[tuple[int, np.ndarray, np.ndarray, np.ndarray]], backend: Any, size: tuple[int, int]
) -> dict[str, Any]:
    """The masked camera lock of the base clip and of the composite on the same sampled frames, the same backend
    and the same footprint: flow from the first sample to each later one, median magnitude outside the union of
    the two frames' dilated footprints; the score is the largest median over the pairs (as
    `reprojection.camera_lock`)."""
    name = reprojection.backend_name(backend)
    per_pair: list[dict[str, Any]] = []
    if len(samples) < 2:
        return {
            "backend": name,
            "width": size[0],
            "height": size[1],
            "per_pair": [],
            "base": None,
            "composite": None,
        }
    j0, base0, comp0, mask0 = samples[0]
    floor = LOCK_MIN_SHARE * size[0] * size[1]
    for j, base, comp, mask in samples[1:]:
        keep = ~(mask0 | mask)
        rec: dict[str, Any] = {"engine_frame": int(j), "outside_px": int(keep.sum())}
        if keep.sum() < floor:
            rec.update(base=None, composite=None)
        else:
            for label, a, b in (("base", base0, base), ("composite", comp0, comp)):
                u, v = backend.flow(a, b)
                mag = np.hypot(np.asarray(u, np.float32), np.asarray(v, np.float32))
                rec[label] = float(np.median(mag[keep]))
        per_pair.append(rec)
    scores = {
        label: max((p[label] for p in per_pair if p.get(label) is not None), default=None)
        for label in ("base", "composite")
    }
    return {
        "backend": name,
        "backend_info": backend.info() if hasattr(backend, "info") else {"impl": name},
        "width": size[0],
        "height": size[1],
        "first_frame": int(j0),
        "per_pair": per_pair,
        **scores,
    }


def composite_clip(
    plan: people.TakePlan,
    base_clip: Path,
    person_video: Path,
    matte_video: Path,
    boxes: Sequence[Sequence[float]],
    out_path: Path,
    *,
    matte_out: Path | None = None,
    timemap: TimeMap | Mapping[str, Any] | None = None,
    flow_backend: Any = None,
    ffmpeg: Any = None,
    samples: int = LOCK_SAMPLES,
    lock_width: int = reprojection.EVAL_WIDTH,
) -> Composited:
    """Composite STAFFAGE's take (`person_video` + `matte_video`, in the coordinates of `plan.base16`, the still the
    clip was cut from) over every frame of `base_clip` through the crop `boxes` (start and end, still pixels), as
    the module docstring says, writing `out_path` (and her matte to `matte_out`). Measures the gate on the way."""
    base_clip, out_path = Path(base_clip), Path(out_path)
    params, base16 = plan.params, plan.base16
    h, w = plan.shape
    for b in (boxes[0], boxes[-1]):
        x0, y0, bw, bh = (float(v) for v in b)
        if x0 < -0.5 or y0 < -0.5 or x0 + bw > w + 0.5 or y0 + bh > h + 0.5:
            raise PipelineError(
                f"crop box {list(b)} lies outside the {w} x {h} still the clip was cut from", code="no_boxes"
            )
    info = pio.probe(base_clip)
    W, H, fps = int(info["width"]), int(info["height"]), info["fps"]
    n = int(info["nb_frames"])
    notes: list[str] = []
    tm = TimeMap.from_json(timemap) if isinstance(timemap, Mapping) else timemap
    if tm is not None and tm.engine_fps != fps:
        notes.append(
            f"time map: the clip is {fps_text(fps)} fps, the map says {fps_text(tm.engine_fps)}; the clip's rate is used"
        )
        tm = None

    def time_of(j: int) -> float:
        return tm.engine_time_s(j) if tm is not None else float(j / fps)

    reach = params.shadow_reach_px or people.shadow_reach_px(h)
    keep = people.occlusion_weight(plan.z_m, plan.person_depth_m)
    p_fps = people.layer_fps(Path(person_video))
    plates = people.read_layer_frames(Path(person_video))
    mattes = people.read_layer_frames(Path(matte_video))
    restore = _restorer(plan, reach)

    def person_of(rgb16: np.ndarray, matte: np.ndarray, shadow: np.ndarray) -> _Person:
        alpha = (matte * keep).astype(np.float32)
        return _Person(
            rgb=rgb16.astype(np.float32) / 65535.0,
            alpha=alpha,
            box=_bbox((alpha >= LAYER_EPS) | (shadow >= LAYER_EPS)),
        )

    writer: ClipWriter | None = None
    matte_writer: ClipWriter | None = None
    try:
        try:
            plate0, matte_layer0 = next(plates), next(mattes)
        except StopIteration:
            raise people.PeopleError("STAFFAGE's take has no frames", code="staffage_output") from None
        rep, warp = _registration(plan, plate0, matte_layer0, reach)
        if rep.get("camera_drift"):
            notes.append(f"registration: STAFFAGE moved the view by {rep['shift_px']:.1f} px (camera drift)")
        rgb0, matte0 = restore(plate0, matte_layer0, warp)
        shadow = np.zeros((h, w), np.float32)
        if params.shadow:
            shadow = people.shadow_layer(
                base16,
                rgb0,
                matte0,
                plan.z_m,
                plan.person_depth_m,
                foot_y=params.foot[1],
                cam=plan.cam,
                plane=plan.ground_plane,
                reach_px=reach,
            )
            shadow[shadow < LAYER_EPS] = 0.0
        person = person_of(rgb0, matte0, shadow)
        k_now, person_count, exhausted, held = 0, 1, False, 0

        writer = ClipWriter(out_path, W, H, fps, ffmpeg=ffmpeg)
        if matte_out is not None:
            matte_writer = ClipWriter(Path(matte_out), W, H, fps, ffmpeg=ffmpeg)
        backend = flow_backend
        if backend is None:
            from plate_pixels.flow import DisFlow  # the Mac / CI fallback; RAFT is chosen by the caller

            backend = DisFlow()
        picks = set(_sample_frames(n, samples))
        work = _work_size(W, H, lock_width)
        lock_samples: list[tuple[int, np.ndarray, np.ndarray, np.ndarray]] = []
        matte_px: list[int] = []
        flick_comp: list[float] = []
        flick_base: list[float] = []
        prev: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        first_frame: np.ndarray | None = None
        landing_first = landing_last = None
        changed_max = 0.0
        frames = 0
        for j, base8 in enumerate(iter_rgb(base_clip, W, H, ffmpeg=ffmpeg)):
            k = int(math.floor(time_of(j) * p_fps + 1e-6)) if p_fps else 0
            pending = None
            while k_now < k and not exhausted:
                try:
                    pending = (next(plates), next(mattes))
                except StopIteration:
                    exhausted = True
                    break
                k_now += 1
                person_count = max(person_count, k_now + 1)
            if pending is not None:
                person = person_of(*restore(pending[0], pending[1], warp), shadow)
            if k > k_now:
                held += 1  # the take is shorter than the clip: her last frame is held

            A = box_affine(box_at(boxes, j / (n - 1) if n > 1 else 0.0), W, H)
            frame = base8.copy()
            placed = place(frame, person, shadow, A)
            if placed.window is not None and placed.footprint is not None:
                masked = _dilated(placed.footprint, placed.window, (H, W))
                u0, v0, u1, v1 = placed.window
                matte_px.append(int((placed.alpha > people.MATTE_EPS).sum()))
            else:
                masked = np.zeros((H, W), bool)
                matte_px.append(0)
            outside = ~masked
            writer.write(frame)
            if matte_writer is not None:
                m8 = np.zeros((H, W), np.uint8)
                if placed.window is not None and placed.alpha is not None:
                    m8[v0:v1, u0:u1] = np.clip(np.rint(placed.alpha * 255.0), 0, 255).astype(np.uint8)
                matte_writer.write(np.repeat(m8[..., None], 3, axis=2))

            # the gate, measured on the frames as encoded
            moved = np.any(np.abs(frame.astype(np.int16) - base8.astype(np.int16)) > 1, axis=-1)
            changed_max = max(changed_max, float((moved & outside).sum()) / float(W * H))
            mad = _mad(frame, base8, outside)
            if j == 0:
                first_frame = frame.copy()
                landing_first = mad
            landing_last = mad
            yc, yb = _luma(frame), _luma(base8)
            if prev is not None:
                both = outside & prev[2]
                if both.any():
                    flick_comp.append(abs(float(yc[both].mean()) - float(prev[0][both].mean())))
                    flick_base.append(abs(float(yb[both].mean()) - float(prev[1][both].mean())))
            prev = (yc, yb, outside)
            if j in picks:
                small_mask = cv2.resize(masked.astype(np.uint8), work, interpolation=cv2.INTER_AREA) > 0
                lock_samples.append(
                    (
                        j,
                        cv2.resize(base8, work, interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0,
                        cv2.resize(frame, work, interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0,
                        small_mask,
                    )
                )
            frames += 1
        writer.close()
        writer = None
        if matte_writer is not None:
            matte_writer.close()
            matte_writer = None
    except BaseException:
        for wr in (writer, matte_writer):
            if wr is not None:
                wr.abort()
        raise
    finally:
        plates.close()
        mattes.close()

    if frames == 0 or first_frame is None:
        raise PipelineError(f"the base clip {base_clip.name} has no frames", code="no_clip")
    if frames != n:
        notes.append(f"the base clip decoded to {frames} frames, its container says {n}")
    back = pio.frame_at(out_path, 0.0)
    if back.shape != first_frame.shape or not np.array_equal(back, first_frame):
        raise PipelineError(
            "the intermediate clip is not lossless (its first frame does not read back as written); "
            "an ffmpeg with libx264rgb is needed",
            code="encode",
        )
    notes.append("composite encoded H.264 RGB CRF 0 (lossless; the first frame read back bit-exact)")
    if held:
        notes.append(f"the STAFFAGE take is shorter than the clip: its last frame is held for {held} frames")
    lock = _lock(lock_samples, backend, work)
    return Composited(
        clip=out_path,
        matte_clip=Path(matte_out) if matte_out is not None else None,
        width=W,
        height=H,
        fps=fps,
        frames=frames,
        person_frames=person_count,
        person_fps=p_fps,
        held_frames=held,
        matte_px=matte_px,
        landing_first=landing_first,
        landing_last=landing_last,
        flicker={"composite": _flicker(flick_comp), "base": _flicker(flick_base)},
        lock=lock,
        outside_changed_max=changed_max,
        registration=plain({k: v for k, v in rep.items() if k != "registration"}),
        shadow_px=int((shadow > 0).sum()),
        timemap=tm.to_json() if tm is not None else None,
        notes=notes,
    )


# ---------------------------------------------------------------------------- the gate


def gate_report(
    c: Composited,
    *,
    expect_locked: bool,
    shot_s: float | None,
    engine: Mapping[str, Any],
    tier: str | None = "A",
) -> dict[str, Any]:
    """The composite's video gate report (GateReport JSON), from what `composite_clip` measured."""
    info = pio.probe(c.clip)
    values: dict[str, float] = {}
    notes = [
        f"person footprint dilated by {MATTE_DILATE_PX} px is masked out of landing, camera lock and flicker",
        "landing is scored against the base clip's own first and last frames",
        *c.notes,
    ]
    if c.landing_first is not None:
        values["video.first_frame"] = c.landing_first
    if c.landing_last is not None:
        values["video.last_frame"] = c.landing_last
    lock = c.lock
    backend = str(lock.get("backend") or "dis")
    notes.append(f"flow backend {backend} @{lock.get('width')}")
    base_lock, comp_lock = lock.get("base"), lock.get("composite")
    if expect_locked:
        if comp_lock is not None:
            values[reprojection.LOCK_METRIC_ID] = float(comp_lock)
        else:
            notes.append("camera lock not measured: too little of the frame lies outside the person")
    else:
        notes.append(
            "camera lock not scored: the base clip's camera moves by design (crop pair); "
            f"{LOCK_DELTA_ID} still holds the composite to the base clip's own masked score"
        )
    targets: dict[str, float] = {}
    if shot_s is not None and shot_s > 0:
        values["video.duration_s"] = float(info["duration_s"])
        targets["video.duration_s"] = float(shot_s)
    delta = None if base_lock is None or comp_lock is None else abs(float(comp_lock) - float(base_lock))
    flicker = dict(c.flicker["composite"])
    flicker["base"] = c.flicker["base"]
    flicker["delta"] = abs(
        float(c.flicker["composite"]["mean_abs_luma_delta"]) - float(c.flicker["base"]["mean_abs_luma_delta"])
    )
    flicker["masked_px"] = MATTE_DILATE_PX
    extra: dict[str, Any] = {
        "landing": {
            "first": c.landing_first,
            "last": c.landing_last,
            "against": "base_clip",
            "masked_px": MATTE_DILATE_PX,
        },
        "camera_lock": {
            "metric_id": reprojection.LOCK_METRIC_ID,
            "median_flow_px": comp_lock,
            "base_median_flow_px": base_lock,
            "delta_px": delta,
            "masked_px": MATTE_DILATE_PX,
            **{k: v for k, v in lock.items() if k not in ("base", "composite")},
        },
        "flicker": flicker,
        "duration": {
            "duration_s": float(info["duration_s"]),
            "required_s": shot_s,
            "ok": None if shot_s is None else float(info["duration_s"]) + 1e-6 >= float(shot_s),
        },
        "output": video_gates.output_tags(info),
        "person.coverage": round(c.coverage, 6),
        "person": c.people_record(),
        "outside_matte": {"changed_fraction_max": c.outside_changed_max, "tolerance_lsb": 1},
        "registration": c.registration,
    }
    if c.timemap is not None:
        extra["timemap"] = c.timemap
    report = GateReport.score(
        values,
        tier=tier,
        engine=dict(engine),
        backend=backend,
        targets=targets,
        extra_metrics=extra,
        backend_notes=notes,
    )
    check = Check(
        metric_id=LOCK_DELTA_ID,
        value=delta,
        # unmeasured is a fail: a hard gate never passes on nothing
        status="fail" if delta is None or not delta <= LOCK_DELTA_MAX_PX else "pass",
        threshold=LOCK_DELTA_MAX_PX,
        op="le",
        unit="px",
    )
    data = report.model_dump(mode="json")
    data["checks"].append(check.model_dump(mode="json"))
    return GateReport.model_validate(data).model_dump(mode="json")


# ---------------------------------------------------------------------------- the engine (the worker's protocol)


class PeopleVideoEngine:
    """STAFFAGE as the provider of a People-in-motion run. `submit` runs the take to its end and keeps the person
    layer in the CAS with a JSON record that the job id names; poll and fetch read it back. Never idempotent: a
    lost submit is a STAFFAGE job that may exist, so recovery fails the run for a human."""

    stub = False
    idempotent_submit = False
    supports_lookup = False

    def __init__(self, runner: PeopleVideoRunner, run: Run, cas: CAS):
        self.runner, self.run, self.cas = runner, run, cas

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        blob = self.runner.take(self.run, self.cas)
        return f"{JOB_PREFIX}{self.cas.put_bytes(canonical_json(plain(blob)), 'json')}"

    def poll(self, provider_job_id: str) -> PollResult:
        key = provider_job_id.removeprefix(JOB_PREFIX)
        if not cas_has(self.cas, key):
            return PollResult("failed", detail=f"the People take {provider_job_id} is not in the CAS")
        return PollResult("succeeded", result_url=f"{RESULT_URL}{key}")

    def fetch(self, result_url: str) -> bytes:
        return self.cas.read_bytes(result_url.removeprefix(RESULT_URL))

    def cancel(self, provider_job_id: str) -> None:
        return None  # the STAFFAGE job had finished before its id was recorded; STAFFAGE has no cancel


class _NoEngine:
    def cancel(self, provider_job_id: str) -> None:
        raise NotImplementedError("no engine could be built for this run")


# ---------------------------------------------------------------------------- the runner


class PeopleVideoRunner:
    """Quotes and drives People-in-motion runs. `people_client` is the StaffageClient (or a factory of one) the
    takes talk to (None: the Motion runner's, else the default); `flow_backend` the lock gate's flow (None: DIS)."""

    def __init__(self, pipeline: Any):
        self.pipeline = pipeline
        self.people_client: Any = None
        self.staffage_settings: Any = None
        self.flow_backend: Any = None
        self.ffmpeg: Any = None
        self._guard = threading.Lock()
        self._engines: dict[str, Any] = {}

    # ------------------------------------------------------------------ graph

    def _graph_node(self, project_id: str, node_id: str) -> tuple[Any, Any, dict[str, Any]]:
        project = self.pipeline.load_project(project_id)
        if project is None:
            raise PipelineError(f"no project {project_id}", code="not_found", status=404)
        g = canvas_rules.as_graph(project.graph)
        node = next((n for n in g.nodes if n.get("id") == node_id), None)
        if node is None:
            raise PipelineError(f"project {project_id} has no node {node_id}", code="not_found", status=404)
        return project, g, node

    def handles(self, project_id: str, node_id: str, body: Mapping[str, Any] | None) -> bool:
        try:
            node = self._graph_node(project_id, node_id)[2]
        except (PipelineError, ValueError):
            return False
        return wants_video(node, body)

    def _settings(self) -> Any:
        motion = getattr(self.pipeline, "motion_runner", None)
        return (
            self.staffage_settings
            or getattr(motion, "staffage_settings", None)
            or people.StaffageSettings.load(self.pipeline.settings)
        )

    def _client(self) -> Any:
        motion = getattr(self.pipeline, "motion_runner", None)
        client = (
            self.people_client if self.people_client is not None else getattr(motion, "people_client", None)
        )
        return client() if callable(client) else client

    # ------------------------------------------------------------------ quote

    def quote(
        self,
        conn: Any,
        project_id: str,
        node_id: str,
        body: Mapping[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> tuple[Run, Quote]:
        """A People node with `params.video` -> a QUOTED run (see the module docstring for the refusals, all raised
        before a run row exists)."""
        pl = self.pipeline
        body = dict(body or {})
        now = now or pl.clock()
        project, g, node = self._graph_node(project_id, node_id)
        overlay = body.get("params") if isinstance(body.get("params"), Mapping) else {}
        node_params = node.get("params") if isinstance(node.get("params"), Mapping) else {}
        params = {**node_params, **overlay}
        src = check_clip_unlocked(g, node_id)  # the video lock before anything is read, routed or priced
        motion_runs.MotionRunner._upstream_ok(g, node_id)
        pv = self._plate(project_id, g, node_id, body)
        settings = self._settings()
        route = motion_runs.MotionRunner._people_route(settings)
        allowed = pl.allowed_grades(project_id, pv.manifest)
        custody.require_route(route, allowed, what="People in motion (STAFFAGE)")  # NDA -> 403, first
        snapshot = {"id": node_id, "kind": KIND, "params": plain(params)}
        try:
            parsed = people.parse_params(snapshot)
        except people.PeopleError as e:
            raise PipelineError(str(e), code=e.code, status=e.status, **plain(e.extra)) from e
        cas = pl.cas_for(project_id)
        take = clip_take(conn, src, cas)
        if take.packet_id != pv.packet_id:  # the boxes are in the clip's still: its packet decides
            pv = pl.packet(project_id, take.packet_id)
            custody.require_route(route, pl.allowed_grades(project_id, pv.manifest), what="People in motion")
        norm = take.normalized or {}
        motion = motion_record(take)
        take_spec = take.inputs.get(SPEC_KEY) or {}
        clip_key = take.outputs[0]
        duration = _number(motion.get("duration_s")) or _number((norm.get("timemap") or {}).get("duration_s"))
        duration = duration or float(pio.probe(cas.find(clip_key))["duration_s"])
        price = round(USD_PER_S * duration, 6)
        quote = Quote(
            provider=route["provider"],
            model=ENGINE_ID,
            model_version=route["model_version"],
            region=route["region"],
            custody_grade=custody.effective_grade(route).value,
            price_usd=price,
            eta_s=ETA_S,
            created_utc=now,
            expires_utc=now + QUOTE_TTL,
            route_id=route["route_id"],
            price_status="unconfirmed",
        )
        still_key = take_spec.get("still")
        spec = {
            "flavour": FLAVOUR,
            "kind": KIND,
            "node": snapshot,
            "clip": clip_key,
            "clip_run": take.id,
            "clip_node": str(src["id"]),
            "still": still_key,
            "boxes": [list(b) for b in motion["boxes"]],
            "timemap": norm.get("timemap"),
            "shot_s": _number(motion.get("shot_s")) or duration,
            "duration_s": duration,
            "move_kind": _move_kind(motion),
            "tier": pv.tier,
        }
        inputs: dict[str, Any] = {
            "clip": clip_key,
            "clip_run": take.id,
            "clip_node": str(src["id"]),
            "plate": pv.plate_hash,
            "still": still_key or pv.plate_hash,
            "ground_point": [parsed.foot[0], parsed.foot[1]],
            "behaviour": parsed.behavior,
            "motion": {"task": TASK, "shot_class": SHOT_CLASS},
            STAGING_KEY: {"node_kind": KIND, "motion": True, "video": True, "region_crop": False},
            SPEC_KEY: spec,
        }
        for k, v in params.items():  # the rest of the People parameters, under their own names
            if k not in inputs and k not in RESERVED_INPUTS:
                inputs[k] = v
        run_spec = RunCreate(
            project_id=project_id,
            packet_id=pv.packet_id,
            node_id=node_id,
            priority=Priority.parse(body.get("priority") or "interactive"),
            inputs=plain(inputs),
            params={"duration": duration, "video": True},
            route=route,
            attempt=motion_runs.MotionRunner._attempt(conn, node_id, body),
        )
        reason = (
            f"People in motion over STAFFAGE ({quote.custody_grade}) on clip {src['id']} "
            f"(take {take.id}), about {price:.4f} USD for a {duration:g} s take"
        )
        return self._create_quoted(conn, project_id, run_spec, quote, reason, now)

    def _plate(self, project_id: str, g: Any, node_id: str, body: Mapping[str, Any]) -> Any:
        plates = canvas_rules.upstream_plates(g, node_id)
        packet_id = body.get("packet_id") or next(
            (p.get("packetId") for p in plates if p.get("packetId")), None
        )
        if not isinstance(packet_id, str) or not packet_id:
            raise PipelineError(
                "generation must be anchored to a plate camera (source-view-only)", code="NO_PLATE_ANCHOR"
            )
        return self.pipeline.packet(project_id, packet_id)

    def _create_quoted(
        self, conn: Any, project_id: str, spec: RunCreate, quote: Quote, reason: str, now: datetime
    ) -> tuple[Run, Quote]:
        run = create_run(conn, spec, now=now)
        self.pipeline.index.remember(run.id, project_id)
        quote = quote.model_copy(update={"run_id": run.id})
        with tx(conn):
            insert_quote(conn, quote)
            run = transition(
                conn,
                run.id,
                S.QUOTED,
                actor="system",
                now=now,
                reason=reason[:500],
                quote_id=quote.id,
                quote_json=quote.terms_json(),
            )
        self._after(conn, project_id, run.id, S.DRAFT)
        return run, quote

    # ------------------------------------------------------------------ custody and the lock, again

    def allowed_for(self, run: Run) -> set[Any]:
        pl = self.pipeline
        return pl.allowed_grades(run.project_id, pl.packet(run.project_id, run.packet_id).manifest)

    def require_custody(self, run: Run) -> Any:
        return custody.require_route(
            run.route_dict, self.allowed_for(run), what="People in motion (STAFFAGE)"
        )

    def check_lock(self, run: Run) -> None:
        """The clip lock on the graph as it is now (409 VIDEO_LOCKED), and the quoted take still the node's pick."""
        project = self.pipeline.load_project(run.project_id)
        if project is None:
            raise PipelineError(f"no project {run.project_id}", code="not_found", status=404)
        src = check_clip_unlocked(canvas_rules.as_graph(project.graph), str(run.node_id))
        want = src.get("outputHash")
        if isinstance(want, str) and want and want != run.inputs.get("clip"):
            raise PipelineError(
                f"run {run.id}: the clip {src['id']} now shows another take; re-quote the node",
                code="inputs_changed",
            )

    # ------------------------------------------------------------------ execution

    def _still(self, run: Run, pv: Any, cas: CAS) -> np.ndarray:
        key = (run.inputs.get(SPEC_KEY) or {}).get("still")
        return decode_png16(cas.read_bytes(key)) if key else pv.plate16()

    def _plan(self, run: Run, cas: CAS) -> tuple[Any, Any, people.TakePlan]:
        pl = self.pipeline
        project = pl.load_project(run.project_id)
        if project is None:
            raise PipelineError(f"no project {run.project_id}", code="not_found", status=404)
        pv = pl.packet(run.project_id, run.packet_id)
        spec = run.inputs[SPEC_KEY]
        plan = people.plan_take(
            project,
            dict(spec["node"]),
            pv,
            self._still(run, pv, cas),
            settings=self._settings(),
            run_id=run.id,
        )
        return project, pv, plan

    def take(self, run: Run, cas: CAS) -> dict[str, Any]:
        """SUBMIT: the STAFFAGE take on the clip's still (custody and the clip lock re-checked first); its layers
        into the CAS."""
        from .outbound_log import OutboundLog

        self.require_custody(run)
        self.check_lock(run)
        if not cas_has(cas, run.inputs.get("clip")):
            raise PipelineError(f"run {run.id}: the clip is no longer in the CAS", code="inputs_changed")
        project, _pv, plan = self._plan(run, cas)
        job, staged = people.start_take(
            plan,
            project,
            client=self._client(),
            settings=self._settings(),
            log=OutboundLog.for_project(project.root),
            sleep=self.pipeline.sleep,
        )
        layers: dict[str, str] = {}
        for name, key in (("plate", "platePath"), ("matte", "mattePath")):
            path = Path(str(job[key]))
            if not path.is_file():
                raise people.PeopleError(
                    f"STAFFAGE reported {path.name}, which cannot be read from here", code="staffage_output"
                )
            layers[name] = cas.put_file(path, path.suffix.lstrip(".").lower() or "bin")
        return {
            "job": plain(job),
            "layers": layers,
            "sent_still": cas.put_bytes(plan.png, "png"),
            "staged": staged.name,
            "grade": plan.grade,
            "person_depth_m": round(plan.person_depth_m, 4),
            "depth_source": plan.depth_source,
            "ground_plane": None if plan.ground_plane is None else plan.ground_plane.to_dict(),
            "notes": list(plan.notes),
        }

    def _work_dir(self, run: Run) -> Path:
        return self.pipeline.settings.home / "cache" / "people_video" / run.id

    def normalize(self, run: Run, cas: CAS) -> dict[str, Any]:
        """NORMALIZING: the frame-by-frame composite and its gate, both into the CAS."""
        blob = json.loads(cas.read_bytes(run.result_key).decode("utf-8"))
        spec = run.inputs[SPEC_KEY]
        _project, pv, plan = self._plan(run, cas)
        if abs(plan.person_depth_m - float(blob.get("person_depth_m") or plan.person_depth_m)) > 1e-3:
            raise PipelineError(
                f"run {run.id}: the person's depth no longer matches the take; re-quote the node",
                code="inputs_changed",
            )
        layers = blob.get("layers") or {}
        paths = {k: cas.find(v) if cas_has(cas, v) else None for k, v in layers.items()}
        base = cas.find(spec["clip"]) if cas_has(cas, spec.get("clip")) else None
        if base is None or paths.get("plate") is None or paths.get("matte") is None:
            raise PipelineError("the clip or STAFFAGE's layers are not in the CAS", code="normalize_error")
        work = self._work_dir(run)
        work.mkdir(parents=True, exist_ok=True)
        try:
            c = composite_clip(
                plan,
                base,
                paths["plate"],
                paths["matte"],
                spec["boxes"],
                work / f"{run.id}.{CLIP_EXT}",
                matte_out=work / f"{run.id}.matte.{CLIP_EXT}",
                timemap=spec.get("timemap"),
                flow_backend=self.flow_backend,
                ffmpeg=self.ffmpeg,
            )
            report = gate_report(
                c,
                expect_locked=spec.get("move_kind") == kling_job.LOCKED_MOVE,
                shot_s=_number(spec.get("shot_s")),
                engine={"id": ENGINE_ID, "version": str((blob.get("job") or {}).get("model") or "") or None},
                tier=spec.get("tier") or pv.tier,
            )
            clip_key = cas.put_file(c.clip, CLIP_EXT)
            matte_key = cas.put_file(c.matte_clip, CLIP_EXT) if c.matte_clip is not None else None
        finally:
            shutil.rmtree(work, ignore_errors=True)
        record = {
            **c.people_record(),
            "job_id": (blob.get("job") or {}).get("id"),
            "model": (blob.get("job") or {}).get("model"),
            "grade": blob.get("grade"),
            "person_depth_m": blob.get("person_depth_m"),
            "depth_source": blob.get("depth_source"),
            "ground_plane": blob.get("ground_plane"),
            "clip_run": spec.get("clip_run"),
            "base_clip": spec.get("clip"),
            "notes": [*list(blob.get("notes") or []), *c.notes],
        }
        assets = {
            "base_clip": spec.get("clip"),
            "person_plate": layers.get("plate"),
            "person_matte": layers.get("matte"),
            "person_matte_clip": matte_key,
            "sent_still": blob.get("sent_still"),
        }
        return plain(
            {
                "outputs": [clip_key],
                "clip": clip_key,
                "media": "video",
                "timemap": spec.get("timemap") or c.timemap,
                "people": record,
                "assets": {k: v for k, v in assets.items() if v is not None},
                "gate_key": cas.put_bytes(canonical_json(plain(report)), "json"),
            }
        )

    def gate(self, run: Run, cas: CAS) -> GateOutcome:
        """GATING: the stored report, versioned in the project's gates/ folder. An unmeasured take never passes."""
        pl = self.pipeline
        norm = run.normalized or {}
        if norm.get("gate_key"):
            report = json.loads(cas.read_bytes(norm["gate_key"]).decode("utf-8"))
        else:
            report = {"verdict": "fail", "metrics": {}, "checks": [], "backend_notes": ["no gate report"]}
        project_dir = pl.project_dir(run.project_id)
        svc = pl.report_service(run.project_id)
        ref = None
        if svc is not None:
            path = svc.write(run.id, report)
            try:
                ref = Path(path).relative_to(project_dir).as_posix()
            except ValueError:
                ref = Path(path).as_posix()
        out = dict(report)
        out["landing"] = (report.get("metrics") or {}).get("landing")
        out["timemap"] = norm.get("timemap")
        return GateOutcome(passed=report.get("verdict") != "fail", report=out, report_path=ref)

    # ------------------------------------------------------------------ driving

    def engine_for(self, run: Run, cas: CAS | None = None) -> Any:
        with self._guard:
            hit = self._engines.get(run.id)
            if hit is None:
                hit = self._engines[run.id] = PeopleVideoEngine(
                    self, run, cas or self.pipeline.cas_for(run.project_id)
                )
        return hit

    def _forget(self, run_id: str) -> None:
        with self._guard:
            self._engines.pop(run_id, None)

    def step(self, conn: Any, project_id: str, run_id: str, cas: CAS | None = None) -> RunState:
        pl = self.pipeline
        cas = cas or pl.cas_for(project_id)
        run = get_run(conn, run_id)
        before = run.state
        if before in TERMINAL or before not in WORKER_STATES:
            return before
        try:
            engine = self.engine_for(run, cas)
        except (PipelineError, CustodyViolation) as e:  # no engine could be built: nothing was sent
            to = S.CANCELLED if before is S.QUEUED else S.FAILED
            code = getattr(e, "code", None) or "no_engine"
            transition(conn, run_id, to, actor="worker", reason=str(e)[:500], reason_code=code)
            self._after(conn, project_id, run_id, before)
            self._forget(run_id)
            return to
        after = worker_step(
            conn,
            engine,
            run_id,
            pl.clock,
            cas=cas,
            poll_timeout_s=pl.poll_timeout_s,
            normalize_fn=self.normalize,
            gate_fn=self.gate,
        )
        if after is not before:
            self._after(conn, project_id, run_id, before)
        if after in TERMINAL:
            self._forget(run_id)
        return after

    def cancel(
        self, conn: Any, project_id: str, run_id: str, *, actor: str = "human", reason: str | None = None
    ) -> Run:
        run = get_run(conn, run_id)
        before = run.state
        try:
            engine: Any = self.engine_for(run, self.pipeline.cas_for(project_id))
        except (PipelineError, CustodyViolation):
            engine = _NoEngine()
        out = cancel_run(conn, engine, run_id, actor=actor, reason=reason)
        self._after(conn, project_id, run_id, before)
        self._forget(run_id)
        return out

    # ------------------------------------------------------------------ bus and ledger

    def _ledger_data(self, run: Run) -> dict[str, Any]:
        motion = run.inputs.get("motion") if isinstance(run.inputs.get("motion"), Mapping) else {}
        data: dict[str, Any] = {
            "model": run.route_dict.get("model_version") or run.route.engine_id,
            "task": motion.get("task"),
            "shot_class": motion.get("shot_class"),
            "node_kind": KIND,
            "video": True,
            "clip_run": run.inputs.get("clip_run"),
        }
        return {k: v for k, v in data.items() if v is not None}

    def _after(self, conn: Any, project_id: str, run_id: str, before: RunState) -> None:
        pl = self.pipeline
        run = get_run(conn, run_id)
        event = {"kind": "run.state", "run_id": run_id, "from": before.value, "to": run.state.value}
        if run.reason_code:
            event["reason_code"] = run.reason_code
        pl.publish(project_id, run_id, event)
        kind = LEDGER_KINDS.get(run.state)
        if kind is not None:
            pl.ledger(project_id, kind, run, data=self._ledger_data(run))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


# ---------------------------------------------------------------------------- wiring into a Pipeline


def install(pipeline: Any) -> PeopleVideoRunner:
    """Let `pipeline` quote and drive People nodes with `params.video` (idempotent; the runner is kept at
    `pipeline.people_video`). Wraps `quote_node`, `approve` (custody and the clip lock again), `step`, `engine_for`
    and `cancel` on this instance and leaves every other node and run to what was there before. Install AFTER
    `motion_runs.install` (so a moving People node is consulted here first) and before the boot-time `resume`."""
    existing = getattr(pipeline, "people_video", None)
    if existing is not None:
        return existing
    runner = PeopleVideoRunner(pipeline)
    pipeline.people_video = runner
    orig_quote, orig_step, orig_engine_for = pipeline.quote_node, pipeline.step, pipeline.engine_for
    orig_approve, orig_cancel = pipeline.approve, pipeline.cancel

    def quote_node(
        conn: Any, project_id: str, node_id: str, body: Mapping[str, Any], *, now: datetime | None = None
    ) -> Any:
        if runner.handles(project_id, node_id, body):
            return runner.quote(conn, project_id, node_id, body, now=now)
        return orig_quote(conn, project_id, node_id, body, now=now)

    def approve(
        conn: Any,
        project_id: str,
        run_id: str,
        by: str,
        envelope_id: str | None = None,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> list[Run]:
        run = get_run(conn, run_id)
        if is_people_video_run(run):
            runner.require_custody(run)  # the project's custody may have tightened since the quote
            runner.check_lock(run)
        return orig_approve(conn, project_id, run_id, by, envelope_id, reason=reason, now=now)

    def step(conn: Any, project_id: str, run_id: str, cas: CAS | None = None) -> RunState:
        if is_people_video_run(get_run(conn, run_id)):
            return runner.step(conn, project_id, run_id, cas)
        return orig_step(conn, project_id, run_id, cas)

    def engine_for(run: Run, cas: CAS) -> Any:
        return runner.engine_for(run, cas) if is_people_video_run(run) else orig_engine_for(run, cas)

    def cancel(
        conn: Any, project_id: str, run_id: str, *, actor: str = "human", reason: str | None = None
    ) -> Run:
        if is_people_video_run(get_run(conn, run_id)):
            return runner.cancel(conn, project_id, run_id, actor=actor, reason=reason)
        return orig_cancel(conn, project_id, run_id, actor=actor, reason=reason)

    pipeline.quote_node, pipeline.approve, pipeline.step = quote_node, approve, step
    pipeline.engine_for, pipeline.cancel = engine_for, cancel
    return runner
