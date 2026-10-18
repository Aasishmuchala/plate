"""The delivery route (router plan T74, doctrine 6: gates can block delivery) and the helper behind it.

- `POST /runs/{id}/deliver {format, out_dir, name?, override_reason?, timeline?, full_frame?}` ->
  `{path, sidecar, format}`.
  `format` is one of `prores4444` (`<name>.mov`), `hevc` (`<name>.mp4`), `exr_sequence` (`<name>.%04d.exr`),
  `av1_retag` (`<name>.mp4`, the lossless Vantage 3.3 colour re-tag of an AV1 take) or `fcpxml`
  (`<name>.fcpxml`); `name` defaults to the run id. The take is the run's `outputs[0]` in the project's CAS:
  a clip goes to any format, a still only to `exr_sequence` (one frame, `<name>.0001.exr`), the video formats
  answer 422 `not_a_clip`. FCPXML lays the run's clip on the spine where it sits in the CAS unless `timeline`
  (`fcpxml.write`'s timeline: name, fps, width, height, clips) says otherwise; its keys win over the defaults.
- `delivery.guard.authorize(Provenance.from_run(...))` decides first, before a byte is produced: previs,
  a state other than DONE or GATE_FAIL, a missing gate report (except for the re-tag and the timeline, which
  change no pixel) and GATE_FAIL without an override are 409 `delivery_refused` with the guard's `code`. An
  override recorded earlier (`POST /runs/{id}/override`, an export) counts; one typed here
  (`override_reason`, 10 characters at least) is appended to the run's event log and written to the ledger
  as `kind: "override"` with `by` and `reason` before the encode starts (decision 39).
- Then `delivery.video.prores4444 | hevc | exr_sequence | av1_retag` or `delivery.fcpxml.write`: the lineage
  inside the file, a `.gate.json` sidecar beside it, an existing file never overwritten (409 `exists`). The
  stored report is marked shipped and a ledger `export` record is written with `data.delivery: true`.
  ffmpeg or ffprobe missing is 503 `tool_missing`; any other delivery error (an AV1 re-tag of a take that is
  not AV1, a bad timeline) is 422 `delivery_error`.
- Framing (router plan T114). When the run's packet declares `camera.safe_frame`, the pixel formats
  (`prores4444`, `hevc`, `exr_sequence`) write the safe-frame region by default, placed on the take by
  `export.framing` (a pixel crop, nothing resampled; HEVC's 4:2:0 snaps it inward to even coordinates). A clip
  that is cut is decoded to 16-bit frames, cropped and encoded from them; a take the safe frame does not cut
  goes straight through as before. `full_frame: true` keeps the whole take. The re-tag and the timeline write
  no pixel of their own, so they always carry the whole take and say so. The sidecar and the ledger `export`
  record carry `crop: {x, y, width, height}`, or `full_frame: true` (plus `safe_frame` when one was placed);
  a packet with no safe frame adds neither. A safe frame that cannot be placed on the take is 422
  `safe_frame_mismatch`, and a registered packet whose manifest cannot be read is 422 `packet_unreadable`.

`deliver_run(pipeline, run_id, fmt, out_dir, ...)` is the same thing as a function (`plate deliver`, T78): it
raises `PipelineError` (404 unknown run; 422 `bad_format`, `bad_name`, `bad_out_dir`, `not_a_clip`,
`no_output`, `safe_frame_mismatch`, `packet_unreadable`), `DeliveryRefused`, `ToolMissing`, `DeliveryError` or
`FileExistsError`.
"""

from __future__ import annotations

import dataclasses
import shutil
import uuid
from collections.abc import Callable, Mapping
from fractions import Fraction
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..delivery import fcpxml, video
from ..delivery.frames import crop_frames, is_video_file, open_frames
from ..delivery.guard import DeliveryError, DeliveryRefused, Provenance, ToolMissing, authorize, sidecar_path
from ..delivery.tools import probe_video, run_ffmpeg
from ..export import (
    NAME_RE,
    Framing,
    SafeFrameError,
    declared_safe_frame,
    framing,
    packet_manifest,
    recorded_override,
)
from ..fsutil import read_json, write_json_atomic
from ..gate_reports import NoReport
from ..gate_reports import Service as ReportService
from ..lineage import LineageError
from ..machine import append_note, get_run
from ..models import Run
from ..pipeline import Pipeline, PipelineError
from ..states import RunState
from . import API, ApiError, ctx, pipeline_error, require_token, run_project

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

S = RunState
FORMATS = ("prores4444", "hevc", "exr_sequence", "av1_retag", "fcpxml")
CLIP_ONLY = ("prores4444", "hevc", "av1_retag", "fcpxml")
# The re-tag and the timeline change no pixel: they need no gate report (the guard still refuses previs, GATE_FAIL).
NO_REPORT_NEEDED = ("av1_retag", "fcpxml")
# ...and they cannot crop: a stream copy and a timeline carry the whole take (T114).
WHOLE_TAKE = ("av1_retag", "fcpxml")
# 4:2:0 encodes crop on even coordinates.
EVEN_FORMATS = ("hevc",)
SUFFIXES = {"prores4444": ".mov", "hevc": ".mp4", "av1_retag": ".mp4", "fcpxml": ".fcpxml"}
EXR_FRAME_FIELD = ".%04d.exr"
FRAME_NAME = "frame_%06d.png"
# ffprobe colour_space -> the matrix ffmpeg's scale filter decodes with (an untagged clip is BT.709).
DECODE_MATRICES = {
    "bt709": "bt709",
    "smpte170m": "smpte170m",
    "bt470bg": "bt470",
    "fcc": "fcc",
    "smpte240m": "smpte240m",
    "bt2020nc": "bt2020",
    "bt2020c": "bt2020",
}


class DeliverBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str = Field(min_length=1)
    out_dir: str = Field(min_length=1)
    name: str | None = None
    override_reason: str | None = Field(default=None, max_length=500)
    timeline: dict[str, Any] | None = None
    full_frame: bool = False


# ---------------------------------------------------------------------------- the take's file


def _take(run: Run, cas: Any) -> Path:
    key = run.outputs[0] if run.outputs else None
    path = cas.find(key) if key else None
    if path is None:
        raise PipelineError(f"run {run.id} has no output in the project CAS", code="no_output", run_id=run.id)
    return path


def _clip_frames(src: Path, work: Path) -> Path:
    """The clip's frames as 16-bit sRGB PNGs in `work` (decoded with the clip's own matrix and range; an
    untagged clip is BT.709 limited, as every PLATE render is). Nothing is resized or retimed."""
    stream = probe_video(src)
    matrix = DECODE_MATRICES.get(str(stream.get("color_space") or ""), "bt709")
    rng = "pc" if stream.get("color_range") == "pc" else "tv"
    work.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-vf",
            f"scale=in_color_matrix={matrix}:in_range={rng}:out_range=pc"
            ":flags=accurate_rnd+full_chroma_int,format=rgb48be",
            "-c:v",
            "png",
            "-start_number",
            "1",
            str(work / FRAME_NAME),
        ]
    )
    return work


def _timeline(src: Path, name: str, given: Mapping[str, Any] | None) -> dict[str, Any]:
    """`fcpxml.write`'s timeline: the run's clip on the spine at its own rate and size; `given` keys win."""
    stream = probe_video(src)
    fps = stream["fps"]
    frames = int(stream.get("nb_frames") or 0)
    if frames > 0 and fps > 0:
        duration: Any = Fraction(frames) / fps
    else:
        duration = Fraction(str(stream.get("duration") or "0"))
    timeline: dict[str, Any] = {
        "name": name,
        "fps": fps,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "clips": [{"path": str(src), "name": name, "start_s": 0, "duration_s": duration}],
    }
    timeline.update(dict(given or {}))
    return timeline


def _override_hook(pl: Pipeline, conn: Any, project_id: str, run: Run, by: str) -> Callable[[str], None]:
    """What runs when the guard accepts an override typed with this delivery: the event log, the ledger
    (`kind: "override"`, decision 39) and the bus, before the encode starts."""

    def hook(reason: str) -> None:
        append_note(conn, run.id, kind="override", actor=by, reason=f"override: {reason}")
        pl.ledger(project_id, "override", run, by=by, note=reason)
        pl.publish(project_id, run.id, {"kind": "run.override", "run_id": run.id, "by": by, "reason": reason})

    return hook


def _report_n(ref: str | None, run_id: str) -> int | None:
    stem = Path(ref).stem if ref else ""
    head, _, n = stem.rpartition(".")
    return int(n) if head == run_id and n.isdigit() else None


# ---------------------------------------------------------------------------- the safe frame (T114)


def _framing(
    conn: Any, run: Run, fmt: str, full_frame: bool, take_size: Callable[[], tuple[int, int]]
) -> Framing:
    """Which part of the take this delivery writes (see the module docstring). `take_size` is only called when
    the packet declares a safe frame. Raises `PipelineError` (`safe_frame_mismatch`, `packet_unreadable`)."""
    keep = full_frame or fmt in WHOLE_TAKE
    try:
        manifest = packet_manifest(conn, run.packet_id)
        if declared_safe_frame(manifest) is None:
            return Framing(full_frame=full_frame)
        width, height = take_size()
        return framing(manifest, width, height, full_frame=keep, even=fmt in EVEN_FORMATS)
    except SafeFrameError as e:
        if keep:  # the whole take leaves anyway; the safe frame is not known, so none is named
            return Framing(full_frame=True)
        raise PipelineError(str(e), code=e.code.lower(), run_id=run.id) from e


def _note_framing(sidecar: Path, record: dict[str, Any]) -> None:
    """Add the framing (`crop` or `full_frame`) to the sidecar the encoder wrote beside the delivery."""
    body = read_json(sidecar)
    body.update(record)
    write_json_atomic(sidecar, body, indent=1)


def _work_dir(pl: Pipeline, run_id: str) -> Path:
    return pl.settings.home / "cache" / "deliver" / f"{run_id}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------- the helper


def deliver_run(
    pipeline: Pipeline,
    run_id: str,
    fmt: str,
    out_dir: str | Path,
    *,
    name: str | None = None,
    override_reason: str | None = None,
    timeline: Mapping[str, Any] | None = None,
    by: str = "human",
    full_frame: bool = False,
) -> dict[str, Any]:
    """Deliver the run's take as `fmt` into `out_dir` (see the module docstring); `{path, sidecar, format}`.
    The packet's safe frame is cropped out of a pixel format unless `full_frame`."""
    pl = pipeline
    if fmt not in FORMATS:
        raise PipelineError(f"format {fmt!r}: use one of {', '.join(FORMATS)}", code="bad_format")
    out = Path(out_dir)
    if not out.is_absolute():
        raise PipelineError(
            f"out_dir must be an absolute folder path, got {str(out_dir)!r}", code="bad_out_dir"
        )
    stem = name or run_id
    if not NAME_RE.match(stem):
        raise PipelineError(
            f"delivery name {stem!r}: letters, digits, '.', '_' and '-' only (a Windows file name)",
            code="bad_name",
        )
    project_id = pl.index.locate_run(run_id)
    if project_id is None:
        raise PipelineError(f"no run {run_id}", code="not_found", status=404, run_id=run_id)
    project_dir = pl.project_dir(project_id)
    cas = pl.cas_for(project_id)
    with pl.index.project(project_id) as conn:
        run = get_run(conn, run_id)
        typed = (override_reason or "").strip() or None
        recorded = recorded_override(conn, run_id)
        prov = Provenance.from_run(
            run,
            project_dir=project_dir,
            override_reason=typed or recorded,
            engine_version=str(run.route_dict.get("model_version") or "") or None,
        )
        need_report = fmt not in NO_REPORT_NEEDED
        authorize(prov, require_report=need_report)  # refuse before anything is read or written (no hook yet)
        if typed is not None and typed != recorded:
            prov = dataclasses.replace(prov, on_override=_override_hook(pl, conn, project_id, run, by))
        src = _take(run, cas)
        clip = is_video_file(src)
        if fmt in CLIP_ONLY and not clip:
            raise PipelineError(
                f"run {run_id} is a still; {fmt} delivers clips (a still goes out as exr_sequence)",
                code="not_a_clip",
                run_id=run_id,
            )
        taken: dict[str, Any] = {}  # what reading the take's size opened: the clip's stream, a still's frames

        def take_size() -> tuple[int, int]:
            if clip:
                stream = taken["stream"] = probe_video(src)
                taken["size"] = (int(stream.get("width") or 0), int(stream.get("height") or 0))
            else:
                still = taken["frames"] = open_frames(src)
                taken["size"] = (still.width, still.height)
            return taken["size"]

        frame = _framing(conn, run, fmt, full_frame, take_size)
        crop = frame.crop if frame.crop is not None and frame.cuts(*taken["size"]) else None
        work: Path | None = None
        try:
            if fmt in ("prores4444", "hevc"):
                source: Any = src
                fps: Any = None
                if crop is not None:  # cut to the safe frame: decoded, cropped, encoded from the frames
                    work = _work_dir(pl, run_id)
                    source, fps = crop_frames(_clip_frames(src, work), crop), taken["stream"]["fps"]
                encode = video.prores4444 if fmt == "prores4444" else video.hevc
                done = encode(source, out / f"{stem}{SUFFIXES[fmt]}", fps, provenance=prov)
            elif fmt == "av1_retag":
                done = video.av1_retag(src, out / f"{stem}{SUFFIXES[fmt]}", provenance=prov)
            elif fmt == "exr_sequence":
                frames: Any = taken.get("frames", src)
                if clip:
                    work = _work_dir(pl, run_id)
                    frames = _clip_frames(src, work)
                if crop is not None:
                    frames = crop_frames(frames, crop)
                done = video.exr_sequence(frames, out / (stem + EXR_FRAME_FIELD), provenance=prov)
            else:
                path = fcpxml.write(
                    _timeline(src, stem, timeline), out / f"{stem}{SUFFIXES[fmt]}", provenance=prov
                )
                done = video.Delivered(
                    path=path,
                    sidecar=sidecar_path(path),
                    format=fmt,
                    files=(path,),
                    override={"reason": prov.override_reason or ""} if prov.failed else None,
                )
        finally:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
        record = frame.record()
        if record:
            _note_framing(done.sidecar, record)
        shipped = False
        n = _report_n(prov.report_ref, run_id)
        if n is not None:
            try:  # the version the sidecar copied; the report file itself is never touched
                shipped = ReportService(project_dir).shipped(run_id, n).shipped
            except NoReport:
                shipped = False
        pl.ledger(
            project_id,
            "export",
            get_run(conn, run_id),
            data={
                "delivery": True,
                "format": fmt,
                "dest_name": done.path.name,
                "files": len(done.files),
                "sidecar": done.sidecar.name,
                "override_reason": (done.override or {}).get("reason") or None,
                "shipped": shipped,
                "by": by,
                **record,
            },
        )
    return {"path": str(done.path), "sidecar": str(done.sidecar), "format": fmt}


# ---------------------------------------------------------------------------- the route


@router.post("/runs/{run_id}/deliver")
def deliver_route(request: Request, run_id: str, body: DeliverBody) -> dict[str, Any]:
    c = ctx(request)
    run_project(c, run_id)
    try:
        return deliver_run(
            c.pipeline,
            run_id,
            body.format,
            body.out_dir,
            name=body.name,
            override_reason=body.override_reason,
            timeline=body.timeline,
            by="human",
            full_frame=body.full_frame,
        )
    except PipelineError as e:
        raise pipeline_error(e) from e
    except DeliveryRefused as e:
        raise ApiError(409, {"error": "delivery_refused", "code": e.code, "detail": str(e)}) from e
    except ToolMissing as e:
        raise ApiError(503, {"error": "tool_missing", "detail": str(e)}) from e
    except FileExistsError as e:
        raise ApiError(409, {"error": "exists", "detail": str(e)}) from e
    except (DeliveryError, LineageError) as e:
        raise ApiError(422, {"error": "delivery_error", "detail": str(e)}) from e
