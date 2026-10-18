"""The Comp node: approved, mask-locked layers stacked in order over a base (decision 48, router plan T93), and the
helpers the Wave 9 numeric nodes (Comp, Colour, Atmosphere, Sky) share.

    prepare    the base is the node wired into `base`: a Plate (or nothing) is the packet's display master; any
               other node is its picture (`take`: the picked `outputHash` when it is in the CAS, as the pipeline
               reads an Edit's base, else its latest finished run; none is 422 `no_base`). The layers are the
               nodes wired into `layers`, ordered by `params.order` (node ids; wired layers it does not name
               follow in edge order) else by edge order. A layer is a run: the finished run whose output is the
               node's picked `outputHash`, else its latest finished run, where finished is DONE or a GATE_FAIL
               with a ledgered override (none: 409 `LAYER_NOT_READY`). With `params.require_accept` (default
               true) a layer whose run carries no `accept` note is refused 409
               `{"error": "LAYER_NOT_ACCEPTED", "node": id}`. Every layer is a run on the Comp's packet (the plate
               camera) at the plate's size.
    normalize  out = base, then for each layer in order out[m] = layer[m]. m is the mask the layer run stored
               (`normalized.mask_union_key`, else `mask_key`); a run that stored neither (a numeric output) gives
               the exact changed-pixel mask any(|layer - base| > 0). One PNG16 and
               `normalized = {layers: [{node, run, mask_key, px, visible_px, mask_source}], mask_union_key}`.
               The union holds every layer's mask and, when the base is a take, the base's own chain mask
               (decision 47), so outside the union the output is the plate bit for bit.
    gate       the full still gate, `run_gates(plate, out, mask=union)`, with the registration the node knows by
               construction: a composite moves no pixel geometrically, and every layer was registered by its own
               gate before it could be accepted. The invention flags count the edges every layer take (and a base
               take) already shows as known: each was gated on its own, so what is left is a seam the stack drew.
    preview    {layers: [{node, coverage}], overlap_px}: how much of the frame each layer covers and how many
               pixels two or more layers claim (the later one wins there).

Nothing here makes a pixel: the Comp only chooses, per pixel, which stored take shows. The shared helpers below
(`wired`, `take`, `finished_run`, masks in and out of the CAS, `still_gates`) are what Colour, Atmosphere and Sky
use too, so the four read the graph, the takes and the gates the same way.
"""

from __future__ import annotations

import contextlib
import struct
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import cv2
import numpy as np

from .. import canvas_rules
from ..machine import get_run
from ..models import Run
from ..pipeline import PipelineError
from ..states import RunState
from .common import NUMERIC_ENGINE_ID, Prepared, QuoteContext, RunContext, cas_has, pick, plain

KIND = "Comp"

# The registration of a node that moves no pixel geometrically (per-pixel grades, a composite of registered
# takes, a sky placed inside its own coverage): what `gating.run_gates` reads from an ingress report.
NO_GEOMETRY = {
    "shift_px": 0.0,
    "residual_px": 0.0,
    "registered": True,
    "camera_drift": False,
    "dx": 0.0,
    "dy": 0.0,
    "warp_applied": False,
}
NO_GEOMETRY_NOTE = "registration: a numeric node moves no pixel geometrically (identity by construction)"
TAKE_STATES = (RunState.DONE.value, RunState.GATE_FAIL.value)


# ---------------------------------------------------------------------------- the graph around a node


def wired(q: QuoteContext, handle: str) -> list[dict[str, Any]]:
    """The nodes wired into `handle` of this node's inputs, in edge order. An edge's handle resolves as the canvas
    rules resolve it (a missing target handle is the first input that takes the source's output type)."""
    nodes = {n.get("id"): n for n in q.graph.nodes}
    kind = q.node.get("kind")
    out: list[dict[str, Any]] = []
    for e in q.graph.edges:
        if e["target"] != q.node_id:
            continue
        source = nodes.get(e["source"])
        if source is None:
            continue
        _type, port = canvas_rules._edge_ports(source.get("kind"), kind, e)
        if port is not None and port[0] == handle and source not in out:
            out.append(source)
    return out


@contextlib.contextmanager
def connection(q: QuoteContext) -> Iterator[Any]:
    """The quote's index connection; a preview has none, so it opens the project's."""
    if q.conn is not None:
        yield q.conn
        return
    with q.pipeline.index.project(q.project_id) as conn:
        yield conn


def has_note(conn: Any, run_id: str, kind: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM run_events WHERE run_id = ? AND kind = ? LIMIT 1", (run_id, kind)
    ).fetchone()
    return row is not None


def finished_run(conn: Any, project_id: str, node_id: str, prefer: str | None = None) -> Run | None:
    """The node's take: the finished run whose `outputs[0]` is `prefer` (the picked take), else the latest
    finished run. Finished: DONE, or GATE_FAIL with a ledgered override. None when the node has none."""
    rows = conn.execute(
        f"""SELECT id, state FROM runs WHERE node_id = ? AND project_id = ?
            AND state IN ({", ".join("?" * len(TAKE_STATES))}) ORDER BY created_utc DESC, id DESC""",
        (node_id, project_id, *TAKE_STATES),
    ).fetchall()
    latest: Run | None = None
    for row in rows:
        if row["state"] == RunState.GATE_FAIL.value and not has_note(conn, row["id"], "override"):
            continue
        run = get_run(conn, row["id"])
        if not run.outputs:
            continue
        if prefer is None or run.outputs[0] == prefer:
            return run
        latest = latest or run
    return latest


def _stored_mask(cas: Any, norm: Mapping[str, Any]) -> str | None:
    for name in ("mask_union_key", "mask_key"):
        if cas_has(cas, norm.get(name)):
            return str(norm[name])
    return None


def take(q: QuoteContext, conn: Any, node: Mapping[str, Any], what: str) -> dict[str, Any]:
    """A non-Plate upstream node's picture as a spec entry: {node, run, output, mask_key}. The picked
    `outputHash` wins when it is in the CAS (a take with no run of this node, e.g. an import, has `run` None);
    a node with no picture is 422 `no_base` (a node never silently falls back to the plate)."""
    nid = str(node.get("id"))
    prefer = node.get("outputHash") if isinstance(node.get("outputHash"), str) else None
    run = finished_run(conn, q.project_id, nid, prefer)
    if prefer is not None and cas_has(q.cas, prefer) and (run is None or run.outputs[0] != prefer):
        return {"node": nid, "run": None, "output": prefer, "mask_key": None}
    if run is None or not cas_has(q.cas, run.outputs[0]):
        raise PipelineError(
            f"{nid} has no output in this project's CAS; make and pick a take first ({what} never falls "
            "back to the plate)",
            code="no_base",
            node=nid,
        )
    return {
        "node": nid,
        "run": run.id,
        "output": run.outputs[0],
        "mask_key": _stored_mask(q.cas, run.normalized or {}),
    }


def png_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG's IHDR, without decoding the picture; None for anything else."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return int(w), int(h)


def need_size(q: QuoteContext, key: str, plate_wh: tuple[int, int], what: str) -> None:
    """A CAS picture at the plate's size, checked from its header (422 `size_mismatch` / `bad_image`)."""
    size = png_size(q.cas.read_bytes(key))
    if size is None:
        raise PipelineError(f"{what}: {key[:12]} is not a PNG picture", code="bad_image")
    if size != plate_wh:
        raise PipelineError(
            f"{what} is {size[0]} x {size[1]}, the plate {plate_wh[0]} x {plate_wh[1]}", code="size_mismatch"
        )


def plate_wh(pv: Any) -> tuple[int, int]:
    h, w = pv.plate16().shape[:2]
    return int(w), int(h)


# ---------------------------------------------------------------------------- masks in and out of the CAS


def put_mask(c: RunContext, mask: np.ndarray) -> str:
    """A bool (H, W) mask as the pipeline stores masks: an 8-bit grey PNG, 0 or 255."""
    ok, buf = cv2.imencode(".png", np.where(mask, 255, 0).astype(np.uint8))
    if not ok:
        raise ValueError("cv2 could not encode the mask")
    return c.cas.put_bytes(bytes(buf), "png")


def read_mask(cas: Any, key: str, shape: tuple[int, int]) -> np.ndarray:
    """bool (H, W) from a grey (or RGB) PNG in the CAS: > 127 is inside."""
    raw = cv2.imdecode(np.frombuffer(cas.read_bytes(key), np.uint8), cv2.IMREAD_GRAYSCALE)
    if raw is None or raw.shape[:2] != tuple(shape):
        raise PipelineError(f"mask {key[:12]} is not a {shape[1]} x {shape[0]} grey PNG", code="mask_size")
    return raw > 127


def chain_mask(
    c: RunContext, entry: Mapping[str, Any] | None, img16: np.ndarray, plate16: np.ndarray
) -> np.ndarray:
    """What an upstream take may differ from the plate in: its stored mask, else the exact changed pixels.
    Empty for the plate itself (`entry` None)."""
    if entry is None:
        return np.zeros(plate16.shape[:2], dtype=bool)
    if entry.get("mask_key"):
        return read_mask(c.cas, entry["mask_key"], plate16.shape[:2]) | np.any(img16 != plate16, axis=2)
    return np.any(img16 != plate16, axis=2)


def read_take(c: RunContext, entry: Mapping[str, Any], shape: tuple[int, ...], what: str) -> np.ndarray:
    img = c.read_png16(entry["output"])
    if img.shape != shape:
        raise PipelineError(
            f"{what} {entry.get('node')} is {img.shape[1]} x {img.shape[0]}, the plate {shape[1]} x {shape[0]}",
            code="size_mismatch",
        )
    return img


# ---------------------------------------------------------------------------- the still gate for numeric nodes


def still_gates(
    c: RunContext,
    pv: Any,
    out16: np.ndarray,
    mask: np.ndarray | None,
    *,
    advisory: Iterable[str] = (),
    checks: Iterable[Any] = (),
    notes: Iterable[str] = (),
    metrics: Mapping[str, Any] | None = None,
    material_advisory: bool = False,
    known: Mapping[str, np.ndarray] | None = None,
) -> Any:
    """`gating.run_gates` on a stored numeric output against the packet's plate, the registration known by
    construction. Checks whose id starts with one of `advisory` are scored but cannot fail (a grade moves tone
    and materials by design); `material_advisory` also adds an advisory `material.de00_p90` over every measured
    material when the gate had no locked material to hold (a whole-frame change). `checks` are added as they
    are (e.g. a hard `outside_mask.changed_px`). `known` ({label: picture}) re-scores the invention flags with
    the edges those pictures already show counted as known (`invention_against`); the check stays hard."""
    from plate_gates import thresholds
    from plate_gates.report import GateReport, scored_check

    from .. import gating

    route = c.run.route_dict if c.run is not None else {}
    engine = {
        "id": c.run.route.engine_id if c.run is not None else NUMERIC_ENGINE_ID,
        "version": str(route.get("model_version") or "numeric-1"),
    }
    report = gating.run_gates(
        pv.plate16(),
        out16,
        {"path": pv.root, "manifest": pv.manifest},
        pv.legend,
        pv.tier,
        engine,
        mask=mask,
        registration=NO_GEOMETRY,
        depth_fn=getattr(c.pipeline, "depth_fn", None),
        edges=pv.edges(),
        crypto_material=pv.crypto("material") if pv.tier == "A" else None,
        extra_metrics=metrics,
    )
    data = report.model_dump(mode="json")
    if known and pv.tier == "A" and pv.edges() is not None:
        _rescore_invention(data, pv, out16, known)
    prefixes = tuple(advisory)
    forced: list[str] = []
    for check in data["checks"]:
        if prefixes and check["metric_id"].startswith(prefixes) and check["status"] == "fail":
            check["status"] = "advisory"
            forced.append(check["metric_id"])
    if material_advisory and not any(ch["metric_id"] == "material.de00_p90" for ch in data["checks"]):
        per = ((data["metrics"].get("material") or {}).get("per_id")) or {}
        worst = max(
            (row.get("de00_p90") for row in per.values() if row.get("de00_p90") is not None), default=None
        )
        if worst is not None:
            data["checks"].append(
                scored_check(
                    thresholds.load(), "material.de00_p90", float(worst), status="advisory"
                ).model_dump()
            )
    data["checks"].extend(ch.model_dump() if hasattr(ch, "model_dump") else dict(ch) for ch in checks)
    reg = data["metrics"].get("registration")
    if isinstance(reg, dict):
        reg["source"] = "numeric"
    data["backend_notes"] = [*data["backend_notes"], NO_GEOMETRY_NOTE, *notes]
    if forced:
        data["backend_notes"].append(f"advisory by design for this node: {', '.join(sorted(set(forced)))}")
    data.pop("verdict", None)
    return GateReport.model_validate(data)


def invention_against(pv: Any, out16: np.ndarray, known: Mapping[str, np.ndarray]) -> dict[str, Any]:
    """`invention.report` of `out16` with the edges of the geometry, the plate and every picture in `known`
    counted as known. The detector's plate subtraction is not tone-invariant: on room_a a +0.3 EV grade of the
    plate scores 6 flags, +1 EV 22 and +30 contrast 27, every one an edge the plate already has, lifted over the
    Canny threshold. A picture the output is a per-pixel function of (the plate graded the same way) or a take
    already gated on its own (a Comp's layers) shows those edges, so what is left is what this node drew:
    a grade's step across a wall at a painted mask's border, a seam between two layers."""
    from dataclasses import replace

    from plate_gates import gbuffer, invention
    from plate_gates.io import to_uint8

    emap, ctx = pv.edges()
    detector = invention.InventionParams().detector
    extra = np.zeros(emap.shape, dtype=bool)
    for img in known.values():
        extra |= gbuffer.test_edges(gbuffer.resample_rgb(to_uint8(img), emap.width, emap.height), detector)
    ref = replace(emap, edges=emap.edges | extra)
    rec = invention.report(
        to_uint8(out16),
        ref,
        ctx["ids"],
        pv.legend or ctx.get("legend"),
        plate_rgb=to_uint8(pv.plate16()),
        tier="A",
    )
    rec["known"] = ["geometry", "plate", *known]
    return rec


def _rescore_invention(
    data: dict[str, Any], pv: Any, out16: np.ndarray, known: Mapping[str, np.ndarray]
) -> None:
    from plate_gates import invention, thresholds
    from plate_gates.report import scored_check

    from .. import gating

    rec = invention_against(pv, out16, known)
    emap, _ctx = pv.edges()
    h, w = out16.shape[:2]
    data["metrics"]["invention"] = rec
    data["metrics"]["invention_flags"] = rec["count"]
    count = rec["count"]
    check = scored_check(
        thresholds.load(),
        invention.METRIC_ID,
        None if count is None else float(count),
        status="advisory" if count is None else None,
    ).model_dump()
    data["checks"] = [check if ch["metric_id"] == invention.METRIC_ID else ch for ch in data["checks"]]
    points = [p for p in data.get("flagged_points") or [] if p.get("metric_id") != invention.METRIC_ID]
    points.extend(p.model_dump() for p in gating._invention_points(rec, (emap.width, emap.height), w, h))
    data["flagged_points"] = points
    data["backend_notes"] = [
        *data["backend_notes"],
        f"invention: scored with the edges of {', '.join(rec['known'])} as known (tone-invariant)",
    ]


def changed_outside(out16: np.ndarray, src16: np.ndarray, scope: np.ndarray) -> Any:
    """The hard `outside_mask.changed_px` check: pixels outside `scope` that differ from the node's input."""
    from plate_gates.report import Check

    n = int(np.any(out16 != src16, axis=2)[~scope].sum())
    return Check(metric_id="outside_mask.changed_px", value=float(n), status="pass" if n == 0 else "fail")


def score(report: Any) -> float:
    from .. import gating

    return gating.score_of(report)


# ---------------------------------------------------------------------------- Comp


def _ordered(q: QuoteContext, layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = pick(q.params, "order")
    if order is None:
        return layers
    if not isinstance(order, (list, tuple)) or not all(isinstance(i, str) for i in order):
        raise PipelineError("Comp params.order is a list of layer node ids", code="bad_params")
    by_id = {str(n["id"]): n for n in layers}
    unknown = [i for i in order if i not in by_id]
    if unknown:
        raise PipelineError(
            f"Comp params.order names {unknown}, which are not wired into its layers input", code="bad_params"
        )
    if len(set(order)) != len(order):
        raise PipelineError("Comp params.order names a layer twice", code="bad_params")
    named = [by_id[i] for i in order]
    return named + [n for n in layers if str(n["id"]) not in order]


def prepare(q: QuoteContext) -> Prepared:
    packet_id = q.plate_packet_id()
    pv = q.packet(packet_id)
    wh = plate_wh(pv)
    bases = wired(q, "base")
    if len(bases) > 1:
        raise PipelineError(f"Comp {q.node_id} takes one base, {len(bases)} are wired", code="bad_graph")
    layer_nodes = _ordered(q, wired(q, "layers"))
    if not layer_nodes:
        raise PipelineError(
            f"Comp {q.node_id} has no layers: wire accepted takes into its layers input",
            code="comp_needs_layers",
        )
    require_accept = bool(pick(q.params, "require_accept", "requireAccept", default=True))
    with connection(q) as conn:
        base: dict[str, Any]
        if not bases or bases[0].get("kind") == "Plate":
            plate_packet = (bases[0].get("packetId") if bases else None) or packet_id
            if plate_packet != packet_id:
                raise PipelineError(
                    f"the Comp's base is packet {plate_packet}, its layers' plate is {packet_id}",
                    code="packet_mismatch",
                )
            base = {"kind": "plate", "packet_id": packet_id, "hash": pv.plate_hash}
        else:
            base = {"kind": "take", **take(q, conn, bases[0], "a Comp base")}
            need_size(q, base["output"], wh, f"the base {base['node']}")
        layers: list[dict[str, Any]] = []
        for node in layer_nodes:
            nid = str(node["id"])
            if node.get("kind") == "Plate":
                raise PipelineError(
                    f"{nid} is a Plate: wire a Plate into the base, not the layers", code="bad_graph"
                )
            prefer = node.get("outputHash") if isinstance(node.get("outputHash"), str) else None
            run = finished_run(conn, q.project_id, nid, prefer)
            if run is None:
                raise PipelineError(
                    f"layer {nid} has no finished take (DONE, or GATE_FAIL with an override) to stack",
                    code="LAYER_NOT_READY",
                    status=409,
                    node=nid,
                )
            if require_accept and not has_note(conn, run.id, "accept"):
                raise PipelineError(
                    f"layer {nid}: take {run.id} is not accepted (accept it after a full scrub, or set "
                    "params.require_accept false)",
                    code="LAYER_NOT_ACCEPTED",
                    status=409,
                    node=nid,
                )
            if run.packet_id != packet_id:
                raise PipelineError(
                    f"layer {nid} was made on packet {run.packet_id}; this Comp stacks on {packet_id}",
                    code="packet_mismatch",
                    node=nid,
                )
            output = run.outputs[0]
            if not cas_has(q.cas, output):
                raise PipelineError(
                    f"layer {nid}: {output[:12]} is not in this project's CAS", code="asset_missing"
                )
            need_size(q, output, wh, f"layer {nid}")
            layers.append(
                {
                    "node": nid,
                    "run": run.id,
                    "state": run.state.value,
                    "output": output,
                    "mask_key": _stored_mask(q.cas, run.normalized or {}),
                }
            )
    spec = {
        "packet": packet_id,
        "plate": pv.plate_hash,
        "base": base,
        "layers": layers,
        "require_accept": require_accept,
    }
    return Prepared(spec, packet_id)


def _stack(c: RunContext) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]], np.ndarray, np.ndarray]:
    """(out16, base16, rows, union, layer masks (n, H, W)) of the spec; nothing stored."""
    spec = c.spec
    pv = c.packet()
    plate16 = pv.plate16()
    base_entry = spec["base"] if spec["base"]["kind"] == "take" else None
    base16 = plate16 if base_entry is None else read_take(c, base_entry, plate16.shape, "the base")
    out = base16.copy()
    union = chain_mask(c, base_entry, base16, plate16)
    masks = np.zeros((len(spec["layers"]), *plate16.shape[:2]), dtype=bool)
    rows: list[dict[str, Any]] = []
    for i, layer in enumerate(spec["layers"]):
        img = read_take(c, layer, plate16.shape, "layer")
        if layer.get("mask_key"):
            m = read_mask(c.cas, layer["mask_key"], plate16.shape[:2])
            source = "stored"
        else:
            m = np.any(img != base16, axis=2)
            source = "changed"
        out[m] = img[m]
        union |= m
        masks[i] = m
        rows.append(
            {
                "node": layer["node"],
                "run": layer["run"],
                "mask_key": layer.get("mask_key"),
                "mask_source": source,
            }
        )
    return out, base16, rows, union, masks


def normalize(c: RunContext) -> dict[str, Any]:
    out, _base16, rows, union, masks = _stack(c)
    top = np.full(union.shape, -1, dtype=np.int32)
    for i in range(len(rows)):
        top[masks[i]] = i
    for i, row in enumerate(rows):
        if row["mask_source"] == "changed":
            row["mask_key"] = put_mask(c, masks[i])
        row["px"] = int(masks[i].sum())
        row["visible_px"] = int((top == i).sum())
    return {
        "outputs": [c.put_png16(out)],
        "layers": rows,
        "base": c.spec["base"],
        "mask_union_key": put_mask(c, union),
        "mask_px": int(union.sum()),
    }


def preview(c: RunContext) -> dict[str, Any]:
    _out, _base16, rows, union, masks = _stack(c)
    total = float(union.size)
    claimed = masks.sum(axis=0)
    return plain(
        {
            "layers": [
                {"node": row["node"], "coverage": float(masks[i].sum()) / total} for i, row in enumerate(rows)
            ],
            "overlap_px": int((claimed >= 2).sum()),
            "union_coverage": float(union.sum()) / total,
        }
    )


def gate(c: RunContext) -> Any:
    norm = c.normalized
    pv = c.packet()
    out16 = c.read_png16(norm["outputs"][0])
    union = read_mask(c.cas, norm["mask_union_key"], out16.shape[:2])
    # Each take was gated on its own; what the stack itself can draw is a seam where a layer meets what is under it.
    known = {f"layer {layer['node']}": c.read_png16(layer["output"]) for layer in c.spec["layers"]}
    if c.spec["base"]["kind"] == "take":
        known["the base take"] = c.read_png16(c.spec["base"]["output"])
    return still_gates(
        c,
        pv,
        out16,
        union,
        known=known,
        metrics={
            "comp": {"layers": norm.get("layers"), "mask_px": norm.get("mask_px"), "base": norm.get("base")}
        },
        notes=[f"comp: {len(norm.get('layers') or [])} layers over the {c.spec['base']['kind']}"],
    )


__all__ = [
    "KIND",
    "NO_GEOMETRY",
    "chain_mask",
    "changed_outside",
    "connection",
    "finished_run",
    "gate",
    "has_note",
    "invention_against",
    "need_size",
    "normalize",
    "plate_wh",
    "png_size",
    "prepare",
    "preview",
    "put_mask",
    "read_mask",
    "read_take",
    "score",
    "still_gates",
    "take",
    "wired",
]
