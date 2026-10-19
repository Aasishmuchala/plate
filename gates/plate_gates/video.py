"""Video gates: landing on the start / end stills, camera lock by phase correlation, region motion, flicker,
duration, plus the OpenCV plumbing the lock gates run on.

Consolidates the ffprobe/ffmpeg shell-outs that clip_gate.py, pushin_gate.py and tools_v4_gate.py each repeated
(seeds: couple-dining/clip_gate.py, couple-dining/pushin_gate.py, car-entry/tools_v4_gate.py in the historical
project folders).

Two decode paths, on purpose:
* probe / frame_at / write_test_clip here use OpenCV, so the lock and motion gates (and their tests) need no
  ffmpeg binary;
* landing, flicker and duration_check use io.probe / io.frame_at (ffprobe + ffmpeg pipes), which read every
  codec a delivered clip can carry (AV1, HEVC, ProRes) and report an exact rational frame rate.

Thresholds live in thresholds.yaml: video.first_frame <= 12 and video.last_frame <= 16 (levels of 255,
calibration_needed), video.phase_corr_px <= 2 (advisory), video.duration_s >= shot (hard). The flow-based lock
(RAFT on the box, DIS on the Mac) is T29's; the phase-correlation lock here is triage.
"""

from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import io as pio
from . import thresholds
from .io import ffmpeg, ffprobe, find_tool, to_uint8
from .register import phase_shift

__all__ = [
    "find_tool",
    "ffmpeg",
    "ffprobe",
    "probe",
    "frame_at",
    "write_test_clip",
    "landing",
    "landing_legacy",
    "camera_lock_phasecorr",
    "region_motion",
    "duration_check",
    "flicker",
]

Box = tuple[int, int, int, int]  # (x, y, w, h) in native pixels

TEXTURELESS_STD = 2.0  # luma levels: a region flatter than this cannot carry a phase-correlation peak


# --- OpenCV plumbing ---------------------------------------------------------------------------------------


def _has_audio(clip: Path) -> bool | None:
    tool = ffprobe()
    if tool is None:
        return None
    cmd = [str(tool), "-v", "error", "-select_streams", "a:0"]
    cmd += ["-show_entries", "stream=codec_name", "-of", "csv=p=0", str(clip)]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    if res.returncode != 0:
        return None
    return bool(res.stdout.strip())


def _open(clip: Path) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(str(clip))
    if not cap.isOpened():
        cap.release()
        raise FileNotFoundError(f"cannot open video: {clip}")
    return cap


def _count_frames(clip: Path) -> int:
    cap = _open(clip)
    n = 0
    while cap.grab():
        n += 1
    cap.release()
    return n


def probe(clip: Path) -> dict:
    """Return width, height, fps, duration_s, nb_frames and has_audio (None when ffprobe is absent). OpenCV path."""
    clip = Path(clip)
    cap = _open(clip)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    nb = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    if nb <= 0:  # some containers do not report a count: walk the stream
        nb = _count_frames(clip)
    duration = nb / fps if fps > 0 else 0.0
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration_s": duration,
        "nb_frames": nb,
        "has_audio": _has_audio(clip),
    }


def frame_at(clip: Path, t_s: float, *, info: dict | None = None) -> np.ndarray:
    """RGB uint8 frame at time t_s (frame index = round(t*fps), clamped to the last frame). OpenCV path.

    Pass `info` (a probe result) when grabbing several frames from one clip to skip the repeated probe.
    """
    clip = Path(clip)
    info = info or probe(clip)
    last = max(info["nb_frames"] - 1, 0)
    idx = min(max(int(round(t_s * info["fps"])), 0), last)
    cap = _open(clip)
    try:
        # Step back from the target if the container over-reports the count or the seek lands past EOF.
        for target in range(idx, -1, -1):
            cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, bgr = cap.read()
            if ok and bgr is not None:
                return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        raise ValueError(f"no decodable frame in {clip}")
    finally:
        cap.release()


def write_test_clip(path: Path, frames: list[np.ndarray], fps: float = 24) -> Path:
    """Write RGB uint8 frames to an mp4v file (OpenCV only; no ffmpeg binary needed). Returns the path."""
    if not frames:
        raise ValueError("write_test_clip needs at least one frame")
    path = Path(path)
    h, w = frames[0].shape[:2]
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (w, h))
    if not writer.isOpened():
        raise RuntimeError(f"cannot open VideoWriter for {path}")
    try:
        for f in frames:
            if f.shape[:2] != (h, w):
                raise ValueError("all frames must share one size")
            writer.write(cv2.cvtColor(np.ascontiguousarray(f), cv2.COLOR_RGB2BGR))
    finally:
        writer.release()
    return path


# --- landing -----------------------------------------------------------------------------------------------


def _still_rgb8(src) -> np.ndarray:
    """Path, PIL image or array (uint8 / uint16 / float 0..1) -> RGB uint8 (H, W, 3)."""
    if isinstance(src, (str, Path)):
        return to_uint8(pio.load_rgb(src))
    if isinstance(src, Image.Image):
        return np.asarray(src.convert("RGB"))
    return to_uint8(np.asarray(src))


def _fit_still(still: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Centre-crop `still` to the aspect of `size` = (w, h), then Lanczos to exactly that size (never squash)."""
    w, h = size
    sh, sw = still.shape[:2]
    want = w / h
    if sw / sh > want:  # still is wider than the clip: trim the sides
        cw, ch = int(round(sh * want)), sh
    else:  # taller (or equal): trim top and bottom
        cw, ch = sw, int(round(sw / want))
    x0, y0 = (sw - cw) // 2, (sh - ch) // 2
    im = Image.fromarray(still).resize((w, h), Image.LANCZOS, box=(x0, y0, x0 + cw, y0 + ch))
    return np.asarray(im)


def _mad(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean())


def _last_frame(v: Path, info: dict) -> np.ndarray:
    """The stream's last frame: -ss at duration - 1.5 frames lands on the first timestamp >= that, i.e. the last."""
    return pio.frame_at(v, max(info["duration_s"] - 1.5 / float(info["fps"]), 0.0), info=info)


def landing(video: str | Path, start_still, end_still=None) -> dict:
    """{first, last, last_vs_start}: mean absolute difference (levels of 255, RGB) between the clip's first
    frame and the start still, its last frame and the end still, and its last frame and the start still.

    Aspect-correct: each still is centre-cropped to the clip's aspect and then Lanczos-resized to the clip's
    size, so a 3856x2148 Kling clip against 3840x2160 stills carries no squash bias. `last_vs_start` should be
    much larger than `last` for a real push-in. end_still=None gives last = None. Stills may be paths, PIL
    images or arrays. Thresholds: video.first_frame <= 12, video.last_frame <= 16 (calibration_needed).
    """
    v = Path(video)
    info = pio.probe(v)
    size = (info["width"], info["height"])
    first = pio.frame_at(v, 0.0, info=info)
    last = _last_frame(v, info)
    start = _fit_still(_still_rgb8(start_still), size)
    out = {
        "first": _mad(first, start),
        "last": None,
        "last_vs_start": _mad(last, start),
    }
    if end_still is not None:
        out["last"] = _mad(last, _fit_still(_still_rgb8(end_still), size))
    return out


def landing_legacy(video: str | Path, start_still, end_still) -> dict:
    """The pushin_gate.py method kept for reproducing recorded numbers: frames at t=0 and duration-0.05 read
    through ffmpeg, stills squashed (Lanczos) to the clip size ignoring aspect. Returns {first, last,
    last_vs_start} in levels of 255. On a clip whose aspect matches the stills it equals landing(); it
    differs only by the squash bias on a mismatched aspect. Not for new gates: use landing."""
    v = Path(video)
    info = pio.probe(v)
    f0 = pio.frame_at(v, 0.0, info=info)
    try:
        f1 = pio.frame_at(v, max(info["duration_s"] - 0.05, 0.0), info=info)
    except ValueError:  # low frame rates: the last frame starts before duration - 0.05
        f1 = _last_frame(v, info)
    size = (f0.shape[1], f0.shape[0])
    s = np.asarray(Image.fromarray(_still_rgb8(start_still)).resize(size, Image.LANCZOS))
    e = np.asarray(Image.fromarray(_still_rgb8(end_still)).resize(size, Image.LANCZOS))
    return {"first": _mad(f0, s), "last": _mad(f1, e), "last_vs_start": _mad(f1, s)}


# --- camera lock and region motion ---------------------------------------------------------------------------


def _luma(clip: Path, t_s: float, info: dict) -> np.ndarray:
    return cv2.cvtColor(frame_at(clip, t_s, info=info), cv2.COLOR_RGB2GRAY).astype(np.float32)


def _crop(luma: np.ndarray, box: Box) -> np.ndarray:
    x, y, w, h = (int(v) for v in box)
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > luma.shape[1] or y + h > luma.shape[0]:
        raise ValueError(f"region {box} lies outside the {luma.shape[1]}x{luma.shape[0]} frame")
    return luma[y : y + h, x : x + w]


def _lock(clip: Path, regions: list[Box], times: list[float], tol_px: float) -> dict:
    if not regions:
        raise ValueError("camera lock needs at least one region")
    info = probe(clip)
    lumas = [_luma(clip, t, info) for t in times]
    out = []
    moved_any = False
    usable = 0
    for box in regions:
        ref = _crop(lumas[0], box)
        textured = float(ref.std()) >= TEXTURELESS_STD
        worst, dx, dy, mad = 0.0, 0, 0, 0.0
        for g in lumas[1:]:
            cur = _crop(g, box)
            if textured:
                dx, dy = phase_shift(ref, cur)
                worst = max(worst, abs(dx), abs(dy))
            mad = float(np.abs(ref - cur).mean() / 255.0)
        moved = textured and worst > tol_px
        moved_any |= moved
        usable += bool(textured)
        out.append(
            {
                "box": tuple(int(v) for v in box),
                "dx": dx,
                "dy": dy,
                "mad": mad,
                "max_shift_px": worst,
                "moved": bool(moved),
                "textured": textured,
            }
        )
    verdict = "unknown" if usable == 0 else ("moved" if moved_any else "locked")
    return {
        "regions": out,
        "verdict": verdict,
        "duration_s": info["duration_s"],
        "samples": len(times),
        "backend": "phasecorr",
    }


def camera_lock_phasecorr(
    clip: Path,
    regions: list[Box],
    samples: int = 8,
    tol_px: float = 2,
    t0: float = 0.0,
    t1: float | None = None,
) -> dict:
    """Phase-correlate each static region at `samples` times between t0 and t1 (default: 0.05 s before the end)
    against the frame at t0.

    verdict is "moved" when ANY textured region shifts by more than tol_px (either axis, any sample): one
    region that moves means the camera moved (the old clip_gate rule demanded ALL regions move, which let a
    pan slip through whenever a single region held still). "locked" when none do; "unknown" when every region
    is too flat to correlate. Per region: dx, dy at the last sample (a -> b sign convention of
    register.phase_shift), max_shift_px over all samples, mad (mean abs luma diff /255 at the last sample).
    Triage only (video.phase_corr_px is advisory): the hard lock gate is the flow-based one.
    """
    clip = Path(clip)
    if samples < 2:
        raise ValueError("samples must be >= 2")
    if t1 is None:
        t1 = max(probe(clip)["duration_s"] - 0.05, 0.0)
    times = [t0 + (t1 - t0) * i / (samples - 1) for i in range(samples)]
    return _lock(clip, list(regions), times, tol_px)


def region_motion(clip: Path, regions: dict[str, Box], times: list[float]) -> dict[str, list[float]]:
    """Mean abs luma diff /255 of each region at times[1:] against times[0] (list has len(times)-1 entries)."""
    clip = Path(clip)
    if len(times) < 2:
        raise ValueError("region_motion needs at least two times")
    info = probe(clip)
    base = _luma(clip, times[0], info)
    later = [_luma(clip, t, info) for t in times[1:]]
    out: dict[str, list[float]] = {}
    for name, box in regions.items():
        ref = _crop(base, box)
        out[name] = [float(np.abs(_crop(g, box) - ref).mean() / 255.0) for g in later]
    return out


# --- duration and flicker ------------------------------------------------------------------------------------


def duration_check(video: str | Path, min_duration_s: float) -> dict:
    """{duration_s, required_s, ok, fps, nb_frames}: ok when the clip is at least the shot's length
    (video.duration_s, hard). Duration comes from ffprobe (container, else frames / fps)."""
    info = pio.probe(Path(video))
    status, _ = thresholds.check("video.duration_s", info["duration_s"], target=min_duration_s)
    fps: Fraction = info["fps"]
    return {
        "duration_s": info["duration_s"],
        "required_s": float(min_duration_s),
        "ok": status == "pass",
        "fps": f"{fps.numerator}/{fps.denominator}",
        "nb_frames": info["nb_frames"],
    }


def flicker(video: str | Path, width: int = 480) -> dict:
    """{mean_abs_luma_delta, p95, frames, status}: frame-to-frame change of the mean luma (levels of 255).

    Every frame is decoded once through ffmpeg at `width` wide. A steady clip sits near 0; exposure pumping from
    a generative model shows as a high mean and a fat p95. A clip of fewer than two frames has no frame-to-frame
    change to measure (BUILD_PLAN decision 90): both values are None and status is "unscored", never a 0.0 that
    would read as a perfectly steady clip. Otherwise status is "scored".
    """
    means = np.array([float(g.mean()) for g in pio.iter_gray_frames(Path(video), width=width)])
    if len(means) < 2:
        return {"mean_abs_luma_delta": None, "p95": None, "frames": int(len(means)), "status": "unscored"}
    d = np.abs(np.diff(means))
    return {
        "mean_abs_luma_delta": float(d.mean()),
        "p95": float(np.percentile(d, 95)),
        "frames": int(len(means)),
        "status": "scored",
    }
