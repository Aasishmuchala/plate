"""The social cutdown route (router plan T86, BUILD_PLAN decision 56) and the helper behind it.

- `POST /runs/{id}/cutdown {preset, cx?, cy?, out_dir, name?, format?, codec?, long_side?}` ->
  `{path, sidecar, box}`. `preset` is `9:16`, `4:5`, `1:1` or `16:9`; `cx` and `cy` (default 0.5) are where the
  crop is centred, as fractions of the frame. The take is the run's `outputs[0]` in the project's CAS: a still
  becomes an 8-bit sRGB `png` (default) or `jpeg`, a clip an `.mp4` `h264` (default) or `hevc` (`format` applies
  to stills and `codec` to clips; the other is ignored). `name` defaults to `<run id>_<preset with x for :>`.
  `long_side` can only shrink the crop.
- The crop is the largest box of the aspect inside the packet's `camera.safe_frame` when the take is the full
  overscan frame, else inside the take (`cutdown.crop_box`).
- `delivery.guard.authorize(Provenance.from_run(...))` decides first, exactly as `POST /runs/{id}/deliver` does
  (a pixel-changing output needs a stored gate report): previs, a state other than DONE or GATE_FAIL, a missing
  report and GATE_FAIL without a recorded override are 409 `delivery_refused` with the guard's `code`. Then
  the lineage goes inside the file (with the IPTC AI marker on a generative take), `<name>.cutdown.json` beside
  it, a ledger `export` record (`data.cutdown: true`) is written and the stored gate report is marked shipped.
  An existing file is 409 `exists` (the name is claimed, so of concurrent cutdowns to one name exactly one is
  written); ffmpeg or ffprobe missing is 503 `tool_missing`; a crop that cannot be made (unknown preset, an
  upscale) is 422 `cutdown_error`. A project folder whose `gates/` cannot take the shipped mark is refused after
  the guard and before the take is read (503 `not_exportable`, `export.check_project_writable`).

`cutdown_run(pipeline, run_id, preset, out_dir, ...)` is the same thing as a function; it raises `PipelineError`
(404 unknown run; 422 `bad_name`, `bad_out_dir`, `no_output`, `unreadable_take`), `DeliveryRefused`,
`ProjectUnwritable` (a `NotExportable`), `ToolMissing`, `DeliveryError`, `CutdownError` or `FileExistsError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import cutdown
from ..delivery.frames import is_video_file
from ..delivery.guard import DeliveryError, DeliveryRefused, Provenance, ToolMissing, authorize
from ..delivery.tools import probe_video
from ..export import NAME_RE, NotExportable, check_project_writable, recorded_override
from ..gate_reports import NoReport
from ..gate_reports import Service as ReportService
from ..lineage import LineageError
from ..machine import get_run
from ..pipeline import Pipeline, PipelineError, decode_png16
from . import API, ApiError, ctx, pipeline_error, require_token, run_project

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])


class CutdownBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: str = Field(min_length=1)
    cx: float = Field(default=0.5, ge=0.0, le=1.0)
    cy: float = Field(default=0.5, ge=0.0, le=1.0)
    out_dir: str = Field(min_length=1)
    name: str | None = None
    format: Literal["png", "jpeg"] | None = None
    codec: Literal["h264", "hevc"] | None = None
    long_side: int | None = Field(default=None, ge=2)


def _report_n(ref: str | None, run_id: str) -> int | None:
    stem = Path(ref).stem if ref else ""
    head, _, n = stem.rpartition(".")
    return int(n) if head == run_id and n.isdigit() else None


def _safe_frame(
    manifest: Mapping[str, Any] | None, width: int, height: int
) -> tuple[int, int, int, int] | None:
    """The packet's deliverable safe frame when the take is the whole overscan frame around it, else None (a
    take that is already the safe frame, or a packet that declares none, is cropped as it stands)."""
    sf = ((manifest or {}).get("camera") or {}).get("safe_frame")
    if not isinstance(sf, Mapping):
        return None
    try:
        x, y, w, h = (int(sf[k]) for k in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return None
    if (w, h) == (width, height) or w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
        return None
    return x, y, w, h


def _manifest(pl: Pipeline, project_id: str, packet_id: str) -> Mapping[str, Any] | None:
    try:
        return pl.packet(project_id, packet_id).manifest
    except PipelineError:  # a run whose packet is gone or not ready is cropped as the take stands
        return None


def cutdown_run(
    pipeline: Pipeline,
    run_id: str,
    preset: str,
    out_dir: str | Path,
    *,
    cx: float = 0.5,
    cy: float = 0.5,
    name: str | None = None,
    fmt: str | None = None,
    codec: str | None = None,
    long_side: int | None = None,
    by: str = "human",
) -> dict[str, Any]:
    """Cut the run's take down to `preset` into `out_dir` (see the module docstring); `{path, sidecar, box}`."""
    pl = pipeline
    out = Path(out_dir)
    if not out.is_absolute():
        raise PipelineError(
            f"out_dir must be an absolute folder path, got {str(out_dir)!r}", code="bad_out_dir"
        )
    stem = name or f"{run_id}_{preset.replace(':', 'x')}"
    if not NAME_RE.fullmatch(stem):
        raise PipelineError(
            f"cutdown name {stem!r}: letters, digits, '.', '_' and '-' only (a Windows file name)",
            code="bad_name",
        )
    project_id = pl.index.locate_run(run_id)
    if project_id is None:
        raise PipelineError(f"no run {run_id}", code="not_found", status=404, run_id=run_id)
    project_dir = pl.project_dir(project_id)
    cas = pl.cas_for(project_id)
    with pl.index.project(project_id) as conn:
        run = get_run(conn, run_id)
        prov = Provenance.from_run(
            run,
            project_dir=project_dir,
            override_reason=recorded_override(conn, run_id),
            engine_version=str(run.route_dict.get("model_version") or "") or None,
        )
        authorize(prov)  # refuse before anything is read or written
        check_project_writable(project_dir)  # the shipped mark goes there: refuse before a client file
        key = run.outputs[0] if run.outputs else None
        src = cas.find(key) if key else None
        if src is None:
            raise PipelineError(
                f"run {run_id} has no output in the project CAS", code="no_output", run_id=run_id
            )
        manifest = _manifest(pl, project_id, run.packet_id)
        if is_video_file(src):
            use_codec = codec or "h264"
            stream = probe_video(src)
            width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
            box = cutdown.crop_box(
                width, height, preset, cx=cx, cy=cy, safe_frame=_safe_frame(manifest, width, height)
            )
            done = cutdown.clip_cutdown(
                src,
                box,
                out / f"{stem}.mp4",
                use_codec,
                long_side,
                lineage=prov.lineage,
                preset=preset,
            )
            kind = {"codec": use_codec}
        else:
            use_fmt = fmt or "png"
            try:
                rgb16 = decode_png16(src.read_bytes())
            except (OSError, ValueError) as e:
                raise PipelineError(
                    f"run {run_id}: the take is not an image a cutdown can crop ({e})",
                    code="unreadable_take",
                    run_id=run_id,
                ) from e
            height, width = rgb16.shape[:2]
            box = cutdown.crop_box(
                width, height, preset, cx=cx, cy=cy, safe_frame=_safe_frame(manifest, width, height)
            )
            suffix = ".png" if use_fmt == "png" else ".jpg"
            done = cutdown.still_cutdown(
                rgb16,
                box,
                out / f"{stem}{suffix}",
                use_fmt,
                long_side=long_side,
                lineage=prov.lineage,
                preset=preset,
            )
            kind = {"format": use_fmt}
        side = cutdown.sidecar_path(done)
        shipped = False
        n = _report_n(prov.report_ref, run_id)
        if n is not None:
            try:  # the version the cutdown names; the report file itself is never touched
                shipped = ReportService(project_dir).shipped(run_id, n).shipped
            except NoReport:
                shipped = False
        pl.ledger(
            project_id,
            "export",
            get_run(conn, run_id),
            data={
                "cutdown": True,
                "preset": preset,
                "box": list(box),
                "dest_name": done.name,
                "sidecar": side.name,
                "shipped": shipped,
                "by": by,
                **kind,
            },
        )
    return {"path": str(done), "sidecar": str(side), "box": list(box)}


@router.post("/runs/{run_id}/cutdown")
def cutdown_route(request: Request, run_id: str, body: CutdownBody) -> dict[str, Any]:
    c = ctx(request)
    run_project(c, run_id)
    try:
        return cutdown_run(
            c.pipeline,
            run_id,
            body.preset,
            body.out_dir,
            cx=body.cx,
            cy=body.cy,
            name=body.name,
            fmt=body.format,
            codec=body.codec,
            long_side=body.long_side,
            by="human",
        )
    except PipelineError as e:
        raise pipeline_error(e) from e
    except DeliveryRefused as e:
        raise ApiError(409, {"error": "delivery_refused", "code": e.code, "detail": str(e)}) from e
    except NotExportable as e:
        raise ApiError(getattr(e, "status", 409), {"error": "not_exportable", "detail": str(e)}) from e
    except ToolMissing as e:
        raise ApiError(503, {"error": "tool_missing", "detail": str(e)}) from e
    except FileExistsError as e:
        raise ApiError(409, {"error": "exists", "detail": str(e)}) from e
    except cutdown.CutdownError as e:
        raise ApiError(422, {"error": "cutdown_error", "detail": str(e)}) from e
    except (DeliveryError, LineageError) as e:
        raise ApiError(422, {"error": "delivery_error", "detail": str(e)}) from e
