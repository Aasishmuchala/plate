"""The Set node: one look across the cameras of a set, by measurement (T30's cross-view gate, run as a node).

    prepare    hero = the packet the look is taken from (`params.hero`, else the nearest upstream Plate), views
               = the other cameras (`params.views`, else the other upstream Plates)
    normalize  setgate.run(hero, views) measures, per material, the Lab offset hero - view at world-point
               correspondences; setgate.numeric_correction then moves each view's material by that offset
               (clipped to +-8 L, +-6 a/b, bit-identical outside the ids it touches). One PNG16 per view.
    gate       setgate.run again, on the STORED outputs: cross-view p90 <= 3 on the materials the packets lock
               (hard), the editable classes (vegetation, people, sky, water) advisory, plus a hard check that
               no pixel moved outside the materials that were corrected.

Which ids are corrected: the editable ones, by default. A locked material (architecture, ground, signage) is
the plate; a Set node that quietly regraded it would break "outside the mask is the plate". When a locked
material differs between views the gate reports it and fails honestly. `params.include_locked: true` is the
artist's explicit choice to correct locked materials too (it is recorded in the report); the numeric shift is
physical grading of a render, not generation. A material with too few correspondences to score is never
corrected: a p90 over a handful of samples is not a statistic, and a whole-mask mean would add error to an
identical render.

Inputs are the packets' display masters, or CAS PNGs named in `params.images` ({packet_id: hash}: the output of
an upstream node for that camera). Needs Tier A packets (Z and normals). No generator, no spend.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..pipeline import PipelineError, decode_png16
from .common import (
    NUMERIC_ENGINE_ID,
    Prepared,
    QuoteContext,
    RunContext,
    engine_ref,
    finite_or_none,
    pick,
    plain,
)

KIND = "Set"
DEFAULT_SAMPLES = 2000
MAX_SAMPLES = 20000


def _plate_ids(q: QuoteContext) -> list[str]:
    return [str(p["packetId"]) for p in q.plates if p.get("packetId")]


def prepare(q: QuoteContext) -> Prepared:
    plates = _plate_ids(q)
    hero = q.param("hero") or q.body.get("packet_id") or (plates[0] if plates else None)
    if not isinstance(hero, str) or not hero:
        raise PipelineError(
            f"Set node {q.node_id} needs a hero: params.hero or a Plate with a packet upstream",
            code="unknown_packet",
        )
    raw_views = q.param("views")
    if raw_views is None:
        raw_views = [p for p in plates if p != hero]
    if not isinstance(raw_views, (list, tuple)):
        raise PipelineError("Set params.views must be a list of packet ids", code="bad_params")
    views: list[str] = []
    for v in raw_views:
        if not isinstance(v, str) or not v:
            raise PipelineError("Set params.views must be a list of packet ids", code="bad_params")
        if v != hero and v not in views:
            views.append(v)
    if not views:
        raise PipelineError(
            "a Set needs at least two cameras: connect more Plate nodes (or name params.views)",
            code="set_needs_views",
        )

    scene_hashes: dict[str, str] = {}
    plate_hashes: dict[str, str] = {}
    for pid in [hero, *views]:
        pv = q.packet(pid)
        if pv.tier != "A" or "beauty_exr" not in pv.files:
            raise PipelineError(
                f"packet {pid} is tier {pv.tier}: a Set needs Tier A packets (Z, normals and Cryptomatte)",
                code="tier_required",
            )
        plate_hashes[pid] = pv.plate_hash
        scene_hashes[pid] = str((pv.manifest.get("scene_hash") or {}).get("hex") or "")
    if len(set(scene_hashes.values())) > 1:
        q.notes.append(
            "the cameras come from different scene hashes: world positions are compared across scene versions"
        )

    images_in = q.param("images") or {}
    if not isinstance(images_in, dict):
        raise PipelineError("Set params.images must map packet ids to CAS hashes", code="bad_params")
    images: dict[str, str] = {}
    for pid, key in images_in.items():
        if pid not in plate_hashes:
            raise PipelineError(
                f"params.images names {pid}, which is not one of this Set's cameras", code="bad_params"
            )
        images[str(pid)] = q.need_asset(key, f"params.images[{pid}]")
        try:
            shape = decode_png16(q.cas.read_bytes(images[str(pid)])).shape[:2]
        except ValueError as e:
            raise PipelineError(f"params.images[{pid}] is not a decodable picture", code="bad_params") from e
        want = q.packet(pid).plate16().shape[:2]
        if shape != want:
            raise PipelineError(
                f"params.images[{pid}] is {shape[1]} x {shape[0]}, the packet {want[1]} x {want[0]}",
                code="size_mismatch",
            )

    samples = int(pick(q.params, "samples", "n", default=DEFAULT_SAMPLES))
    if not 100 <= samples <= MAX_SAMPLES:
        raise PipelineError(f"Set params.samples must be {100}..{MAX_SAMPLES}", code="bad_params")
    spec = {
        "hero": hero,
        "views": views,
        "images": images,
        "include_locked": bool(pick(q.params, "include_locked", "includeLocked", default=False)),
        "samples": samples,
        "seed": int(q.param("seed", default=0)),
        "plates": plate_hashes,
    }
    return Prepared(spec, hero)


def _scene(c: RunContext) -> tuple[Any, np.ndarray, list[tuple[str, Any, np.ndarray]]]:
    spec = c.spec
    hero_view = c.runner.view(c.packet(spec["hero"]))
    hero_rgb = c.image16(spec["hero"])
    views = [(pid, c.runner.view(c.packet(pid)), c.image16(pid)) for pid in spec["views"]]
    return hero_view, hero_rgb, views


def _allowed(view: Any, include_locked: bool) -> set[str]:
    """Material ids a correction may touch in this view."""
    mats = set(view.material_entries())
    return mats if include_locked else {m for m in mats if m in view.editable_ids}


def _brief(result: dict[str, Any]) -> dict[str, Any]:
    """The part of a setgate.run result worth keeping in `normalized` and the report (no per-point data)."""
    return {
        "cross_view_p90": finite_or_none(result.get("cross_view_p90")),
        "overall_p90": finite_or_none(result.get("overall_p90")),
        "n_corr": result.get("n_corr"),
        "per_material": {
            mid: {
                "name": row.get("name"),
                "de00_p90": finite_or_none(row.get("de00_p90")),
                "de00_median": finite_or_none(row.get("de00_median")),
                "n": row.get("n"),
                "hard": row.get("hard"),
                "scored": row.get("scored"),
            }
            for mid, row in (result.get("per_material") or {}).items()
        },
    }


def normalize(c: RunContext) -> dict[str, Any]:
    from plate_gates import setgate

    spec = c.spec
    hero_view, hero_rgb, views = _scene(c)
    before = setgate.run(
        hero_view,
        hero_rgb,
        [(v, rgb) for _, v, rgb in views],
        n=spec["samples"],
        seed=spec["seed"],
    )
    outputs: list[str] = []
    per_view: list[dict[str, Any]] = []
    for (pid, view, rgb), measured in zip(views, before["per_view"], strict=True):
        allowed = _allowed(view, spec["include_locked"])
        rows = measured["per_material"]
        offsets = {
            mid: off
            for mid, off in measured["offsets"].items()
            if mid in allowed and (rows.get(mid) or {}).get("n", 0) >= setgate.MIN_N
        }
        out = setgate.numeric_correction(rgb, view, {}, offsets=offsets) if offsets else rgb.copy()
        outputs.append(c.put_png16(out))
        per_view.append(
            {
                "packet_id": pid,
                "output": outputs[-1],
                "corrected_ids": sorted(offsets),
                "offsets": {mid: [float(v) for v in off] for mid, off in offsets.items()},
                "skipped_locked": sorted(m for m in measured["offsets"] if m not in allowed),
                "n_corr": measured["n_corr"],
                "changed_px": int(np.any(out != rgb, axis=2).sum()),
            }
        )
    return {
        "outputs": outputs,
        "hero": spec["hero"],
        "views": per_view,
        "include_locked": spec["include_locked"],
        "before": _brief(before),
    }


def preview(c: RunContext) -> dict[str, Any]:
    """setgate.run on the spec's cameras as they are now: targets, per-view numbers, cross-view p90, verdict.
    No correction, nothing stored."""
    from plate_gates import setgate

    hero_view, hero_rgb, views = _scene(c)
    result = setgate.run(
        hero_view, hero_rgb, [(v, rgb) for _, v, rgb in views], n=c.spec["samples"], seed=c.spec["seed"]
    )
    return plain(result)


def gate(c: RunContext) -> Any:
    from plate_gates import setgate
    from plate_gates.report import Check

    spec, norm = c.spec, c.normalized
    hero_view, hero_rgb, views = _scene(c)
    outs = [c.read_png16(key) for key in norm["outputs"]]
    after = setgate.run(
        hero_view,
        hero_rgb,
        [(v, out) for (_, v, _), out in zip(views, outs, strict=True)],
        n=spec["samples"],
        seed=spec["seed"],
    )
    notes = list(after.get("notes", []))
    metrics: dict[str, Any] = {"before": norm.get("before"), "views": []}
    stray_total = 0
    for (_pid, view, rgb), out, row in zip(views, outs, norm["views"], strict=True):
        # The invariant of numeric_correction: a pixel with no coverage of any corrected id is the input's.
        cover = np.zeros(rgb.shape[:2], dtype=np.float32)
        for mid in row["corrected_ids"]:
            cover = np.maximum(cover, view.material_share(mid))
        changed = np.any(out != rgb, axis=2)
        stray = int(changed[cover <= 0.0].sum())
        stray_total += stray
        # Informational: locked-dominant pixels that moved are anti-aliased edges shared with a corrected id.
        locked = np.zeros(rgb.shape[:2], dtype=bool)
        for mid in view.material_entries():
            if mid not in view.editable_ids:
                locked |= view.material_mask(mid)
        metrics["views"].append(
            {
                **{k: v for k, v in row.items() if k != "offsets"},
                "stray_px": stray,
                "locked_edge_px_moved": int(changed[locked].sum()),
            }
        )
    after["checks"].append(
        Check(
            metric_id="set.outside_corrected_px",
            value=stray_total,
            status="pass" if stray_total == 0 else "fail",
        ).model_dump()
    )
    if spec["include_locked"]:
        notes.append("include_locked: locked materials were corrected on the artist's say-so")
    metrics["include_locked"] = bool(spec["include_locked"])
    failed = (after.get("cross_view_p90") or 0.0) > after["threshold"]["value"]
    if failed and not spec["include_locked"]:
        notes.append(
            "a locked material differs between the cameras and a Set leaves locked materials alone: "
            "re-run with include_locked to correct it, or fix the look in the render"
        )
    report = setgate.gate_report(after, tier=c.packet(spec["hero"]).tier)
    report.engine = engine_ref(c.run.route.engine_id if c.run is not None else NUMERIC_ENGINE_ID)
    report.metrics.update(metrics)
    report.backend_notes.extend(n for n in notes if n not in report.backend_notes)
    return report


def score(report: Any) -> float:
    p90 = (report.metrics or {}).get("cross_view_p90")
    return -float(p90) if isinstance(p90, (int, float)) else 0.0


__all__ = ["KIND", "gate", "normalize", "prepare", "preview", "score"]
