"""The Light node: match the light of a reference picture, in Max, by the MaxGaffer wrapper (T37).

The daemon cannot relight a scene; Max can. So this node is a hand-off with a receipt:

    quote       packet + reference picture (a CAS hash) + MaxGaffer locks + `deep` -> a run, local C0, 0 USD
    submit      one job on the plugin queue (`plugin_queue.py`): {action: 'light.match', packet_id, camera,
                reference_hash, locks, deep}; the Max dock polls `GET /api/v0/plugin/jobs`, runs the match,
                re-renders and sends a NEW packet that `supersedes` the old one, then answers
                `POST /api/v0/plugin/jobs/{id}/result` {ok, packet_id?, metrics?}
    poll        the run waits (RUNNING) until the job is finished AND the superseding packet is ingested: the
                packet the dock names, else the newest ingested packet that arrived after the job and whose
                manifest says `supersedes: <this packet>`. A packet that was already there when the job was
                queued is stale and never counts.
    normalize   the new packet's display master becomes the output (a PNG16 in the CAS) and the run records
                the new packet id
    gate        hard: the new packet supersedes this one; it is the same camera (source-view-only: a relight
                that moved the camera is a different shot) and the same size

Nothing here is generated: the new light is a V-Ray render of the real scene.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..pipeline import PipelineError
from ..timeutil import parse_iso
from .common import (
    Prepared,
    QuoteContext,
    RunContext,
    Settle,
    finite_or_none,
    gate_report,
    pick,
    same_camera,
)

KIND = "Light"
ACTION = "light.match"
SCAN_LIMIT = 200  # newest ingested packets looked at when the dock does not name the new packet


def prepare(q: QuoteContext) -> Prepared:
    packet_id = q.plate_packet_id()
    pv = q.packet(packet_id)
    if pv.tier != "A":
        raise PipelineError(
            f"packet {packet_id} is tier {pv.tier}: Light relights a Max scene, which only a Tier A packet has",
            code="tier_required",
        )
    reference = q.need_asset(pick(q.params, "reference_hash", "referenceHash"), "Light reference picture")
    locks = q.param("locks", default=[])
    if not isinstance(locks, (list, tuple)) or not all(isinstance(x, str) for x in locks):
        raise PipelineError(
            "Light params.locks must be a list of MaxGaffer parameter keys", code="bad_params"
        )
    camera = q.param("camera") or (pv.manifest.get("camera") or {}).get("name")
    spec = {
        "action": ACTION,
        "camera": camera,
        "reference_hash": reference,
        "locks": sorted(set(locks)),
        "deep": bool(q.param("deep", default=False)),
        "plate": pv.plate_hash,
    }
    return Prepared(spec, packet_id)


def job_payload(spec: dict[str, Any], packet_id: str) -> dict[str, Any]:
    """The arguments the dock receives next to the job's bookkeeping."""
    return {
        "packet_id": packet_id,
        "camera": spec.get("camera"),
        "reference_hash": spec["reference_hash"],
        "locks": list(spec.get("locks") or []),
        "deep": bool(spec.get("deep")),
        "supersedes": packet_id,
    }


def find_successor(
    pipeline: Any, project_id: str, source_id: str, *, named: str | None, after_utc: str | None
) -> tuple[str, str | None, str | None]:
    """(status, packet_id, detail): `ready` with the new packet, `waiting`, or `failed`."""
    with pipeline.index.project(project_id) as conn:
        if named:
            row = conn.execute(
                "SELECT packet_id, status, error FROM packets WHERE packet_id = ?", (named,)
            ).fetchone()
            if row is None:
                return "waiting", None, f"packet {named} has not arrived yet"
            if row["status"] == "failed":
                return "failed", named, f"packet {named} failed ingest: {row['error']}"
            if row["status"] != "ingested":
                return "waiting", named, f"packet {named} is {row['status']}"
            return "ready", named, None
        rows = conn.execute(
            "SELECT packet_id, path, received_utc FROM packets WHERE status = 'ingested' AND packet_id != ? "
            "ORDER BY received_utc DESC LIMIT ?",
            (source_id, SCAN_LIMIT),
        ).fetchall()
    cutoff = parse_iso(after_utc) if after_utc else None
    for row in rows:
        if cutoff is not None and parse_iso(row["received_utc"]) < cutoff:
            break  # newest first: everything further down is older than the job
        try:
            manifest = json.loads((Path(row["path"]) / "manifest.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if manifest.get("supersedes") == source_id:
            return "ready", row["packet_id"], None
    return "waiting", None, None


def settle(pipeline: Any, run: Any, job: Any) -> Settle:
    """The job is done (the dock said ok): is the superseding packet here?"""
    named = (job.result or {}).get("packet_id")
    status, packet_id, detail = find_successor(
        pipeline,
        run.project_id,
        run.packet_id,
        named=named if isinstance(named, str) else None,
        after_utc=job.created_utc,
    )
    return Settle(status, detail, {"packet_id": packet_id})


def result_blob(run: Any, job: Any, settled: Settle) -> dict[str, Any]:
    result = job.result or {}
    return {
        "job_id": job.id,
        "action": job.action,
        "packet_id": settled.extra.get("packet_id"),
        "supersedes": run.packet_id,
        "metrics": result.get("metrics"),
        "detail": result.get("detail"),
    }


def normalize(c: RunContext) -> dict[str, Any]:
    run = c.run
    blob = json.loads(c.cas.read_bytes(run.result_key).decode("utf-8"))
    new_id = blob.get("packet_id")
    if not isinstance(new_id, str):
        raise PipelineError("the plugin finished without a superseding packet", code="no_successor")
    new = c.packet(new_id)
    return {
        "outputs": [c.put_png16(new.plate16())],
        "packet_id": new_id,
        "supersedes": new.manifest.get("supersedes"),
        "job_id": blob.get("job_id"),
        "plugin_metrics": blob.get("metrics"),
    }


def gate(c: RunContext) -> Any:
    from plate_gates.report import Check

    norm, spec = c.normalized, c.spec
    src = c.packet()
    new = c.packet(norm["packet_id"])
    supersedes = new.manifest.get("supersedes") == src.packet_id
    same = same_camera(src.manifest["camera"], new.manifest["camera"])
    shape_ok = src.plate16().shape == c.read_png16(norm["outputs"][0]).shape
    checks = [
        Check(
            metric_id="light.supersedes",
            value=1.0 if supersedes else 0.0,
            status="pass" if supersedes else "fail",
        ),
        Check(metric_id="light.same_camera", value=1.0 if same else 0.0, status="pass" if same else "fail"),
        Check(
            metric_id="light.same_size", value=1.0 if shape_ok else 0.0, status="pass" if shape_ok else "fail"
        ),
    ]
    notes = []
    if not same:
        notes.append("the new packet's camera differs from the source: a Light result must keep the view")
    metrics = {
        "light": {
            "job_id": norm.get("job_id"),
            "source_packet": src.packet_id,
            "new_packet": new.packet_id,
            "camera": spec.get("camera"),
            "locks": spec.get("locks"),
            "deep": spec.get("deep"),
            "plugin_metrics": norm.get("plugin_metrics"),
        }
    }
    return gate_report(c, tier=src.tier, metrics=metrics, checks=checks, notes=notes, engine=None)


def score(report: Any) -> float:
    m = ((report.metrics or {}).get("light") or {}).get("plugin_metrics") or {}
    value = finite_or_none(m.get("de00") if isinstance(m, dict) else None)
    return -value if value is not None else 0.0


__all__ = [
    "ACTION",
    "KIND",
    "find_successor",
    "gate",
    "job_payload",
    "normalize",
    "prepare",
    "result_blob",
    "score",
    "settle",
]
