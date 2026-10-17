"""Batch export (router plan T86): many takes through `export.export` in one request.

- `POST /projects/{id}/export {run_ids?, accepted?, out_dir, format?, name_pattern?}` ->
  `{exported: [{run_id, path, sidecar}], refused: [{run_id, error, ...}]}`. `format` is `tiff16` (default),
  `png16` or `exr`; `out_dir` an absolute folder. Each run goes through `export.export` with the single route's
  guards (`POST /runs/{id}/export`), and one run that cannot leave never stops the others:
  - GATE_FAIL needs an override recorded earlier (`POST /runs/{id}/override`); there is no typed override here;
  - previs is refused (`PREVIS_NOT_DELIVERABLE`), and so is any state but DONE (or an overridden GATE_FAIL);
  - an existing file is never overwritten, so two runs that format to one name leave the second refused.
  Each exported run is marked shipped and gets a ledger `export` record (`data.batch: true`).
- `accepted: true` without `run_ids` exports every run of the project that has an `accept` note in the index,
  oldest first; with `run_ids` it exports those, and any run without an `accept` note is refused
  (`not_accepted`). Neither `run_ids` nor `accepted` is 422 `no_runs`.
- `name_pattern` (default `{node}_{run8}`) names each file. Placeholders: `{node}` (the run's node id),
  `{run}` (its id), `{run8}` (the last 8 characters of the id; the leading ones are a timestamp, so siblings
  made together share them), `{packet}`, `{engine}` and `{n}` (the 1-based position in this batch, three
  digits). Characters outside letters, digits, `.`, `_` and `-` become `-` (a Windows file name; no `:`).

A refusal is `{run_id, error, detail}`: `error` is `not_found`, `not_accepted`, `not_exportable` (with `code`
`PREVIS_NOT_DELIVERABLE`, `GATE_FAIL` or `RUN_NOT_DELIVERABLE` when the guard says why), `bad_name` or
`bad_format`.

`export_many(pipeline, project_id, ...)` is the same thing as a function (`plate export --runs/--accepted`,
T103). It raises `PipelineError` for a problem with the request itself (404 unknown project; 422 `bad_format`,
`bad_out_dir`, `bad_name_pattern`, `no_runs`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..config import check_project_id
from ..delivery.guard import PREVIS_NOT_DELIVERABLE, Provenance
from ..export import FORMATS, NAME_RE, NotExportable, export, recorded_override
from ..gate_reports import NoReport
from ..gate_reports import Service as ReportService
from ..machine import RunNotFound, get_run
from ..models import Run
from ..pipeline import Pipeline, PipelineError
from ..states import RunState
from . import API, ApiError, ctx, pipeline_error, require_token

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

DEFAULT_FORMAT = "tiff16"
DEFAULT_PATTERN = "{node}_{run8}"
MAX_RUNS = 500
PLACEHOLDERS = ("node", "run", "run8", "packet", "engine", "n")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ExportBatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_ids: list[str] | None = Field(default=None, max_length=MAX_RUNS)
    accepted: bool = False
    out_dir: str = Field(min_length=1)
    format: str = DEFAULT_FORMAT
    name_pattern: str = Field(default=DEFAULT_PATTERN, min_length=1, max_length=200)


class _Refused(Exception):
    def __init__(self, error: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.body = {"error": error, "detail": detail, **extra}


def _safe(value: object) -> str:
    return _UNSAFE.sub("-", str(value)).strip("-") or "none"


def _values(run: Run, n: int) -> dict[str, str]:
    return {
        "node": _safe(run.node_id or "none"),
        "run": _safe(run.id),
        "run8": _safe(run.id[-8:]),
        "packet": _safe(run.packet_id),
        "engine": _safe(run.route.engine_id),
        "n": f"{n:03d}",
    }


def _file_name(pattern: str, run: Run, n: int) -> str:
    try:
        stem = pattern.format_map(_values(run, n))
    except (KeyError, IndexError, ValueError, AttributeError) as e:
        raise _Refused(
            "bad_name", f"name_pattern {pattern!r}: use {', '.join('{' + p + '}' for p in PLACEHOLDERS)}"
        ) from e
    if not NAME_RE.match(stem):
        raise _Refused(
            "bad_name",
            f"export name {stem!r}: letters, digits, '.', '_' and '-' only, 120 characters at most",
        )
    return stem


def _check_pattern(pattern: str) -> None:
    """Refuse a pattern that cannot name any run before the batch starts."""
    probe = dict.fromkeys(PLACEHOLDERS, "x")
    try:
        stem = pattern.format_map(probe)
    except (KeyError, IndexError, ValueError, AttributeError) as e:
        raise PipelineError(
            f"name_pattern {pattern!r}: use {', '.join('{' + p + '}' for p in PLACEHOLDERS)}",
            code="bad_name_pattern",
        ) from e
    if not NAME_RE.match(stem):
        raise PipelineError(
            f"name_pattern {pattern!r}: a name is letters, digits, '.', '_' and '-' only (a Windows file name)",
            code="bad_name_pattern",
        )


def _is_accepted(conn: Any, run_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM run_events WHERE run_id = ? AND kind = 'accept' LIMIT 1", (run_id,))
    return row.fetchone() is not None


def _accepted_ids(conn: Any) -> list[str]:
    rows = conn.execute(
        "SELECT r.id FROM runs r WHERE EXISTS "
        "(SELECT 1 FROM run_events e WHERE e.run_id = r.id AND e.kind = 'accept') "
        "ORDER BY r.created_utc, r.id"
    ).fetchall()
    return [row[0] for row in rows]


def _report_n(ref: str | None, run_id: str) -> int | None:
    stem = Path(ref).stem if ref else ""
    head, _, n = stem.rpartition(".")
    return int(n) if head == run_id and n.isdigit() else None


def export_many(
    pipeline: Pipeline,
    project_id: str,
    *,
    out_dir: str | Path,
    run_ids: Sequence[str] | None = None,
    accepted: bool = False,
    fmt: str = DEFAULT_FORMAT,
    name_pattern: str = DEFAULT_PATTERN,
    by: str = "human",
) -> dict[str, list[dict[str, Any]]]:
    """Export the runs into `out_dir` (see the module docstring); `{exported, refused}`."""
    pl = pipeline
    if fmt not in FORMATS:
        raise PipelineError(f"format {fmt!r}: use one of {', '.join(FORMATS)}", code="bad_format")
    out = Path(out_dir)
    if not out.is_absolute():
        raise PipelineError(
            f"out_dir must be an absolute folder path, got {str(out_dir)!r}", code="bad_out_dir"
        )
    _check_pattern(name_pattern)
    if not run_ids and not accepted:
        raise PipelineError("give run_ids, or accepted: true for every accepted run", code="no_runs")
    if pl.load_project(project_id) is None:
        raise PipelineError(f"no project {project_id}", code="not_found", status=404, project_id=project_id)
    project_dir = pl.project_dir(project_id)
    cas = pl.cas_for(project_id)
    exported: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    with pl.index.project(project_id) as conn:
        ids = list(dict.fromkeys(run_ids)) if run_ids else _accepted_ids(conn)
        for position, rid in enumerate(ids, start=1):
            try:
                if pl.index.locate_run(rid) != project_id:
                    raise _Refused("not_found", f"run {rid} is not in project {project_id}")
                try:
                    run = get_run(conn, rid)
                except RunNotFound as e:
                    raise _Refused("not_found", f"no run {rid}") from e
                if accepted and run_ids and not _is_accepted(conn, rid):
                    raise _Refused("not_accepted", f"run {rid} has not been accepted")
                if Provenance.from_run(run).previs:
                    raise _Refused(
                        "not_exportable",
                        "previs output is a draft; it is never exported",
                        code=PREVIS_NOT_DELIVERABLE,
                    )
                if run.state is RunState.GATE_FAIL and recorded_override(conn, rid) is None:
                    raise _Refused(
                        "not_exportable",
                        f"run {rid} failed its gates; export needs an override recorded first "
                        "(POST /runs/{id}/override)",
                        code="GATE_FAIL",
                    )
                stem = _file_name(name_pattern, run, position)
                try:
                    done = export(run, fmt, out, None, conn=conn, cas=cas, project_dir=project_dir, name=stem)
                except NotExportable as e:
                    code = (
                        "RUN_NOT_DELIVERABLE"
                        if run.state not in (RunState.DONE, RunState.GATE_FAIL)
                        else None
                    )
                    extra = {"code": code} if code else {}
                    raise _Refused("not_exportable", str(e), **extra) from e
                except ValueError as e:
                    raise _Refused("bad_name", str(e)) from e
                shipped = False
                n = _report_n(done.report_ref, rid)
                if n is not None:
                    try:  # the version the sidecar copied; the report file itself is never touched
                        shipped = ReportService(project_dir).shipped(rid, n).shipped
                    except NoReport:  # a seed run's stub gate wrote no report file
                        shipped = False
                pl.ledger(
                    project_id,
                    "export",
                    get_run(conn, rid),
                    data={
                        "batch": True,
                        "dest_name": done.path.name,
                        "format": done.fmt,
                        "sidecar": done.report_path.name,
                        "override_reason": done.override_reason,
                        "shipped": shipped,
                        "by": by,
                    },
                )
                exported.append({"run_id": rid, "path": str(done.path), "sidecar": str(done.report_path)})
            except _Refused as r:
                refused.append({"run_id": rid, **r.body})
    return {"exported": exported, "refused": refused}


@router.post("/projects/{project_id}/export")
def export_batch_route(request: Request, project_id: str, body: ExportBatchBody) -> dict[str, Any]:
    c = ctx(request)
    try:
        check_project_id(project_id)
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    try:
        return export_many(
            c.pipeline,
            project_id,
            out_dir=body.out_dir,
            run_ids=body.run_ids,
            accepted=body.accepted,
            fmt=body.format,
            name_pattern=body.name_pattern,
            by="human",
        )
    except PipelineError as e:
        raise pipeline_error(e) from e
