"""Run routes: quote -> approve -> run, the gate report, override and accept, best-of-N, export, envelopes.

- `GET /runs/{id}`: the run (`models.Run` JSON) plus `override {reason, by, at}`, `accepted {...}` and
  `exportable`; `GET /runs/{id}/events` its append-only event log.
- `POST /runs/{id}/approve {by, envelope_id?, reason?, start?}`: QUOTED -> APPROVED as `human`, `auto` or
  `agent[:<envelope_id>]` (a best-of-N group approves as one, against the sum of its prices), then QUEUED
  and run in the background by the pipeline (`start`, default true). The seed's `{actor}` body approves
  only (`start` defaults to false), which keeps `POST /queue` and `POST /step` the dev driver they were.
  Refusals: 403 custody, 409 `quote_expired` (the run is then EXPIRED) / illegal transition / `auto_cap`,
  `daily_cap`, `unattended_cap`, 422 `envelope_required` / `envelope_exceeded`.
- `POST /runs/{id}/cancel`, `POST /runs/{id}/fanout {n}` (a QUOTED run becomes sibling 0 of n under one
  quote; the list is returned), `GET /runs/{id}/gate[?n=]` (the stored report, `gates/<run>.<n>.json`,
  with `n`, `versions` and `shipped`; 404 before gating).
- `POST /runs/{id}/override {reason, by}`: a GATE_FAIL run becomes exportable; the reason (10 characters at
  least) is in the run's event log and the ledger. `POST /runs/{id}/accept {scrub_log_id, by?}`: a DONE (or
  overridden) take is accepted only against a logged FULL scrub of this run: a ledger record of kind
  `scrub` (written by `POST /runs/{id}/scrub`, T38) whose `opaque_id` or `data.scrub_log_id` is the id and
  whose `data.full_scrub` is true, or `data.quadrants_visited >= 4` with `data.zoom_100`. Else 409.
- `POST /runs/{id}/export {dest, override?, reason?}`: a pipeline run exports its gated composite
  (`outputs[0]`), a seed run its result blob; GATE_FAIL needs the override (recorded or in the body).
- The seed's dev routes stay: `POST /runs` (a run outside the canvas), `/runs/{id}/quote`, `/queue`, `/step`,
  and `POST|GET /envelopes`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..adapter import SimulatedCrash
from ..custody import CustodyViolation
from ..export import MIN_OVERRIDE_REASON, NotExportable, export_run
from ..fsutil import write_bytes_atomic
from ..ledger import Ledger
from ..machine import IllegalTransition, RunNotFound, append_note, create_run, get_run, run_events
from ..models import RunCreate
from ..pipeline import PipelineError, is_pipeline_run
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
from . import API, ApiError, ctx, custody_error, illegal, pipeline_error, require_token, run_project

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

S = RunState
APPROVER_PREFIXES = ("human", "auto", "agent")
NOTE_KINDS = ("override", "accept")


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
    model_config = ConfigDict(extra="forbid")
    dest: str = Field(min_length=1)
    override: bool = False
    reason: str | None = None


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


@router.post("/runs", status_code=201)
def create_run_route(request: Request, body: RunCreate) -> JSONResponse:
    c = ctx(request)
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


@router.post("/runs/{run_id}/quote")
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


@router.post("/runs/{run_id}/queue")
def queue_route(request: Request, run_id: str) -> dict[str, Any]:
    c = ctx(request)
    pid = run_project(c, run_id)
    with c.index.project(pid) as conn:
        try:
            return c.pipeline.queue(conn, pid, run_id).model_dump(mode="json")
        except IllegalTransition as e:
            raise illegal(e) from e


@router.post("/runs/{run_id}/step")
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
            stored = svc.get(run_id, want)
            return {
                **stored.report,
                "n": stored.n,
                "versions": versions,
                "shipped": stored.shipped,
                "path": f"gates/{stored.path.name}",
            }
    with c.index.project(pid) as conn:
        run = get_run(conn, run_id)
    if run.gate_report is None or n is not None:
        raise ApiError(404, {"error": "no_report", "run_id": run_id, "state": run.state.value})
    return {**run.gate_report, "n": None, "versions": [], "shipped": False}


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


def _export_output(
    conn: sqlite3.Connection, run_id: str, key: str, dest: Path, override: bool, reason: str | None, cas: Any
) -> Path:
    """export.export_run's rules, for the run's gated composite rather than the engine's raw bytes."""
    run = get_run(conn, run_id)
    if run.state is S.GATE_FAIL:
        if not override:
            raise NotExportable(
                f"run {run_id} failed its gates; export needs an override and a written reason"
            )
        if reason is None or len(reason.strip()) < MIN_OVERRIDE_REASON:
            raise NotExportable(
                f"a GATE_FAIL override needs a reason of at least {MIN_OVERRIDE_REASON} characters"
            )
    elif run.state is not S.DONE:
        raise NotExportable(
            f"run {run_id} is {run.state.value}; only DONE (or GATE_FAIL with override) exports"
        )
    if not cas.verify(key):
        raise NotExportable(f"output {key} is missing or fails its BLAKE3 check")
    if dest.exists():
        raise NotExportable(f"{dest} already exists; exports never overwrite")
    if run.state is S.GATE_FAIL:
        append_note(conn, run_id, kind="override", actor="human", reason=f"override: {reason}")
    write_bytes_atomic(dest, cas.read_bytes(key))
    return dest


@router.post("/runs/{run_id}/export")
def export_route(request: Request, run_id: str, body: ExportBody) -> dict[str, Any]:
    c = ctx(request)
    dest = Path(body.dest)
    if not dest.is_absolute():
        raise ApiError(422, {"errors": [f"dest must be an absolute file path, got {body.dest!r}"]})
    pid = run_project(c, run_id)
    with c.index.project(pid) as conn:
        view = run_view(conn, run_id)
        override, reason = body.override, body.reason
        if not override and view["override"] is not None:
            override, reason = True, view["override"]["reason"]
        run = get_run(conn, run_id)
        try:
            if is_pipeline_run(run) and run.outputs:
                out = _export_output(conn, run_id, run.outputs[0], dest, override, reason, c.cas_for(pid))
            else:
                out = export_run(conn, run_id, dest, override=override, reason=reason, cas=c.cas_for(pid))
        except NotExportable as e:
            raise ApiError(409, {"error": "not_exportable", "detail": str(e)}) from e
        c.pipeline.ledger(pid, "export", get_run(conn, run_id), data={"dest_name": dest.name})
        return {"run_id": run_id, "path": str(out)}


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
