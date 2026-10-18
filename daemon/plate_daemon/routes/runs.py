"""Run routes: quote -> approve -> run, run history, the gate report and its rescore, override and accept,
best-of-N, export, envelopes.

- `GET /projects/{id}/runs[?node_id=&state=&limit=200]`: the project's runs, newest first (`created_utc`
  desc), each as `GET /runs/{id}` answers it; `state` is one state or a comma list, `limit` 1-1000. 404
  unknown project.
- `GET /runs/{id}`: the run (`models.Run` JSON) plus `override {reason, by, at}`, `accepted {...}`,
  `exportable` and `subgraph` (the canvas the run was quoted from, `inputs.subgraph`, T58); `GET
  /runs/{id}/events` its append-only event log.
- `POST /runs/{id}/approve {by, envelope_id?, reason?, start?}`: QUOTED -> APPROVED as `human`, `auto` or
  `agent[:<envelope_id>]` (a best-of-N group approves as one, against the sum of its prices), then QUEUED
  and run in the background by the pipeline (`start`, default true). The seed's `{actor}` body approves
  only (`start` defaults to false), which keeps `POST /queue` and `POST /step` the dev driver they were.
  Refusals: 403 custody, 409 `quote_expired` (the run is then EXPIRED) / illegal transition / `auto_cap`,
  `daily_cap`, `unattended_cap`, 422 `envelope_required` / `envelope_exceeded`.
- `POST /runs/{id}/cancel`, `POST /runs/{id}/fanout {n}` (a QUOTED run becomes sibling 0 of n under one
  quote; the list is returned), `GET /runs/{id}/gate[?n=]` (the stored report, `gates/<run>.<n>.json`,
  with `n`, `versions`, `shipped` and `overlays`; 404 before gating).
- `overlays` (T109, verification gap G1) is the report's `metrics.overlays` block lifted to the top: the hashes,
  formats and sizes of what the viewer's `e` and `h` modes draw, stored in the project CAS at gate time and
  served by `GET /assets/{hash}`: `edges_plate` and `edges_candidate` (8-bit grey PNGs, 255 = edge, the G-buffer
  edge masks edge recall scored; Tier A only, else null) and `de00_map` (raw little-endian R16F at the output's
  size, `gating` module docstring). null for a report written without them (gated before T109). Every request to
  this router makes sure `Pipeline.gate` stores them (`gating.install`, idempotent), so a run approved here is
  gated with them; the rescore stores them for its version too.
- `POST /runs/{id}/gate/rescore`: the stored take scored again by today's gate code and thresholds, with the
  arguments `Pipeline.gate` passes (the plate from the packet, `outputs[0]`, the mask the compositor used
  read back from `normalized.mask_key`), written as the next version with `rescored_from`
  (`gate_reports.Service.rescore`); no version is ever rewritten, so a shipped report stays byte for byte
  what shipped, and with its own `overlays`. 409 `no_report` when the run was never gated, `not_rescorable`
  for a take `Pipeline.gate` did not score (numeric nodes, motion, carried-over layers).
- `POST /runs/{id}/override {reason, by}`: a GATE_FAIL run becomes exportable; the reason (10 characters at
  least) is in the run's event log and in a ledger `override` record with top-level `by` and `reason`.
  `POST /runs/{id}/accept {scrub_log_id, by?}`: a DONE (or overridden) take is accepted only against a
  logged FULL scrub of this run: a ledger record of kind `scrub` (written by `POST /runs/{id}/scrub`, T38)
  whose `opaque_id` or `data.scrub_log_id` is the id and whose `data.full_scrub` is true, or
  `data.quadrants_visited >= 4` with `data.zoom_100`. Else 409.
- `POST /runs/{id}/export {out_dir, format?, name?, override?, reason?}` (or the legacy `{dest}`: its folder,
  stem and suffix): through `export.export` (decision 39), the run's gated composite as tiff16 / png16 / exr
  with its lineage embedded and `<name>.gate.json` beside it; the report is marked shipped and a ledger
  `export` record written. Export does not require accept. GATE_FAIL needs an override, recorded earlier or
  typed here (`override: true` + `reason`); one typed here is a ledger `override` record too.
- The seed's dev routes `POST /runs` (a run outside the canvas), `/runs/{id}/quote`, `/queue` and `/step`
  answer 404 unless `PLATE_DEV=1` (decision 36); under dev `POST /runs` refuses `inputs.staging`, `mask` and
  `plate` (a pipeline run is quoted from the canvas, never made by hand). `POST|GET /envelopes` stay.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Annotated, Any

import cv2
import numpy as np
from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..adapter import SimulatedCrash
from ..config import check_project_id
from ..custody import CustodyViolation
from ..export import FORMATS, MIN_OVERRIDE_REASON, NAME_RE, NotExportable, export, recorded_override
from ..gate_reports import NoReport
from ..gate_reports import Service as ReportService
from ..ledger import Ledger
from ..machine import IllegalTransition, RunNotFound, append_note, create_run, get_run, run_events
from ..models import Run, RunCreate
from ..pipeline import QUOTABLE_KINDS, STAGING_KEY, PipelineError, decode_png16, is_pipeline_run
from ..quotes import (
    AutoApprovalDenied,
    EnvelopeExceeded,
    EnvelopeRequired,
    QuoteExpired,
    QuoteNotFound,
    create_envelope,
    get_envelope,
    quote_run,
)
from ..states import RunState
from ..worker import FanOutError, fan_out
from . import API, ApiError, Ctx, ctx, custody_error, illegal, pipeline_error, require_token, run_project


def gate_overlays(request: Request) -> None:
    """`Pipeline.gate` stores the viewer overlays (T109): `gating.install` on the app's pipeline, idempotent,
    before any run this router starts is gated (imported lazily, as the rescore does)."""
    from .. import gating

    gating.install(ctx(request).pipeline)


router = APIRouter(prefix=API, dependencies=[Depends(require_token), Depends(gate_overlays)])

S = RunState
APPROVER_PREFIXES = ("human", "auto", "agent")
NOTE_KINDS = ("override", "accept")
# What a hand-made dev run may not carry: these make a pipeline run, which only the canvas quote builds.
DEV_ROUTE_INPUTS = ("staging", "mask", "plate")
# The legacy export body's `dest` suffix -> `export.export` format.
SUFFIX_FORMATS = {".tif": "tiff16", ".tiff": "tiff16", ".png": "png16", ".exr": "exr"}
DEFAULT_EXPORT_FORMAT = "tiff16"
LIST_LIMIT_DEFAULT = 200
LIST_LIMIT_MAX = 1000


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: str | None = None
    actor: str | None = None  # the seed's field
    envelope_id: str | None = None
    reason: str | None = Field(default=None, max_length=500)
    start: bool | None = None


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = "human"
    reason: str | None = None


class ExportBody(BaseModel):
    """`out_dir` (+ `format`, default tiff16, and `name`, default the run id), or the legacy `dest` file path."""

    model_config = ConfigDict(extra="forbid")
    dest: str | None = Field(default=None, min_length=1)
    out_dir: str | None = Field(default=None, min_length=1)
    format: str | None = None
    name: str | None = None
    override: bool = False
    reason: str | None = None


class RescoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FanoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int


class OverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    by: str = Field(default="human", min_length=1)


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scrub_log_id: str = Field(min_length=1)
    by: str = Field(default="human", min_length=1)


class EnvelopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    owner: str = Field(min_length=1)
    max_total_usd: float = Field(ge=0)
    max_per_run_usd: float = Field(ge=0)
    ttl_s: float = Field(gt=0)


# ---------------------------------------------------------------------------- the run as the UI reads it


def _notes(conn: sqlite3.Connection, run_id: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    marks = ",".join("?" * len(NOTE_KINDS))
    rows = conn.execute(
        f"SELECT kind, actor, reason, at_utc FROM run_events WHERE run_id = ? AND kind IN ({marks}) ORDER BY seq",
        (run_id, *NOTE_KINDS),
    ).fetchall()
    for r in rows:
        reason = r["reason"] or ""
        if r["kind"] == "override":
            text = reason[len("override: ") :] if reason.startswith("override: ") else reason
            out["override"] = {"reason": text, "by": r["actor"], "at": r["at_utc"]}
        else:
            try:
                body = json.loads(reason)
            except ValueError:
                body = {"note": reason}
            out["accept"] = {**(body if isinstance(body, dict) else {}), "by": r["actor"], "at": r["at_utc"]}
    return out


def run_view(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = get_run(conn, run_id)
    d = run.model_dump(mode="json")
    notes = _notes(conn, run_id)
    d["override"] = notes.get("override")
    d["accepted"] = notes.get("accept")
    d["exportable"] = run.state is S.DONE or (run.state is S.GATE_FAIL and d["override"] is not None)
    d["subgraph"] = run.inputs.get("subgraph")
    return d


def _check_approver(by: str) -> str:
    head = by.split(":", 1)[0]
    if head not in APPROVER_PREFIXES or (head != "agent" and ":" in by):
        raise ApiError(422, {"errors": [f"by must be human, auto or agent[:<envelope_id>], got {by!r}"]})
    return by


def _refusal_code(e: AutoApprovalDenied) -> str:
    text = str(e)
    for needle, code in (
        ("auto-approve cap", "auto_cap"),
        ("daily cap", "daily_cap"),
        ("unattended", "unattended_cap"),
    ):
        if needle in text:
            return code
    return "approval_refused"


# ---------------------------------------------------------------------------- the seed's dev routes


def require_dev(request: Request) -> None:
    """The seed's dev driver exists only under `PLATE_DEV=1` (decision 36): elsewhere it is not a route."""
    if not ctx(request).settings.dev:
        raise ApiError(404, {"error": "not_found", "detail": "dev route; set PLATE_DEV=1"})


DEV_ONLY = [Depends(require_dev)]


@router.post("/runs", status_code=201, dependencies=DEV_ONLY)
def create_run_route(request: Request, body: RunCreate) -> JSONResponse:
    c = ctx(request)
    made = sorted(k for k in DEV_ROUTE_INPUTS if k in body.inputs)
    if made:
        raise ApiError(
            422,
            {
                "error": "dev_route_inputs",
                "inputs": made,
                "detail": "a run with staging, mask or plate inputs is quoted from the canvas "
                "(POST /projects/{id}/nodes/{node}/quote), never made by hand",
            },
        )
    with c.index.project(body.project_id) as conn:
        row = conn.execute("SELECT status FROM packets WHERE packet_id = ?", (body.packet_id,)).fetchone()
        if row is None:
            raise ApiError(
                422,
                {"errors": [f"packet {body.packet_id} is not in project {body.project_id}; send it first"]},
            )
        if row["status"] != "ingested":
            raise ApiError(
                409, {"error": "packet_not_ready", "packet_id": body.packet_id, "status": row["status"]}
            )
        run = create_run(conn, body, now=c.now())
    c.index.remember(run.id, body.project_id)
    return JSONResponse(run.model_dump(mode="json"), status_code=201)


@router.post("/runs/{run_id}/quote", dependencies=DEV_ONLY)
def quote_route(request: Request, run_id: str) -> dict[str, Any]:
    c = ctx(request)
    pid = run_project(c, run_id)
    with c.index.project(pid) as conn:
        try:
            run, q = quote_run(conn, run_id, now=c.now())
        except IllegalTransition as e:
            raise illegal(e) from e
        c.pipeline.after(conn, pid, run_id, S.DRAFT)
        return {"run": run.model_dump(mode="json"), "quote": q.model_dump(mode="json")}


@router.post("/runs/{run_id}/queue", dependencies=DEV_ONLY)
def queue_route(request: Request, run_id: str) -> dict[str, Any]:
    c = ctx(request)
    pid = run_project(c, run_id)
    with c.index.project(pid) as conn:
        try:
            return c.pipeline.queue(conn, pid, run_id).model_dump(mode="json")
        except IllegalTransition as e:
            raise illegal(e) from e


@router.post("/runs/{run_id}/step", dependencies=DEV_ONLY)
def step_route(request: Request, run_id: str) -> dict[str, Any]:
    c = ctx(request)
    pid = run_project(c, run_id)
    with c.index.project(pid) as conn:
        try:
            c.pipeline.step(conn, pid, run_id, c.cas_for(pid))
        except SimulatedCrash as e:
            if c.crash_exits_process:
                sys.stderr.write(f"plate_daemon: {e}; exiting as a crashed process would\n")
                sys.stderr.flush()
                os._exit(70)
            raise ApiError(500, {"error": "simulated_crash", "detail": str(e)}) from None
        except RunNotFound as e:
            raise ApiError(404, {"error": "not_found", "run_id": run_id}) from e
        return run_view(conn, run_id)


# ---------------------------------------------------------------------------- reading a run


def _states(text: str) -> list[str]:
    """`?state=DONE` or `?state=DONE,GATE_FAIL` -> the state values; 422 for one the machine does not have."""
    states = [s.strip().upper() for s in text.split(",") if s.strip()]
    known = {s.value for s in RunState}
    unknown = sorted(set(states) - known)
    if unknown or not states:
        raise ApiError(422, {"errors": [f"unknown run state(s) {unknown or [text]}; use {sorted(known)}"]})
    return states


@router.get("/projects/{project_id}/runs")
def list_runs_route(
    request: Request,
    project_id: str,
    node_id: Annotated[str | None, Query(min_length=1)] = None,
    state: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=LIST_LIMIT_MAX)] = LIST_LIMIT_DEFAULT,
) -> list[dict[str, Any]]:
    """The project's runs, newest first, as `GET /runs/{id}` answers each (the UI's history panel)."""
    c = ctx(request)
    try:
        check_project_id(project_id)
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    if c.pipeline.load_project(project_id) is None:
        raise ApiError(404, {"error": "not_found", "project_id": project_id})
    where: list[str] = []
    args: list[Any] = []
    if node_id is not None:
        where.append("node_id = ?")
        args.append(node_id)
    if state is not None:
        states = _states(state)
        where.append(f"state IN ({','.join('?' * len(states))})")
        args.extend(states)
    sql = "SELECT id FROM runs" + (f" WHERE {' AND '.join(where)}" if where else "")
    sql += " ORDER BY created_utc DESC, id DESC LIMIT ?"
    with c.index.project(project_id) as conn:
        ids = [row[0] for row in conn.execute(sql, (*args, limit)).fetchall()]
        return [run_view(conn, rid) for rid in ids]


@router.get("/runs/{run_id}")
def get_run_route(request: Request, run_id: str) -> dict[str, Any]:
    c = ctx(request)
    with c.index.project(run_project(c, run_id)) as conn:
        return run_view(conn, run_id)


@router.get("/runs/{run_id}/events")
def run_events_route(request: Request, run_id: str) -> list[dict[str, Any]]:
    c = ctx(request)
    with c.index.project(run_project(c, run_id)) as conn:
        return [e.model_dump(mode="json") for e in run_events(conn, run_id)]


@router.get("/runs/{run_id}/gate")
def gate_route(request: Request, run_id: str, n: Annotated[int | None, Query(ge=1)] = None) -> dict[str, Any]:
    c = ctx(request)
    pid = run_project(c, run_id)
    svc = c.pipeline.report_service(pid)
    if svc is not None:
        versions = svc.versions(run_id)
        if versions:
            want = n if n is not None else versions[-1]
            if want not in versions:
                raise ApiError(404, {"error": "not_found", "run_id": run_id, "n": want, "versions": versions})
            return _stored_view(svc, run_id, want, versions)
    with c.index.project(pid) as conn:
        run = get_run(conn, run_id)
    if run.gate_report is None or n is not None:
        raise ApiError(404, {"error": "no_report", "run_id": run_id, "state": run.state.value})
    return {
        **run.gate_report,
        "n": None,
        "versions": [],
        "shipped": False,
        "overlays": _overlays(run.gate_report),
    }


def _overlays(report: dict[str, Any]) -> dict[str, Any] | None:
    from ..gating import overlays_of

    return overlays_of(report)


def _stored_view(
    svc: ReportService, run_id: str, n: int, versions: list[int] | None = None
) -> dict[str, Any]:
    stored = svc.get(run_id, n)
    return {
        **stored.report,
        "n": stored.n,
        "versions": versions if versions is not None else svc.versions(run_id),
        "shipped": stored.shipped,
        "path": f"gates/{stored.path.name}",
        "overlays": _overlays(stored.report),
    }


def _rescore_report(c: Ctx, run: Run) -> Any:
    """`gating.run_gates` on the stored take with the arguments `Pipeline.gate` passes (pipeline.py); the mask
    is the one the compositor used, read back from the CAS instead of re-staged. The viewer overlays go to the
    project CAS as at gate time."""
    from .. import gating

    norm = run.normalized or {}
    staging = run.inputs.get(STAGING_KEY) or {}
    outputs = norm.get("outputs") or []
    kind = staging.get("node_kind") or "Edit"
    if (
        not is_pipeline_run(run)
        or staging.get("numeric")
        or kind not in QUOTABLE_KINDS
        or not outputs
        or not norm.get("mask_key")
    ):
        raise ApiError(
            409,
            {
                "error": "not_rescorable",
                "run_id": run.id,
                "detail": "only a still the pipeline composited and gated (an Edit run) is scored again here",
            },
        )
    cas = c.cas_for(run.project_id)
    pv = c.pipeline.packet(run.project_id, run.packet_id)
    plate16 = pv.plate16()
    raw = cv2.imdecode(np.frombuffer(cas.read_bytes(norm["mask_key"]), np.uint8), cv2.IMREAD_GRAYSCALE)
    if raw is None or raw.shape != plate16.shape[:2]:
        raise ApiError(
            409, {"error": "not_rescorable", "run_id": run.id, "detail": "the stored mask is unreadable"}
        )
    out16 = decode_png16(cas.read_bytes(outputs[0]))
    return gating.run_gates(
        plate16,
        out16,
        {"path": pv.root, "manifest": pv.manifest},
        pv.legend,
        pv.tier,
        {"id": run.route.engine_id, "version": str(run.route_dict.get("model_version") or "") or None},
        mask=raw > 127,
        registration=norm.get("ingress"),
        depth_fn=c.pipeline.depth_fn,
        edges=pv.edges(),
        crypto_material=pv.crypto("material") if pv.tier == "A" else None,
        extra_metrics={"textlock_px": norm.get("textlock_px"), "output": outputs[0]},
        overlay_sink=cas,
    )


@router.post("/runs/{run_id}/gate/rescore")
def rescore_route(
    request: Request, run_id: str, body: Annotated[RescoreBody | None, Body()] = None
) -> dict[str, Any]:
    c = ctx(request)
    pid = run_project(c, run_id)
    svc = ReportService(c.pipeline.project_dir(pid))
    if not svc.versions(run_id):
        raise ApiError(409, {"error": "no_report", "run_id": run_id, "detail": "the run was never gated"})
    with c.index.project(pid) as conn:
        run = get_run(conn, run_id)
    try:
        report = _rescore_report(c, run)
    except PipelineError as e:
        raise pipeline_error(e) from e
    path = svc.rescore(run_id, report)
    n = int(Path(path).stem.rsplit(".", 1)[1])  # gates/<run_id>.<n>.json; a run id has no dot
    return _stored_view(svc, run_id, n)


# ---------------------------------------------------------------------------- approval and the run itself


@router.post("/runs/{run_id}/approve")
def approve_route(request: Request, run_id: str, body: ApproveBody, background: BackgroundTasks) -> Any:
    c = ctx(request)
    pid = run_project(c, run_id)
    by = body.by or body.actor
    if not by:
        raise ApiError(422, {"errors": ["say who approves: by (human, auto or agent[:<envelope_id>])"]})
    _check_approver(by)
    start = body.start if body.start is not None else body.by is not None
    with c.index.project(pid) as conn:
        try:
            runs = c.pipeline.approve(
                conn, pid, run_id, by, body.envelope_id, reason=body.reason, now=c.now()
            )
        except CustodyViolation as e:
            raise custody_error(e) from e
        except PipelineError as e:
            raise pipeline_error(e) from e
        except QuoteExpired as e:
            raise ApiError(409, {"error": "quote_expired", "detail": str(e)}) from e
        except IllegalTransition as e:
            raise illegal(e) from e
        except AutoApprovalDenied as e:
            raise ApiError(409, {"error": _refusal_code(e), "detail": str(e)}) from e
        except EnvelopeExceeded as e:
            raise ApiError(422, {"error": "envelope_exceeded", "detail": str(e)}) from e
        except EnvelopeRequired as e:
            raise ApiError(422, {"error": "envelope_required", "detail": str(e)}) from e
        except QuoteNotFound as e:
            raise ApiError(409, {"error": "quote_missing", "detail": str(e)}) from e
        except ValueError as e:
            raise ApiError(422, {"errors": [str(e)]}) from e
        ids = [r.id for r in runs]
        if start:
            for rid in ids:
                c.pipeline.queue(conn, pid, rid)
            background.add_task(c.pipeline.execute_all, pid, ids)
        views = [run_view(conn, rid) for rid in ids]
    if len(views) == 1 and views[0].get("sibling_group") is None:
        return views[0]
    return {"runs": views}


@router.post("/runs/{run_id}/cancel")
def cancel_route(
    request: Request, run_id: str, body: Annotated[CancelBody | None, Body()] = None
) -> dict[str, Any]:
    c = ctx(request)
    body = body or CancelBody()
    pid = run_project(c, run_id)
    with c.index.project(pid) as conn:
        try:
            c.pipeline.cancel(conn, pid, run_id, actor=body.actor, reason=body.reason)
        except IllegalTransition as e:
            raise illegal(e) from e
        return run_view(conn, run_id)


@router.post("/runs/{run_id}/fanout")
def fanout_route(request: Request, run_id: str, body: FanoutBody) -> list[dict[str, Any]]:
    c = ctx(request)
    pid = run_project(c, run_id)
    with c.index.project(pid) as conn:
        try:
            runs = fan_out(conn, run_id, body.n, now=c.now())
        except FanOutError as e:
            raise ApiError(422, {"errors": [str(e)]}) from e
        for r in runs[1:]:
            c.index.remember(r.id, pid)
            c.pipeline.after(conn, pid, r.id, S.DRAFT)
        c.pipeline.publish(
            pid,
            run_id,
            {"kind": "siblings.created", "group": runs[0].sibling_group, "runs": [r.id for r in runs]},
        )
        return [run_view(conn, r.id) for r in runs]


@router.post("/runs/{run_id}/override")
def override_route(request: Request, run_id: str, body: OverrideBody) -> dict[str, Any]:
    c = ctx(request)
    pid = run_project(c, run_id)
    reason = body.reason.strip()
    if len(reason) < MIN_OVERRIDE_REASON:
        raise ApiError(
            422, {"errors": [f"an override needs a reason of at least {MIN_OVERRIDE_REASON} characters"]}
        )
    with c.index.project(pid) as conn:
        run = get_run(conn, run_id)
        if run.state is not S.GATE_FAIL:
            raise ApiError(
                409,
                {
                    "error": "not_overridable",
                    "state": run.state.value,
                    "detail": "only a GATE_FAIL run is overridden",
                },
            )
        append_note(conn, run_id, kind="override", actor=body.by, reason=f"override: {reason}")
        c.pipeline.ledger(pid, "override", run, by=body.by, note=reason)
        c.pipeline.touch_human()
        c.pipeline.publish(
            pid, run_id, {"kind": "run.override", "run_id": run_id, "by": body.by, "reason": reason}
        )
        return run_view(conn, run_id)


def find_scrub(records: list[dict[str, Any]], run_id: str, scrub_log_id: str) -> dict[str, Any] | None:
    """The logged FULL scrub `scrub_log_id` of `run_id` in the ledger, or None (see the module docstring)."""
    for rec in reversed(records):
        if rec.get("kind") != "scrub":
            continue
        data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        if scrub_log_id not in (rec.get("opaque_id"), data.get("scrub_log_id"), data.get("id")):
            continue
        if rec.get("run_id") != run_id:
            return None
        visited = data.get("quadrants_visited")
        full = data.get("full_scrub") is True or (
            isinstance(visited, int) and visited >= 4 and data.get("zoom_100") is True
        )
        return rec if full else None
    return None


@router.post("/runs/{run_id}/accept")
def accept_route(request: Request, run_id: str, body: AcceptBody) -> dict[str, Any]:
    c = ctx(request)
    pid = run_project(c, run_id)
    with c.index.project(pid) as conn:
        view = run_view(conn, run_id)
        if not view["exportable"]:
            raise ApiError(
                409,
                {
                    "error": "not_acceptable",
                    "state": view["state"],
                    "detail": "accept a DONE (or overridden) take",
                },
            )
        scrub = find_scrub(Ledger(c.pipeline.project_dir(pid)).records(), run_id, body.scrub_log_id)
        if scrub is None:
            raise ApiError(
                409,
                {
                    "error": "scrub_required",
                    "code": "SCRUB_REQUIRED",
                    "detail": f"no logged full scrub {body.scrub_log_id!r} of run {run_id}",
                },
            )
        note = {"scrub_log_id": body.scrub_log_id, "decision": "accepted"}
        append_note(conn, run_id, kind="accept", actor=body.by, reason=json.dumps(note, sort_keys=True))
        run = get_run(conn, run_id)
        c.pipeline.ledger(pid, "done", run, by=body.by, data=note)
        c.pipeline.touch_human()
        c.pipeline.publish(pid, run_id, {"kind": "run.accepted", "run_id": run_id, "by": body.by})
        return run_view(conn, run_id)


def _export_target(body: ExportBody) -> tuple[Path, str, str | None]:
    """(out_dir, format, name) of an export body: `out_dir` with `format` and `name`, or the legacy `dest`
    file path read as its folder, stem and suffix (`.tif`/`.tiff` tiff16, `.png` png16, `.exr` exr)."""
    if (body.dest is None) == (body.out_dir is None):
        raise ApiError(422, {"errors": ["give exactly one of dest (a file path) or out_dir (a folder)"]})
    if body.dest is not None:
        dest = Path(body.dest)
        if not dest.is_absolute():
            raise ApiError(422, {"errors": [f"dest must be an absolute file path, got {body.dest!r}"]})
        fmt = SUFFIX_FORMATS.get(dest.suffix.lower())
        if fmt is None:
            detail = f"{dest.name}: export writes {', '.join(SUFFIX_FORMATS)} files"
            raise ApiError(422, {"error": "bad_format", "detail": detail})
        if body.format is not None and body.format != fmt:
            detail = f"{dest.name} is a {fmt} file name; format says {body.format}"
            raise ApiError(422, {"error": "bad_format", "detail": detail})
        if body.name is not None and body.name != dest.stem:
            raise ApiError(422, {"errors": [f"name {body.name!r} and dest {dest.name!r} disagree; give one"]})
        out_dir, name = dest.parent, dest.stem
    else:
        out_dir = Path(body.out_dir or "")
        if not out_dir.is_absolute():
            raise ApiError(
                422, {"errors": [f"out_dir must be an absolute folder path, got {body.out_dir!r}"]}
            )
        fmt = body.format or DEFAULT_EXPORT_FORMAT
        if fmt not in FORMATS:
            raise ApiError(
                422, {"error": "bad_format", "detail": f"format {fmt!r}: use one of {', '.join(FORMATS)}"}
            )
        name = body.name
    if name is not None and not NAME_RE.match(name):
        raise ApiError(
            422,
            {
                "error": "bad_name",
                "detail": f"export name {name!r}: letters, digits, '.', '_' and '-' only (a Windows file name)",
            },
        )
    return out_dir, fmt, name


def _report_n(ref: str | None, run_id: str) -> int | None:
    """n of a `gates/<run_id>.<n>.json` reference, else None (the latest)."""
    stem = Path(ref).stem if ref else ""
    head, _, n = stem.rpartition(".")
    return int(n) if head == run_id and n.isdigit() else None


@router.post("/runs/{run_id}/export")
def export_route(request: Request, run_id: str, body: ExportBody) -> dict[str, Any]:
    c = ctx(request)
    out_dir, fmt, name = _export_target(body)
    pid = run_project(c, run_id)
    project_dir = c.pipeline.project_dir(pid)
    typed = ((body.reason or "").strip() or None) if body.override else None
    with c.index.project(pid) as conn:
        run = get_run(conn, run_id)
        earlier = recorded_override(conn, run_id)
        # an override typed here, on a run that needs one, that says something the record does not yet
        inline = run.state is S.GATE_FAIL and typed is not None and typed != earlier
        done = None
        try:
            done = export(
                run, fmt, out_dir, typed, conn=conn, cas=c.cas_for(pid), project_dir=project_dir, name=name
            )
        except NotExportable as e:
            raise ApiError(409, {"error": "not_exportable", "detail": str(e)}) from e
        except ValueError as e:
            raise ApiError(422, {"errors": [str(e)]}) from e
        finally:
            noted = recorded_override(conn, run_id)
            if inline and (done is not None or (earlier is None and noted is not None)):
                if noted != typed:  # export.export notes an override only when none was recorded before
                    append_note(conn, run_id, kind="override", actor="human", reason=f"override: {typed}")
                c.pipeline.ledger(pid, "override", run, by="human", note=typed)
                c.pipeline.publish(
                    pid, run_id, {"kind": "run.override", "run_id": run_id, "by": "human", "reason": typed}
                )
        assert done is not None  # every failure above raised
        try:  # the version the sidecar copied; the report file itself is never touched
            shipped = ReportService(project_dir).shipped(run_id, _report_n(done.report_ref, run_id)).shipped
        except NoReport:  # a seed run's stub gate wrote no report file
            shipped = False
        c.pipeline.ledger(
            pid,
            "export",
            get_run(conn, run_id),
            data={
                "dest_name": done.path.name,
                "format": done.fmt,
                "sidecar": done.report_path.name,
                "override_reason": done.override_reason,
                "by": "human",
            },
        )
        return {
            "run_id": run_id,
            "path": str(done.path),
            "sidecar": str(done.report_path),
            "format": done.fmt,
            "shipped": shipped,
        }


# ---------------------------------------------------------------------------- envelopes


@router.post("/envelopes", status_code=201)
def create_envelope_route(request: Request, body: EnvelopeBody) -> JSONResponse:
    c = ctx(request)
    try:
        with c.index.project(body.project_id) as conn:
            env = create_envelope(
                conn,
                owner=body.owner,
                max_total_usd=body.max_total_usd,
                max_per_run_usd=body.max_per_run_usd,
                ttl_s=body.ttl_s,
                now=c.now(),
            )
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    c.index.remember(env.id, body.project_id)
    return JSONResponse(dict(env.model_dump(mode="json"), project_id=body.project_id), status_code=201)


@router.get("/envelopes/{envelope_id}")
def get_envelope_route(request: Request, envelope_id: str) -> dict[str, Any]:
    c = ctx(request)
    pid = c.index.locate_envelope(envelope_id)
    if pid is None:
        raise ApiError(404, {"error": "not_found", "envelope_id": envelope_id})
    with c.index.project(pid) as conn:
        env = get_envelope(conn, envelope_id)
        return dict(env.model_dump(mode="json"), project_id=pid)
