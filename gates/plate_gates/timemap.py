"""Frame-rate time map between a motion packet and an engine clip, and the camera at a fractional packet frame.

A motion packet is rendered at the scene's rate (Max 25 or 30 fps); the engines deliver 24 fps (Seedance, Kling)
or whatever their adapter reports. PACKET.md section 10 keeps the map OUT of the packet and records it on every
motion job, so the same packet can feed several engines. The map is exact rational arithmetic (`Fraction`):

    packet_frame_for(j) = start + j * packet_fps / engine_fps        (fractional packet frame k)
    engine_frame_for(k) = (k - start) * engine_fps / packet_fps
    duration_s          = (end - start + 1) / packet_fps
    engine_frames       = round_half_up(duration_s * engine_fps)

`method` says how a packet FRAME (Z, ids, clay) is chosen for an engine frame: `nearest` (the only method in
v0) takes the packet frame closest to k. The CAMERA is never snapped: `camera_at` interpolates the pose at the
fractional k (translation lerp, rotation slerp, focal and focus lerp), which is what lets the reprojection gate
(reprojection.py) warp the nearest frame's G-buffer edges by the sub-frame camera delta instead of accepting
up to half a frame of built-in drift. 25 -> 24 over 125 frames gives 120 engine frames and
packet_frame_for(119) = 123.9583 (the last engine frame sits 0.04 frame before the last packet frame).

Engine frames past the packet's last frame (a Kling clip is ceil(shot_s) seconds long) clamp to `end` and are
reported as such by the gate; `engine_frames` is the nominal count that covers the shot, not the clip length.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

import numpy as np

__all__ = [
    "TIMEMAP_VERSION",
    "TimeMap",
    "as_fps",
    "fps_text",
    "interpolate_pose",
    "camera_at",
    "intrinsics_for",
]

TIMEMAP_VERSION = "timemap-v0"
METHODS = ("nearest",)


def as_fps(value: Any) -> Fraction:
    """Fraction from an int, a Fraction, '24000/1001', '23.976' or a float (limit_denominator 100000, so
    23.976 -> 2997/125 and 29.97 -> 2997/100 survive; a float 24.0 is exactly 24)."""
    if isinstance(value, Fraction):
        f = value
    elif isinstance(value, bool):
        raise TypeError("fps cannot be a bool")
    elif isinstance(value, (int, np.integer)):
        f = Fraction(int(value))
    elif isinstance(value, str):
        f = Fraction(value.strip())
    elif isinstance(value, float):
        f = Fraction(value).limit_denominator(100000)
    else:
        raise TypeError(f"cannot read a frame rate from {value!r}")
    if f <= 0:
        raise ValueError(f"fps must be positive, got {value!r}")
    return f


def fps_text(f: Fraction) -> str:
    """'24/1' style text (what ffprobe prints and io.probe parses back with Fraction)."""
    return f"{f.numerator}/{f.denominator}"


def _round_half_up(x: Fraction) -> int:
    return int(x + Fraction(1, 2)) if x >= 0 else -int(-x + Fraction(1, 2))


@dataclass
class TimeMap:
    """packet_fps, engine_fps (Fractions), packet_frames = (start, end) inclusive, engine_frames (nominal count
    covering the shot), duration_s (Fraction, exact), method ('nearest'). Recorded on every motion job."""

    packet_fps: Fraction
    engine_fps: Fraction
    packet_frames: tuple[int, int]
    engine_frames: int | None = None
    duration_s: Fraction | None = None
    method: str = "nearest"
    version: str = field(default=TIMEMAP_VERSION)

    def __post_init__(self) -> None:
        self.packet_fps = as_fps(self.packet_fps)
        self.engine_fps = as_fps(self.engine_fps)
        start, end = (int(self.packet_frames[0]), int(self.packet_frames[1]))
        if end < start:
            raise ValueError(f"packet_frames end {end} < start {start}")
        self.packet_frames = (start, end)
        if self.method not in METHODS:
            raise ValueError(f"method must be one of {METHODS}, not {self.method!r}")
        n = Fraction(end - start + 1)
        if self.duration_s is None:
            self.duration_s = n / self.packet_fps
        else:
            self.duration_s = Fraction(self.duration_s).limit_denominator(1_000_000)
        if self.engine_frames is None:
            self.engine_frames = max(_round_half_up(self.duration_s * self.engine_fps), 1)
        else:
            self.engine_frames = int(self.engine_frames)
            if self.engine_frames < 1:
                raise ValueError("engine_frames must be >= 1")

    # --- the map ---------------------------------------------------------------------------------------

    @property
    def start(self) -> int:
        return self.packet_frames[0]

    @property
    def end(self) -> int:
        return self.packet_frames[1]

    @property
    def n_packet_frames(self) -> int:
        return self.end - self.start + 1

    @property
    def ratio(self) -> Fraction:
        """packet frames per engine frame."""
        return self.packet_fps / self.engine_fps

    def packet_frame_exact(self, engine_frame: int) -> Fraction:
        return self.start + int(engine_frame) * self.ratio

    def packet_frame_for(self, engine_frame: int) -> float:
        """Fractional packet frame k shown at engine frame j: start + j * packet_fps / engine_fps (unclamped)."""
        return float(self.packet_frame_exact(engine_frame))

    def engine_frame_for(self, packet_frame: float) -> float:
        """Fractional engine frame at packet frame k: (k - start) * engine_fps / packet_fps (unclamped)."""
        k = Fraction(packet_frame).limit_denominator(1_000_000)
        return float((k - self.start) / self.ratio)

    def clamp(self, packet_frame: float) -> tuple[float, bool]:
        """(k clamped to [start, end], clamped?)."""
        k = float(packet_frame)
        if k < self.start:
            return float(self.start), True
        if k > self.end:
            return float(self.end), True
        return k, False

    def nearest_packet_frame(self, engine_frame: int) -> int:
        """The packet frame whose Z / ids / clay stand in for engine frame j (method 'nearest', clamped)."""
        k = self.packet_frame_exact(engine_frame)
        n = _round_half_up(k)
        return min(max(n, self.start), self.end)

    def engine_time_s(self, engine_frame: int) -> float:
        return float(Fraction(int(engine_frame)) / self.engine_fps)

    def packet_time_s(self, packet_frame: float) -> float:
        k = Fraction(packet_frame).limit_denominator(1_000_000)
        return float((k - self.start) / self.packet_fps)

    def covers(self, engine_frame: int) -> bool:
        """True when engine frame j lies inside the packet's frame range (no clamping needed)."""
        return self.start <= self.packet_frame_exact(engine_frame) <= self.end

    # --- serialisation -----------------------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """Plain JSON data for the run record: fps as 'num/den' text, exact duration as text plus a float."""
        assert self.duration_s is not None and self.engine_frames is not None
        return {
            "version": self.version,
            "packet_fps": fps_text(self.packet_fps),
            "engine_fps": fps_text(self.engine_fps),
            "packet_frames": [self.start, self.end],
            "engine_frames": int(self.engine_frames),
            "duration_s": float(self.duration_s),
            "duration": fps_text(self.duration_s),
            "method": self.method,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> TimeMap:
        frames = data["packet_frames"]
        duration = data.get("duration")
        return cls(
            packet_fps=as_fps(data["packet_fps"]),
            engine_fps=as_fps(data["engine_fps"]),
            packet_frames=(int(frames[0]), int(frames[1])),
            engine_frames=int(data["engine_frames"]) if data.get("engine_frames") is not None else None,
            duration_s=Fraction(duration) if duration else None,
            method=str(data.get("method", "nearest")),
            version=str(data.get("version", TIMEMAP_VERSION)),
        )

    def describe(self) -> str:
        """One line for backend_notes: 'time map 25/1 -> 24/1 (nearest), packet 0..11, 12 engine frames'."""
        return (
            f"time map {fps_text(self.packet_fps)} -> {fps_text(self.engine_fps)} ({self.method}), "
            f"packet {self.start}..{self.end}, {self.engine_frames} engine frames"
        )


# --------------------------------------------------------------------------- camera interpolation


def _frames_list(camera_frames: Any) -> list[Mapping[str, Any]]:
    """Accept the camera_frames.json document ({frames: [...]}) or the bare list; sorted by frame number."""
    if isinstance(camera_frames, Mapping):
        frames = camera_frames["frames"]
    else:
        frames = camera_frames
    out = sorted((f for f in frames), key=lambda f: int(f["frame"]))
    if not out:
        raise ValueError("camera_frames holds no frames")
    return out


def _bracket(frames: Sequence[Mapping[str, Any]], k: float) -> tuple[Mapping[str, Any], Mapping[str, Any], float]:
    """(lo, hi, t): the keyed frames around k and the blend weight toward hi; clamped at both ends."""
    numbers = [int(f["frame"]) for f in frames]
    if k <= numbers[0]:
        return frames[0], frames[0], 0.0
    if k >= numbers[-1]:
        return frames[-1], frames[-1], 0.0
    hi_idx = int(np.searchsorted(np.asarray(numbers), k, side="right"))
    lo_idx = hi_idx - 1
    lo, hi = frames[lo_idx], frames[hi_idx]
    span = numbers[hi_idx] - numbers[lo_idx]
    t = (float(k) - numbers[lo_idx]) / span if span else 0.0
    return lo, hi, float(t)


def _lerp(a: float | None, b: float | None, t: float) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return float(a) * (1.0 - t) + float(b) * t


def interpolate_pose(camera_frames: Any, k: float) -> dict[str, Any]:
    """Pose at fractional packet frame k: translation lerp, rotation slerp (scipy Slerp on the rotation blocks),
    focal_length_mm / focus_distance lerp when the keyed frames carry them.

    Returns {frame: k, world_from_camera: 4x4 row-major list (plate-cam-v0), lo, hi, t, clamped} plus
    focal_length_mm / focus_distance when present. k outside the keyed range clamps to the nearest end.
    """
    from scipy.spatial.transform import Rotation, Slerp  # heavy import kept local; gates stay import-light

    frames = _frames_list(camera_frames)
    lo, hi, t = _bracket(frames, float(k))
    m_lo = np.asarray(lo["world_from_camera"], dtype=np.float64)
    m_hi = np.asarray(hi["world_from_camera"], dtype=np.float64)
    if m_lo.shape != (4, 4) or m_hi.shape != (4, 4):
        raise ValueError("world_from_camera must be 4x4")
    clamped = not (int(frames[0]["frame"]) <= float(k) <= int(frames[-1]["frame"]))
    if lo is hi or t == 0.0:
        m = m_lo.copy()
    else:
        rot = Rotation.from_matrix(np.stack([m_lo[:3, :3], m_hi[:3, :3]]))
        r = Slerp([0.0, 1.0], rot)([t])[0].as_matrix()
        m = np.eye(4, dtype=np.float64)
        m[:3, :3] = r
        m[:3, 3] = m_lo[:3, 3] * (1.0 - t) + m_hi[:3, 3] * t
    out: dict[str, Any] = {
        "frame": float(k),
        "world_from_camera": [[float(v) for v in row] for row in m],
        "lo": int(lo["frame"]),
        "hi": int(hi["frame"]),
        "t": float(t),
        "clamped": bool(clamped),
    }
    for key in ("focal_length_mm", "focus_distance"):
        v = _lerp(lo.get(key), hi.get(key), t)
        if v is not None:
            out[key] = float(v)
    return out


def intrinsics_for(camera: Mapping[str, Any], focal_length_mm: float | None = None) -> dict[str, Any]:
    """intrinsics_px block (PACKET.md 3.3) for a camera block, optionally at another focal length:
    fx = focal * zoom_factor * width / film_width_mm, fy = fx * pixel_aspect, principal point kept."""
    base = camera["intrinsics_px"]
    cx, cy = (float(base["principal_point"][0]), float(base["principal_point"][1]))
    if focal_length_mm is None or "focal_length_mm" not in camera:
        fx, fy = float(base["fx"]), float(base["fy"])
    else:
        res = camera["resolution"]
        width = float(res["width"])
        aspect = float(res.get("pixel_aspect", 1.0))
        zoom = float(camera.get("zoom_factor", 1.0))
        fx = float(focal_length_mm) * zoom * width / float(camera["film_width_mm"])
        fy = fx * aspect
    return {
        "fx": fx,
        "fy": fy,
        "principal_point": [cx, cy],
        "K": [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
    }


def camera_at(camera_frames: Any, k: float, base_camera: Mapping[str, Any]) -> dict[str, Any]:
    """The manifest camera block re-posed at fractional packet frame k, ready for plate_spec.project_point:
    world_from_camera from interpolate_pose, intrinsics_px rebuilt when the focal is keyed, focus_distance
    (for coc.weights) when keyed. `_pose` carries the interpolation record (lo, hi, t, clamped)."""
    pose = interpolate_pose(camera_frames, k)
    cam = dict(base_camera)
    cam["world_from_camera"] = pose["world_from_camera"]
    if "focal_length_mm" in pose:
        cam["focal_length_mm"] = pose["focal_length_mm"]
        cam["intrinsics_px"] = intrinsics_for(base_camera, pose["focal_length_mm"])
        cam.pop("fov_deg", None)  # no longer the manifest's number; readers must use intrinsics_px
    if "focus_distance" in pose:
        cam["focus_distance"] = pose["focus_distance"]
    cam["_pose"] = {key: pose[key] for key in ("frame", "lo", "hi", "t", "clamped")}
    return cam
