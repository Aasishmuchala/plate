"""Client-revision invalidation and approved-layer carry-over (BUILD_PLAN decision 17, PACKET.md section 5).

A client change invalidates only the cameras whose `view_hash` changed. A new packet for such a camera sets
`supersedes`; approved AI layers then carry over onto the new plate wherever the IDs they touched are unchanged.

    on_new_packet(project, packet, *, conn)               -> Revision        mark, re-bind
    affected_cameras(old_scene_hash, new_packets, ...)    -> [camera name]   by view_hash change
    carry_over(old_packet, new_packet, approved_layers)   -> [{run_id, status, reason, ...}]

`on_new_packet` is the daemon's reaction to a packet that `supersedes` another. Every run on the old packet gets a
`superseded` note in its `run_events` (append-only, like the `accept` and `override` notes; the state machine has no
SUPERSEDED state and a DONE run stays DONE, so the exports and history of the old plate keep working) and every
Plate node bound to the old packet is re-bound to the new one in `project.json`. A packet whose `view_hash` equals
its predecessor's changed nothing a pixel can see: the camera keeps its packet and nothing is touched (`force=True`
overrides). The caller holds the project lock (`Project.save_graph` does not take it) and passes the connection of
the project's index. Runs that have not reached a vendor are marked, not cancelled: cancelling spends nothing and
changes no pixel, so it stays a human or pipeline decision.

`affected_cameras` pairs each new packet with the old one it replaces (its `supersedes`, else the same camera name
in the old scene) and returns the cameras whose `view_hash` differs, plus cameras with no old counterpart. The old
scene's packets come from `old_packets=` or from the index (`conn=`, every ingested packet with that scene hash).

`carry_over` decides, layer by layer. A layer carries when

- every ID it touched has an identical legend entry in the new legend: name, material and class equal AND the
  entry's `digest` equal (PACKET.md 4.2: the BLAKE3 of the node's or material's record, "so carry-over can tell an
  unchanged ID from an edited one with the same name"; a recoloured wall keeps its name, material name and class,
  so only the digest says it changed). An object whose own material changed under it counts as a material change;
- the union of those IDs' pixels in the new packet overlaps the old packet's by IoU >= 0.98;
- none of them is locked by the new packet's policy;
- the stored AI pixels, re-composited onto the new plate inside the new mask with `composite_masked`, still pass
  the quick gates: ECC registration of the stored pixels to the new plate on the ring outside the mask
  (`registration.residual_px`, `registration.shift_px`) and `outside_mask.de00` (the new plate, bit for bit,
  outside the mask). The layer was tone matched when it was approved, so no tone match runs again: matching it to
  a ring that may be the very wall that changed colour would alter approved pixels.

The new mask is the layer's stored mask restricted to the new IDs' pixels, less the pixels of the new packet's other
locked IDs (the same contract `Pipeline.editable_mask` keeps), so the AI never lands outside what the new plate
says the IDs cover and never beyond what was approved. A layer that fails any step is `dropped` with a `reason`
(`material changed`, `id removed`, `id mask moved`, ...) and a machine `code`; nothing is written for it.

With `project=` and `conn=` a carried layer becomes a new run on the new packet. The project folder is the truth
(decision 6): a `done` ledger record with `data.carried_from` is appended first, then the index row is built from
that same record by `index.apply_run` (the path `plate reindex` takes), so a rebuilt index holds the same run. The
run has `engine_id: carry-over`, no quote, no spend and no `submitted` record (the ledger counts a generation at
`submitted`, so it counts none), a `provider_job_id` of `carried-<old run id>` (DONE needs one), the composite in the
project's CAS with lineage embedded, a versioned gate report holding the quick gates, and `carried` and `superseded`
notes. Carrying the same layer onto the same packet twice returns the first run. Without them `carry_over` only
decides and nothing is written. A caller that holds the project lock passes `ledger=Ledger(root, lock=lock)`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .cas import CAS, CASError, check_key
from .db import dumps, loads, tx
from .gate_reports import Service as ReportService
from .idempotency import new_id
from .index import apply_run
from .ledger import Ledger
from .machine import annotate, append_note, get_run
from .models import Run
from .pipeline import DEFAULT_FEATHER_PX, PacketView, PipelineError, decode_png16, encode_png16
from .projects import Project
from .states import RunState
from .timeutil import iso, utcnow

__all__ = [
    "CARRY_ENGINE_ID",
    "CARRY_ROUTE",
    "CameraChange",
    "IOU_MIN",
    "Revision",
    "RevisionError",
    "affected_cameras",
    "camera_changes",
    "carry_over",
    "is_superseded",
    "on_new_packet",
    "superseded_info",
    "superseded_runs",
]

log = logging.getLogger(__name__)

IOU_MIN = 0.98
CARRY_ENGINE_ID = "carry-over"
# The route of a carried run: derived on this machine from pixels a vendor already returned, nothing leaves.
CARRY_ROUTE: dict[str, Any] = {
    "engine_id": CARRY_ENGINE_ID,
    "provider": "plate",
    "region": "local",
    "route_id": "carry-over-local",
    "custody_grade": "C0",
    "custody_status": "confirmed",
    "upload_visibility": "local",
    "model_version": CARRY_ENGINE_ID,
}
SUPERSEDED = "superseded"
CARRIED = "carried"
ACTOR = "revisions"
SAME_FIELDS = ("name", "material", "class")
PLATE_ROLES = ("display_tiff16", "client_plate")

# code -> the reason a dropped layer reports
REASONS = {
    "layer_invalid": "layer record is malformed",
    "run_unknown": "run not found",
    "run_elsewhere": "run is not on the old packet",
    "id_missing": "id not in the old legend",
    "id_removed": "id removed",
    "legend_changed": "legend entry changed",
    "material_changed": "material changed",
    "object_changed": "object changed",
    "id_locked": "id locked in the new packet",
    "mask_unavailable": "id mask unavailable",
    "mask_moved": "id mask moved",
    "mask_empty": "mask empty",
    "pixels_missing": "stored pixels missing",
    "size_mismatch": "size changed",
    "gates_failed": "quick gates failed",
}


class RevisionError(RuntimeError):
    """The caller's setup is wrong (a packet not in the index, no cas); never a verdict on a layer."""


class _Drop(Exception):
    def __init__(self, code: str, detail: str | None = None, **extra: Any):
        super().__init__(detail or REASONS[code])
        self.code = code
        self.detail = detail
        self.extra = extra


# ---------------------------------------------------------------------------- packets as the revision sees them


@dataclass(frozen=True)
class PacketMeta:
    packet_id: str
    supersedes: str | None
    scene_hash: str | None
    view_hash: str | None
    camera: str | None
    shot_id: str | None
    plate_hash: str | None


def _hex(h: Any) -> str | None:
    if isinstance(h, Mapping):
        h = h.get("hex")
    return h if isinstance(h, str) and h else None


def _read_manifest(path: Path | str) -> dict[str, Any] | None:
    p = Path(path)
    p = p if p.name == "manifest.json" else p / "manifest.json"
    try:
        doc = json.loads(p.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _plate_hash(manifest: Mapping[str, Any]) -> str | None:
    files = manifest.get("files") or {}
    for role in PLATE_ROLES:
        ref = files.get(role)
        if isinstance(ref, Mapping) and _hex(ref.get("hash")):
            return _hex(ref["hash"])
    return None


def _meta(packet: Any) -> PacketMeta:
    """A PacketView, a manifest, an index row, or a packet folder / manifest.json path."""
    doc: Mapping[str, Any] | None
    if isinstance(packet, (str, Path)):
        doc = _read_manifest(packet)
        if doc is None:
            raise RevisionError(f"no readable manifest.json at {packet}")
    elif isinstance(getattr(packet, "manifest", None), Mapping):
        doc = packet.manifest
    elif isinstance(packet, sqlite3.Row):
        doc = dict(packet)
    elif isinstance(packet, Mapping):
        doc = packet
    else:
        raise RevisionError(f"not a packet: {type(packet).__name__}")
    full = doc
    if "camera" not in doc and isinstance(
        doc.get("path"), str
    ):  # an index row: the manifest names the camera
        full = _read_manifest(doc["path"]) or doc
    camera = (full.get("camera") or {}).get("name") if isinstance(full.get("camera"), Mapping) else None
    pid = doc.get("packet_id") or full.get("packet_id")
    if not isinstance(pid, str) or not pid:
        raise RevisionError("a packet needs a packet_id")
    return PacketMeta(
        packet_id=pid,
        supersedes=full.get("supersedes") or None,
        scene_hash=_hex(full.get("scene_hash")) or _hex(doc.get("scene_hash")),
        view_hash=_hex(full.get("view_hash")) or _hex(doc.get("view_hash")),
        camera=camera if isinstance(camera, str) and camera else None,
        shot_id=full.get("shot_id") or doc.get("shot_id"),
        plate_hash=_plate_hash(full),
    )


def _camera_key(meta: PacketMeta) -> str:
    return meta.camera or meta.shot_id or meta.packet_id


# ---------------------------------------------------------------------------- which cameras a change reaches


@dataclass(frozen=True)
class CameraChange:
    camera: str
    packet_id: str
    old_packet_id: str | None
    changed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "camera": self.camera,
            "packet_id": self.packet_id,
            "old_packet_id": self.old_packet_id,
            "changed": self.changed,
            "reason": self.reason,
        }


def _old_scene_packets(old_scene_hash: str, conn: sqlite3.Connection) -> list[PacketMeta]:
    rows = conn.execute(
        "SELECT * FROM packets WHERE scene_hash = ? AND status = 'ingested' ORDER BY received_utc, packet_id",
        (old_scene_hash,),
    ).fetchall()
    return [_meta(r) for r in rows]


def camera_changes(
    old_scene_hash: str | None,
    new_packets: Iterable[Any],
    *,
    old_packets: Iterable[Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[CameraChange]:
    """One `CameraChange` per camera of `new_packets`, in order, unchanged cameras included."""
    if old_packets is not None:
        olds = [_meta(p) for p in old_packets]
    elif conn is not None and old_scene_hash:
        olds = _old_scene_packets(old_scene_hash, conn)
    else:
        raise ValueError("give old_packets= or conn= (and an old_scene_hash) to find the old scene's packets")
    by_id = {o.packet_id: o for o in olds}
    by_camera: dict[str, PacketMeta] = {}
    for o in olds:
        by_camera[_camera_key(o)] = o  # a later packet of the same camera is the newer one
    out: list[CameraChange] = []
    seen: set[str] = set()
    for raw in new_packets:
        new = _meta(raw)
        cam = _camera_key(new)
        if cam in seen:
            continue
        seen.add(cam)
        old = by_id.get(new.supersedes or "") or by_camera.get(cam)
        if old is None:
            out.append(CameraChange(cam, new.packet_id, None, True, "new camera: no packet in the old scene"))
        elif not (old.view_hash and new.view_hash):
            out.append(CameraChange(cam, new.packet_id, old.packet_id, True, "view_hash unknown"))
        elif old.view_hash != new.view_hash:
            out.append(CameraChange(cam, new.packet_id, old.packet_id, True, "view_hash changed"))
        else:
            out.append(
                CameraChange(
                    cam, new.packet_id, old.packet_id, False, "view_hash unchanged: keeps its packet"
                )
            )
    return out


def affected_cameras(
    old_scene_hash: str | None,
    new_packets: Iterable[Any],
    *,
    old_packets: Iterable[Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """The cameras whose `view_hash` changed between the old scene and `new_packets` (names, in order).
    Cameras whose `view_hash` is unchanged keep their packets and are not listed."""
    return [
        c.camera
        for c in camera_changes(old_scene_hash, new_packets, old_packets=old_packets, conn=conn)
        if c.changed
    ]


# ---------------------------------------------------------------------------- a new packet that supersedes


@dataclass
class Revision:
    packet_id: str
    supersedes: str | None
    invalidated: bool
    reason: str
    superseded_runs: list[str] = field(default_factory=list)
    already_superseded: list[str] = field(default_factory=list)
    rebound_nodes: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "supersedes": self.supersedes,
            "invalidated": self.invalidated,
            "reason": self.reason,
            "superseded_runs": list(self.superseded_runs),
            "already_superseded": list(self.already_superseded),
            "rebound_nodes": [dict(n) for n in self.rebound_nodes],
        }


def superseded_info(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """`{by_packet, by, at}` when the run was marked superseded, else None."""
    row = conn.execute(
        "SELECT actor, reason, at_utc FROM run_events WHERE run_id = ? AND kind = ? ORDER BY seq DESC LIMIT 1",
        (run_id, SUPERSEDED),
    ).fetchone()
    if row is None:
        return None
    try:
        body = json.loads(row["reason"] or "{}")
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    return {"by_packet": body.get("by_packet"), "by": row["actor"], "at": row["at_utc"]}


def is_superseded(conn: sqlite3.Connection, run_id: str) -> bool:
    return superseded_info(conn, run_id) is not None


def superseded_runs(conn: sqlite3.Connection, packet_id: str) -> list[str]:
    """Run ids on `packet_id` that carry the superseded note."""
    rows = conn.execute(
        """SELECT r.id FROM runs r WHERE r.packet_id = ?
           AND EXISTS (SELECT 1 FROM run_events e WHERE e.run_id = r.id AND e.kind = ?)
           ORDER BY r.created_utc, r.id""",
        (packet_id, SUPERSEDED),
    ).fetchall()
    return [r[0] for r in rows]


def _publish(bus: Any, project_id: str, run_id: str | None, event: dict[str, Any]) -> None:
    if bus is None:
        return
    try:
        bus.publish(run_id, event, project_id=project_id)
    except Exception as e:  # the bus is for watchers; a failed publish never undoes a revision
        log.warning("event %s for %s not published: %s", event.get("kind"), run_id, e)


def _rebind(graph: Mapping[str, Any], old: str, new: str, old_plate: str | None, new_plate: str | None):
    """(new graph, [{node_id, from, to}]) with every node bound to `old` re-bound to `new`. A node is the wire
    form `{"id", "kind", "packetId", ...}` or `{"id", "data": {...}}`; `packetId` and `packet_id` are both read.
    A Plate's `outputHash` (its beauty) follows when it names the old plate."""
    nodes = json.loads(json.dumps(graph.get("nodes") or []))
    moved: list[dict[str, str]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        holders = [node] + ([node["data"]] if isinstance(node.get("data"), dict) else [])
        hit = False
        for h in holders:
            for key in ("packetId", "packet_id"):
                if h.get(key) == old:
                    h[key] = new
                    hit = True
                    if old_plate and new_plate and h.get("outputHash") == old_plate:
                        h["outputHash"] = new_plate
        if hit:
            moved.append({"node_id": str(node.get("id")), "from": old, "to": new})
    return {**graph, "nodes": nodes}, moved


def on_new_packet(
    project: Project,
    packet: Any,
    *,
    conn: sqlite3.Connection,
    bus: Any = None,
    force: bool = False,
) -> Revision:
    """React to an ingested packet. With `supersedes` set: mark the old packet's runs `superseded` and re-bind the
    Plate nodes bound to it. See the module docstring for the view_hash guard, locking and `force`."""
    new = _meta(packet)
    old_id = new.supersedes
    if not old_id:
        return Revision(new.packet_id, None, False, "no supersedes: nothing to invalidate")
    row = conn.execute("SELECT * FROM packets WHERE packet_id = ?", (old_id,)).fetchone()
    old = _meta(row) if row is not None else None
    if not force and old is not None and old.view_hash and new.view_hash and old.view_hash == new.view_hash:
        return Revision(new.packet_id, old_id, False, "view_hash unchanged: the camera keeps its packet")

    marked: list[str] = []
    already: list[str] = []
    note = dumps({"by_packet": new.packet_id, "packet_id": old_id})
    rows = conn.execute(
        "SELECT id FROM runs WHERE packet_id = ? ORDER BY created_utc, id", (old_id,)
    ).fetchall()
    for r in rows:
        rid = r["id"]
        if is_superseded(conn, rid):
            already.append(rid)
            continue
        append_note(conn, rid, kind=SUPERSEDED, actor=ACTOR, reason=note)
        marked.append(rid)

    graph, moved = _rebind(
        project.graph, old_id, new.packet_id, old.plate_hash if old else None, new.plate_hash
    )
    if moved:
        project.save_graph(graph)

    rev = Revision(
        new.packet_id,
        old_id,
        True,
        "view_hash changed" if old is not None else "old packet is not in the index",
        superseded_runs=marked,
        already_superseded=already,
        rebound_nodes=moved,
    )
    pid = project.project_id
    for rid in marked:
        _publish(
            bus,
            pid,
            rid,
            {"kind": "run.superseded", "run_id": rid, "packet_id": old_id, "superseded_by": new.packet_id},
        )
    _publish(bus, pid, None, {"kind": "packet.superseded", **rev.to_dict()})
    return rev


# ---------------------------------------------------------------------------- the legend, entry by entry


def _entries(legend: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in (legend or {}).get("entries") or []:
        if isinstance(e, Mapping) and isinstance(e.get("id"), str):
            out[e["id"]] = dict(e)
    return out


def _material_entry(entries: Mapping[str, dict[str, Any]], name: Any) -> dict[str, Any] | None:
    if not name:
        return None
    return next((e for e in entries.values() if e.get("kind") == "material" and e.get("name") == name), None)


def _digest_differs(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    da, db = (a or {}).get("digest"), (b or {}).get("digest")
    return bool(da and db and da != db)


def _entry_change(
    cid: str,
    old: Mapping[str, dict[str, Any]],
    new: Mapping[str, dict[str, Any]],
) -> tuple[str, str] | None:
    """(code, detail) when the legend entry of `cid` is not the same in both packets; None when it is."""
    a, b = old.get(cid), new.get(cid)
    if a is None:
        return "id_missing", f"{cid} is not in the old legend"
    if b is None:
        return "id_removed", f"{cid} ({a.get('name')}) is not in the new legend"
    for f in SAME_FIELDS:
        if a.get(f) != b.get(f):
            return "legend_changed", f"{cid} {f}: {a.get(f)!r} -> {b.get(f)!r}"
    if a.get("kind") == "material":
        if _digest_differs(a, b):
            return "material_changed", f"{cid} ({a.get('name')}): the material's record changed"
        return None
    mat_old, mat_new = _material_entry(old, a.get("material")), _material_entry(new, b.get("material"))
    if _digest_differs(mat_old, mat_new):
        return "material_changed", f"{cid} ({a.get('name')}): material {a.get('material')} changed"
    if _digest_differs(a, b):
        return "object_changed", f"{cid} ({a.get('name')}): the node's record changed"
    return None


# ---------------------------------------------------------------------------- carry-over


@dataclass(frozen=True)
class _Layer:
    run_id: str
    mask_hash: str
    ids: tuple[str, ...]
    rgb16_hash: str
    feather_px: float | None


def _layer(raw: Any) -> _Layer:
    if not isinstance(raw, Mapping):
        raise _Drop("layer_invalid", "a layer is an object {run_id, mask_hash, ids, rgb16_hash}")
    ids = raw.get("ids")
    if not isinstance(ids, (list, tuple)) or not ids or not all(isinstance(i, str) and i for i in ids):
        raise _Drop("layer_invalid", "ids must be a non-empty list of legend ids")
    rid = raw.get("run_id")
    if not isinstance(rid, str) or not rid:
        raise _Drop("layer_invalid", "run_id must be a non-empty string")
    try:
        mask_hash, rgb_hash = check_key(raw.get("mask_hash")), check_key(raw.get("rgb16_hash"))
    except CASError as e:
        raise _Drop("layer_invalid", str(e)) from e
    fp = raw.get("feather_px")
    if fp is not None and (isinstance(fp, bool) or not isinstance(fp, (int, float)) or fp < 0):
        raise _Drop("layer_invalid", "feather_px must be a number >= 0")
    return _Layer(rid, mask_hash, tuple(dict.fromkeys(ids)), rgb_hash, None if fp is None else float(fp))


def _union(packet: PacketView, ids: Sequence[str], side: str) -> np.ndarray:
    mask: np.ndarray | None = None
    for cid in ids:
        try:
            m = packet.id_mask(cid)
        except PipelineError as e:
            raise _Drop("mask_unavailable", f"{side} packet: {e}") from e
        mask = m.copy() if mask is None else (mask | m)
    assert mask is not None
    return mask


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    union = int((a | b).sum())
    return float((a & b).sum()) / union if union else 0.0


def _decode_mask(data: bytes, shape: tuple[int, int]) -> np.ndarray:
    raw = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if raw is None or raw.shape != shape:
        got = "not a grey PNG" if raw is None else f"{raw.shape[1]} x {raw.shape[0]}"
        raise _Drop("size_mismatch", f"stored mask is {got}, the new plate is {shape[1]} x {shape[0]}")
    return raw > 127


def _mask_png(mask: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.where(mask, 255, 0).astype(np.uint8))
    if not ok:
        raise ValueError("cv2 could not encode the mask")
    return bytes(buf)


def _read(cas: CAS, key: str, what: str) -> bytes:
    try:
        if not cas.verify(key):
            raise _Drop("pixels_missing", f"{what} {key[:12]} is missing from the CAS or corrupt")
        return cas.read_bytes(key)
    except CASError as e:
        raise _Drop("pixels_missing", f"{what}: {e}") from e


def _quick_gates(
    plate16: np.ndarray, ai16: np.ndarray, comp16: np.ndarray, mask: np.ndarray
) -> tuple[Any, dict[str, Any]]:
    """The GateReport of the quick gates and the numbers behind it."""
    from plate_gates import iddiff, thresholds
    from plate_gates.report import Check, EngineRef, GateReport
    from plate_pixels.register import estimate, to_gray

    ts = thresholds.load()
    reg = estimate(to_gray(plate16), to_gray(ai16), ring_mask=~mask)
    om = iddiff.outside_mask_check(plate16, comp16, mask)
    metrics: dict[str, Any] = {
        "registration": {
            "shift_px": reg.shift_px,
            "residual_px": reg.residual_px,
            "registered": bool(reg.ok),
            "camera_drift": bool(reg.camera_drift),
            "dx": reg.dx,
            "dy": reg.dy,
            "source": "carry-over",
        },
        "outside_mask": {**om, "mask_share": float(mask.mean())},
    }
    values = {
        "registration.residual_px": reg.residual_px,
        "registration.shift_px": reg.shift_px,
        "outside_mask.de00": om["de00_max_outside"],
    }
    checks = []
    for metric_id, value in values.items():
        thr = ts.get(metric_id)
        if value is None or not np.isfinite(value):  # an unmeasurable hard gate never passes
            status = "fail" if thr.effective_status() == "hard" else "advisory"
            value = None
        else:
            status, _ = ts.check(metric_id, float(value))
        checks.append(Check(metric_id=metric_id, value=value, status=status))
    report = GateReport(
        threshold_set_id=ts.id,
        engine=EngineRef(id=CARRY_ENGINE_ID),
        tier=None,
        metrics=metrics,
        checks=checks,
        backend_notes=[
            "carry-over: quick gates only (registration, outside_mask); the full gate set ran on the original run"
        ],
    )
    return report, metrics


def _with_lineage(png: bytes, name: str, **fields: Any) -> tuple[bytes, bool]:
    """The PNG with its lineage embedded when `lineage.embed` can; else the bytes as given."""
    try:
        from . import lineage

        with tempfile.TemporaryDirectory(prefix="plate-carry-") as td:
            p = Path(td) / f"{name}.png"
            p.write_bytes(png)
            lineage.embed(p, lineage.Lineage(**fields))
            return p.read_bytes(), True
    except Exception as e:  # lineage is a convenience for drop-to-resolve, never a reason to drop a layer
        log.warning("lineage not embedded in %s: %s", name, e)
        return png, False


@dataclass
class _Ctx:
    old: PacketView
    new: PacketView
    cas: CAS | None
    project: Project | None
    conn: sqlite3.Connection | None
    ledger: Ledger | None
    bus: Any
    now: datetime
    feather_px: float
    iou_min: float
    old_legend: dict[str, dict[str, Any]]
    new_legend: dict[str, dict[str, Any]]
    plate16: np.ndarray | None = None

    @property
    def persist(self) -> bool:
        return self.project is not None and self.conn is not None

    def plate(self) -> np.ndarray:
        if self.plate16 is None:
            self.plate16 = self.new.plate16()
        return self.plate16


def _existing_carry(conn: sqlite3.Connection, packet_id: str, old_run_id: str) -> str | None:
    for r in conn.execute(
        "SELECT id, inputs FROM runs WHERE packet_id = ? AND state = ? ORDER BY created_utc, id",
        (packet_id, RunState.DONE.value),
    ):
        inputs = loads(r["inputs"])
        if isinstance(inputs, dict) and inputs.get("carried_from") == old_run_id:
            return r["id"]
    return None


def _carry_one(ctx: _Ctx, raw: Any) -> dict[str, Any]:
    lay = _layer(raw)
    base = {"run_id": lay.run_id, "ids": list(lay.ids)}
    old_run: Run | None = None
    if ctx.persist:
        assert ctx.conn is not None
        again = _existing_carry(ctx.conn, ctx.new.packet_id, lay.run_id)
        if again is not None:
            return {
                **base,
                "status": "carried",
                "code": "already_carried",
                "reason": "already carried",
                "iou": None,
                "gates": None,
                "new_run_id": again,
                "output": (get_run(ctx.conn, again).outputs or [None])[0],
            }
        try:
            old_run = get_run(ctx.conn, lay.run_id)
        except KeyError as e:
            raise _Drop("run_unknown", f"run {lay.run_id} is not in the index") from e
        if old_run.packet_id != ctx.old.packet_id:
            raise _Drop(
                "run_elsewhere", f"run {lay.run_id} is on {old_run.packet_id}, not {ctx.old.packet_id}"
            )

    # 1. the legend: every id the layer touched is the same thing in the new packet
    changed = [(cid, ch) for cid in lay.ids if (ch := _entry_change(cid, ctx.old_legend, ctx.new_legend))]
    if changed:
        code, detail = changed[0][1]
        raise _Drop(
            code,
            detail,
            changed_ids=[{"id": cid, "code": c, "detail": d} for cid, (c, d) in changed],
        )
    new_locked = set(ctx.new.locked_ids())
    locked = [cid for cid in lay.ids if cid in new_locked]
    if locked:
        raise _Drop("id_locked", f"{locked} are locked by the new packet's policy")

    # 2. the ids' pixels did not move
    old_union = _union(ctx.old, lay.ids, "old")
    new_union = _union(ctx.new, lay.ids, "new")
    iou = _iou(old_union, new_union)
    if iou < ctx.iou_min:
        raise _Drop("mask_moved", f"IoU {iou:.3f} < {ctx.iou_min}", iou=iou)

    # 3. the new mask: what was approved, where the new plate still shows the ids
    plate16 = ctx.plate()
    h, w = plate16.shape[:2]
    cas = ctx.cas
    assert cas is not None  # carry_over checked
    mask = _decode_mask(_read(cas, lay.mask_hash, "stored mask"), (h, w)) & new_union
    for cid in sorted(new_locked - set(lay.ids)):
        try:
            mask &= ~ctx.new.id_mask(cid)
        except PipelineError:
            continue  # a locked id of a layer this packet does not carry
    if not mask.any():
        raise _Drop("mask_empty", "the layer's mask is empty on the new plate", iou=iou)

    # 4. re-composite the stored AI pixels onto the new plate and re-run the quick gates
    try:
        ai16 = decode_png16(_read(cas, lay.rgb16_hash, "stored pixels"))
    except ValueError as e:
        raise _Drop("pixels_missing", f"stored pixels: {e}") from e
    if ai16.shape != plate16.shape:
        raise _Drop(
            "size_mismatch",
            f"stored pixels are {ai16.shape[1]} x {ai16.shape[0]}, the new plate is {w} x {h}",
            iou=iou,
        )
    from plate_gates import composite

    feather = lay.feather_px if lay.feather_px is not None else ctx.feather_px
    comp = composite.composite_masked(plate16, ai16, mask, feather_px=feather, tone_match_ring=False)
    report, metrics = _quick_gates(plate16, ai16, comp, mask)
    gates = {"verdict": report.verdict, "checks": {c.metric_id: c.status for c in report.checks}}
    if report.verdict == "fail":
        failed = sorted(m for m, s in gates["checks"].items() if s == "fail")
        raise _Drop("gates_failed", f"failed {failed}", iou=iou, gates=gates)

    result = {
        **base,
        "status": "carried",
        "code": "carried",
        "reason": f"ids unchanged, id mask IoU {iou:.3f}, quick gates {report.verdict}",
        "iou": iou,
        "gates": gates,
        "new_run_id": None,
        "output": None,
    }
    if ctx.persist:
        assert ctx.project is not None and ctx.conn is not None and old_run is not None
        run = _persist(ctx, lay, old_run, comp, mask, report, metrics, iou)
        result["new_run_id"] = run.id
        result["output"] = run.outputs[0] if run.outputs else None
    return result


def _persist(
    ctx: _Ctx,
    lay: _Layer,
    old_run: Run,
    comp: np.ndarray,
    mask: np.ndarray,
    report: Any,
    metrics: Mapping[str, Any],
    iou: float,
) -> Run:
    project, conn, cas = ctx.project, ctx.conn, ctx.cas
    assert project is not None and conn is not None and cas is not None
    rid = new_id("run", ctx.now)
    at = iso(ctx.now)
    svc = ReportService(project.root)
    path = svc.write(rid, report)
    try:
        ref = Path(path).relative_to(project.root).as_posix()
    except ValueError:
        ref = Path(path).as_posix()
    node_id = old_run.node_id
    png, embedded = _with_lineage(
        encode_png16(comp),
        rid,
        project_id=project.project_id,
        node_id=node_id or "none",
        run_id=rid,
        packet_id=ctx.new.packet_id,
        engine_id=CARRY_ENGINE_ID,
        engine_version=f"carried-from-{old_run.id}",
        gate_report=ref,
    )
    out_key = cas.put_bytes(png, "png")
    mask_key = cas.put_bytes(_mask_png(mask), "png")
    job = f"carried-{old_run.id}"
    inputs = {
        "plate": ctx.new.plate_hash,
        "mask": {"kind": "cas", "key": mask_key, "ids": list(lay.ids), "source": "carry-over"},
        "carried_from": old_run.id,
        "layer": {"rgb16": lay.rgb16_hash, "mask": lay.mask_hash},
    }
    gate_report = {**report.model_dump(mode="json"), "report_path": ref}
    normalized = {
        "outputs": [out_key],
        "raw_key": lay.rgb16_hash,
        "mask_key": mask_key,
        "ingress": dict(metrics["registration"]),
        "composite": {
            "feather_px": ctx.feather_px,
            "tone_match_ring": False,
            "mask_share": float(mask.mean()),
        },
        "carried_from": old_run.id,
        "iou": iou,
        "lineage": embedded,
    }
    priority = old_run.priority.label
    # The folder first (decision 6): the `done` record carries everything the index row is rebuilt from.
    ledger = ctx.ledger or Ledger(project.root, clock=lambda: ctx.now)
    ledger.append(
        {
            "kind": "done",
            "at": at,
            "run_id": rid,
            "node_id": node_id,
            "engine_id": CARRY_ENGINE_ID,
            "route_id": CARRY_ROUTE["route_id"],
            "custody_grade": CARRY_ROUTE["custody_grade"],
            "provider_job_id": job,
            "gate_report": ref,
            "data": {
                "state": RunState.DONE.value,
                "packet_id": ctx.new.packet_id,
                "outputs": [out_key],
                "result_key": out_key,
                "verdict": report.verdict,
                "carried_from": old_run.id,
                "carried_from_packet": ctx.old.packet_id,
                "ids": list(lay.ids),
                "iou": iou,
                "priority": priority,
                "attempt": 1,
                "inputs": inputs,
                "params": {},
                "route": dict(CARRY_ROUTE),
            },
        }
    )
    with tx(conn):
        apply_run(
            conn,
            project.project_id,
            {
                "run_id": rid,
                "created_utc": at,
                "updated_utc": at,
                "state": RunState.DONE,
                "packet_id": ctx.new.packet_id,
                "node_id": node_id,
                "priority": priority,
                "inputs": inputs,
                "params": {},
                "route": dict(CARRY_ROUTE),
                "attempt": 1,
                "provider_job_id": job,
                "result_key": out_key,
                "gate_report": gate_report,
            },
        )
        annotate(conn, rid, expect=RunState.DONE, normalized=normalized)
        append_note(
            conn,
            rid,
            kind=CARRIED,
            actor=ACTOR,
            reason=dumps({"carried_from": old_run.id, "packet_id": ctx.old.packet_id, "iou": round(iou, 6)}),
        )
    pid = project.project_id
    _publish(
        ctx.bus,
        pid,
        rid,
        {
            "kind": "run.carried",
            "run_id": rid,
            "carried_from": old_run.id,
            "packet_id": ctx.new.packet_id,
            "node_id": node_id,
        },
    )
    return get_run(conn, rid)


def carry_over(
    old_packet: PacketView,
    new_packet: PacketView,
    approved_layers: Sequence[Mapping[str, Any]],
    *,
    project: Project | None = None,
    conn: sqlite3.Connection | None = None,
    cas: CAS | None = None,
    ledger: Ledger | None = None,
    bus: Any = None,
    now: datetime | None = None,
    feather_px: float = DEFAULT_FEATHER_PX,
    iou_min: float = IOU_MIN,
) -> list[dict[str, Any]]:
    """Decide each approved layer: `{run_id, status: carried | dropped, reason, code, ...}` in input order.

    A layer is `{run_id, mask_hash, ids: [id], rgb16_hash}` (CAS keys of the stored mask and AI pixels; an optional
    `feather_px` overrides the default). With `project=` and `conn=` (the project's index) a carried layer also
    becomes a new run (`new_run_id`, `output`); without them nothing is written and `cas=` reads the layer.
    """
    persist = project is not None and conn is not None
    if (project is None) != (conn is None):
        raise RevisionError("project= and conn= go together: with both a carried layer becomes a run")
    if cas is None and project is not None:
        cas = project.cas()
    if cas is None:
        raise RevisionError("carry_over needs a cas= (or a project=) to read the stored layers")
    if persist:
        assert conn is not None
        if (
            conn.execute("SELECT 1 FROM packets WHERE packet_id = ?", (new_packet.packet_id,)).fetchone()
            is None
        ):
            raise RevisionError(f"packet {new_packet.packet_id} is not in the project index")
    ctx = _Ctx(
        old=old_packet,
        new=new_packet,
        cas=cas,
        project=project,
        conn=conn,
        ledger=ledger,
        bus=bus,
        now=now or utcnow(),
        feather_px=feather_px,
        iou_min=iou_min,
        old_legend=_entries(old_packet.legend),
        new_legend=_entries(new_packet.legend),
    )
    out: list[dict[str, Any]] = []
    for raw in approved_layers:
        try:
            out.append(_carry_one(ctx, raw))
        except _Drop as d:
            rid = raw.get("run_id") if isinstance(raw, Mapping) else None
            ids = raw.get("ids") if isinstance(raw, Mapping) and isinstance(raw.get("ids"), list) else []
            res = {
                "run_id": rid,
                "ids": list(ids),
                "status": "dropped",
                "code": d.code,
                "reason": REASONS[d.code],
                "detail": d.detail,
                "new_run_id": None,
                **d.extra,
            }
            out.append(res)
            if project is not None:
                _publish(
                    bus,
                    project.project_id,
                    rid if isinstance(rid, str) else None,
                    {
                        "kind": "layer.dropped",
                        "run_id": rid,
                        "packet_id": new_packet.packet_id,
                        "code": d.code,
                        "reason": REASONS[d.code],
                    },
                )
    return out
