"""Packet routes: `POST /hash`, `POST /packets`, `GET /packets/{id}`, `GET /packets/{id}/events` (PACKET.md 9).

`POST /packets {packet_path, sender}` needs `X-Plate-Packet-Version` whose major.minor the daemon reads
(`426 {"supported": [...]}` otherwise). The folder is validated and EVERY file it names is re-hashed with
BLAKE3 before anything is accepted (`ingest.prepare_ingest` -> `plate_spec.validate_packet_dir`, check_files):
a mismatch answers `422 {"errors": [...]}` even though the plugin got its hashes from `POST /hash`. Then
202 (new: hashing into the index finishes in a background task, inline when `settings.sync_ingest`),
200 `known` (the same packet again), 409 (the same packet id with a different manifest). The packet stays
where it is (the NAS): the index and the project's `packets.json` hold it by reference.

`POST /hash {paths, payloads}` is BLAKE3 for the stdlib-only Max plugin (Decision 5b), confined to the packets
folders. `GET /packets/{id}/events` streams the ingest log as `text/event-stream` and ends on `ingest.done`
or `ingest.failed` (or after `wait_s`), so a client reconnects with `Last-Event-ID` instead of holding a socket.

The Plate-linking contract (what the UI's packet picker and a Plate node read; both are read-only):

- `GET /projects/{project_id}/packets[?status=ingested|all]` lists the project's packets (index rows ordered by
  `received_utc`): every `packet_entry` field plus `packet_id`, `plate {role, relpath, blake3, width, height}`
  (the display master the manifest names), `supersedes`, `superseded_by` (the latest INGESTED packet of the
  project whose manifest supersedes this one) and `camera_name`. A manifest the share no longer serves gives
  null for the manifest-derived fields; the row still lists. 404 for a project with no `project.json`.
- `GET /packets/{id}/summary` is the one-packet view (`409 not_ingested` until the ingest is done): size, plate,
  camera, policy and the legend entries with `editable` / `locked` / `planar` resolved. The legend stays on
  loopback behind the token.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..config import check_project_id
from ..hashing import BadHashRequest, HashRefused, PayloadTooLarge, hash_paths, hash_payloads
from ..index import PACKET_COLUMNS
from ..ingest import PacketConflict, PacketInvalid, begin_ingest, get_packet, packet_events, prepare_ingest
from ..pipeline import PipelineError
from . import (
    API,
    MAX_WAIT_S,
    SSE_POLL_S,
    SSE_RETRY_MS,
    ApiError,
    ctx,
    cursor,
    packet_project,
    pipeline_error,
    require_token,
    sse_frame,
)

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?\s*$")
PLATE_ROLES = ("display_tiff16", "client_plate")  # tier A display master, tier C client plate
# The schema's id patterns, matched with `fullmatch`: the validator's `pattern` is a `re.search`, where `$`
# also matches before a trailing newline, so "pkt_...\n" validates and would name a folder and an event URL.
PACKET_ID_RE = re.compile(r"pkt_[0-9A-HJKMNP-TV-Z]{26}")
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MANIFEST_IDS = (
    ("packet_id", PACKET_ID_RE),
    ("project_id", SLUG_RE),
    ("shot_id", SLUG_RE),
    ("supersedes", PACKET_ID_RE),
)


def major_minor(header: str | None) -> str | None:
    m = VERSION_RE.match(header or "")
    return f"{int(m.group(1))}.{int(m.group(2))}" if m else None


def require_packet_version(
    request: Request, x_plate_packet_version: Annotated[str | None, Header()] = None
) -> str:
    # Module level, not a closure: under `from __future__ import annotations` FastAPI resolves a
    # parameter's `Depends(...)` from module globals, and the seed's closure silently became a query param.
    supported = list(request.app.state.settings.packet_versions)
    mm = major_minor(x_plate_packet_version)
    if mm is None or mm not in supported:
        raise ApiError(426, {"supported": supported})
    return mm


class Sender(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class PacketPost(BaseModel):
    packet_path: str = Field(min_length=1)
    sender: Sender


class HashBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(default_factory=list, max_length=100_000)
    payloads: dict[str, str] = Field(default_factory=dict)


def id_errors(manifest: dict[str, Any]) -> list[str]:
    """The manifest's ids that do not match the schema's patterns by `fullmatch` (a trailing newline, say)."""
    return [
        f"manifest.json: {key} {value!r} does not match the schema's pattern"
        for key, rx in MANIFEST_IDS
        if isinstance(value := manifest.get(key), str) and not rx.fullmatch(value)
    ]


# ---------------------------------------------------------------------------- hashing for the plugin


@router.post("/hash")
def hash_route(request: Request, body: HashBody) -> dict[str, Any]:
    c = ctx(request)
    if not body.paths and not body.payloads:
        raise ApiError(422, {"errors": ["give paths and/or payloads to hash"]})
    try:
        hashes = hash_paths(body.paths, c.settings.packets_dirs)
        named = hash_payloads(body.payloads)
    except PayloadTooLarge as e:
        raise ApiError(413, {"error": "payload_too_large", "detail": str(e)}) from e
    except BadHashRequest as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    except HashRefused as e:
        raise ApiError(403, {"error": "forbidden_path", "detail": str(e)}) from e
    except FileNotFoundError as e:
        raise ApiError(404, {"error": "not_found", "path": str(e)}) from e
    clash = sorted(set(hashes) & set(named))
    if clash:
        raise ApiError(422, {"errors": [f"payload names collide with paths: {clash[:3]}"]})
    return {"hashes": {**hashes, **named}}


# ---------------------------------------------------------------------------- packets


@router.post("/packets", status_code=202)
def post_packet(
    request: Request,
    body: PacketPost,
    background: BackgroundTasks,
    version: Annotated[str, Depends(require_packet_version)],
) -> JSONResponse:
    c = ctx(request)
    try:
        prepared = prepare_ingest(body.packet_path)
    except PacketInvalid as e:
        raise ApiError(422, {"errors": e.errors}) from e
    if errors := id_errors(prepared.manifest):
        raise ApiError(422, {"errors": errors})
    if major_minor(prepared.manifest.get("packet_version")) != version:
        raise ApiError(
            422,
            {
                "errors": [
                    f"X-Plate-Packet-Version {version} != manifest packet_version "
                    f"{prepared.manifest.get('packet_version')}"
                ]
            },
        )
    packet_id = prepared.packet_id
    owner = c.index.locate_packet(packet_id) or prepared.project_id
    # Packet-first registration: a packet may arrive before its project, so this is one of the few opens that
    # creates the index (index.UnknownProject everywhere else, T144).
    with c.index.project(owner, create=True) as conn:
        try:
            status, _rec = begin_ingest(
                conn, body.packet_path, sender=body.sender.model_dump(), prepared=prepared, now=c.now()
            )
        except PacketConflict as e:
            raise ApiError(409, {"error": "conflict", "packet_id": packet_id, "detail": str(e)}) from e
    c.index.remember(packet_id, owner)
    if status == "known":
        return JSONResponse({"packet_id": packet_id, "status": "known"}, status_code=200)
    if c.settings.sync_ingest:
        c.finish_packet(owner, packet_id)
    else:
        background.add_task(c.finish_packet, owner, packet_id)
    return JSONResponse(
        {"packet_id": packet_id, "status": "ingesting", "events_url": f"{API}/packets/{packet_id}/events"},
        status_code=202,
    )


@router.get("/packets/{packet_id}")
def get_packet_route(request: Request, packet_id: str) -> JSONResponse:
    c = ctx(request)
    pid = packet_project(c, packet_id)
    with c.index.project(pid) as conn:
        rec = get_packet(conn, packet_id)
    project = c.pipeline.load_project(pid)
    rec["registered"] = bool(project is not None and packet_id in project.packets())
    return JSONResponse(rec)


@router.get("/packets/{packet_id}/events")
async def packet_events_route(
    request: Request,
    packet_id: str,
    after: Annotated[int | None, Query()] = None,
    wait_s: Annotated[float, Query(ge=0, le=MAX_WAIT_S)] = 60.0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    c = ctx(request)
    project_id = await run_in_threadpool(packet_project, c, packet_id)

    def read(seq: int) -> tuple[list[dict[str, Any]], str]:
        with c.index.project(project_id) as conn:
            return packet_events(conn, packet_id, seq), get_packet(conn, packet_id, with_blobs=False)[
                "status"
            ]

    async def gen() -> AsyncIterator[str]:
        seq = cursor(after, last_event_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        yield f"retry: {SSE_RETRY_MS}\n\n"
        while True:
            evs, status = await run_in_threadpool(read, seq)
            for ev in evs:
                seq = ev["seq"]
                yield sse_frame(ev, seq)
                if ev["type"] in ("ingest.done", "ingest.failed"):
                    return
            if status != "ingesting" and not evs:
                return
            if loop.time() >= deadline:
                return
            await asyncio.sleep(SSE_POLL_S)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------- listing and summarising (Plate linking)


def plate_of(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    """The display master a manifest names: `{role, relpath, blake3, width, height}` (the file entry's own
    fields), or None when it names neither `display_tiff16` nor `client_plate`."""
    files = (manifest or {}).get("files") or {}
    for role in PLATE_ROLES:
        ref = files.get(role)
        if isinstance(ref, dict):
            return {
                "role": role,
                "relpath": ref.get("path"),
                "blake3": (ref.get("hash") or {}).get("hex"),
                "width": ref.get("width"),
                "height": ref.get("height"),
            }
    return None


def read_manifest(folder: str | Path) -> dict[str, Any] | None:
    """The manifest at `folder/manifest.json`, or None when the folder (a NAS share) is not there to read."""
    try:
        doc = json.loads((Path(folder) / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def packet_row(row: Any, manifest: dict[str, Any] | None, superseded_by: str | None) -> dict[str, Any]:
    camera = (manifest or {}).get("camera")
    return {
        "packet_id": row["packet_id"],
        **{k: row[k] for k in PACKET_COLUMNS if row[k] is not None},
        "plate": plate_of(manifest),
        "supersedes": (manifest or {}).get("supersedes"),
        "superseded_by": superseded_by,
        "camera_name": camera.get("name") if isinstance(camera, dict) else None,
    }


@router.get("/projects/{project_id}/packets")
def list_packets(
    request: Request,
    project_id: str,
    status: Annotated[Literal["ingested", "all"], Query()] = "ingested",
) -> list[dict[str, Any]]:
    c = ctx(request)
    try:
        check_project_id(project_id)
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    if c.pipeline.load_project(project_id) is None:
        raise ApiError(404, {"error": "not_found", "project_id": project_id})
    with c.index.project(project_id) as conn:
        rows = conn.execute(
            f"SELECT packet_id, {', '.join(PACKET_COLUMNS)} FROM packets ORDER BY received_utc, packet_id"
        ).fetchall()
    manifests = {r["packet_id"]: read_manifest(r["path"]) for r in rows}
    successor: dict[str, str] = {}  # superseded packet id -> the latest ingested packet that supersedes it
    for r in rows:
        old = (manifests[r["packet_id"]] or {}).get("supersedes")
        if r["status"] == "ingested" and isinstance(old, str):
            successor[old] = r["packet_id"]
    return [
        packet_row(r, manifests[r["packet_id"]], successor.get(r["packet_id"]))
        for r in rows
        if status == "all" or r["status"] == "ingested"
    ]


@router.get("/packets/{packet_id}/summary")
def packet_summary(request: Request, packet_id: str) -> dict[str, Any]:
    c = ctx(request)
    project_id = packet_project(c, packet_id)
    with c.index.project(project_id) as conn:
        rec = get_packet(conn, packet_id, with_blobs=False)
    if rec["status"] != "ingested":
        raise ApiError(409, {"error": "not_ingested", "status": rec["status"]})
    try:
        pv = c.pipeline.packet(project_id, packet_id)
    except PipelineError as e:
        raise pipeline_error(e) from e
    except (OSError, ValueError) as e:  # the share went away (or the folder was edited) after the ingest
        raise ApiError(
            409, {"error": "packet_unreadable", "packet_id": packet_id, "detail": f"{type(e).__name__}: {e}"}
        ) from e
    manifest = pv.manifest
    plate = plate_of(manifest)
    camera = manifest.get("camera")
    if plate is not None and plate["width"] is not None and plate["height"] is not None:
        size = {"width": plate["width"], "height": plate["height"]}
    else:
        res = camera.get("resolution") if isinstance(camera, dict) else None
        size = {"width": res.get("width"), "height": res.get("height")} if isinstance(res, dict) else None
    editable, locked = set(pv.editable_ids()), set(pv.locked_ids())
    entries = [
        {
            "id": e.get("id"),
            "kind": e.get("kind"),
            "name": e.get("name"),
            "class": e.get("class"),
            "pixel_share": e.get("pixel_share"),
            "editable": e.get("id") in editable,
            "locked": e.get("id") in locked,
            "planar": bool(e.get("planar", False)),
        }
        for e in ((pv.legend or {}).get("entries") or [])
        if isinstance(e, dict)
    ]
    return {
        "packet_id": packet_id,
        "project_id": project_id,
        "shot_id": manifest.get("shot_id"),
        "kind": manifest.get("kind"),
        "tier": manifest.get("tier"),
        "status": rec["status"],
        "size": size,
        "plate": plate,
        "supersedes": manifest.get("supersedes"),
        "camera": camera,
        "policy": manifest.get("policy"),
        "legend": {"entries": entries},
    }
