"""Video gate assembly: one call that scores a delivered clip and returns a versioned gate report.

`run(video, hero_still, end_still, packet_motion, timemap, flow_backend)` gathers

* landing (video.landing, T6): first frame vs the hero / start still, last frame vs the end still
  (`video.first_frame <= 12`, `video.last_frame <= 16`, calibration_needed);
* camera lock (reprojection.camera_lock): flow median at <= 960 wide; scored only when the clip is MEANT to be
  locked (`expect_locked`, default: no motion packet), because a Kling push-in or a dolly moves by design and
  the number is then a description, not a fault; hard on RAFT, advisory on DIS (`status_by_backend`); a clip
  of fewer than two frames comes back `unscored` (decision 90): no value, no check, a `backend_notes` line;
* reprojection drift (reprojection.drift) when a motion packet is given (`video.reproj_edge_recall >= 0.8`,
  hard), through the time map (built from the packet fps and the clip fps when none is passed);
* flicker (video.flicker; reported, no threshold yet; `unscored` with a `backend_notes` line under two frames);
* duration (`video.duration_s >= shot`, hard) against `shot_duration_s`, else the time map's duration;
* output tags from ffprobe: fps as 'num/den', size, codec, pix_fmt, bit depth, frame count.

The result is report.GateReport.score(...) as plain JSON data: `metrics` (flat scored values plus the nested
landing / camera_lock / drift / flicker / duration / output / timemap records), `checks`, `verdict` (fail iff
a hard check fails) and `backend_notes` naming the flow backend and width, the drift reference, the time map
and every part that was skipped and why. T43's Kling job records it next to the time map.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import io as pio
from . import reprojection, video
from .report import EngineRef, GateReport
from .reprojection import _bit_depth
from .timemap import TimeMap, fps_text

__all__ = ["run", "score", "output_tags", "build_timemap"]


def output_tags(info: Mapping[str, Any]) -> dict[str, Any]:
    """Delivery tags of a probed clip: fps text, size, codec, pix_fmt, bit_depth, nb_frames, duration_s."""
    fps = info["fps"]
    return {
        "fps": fps_text(fps),
        "fps_float": float(fps),
        "width": int(info["width"]),
        "height": int(info["height"]),
        "codec": info.get("codec"),
        "pix_fmt": info.get("pix_fmt"),
        "bit_depth": _bit_depth(info.get("pix_fmt")),
        "nb_frames": int(info["nb_frames"]),
        "duration_s": float(info["duration_s"]),
    }


def build_timemap(manifest: Mapping[str, Any], engine_fps: Any) -> TimeMap:
    """TimeMap from a motion packet manifest (camera.frames start/end/fps) and the engine's frame rate."""
    frames = manifest["camera"]["frames"]
    return TimeMap(frames["fps"], engine_fps, (int(frames["start"]), int(frames["end"])))


def score(
    video_path: str | Path,
    hero_still: Any,
    end_still: Any = None,
    packet_motion: str | Path | Mapping[str, Any] | None = None,
    timemap: TimeMap | None = None,
    flow_backend: Any = None,
    *,
    shot_duration_s: float | None = None,
    expect_locked: bool | None = None,
    engine: EngineRef | Mapping[str, Any] | None = None,
    tier: str | None = "A",
    z_seq: Mapping[int, Any] | Sequence[Any] | None = None,
    id_seq: Mapping[int, Any] | Sequence[Any] | None = None,
    clay_seq: Mapping[int, Any] | Sequence[Any] | None = None,
    camera_frames: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    samples: int = 8,
    width: int = reprojection.EVAL_WIDTH,
    flicker_width: int = 480,
) -> GateReport:
    """The assembled video gate as a GateReport model (see module doc; `run` returns the same as a dict)."""
    v = Path(video_path)
    info = pio.probe(v)
    notes: list[str] = []
    values: dict[str, float] = {}
    extra: dict[str, Any] = {"output": output_tags(info)}

    # landing: first frame vs the hero still, last frame vs the end still
    landing = video.landing(v, hero_still, end_still)
    extra["landing"] = landing
    values["video.first_frame"] = float(landing["first"])
    if landing["last"] is not None:
        values["video.last_frame"] = float(landing["last"])
    else:
        notes.append("no end still: last-frame landing not scored")

    # camera lock on the flow backend
    lock = reprojection.camera_lock(v, flow_backend, width, samples)
    extra["camera_lock"] = lock
    backend = lock["backend"]
    notes.append(f"flow backend {backend} @{lock['width']}")
    locked = (packet_motion is None) if expect_locked is None else bool(expect_locked)
    if locked and lock["status"] == "unscored":
        # a clip with fewer than two frames has no flow to measure: no value, so no check, so no pass on it
        notes.append(lock["note"])
    elif locked:
        values[reprojection.LOCK_METRIC_ID] = float(lock["median_flow_px"])
        if lock["threshold_status"] != "hard":
            notes.append(
                f"camera lock is {lock['threshold_status']} on {backend}; RAFT on the box makes it hard"
            )
    else:
        notes.append("camera lock not scored: the camera moves by design (motion job)")

    # reprojection drift through the time map
    tm = timemap
    if packet_motion is not None:
        root, manifest = reprojection.load_packet(packet_motion)
        if tm is None:
            tm = build_timemap(manifest, info["fps"])
            notes.append("time map built from the packet fps and the clip fps (none was passed)")
        dr = reprojection.drift(
            v,
            packet_motion if root is not None else manifest,
            z_seq,
            id_seq,
            camera_frames,
            tm,
            samples,
            width,
            clay_seq=clay_seq,
        )
        extra["drift"] = dr
        if dr["min_recall"] is not None:
            values[reprojection.METRIC_ID] = float(dr["min_recall"])
        notes.append(f"drift reference: {dr['backend']}")
        notes.extend(dr["notes"])
    else:
        notes.append("no motion packet: reprojection drift skipped")
    if tm is not None:
        extra["timemap"] = tm.to_json()
        notes.append(tm.describe())

    # flicker (no threshold in ts-2026.10.17-draft: reported only)
    extra["flicker"] = video.flicker(v, width=flicker_width)
    if extra["flicker"]["status"] == "unscored":
        notes.append("flicker not scored: fewer than 2 frames")

    # duration against the shot
    target = shot_duration_s if shot_duration_s is not None else (float(tm.duration_s) if tm else None)
    targets: dict[str, float] = {}
    if target is not None:
        extra["duration"] = video.duration_check(v, float(target))
        values["video.duration_s"] = float(info["duration_s"])
        targets["video.duration_s"] = float(target)
    else:
        extra["duration"] = {"duration_s": float(info["duration_s"]), "required_s": None, "ok": None}
        notes.append("no shot duration: duration not scored")

    ref = EngineRef(**engine) if isinstance(engine, Mapping) else engine
    return GateReport.score(
        values,
        tier=tier,
        engine=ref,
        backend=backend,
        targets=targets,
        extra_metrics=extra,
        backend_notes=notes,
    )


def run(
    video_path: str | Path,
    hero_still: Any,
    end_still: Any = None,
    packet_motion: str | Path | Mapping[str, Any] | None = None,
    timemap: TimeMap | None = None,
    flow_backend: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Assemble landing, camera lock, drift, flicker, duration and output tags into one gate report (dict).

    Positional arguments follow the plan's `run(video, hero_still, end_still|None, packet_motion|None, timemap,
    flow_backend)`; keyword options are those of `score` (shot_duration_s, expect_locked, engine, tier, the
    explicit sequences, samples, width). The dict is GateReport.model_dump(mode="json").
    """
    return score(
        video_path, hero_still, end_still, packet_motion, timemap, flow_backend, **kwargs
    ).model_dump(mode="json")
