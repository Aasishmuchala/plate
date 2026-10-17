"""The Scout node: ask Max for candidate cameras (T37's MaxDirector scout poses), by the plugin queue.

    quote       the scene to scout (a packet of it), `count` poses (1 to 24) and `keep` (leave the temporary
                cameras in the scene) -> a run, local C0, 0 USD
    submit      {action: 'scout', packet_id, count, keep, scene_hash} on the plugin queue; the dock places
                `__PLATE_scout_N` cameras, renders low-resolution previews and answers
                `POST /api/v0/plugin/jobs/{id}/result` {ok, poses: [...], previews?: [cas hash], packets?: [...]}
    normalize   the poses (kept as the dock sent them), the previews that really are in the project's CAS (the
                run's outputs), and the packets the dock rendered in full
    gate        hard: the dock said ok and returned at least one pose; advisory when fewer than `count`

A scout pose is a real V-Ray camera in the real scene: this is how a new angle comes into the project without
inventing one (source-view-only). It only becomes a plate when its packet is sent.
"""

from __future__ import annotations

import json
from typing import Any

from ..pipeline import PipelineError
from .common import Prepared, QuoteContext, RunContext, Settle, cas_has, gate_report

KIND = "Scout"
ACTION = "scout"
MIN_COUNT = 1
MAX_COUNT = 24
DEFAULT_COUNT = 6
MAX_POSES = 64  # what is kept of a dock's answer; a longer list is the dock's bug, not data to store


def prepare(q: QuoteContext) -> Prepared:
    packet_id = q.plate_packet_id()
    pv = q.packet(packet_id)
    if pv.tier != "A":
        raise PipelineError(
            f"packet {packet_id} is tier {pv.tier}: Scout needs a Max scene, which only a Tier A packet has",
            code="tier_required",
        )
    try:
        count = int(q.param("count", default=DEFAULT_COUNT))
    except (TypeError, ValueError) as e:
        raise PipelineError("Scout params.count must be a whole number", code="bad_params") from e
    if not MIN_COUNT <= count <= MAX_COUNT:
        raise PipelineError(f"Scout params.count must be {MIN_COUNT} to {MAX_COUNT}", code="bad_params")
    spec = {
        "action": ACTION,
        "count": count,
        "keep": bool(q.param("keep", default=False)),
        "scene_hash": str((pv.manifest.get("scene_hash") or {}).get("hex") or ""),
        "plate": pv.plate_hash,
    }
    return Prepared(spec, packet_id)


def job_payload(spec: dict[str, Any], packet_id: str) -> dict[str, Any]:
    return {
        "packet_id": packet_id,
        "count": spec["count"],
        "keep": bool(spec.get("keep")),
        "scene_hash": spec.get("scene_hash"),
    }


def settle(pipeline: Any, run: Any, job: Any) -> Settle:
    return Settle("ready")


def result_blob(run: Any, job: Any, settled: Settle) -> dict[str, Any]:
    result = job.result or {}
    return {
        "job_id": job.id,
        "action": job.action,
        "poses": result.get("poses"),
        "previews": result.get("previews"),
        "packets": result.get("packets"),
        "detail": result.get("detail"),
    }


def _list(value: Any) -> list[Any]:
    return list(value)[:MAX_POSES] if isinstance(value, (list, tuple)) else []


def normalize(c: RunContext) -> dict[str, Any]:
    blob = json.loads(c.cas.read_bytes(c.run.result_key).decode("utf-8"))
    poses = _list(blob.get("poses"))
    previews = [k for k in _list(blob.get("previews")) if cas_has(c.cas, k)]
    packets = [p for p in _list(blob.get("packets")) if isinstance(p, str)]
    return {
        "outputs": previews,
        "scout": {
            "job_id": blob.get("job_id"),
            "requested": c.spec["count"],
            "returned": len(poses),
            "poses": poses,
            "packets": packets,
            "previews": previews,
            "kept": bool(c.spec.get("keep")),
            "detail": blob.get("detail"),
        },
    }


def gate(c: RunContext) -> Any:
    from plate_gates.report import Check

    info = c.normalized["scout"]
    returned, wanted = int(info["returned"]), int(info["requested"])
    checks = [
        Check(
            metric_id="scout.poses_returned",
            value=float(returned),
            status="pass" if returned >= 1 else "fail",
        ),
        Check(
            metric_id="scout.poses_vs_requested",
            value=float(returned) / wanted if wanted else None,
            status="pass" if returned >= wanted else "advisory",
        ),
    ]
    notes = [] if returned >= wanted else [f"the dock returned {returned} of {wanted} poses"]
    return gate_report(c, tier=c.packet().tier, metrics={"scout": info}, checks=checks, notes=notes)


def score(report: Any) -> float:
    info = (report.metrics or {}).get("scout") or {}
    return float(info.get("returned") or 0)


__all__ = ["ACTION", "KIND", "gate", "job_payload", "normalize", "prepare", "result_blob", "score", "settle"]
