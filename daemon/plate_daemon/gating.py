"""Gate wiring: one call scores a composited output against its plate and packet (BUILD_PLAN decision 14).

    run_gates(plate, out, packet, legend, tier, engine, *, mask=None, registration=None) -> GateReport

`plate` and `out` are the 16-bit sRGB display master and the composite at the plate's size (uint8 is promoted),
`packet` the packet folder (or `{"path", "manifest"}`), `legend` its `id_legend.json`, `tier` "A" or "C",
`engine` `{"id", "version"}`. `mask` is the editable mask the compositor used (True = the edit may change
it; None = the whole frame). `registration` is the ingress report (`plate_pixels.ingress.restore`), which
measured the engine's pixels against the plate BEFORE compositing; without it the plate and the output are
registered here on the unchanged ring.

What runs, with the threshold ids of `plate_gates/thresholds.yaml`:

    registration.residual_px, registration.shift_px   ECC residual and shift (hard)
    outside_mask.de00, feather_band.de00_p99          bit identity outside the mask, drift in the feather (hard)
    gbuffer_edge_recall_r2                            G-buffer edge recall, CoC weighted, plate visibility (Tier A, hard)
    invention_flags                                   new long edges in planar architecture ids (Tier A, hard)
    material.de00_median, material.de00_p90           the worst LOCKED material (most pixels outside the mask)
    texture.lapvar, texture.fine_ratio                the texture band; advisory off its 3840 px width
    tone.luma, tone.contrast, tone.sat                the tone band (advisory)
    image_edge_agreement                              image-only Canny agreement (advisory triage)
    depth agreement                                   Tier C only, with a depth backend; advisory and not in
                                                      the threshold set yet, so reported without a check

Nothing is ever upsampled to be measured: the texture band is measured at native width below 3840 (and its
misses downgraded to advisory, as `texture.band_check` says), image edge agreement at min(1920, native). The
verdict is `GateReport`'s: fail if a hard check fails, advisory if only advisory checks miss, else pass. A metric
that could not be computed scores `fail` when its threshold is hard: an unmeasured hard gate never passes.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from plate_gates import align, coc, gbuffer, iddiff, invention, material_de, texture, thresholds, tone
from plate_gates.io import to_uint8
from plate_gates.report import Check, EngineRef, GateReport

__all__ = ["DepthFn", "LOCKED_SHARE", "TEXTURE_WIDTH", "EDGE_AGREEMENT_WIDTH", "run_gates", "score_of"]

DepthFn = Callable[[np.ndarray], np.ndarray]

TEXTURE_WIDTH = 3840
EDGE_AGREEMENT_WIDTH = 1920
LOCKED_SHARE = 0.5  # a material with less than this share of its pixels inside the mask is locked


# ---------------------------------------------------------------------------- inputs


def _u16(img: np.ndarray, name: str) -> np.ndarray:
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] != 3:
        raise ValueError(f"{name} must be an RGB array (h, w, 3), got {a.shape}")
    if a.dtype == np.uint16:
        return a
    if a.dtype == np.uint8:
        return a.astype(np.uint16) * 257
    if np.issubdtype(a.dtype, np.floating):
        return np.clip(np.rint(a * 65535.0), 0, 65535).astype(np.uint16)
    raise ValueError(f"{name} must be uint16, uint8 or float in [0, 1], got {a.dtype}")


def _packet(packet: Any) -> tuple[Path | None, dict[str, Any]]:
    if packet is None:
        return None, {}
    if isinstance(packet, Mapping):
        path = packet.get("path")
        manifest = packet.get("manifest")
        root = Path(path) if path else None
        if manifest is None and root is not None:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        return root, dict(manifest or {})
    root = Path(packet)
    return root, json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


class _Checks:
    """Builds `Check`s against the committed threshold set; an unmeasurable hard metric fails."""

    def __init__(self) -> None:
        self.ts = thresholds.load()
        self.items: list[Check] = []

    def add(
        self,
        metric_id: str,
        value: Any,
        *,
        status: str | None = None,
        variant: str | None = None,
        backend: str | None = None,
    ) -> str:
        thr = self.ts.get(metric_id)
        v = _finite(value)
        if status is None:
            if v is None:
                status = "fail" if thr.effective_status(backend) == "hard" else "advisory"
            else:
                status, _ = self.ts.check(
                    metric_id, v, variant=variant, backend=backend if thr.status_by_backend else None
                )
        self.items.append(Check(metric_id=metric_id, value=v, status=status))
        return status


# ---------------------------------------------------------------------------- the gates


def _registration(
    plate16: np.ndarray, out16: np.ndarray, mask: np.ndarray | None, given: Mapping[str, Any] | None
) -> dict[str, Any]:
    if given is not None:
        keep = ("shift_px", "residual_px", "registered", "camera_drift", "dx", "dy", "warp_applied")
        rec = {k: given.get(k) for k in keep if k in given}
        rec["source"] = "ingress"
        return rec
    from plate_pixels.register import estimate, to_gray  # the worker's ECC, imported only when needed

    ring = None if mask is None else ~mask
    reg = estimate(to_gray(plate16), to_gray(out16), ring_mask=ring)
    return {
        "shift_px": reg.shift_px,
        "residual_px": reg.residual_px,
        "registered": bool(reg.ok),
        "camera_drift": bool(reg.camera_drift),
        "dx": reg.dx,
        "dy": reg.dy,
        "source": "gate",
    }


def _locked_materials(
    per: Mapping[str, Mapping[str, Any]],
    ranks: np.ndarray,
    cov: np.ndarray,
    mask: np.ndarray | None,
) -> tuple[list[str], list[str]]:
    """(locked ids, edited ids) of the measured materials, by the share of their pixels inside the mask."""
    if mask is None:
        return [], list(per)
    if ranks.ndim == 2:
        ranks, cov = ranks[..., None], cov[..., None]
    locked, edited = [], []
    for cid in per:
        target = np.uint32(material_de.id_to_u32(cid))
        px = np.where(ranks == target, cov, 0.0).sum(axis=2) >= material_de.MASK_THRESHOLD
        n = int(px.sum())
        inside = int((px & mask).sum())
        (edited if n and inside / n >= LOCKED_SHARE else locked).append(cid)
    return locked, edited


def run_gates(
    plate: np.ndarray,
    out: np.ndarray,
    packet: Any,
    legend: Mapping[str, Any] | None,
    tier: str,
    engine: Mapping[str, Any] | EngineRef | None,
    *,
    mask: np.ndarray | None = None,
    registration: Mapping[str, Any] | None = None,
    depth_fn: DepthFn | None = None,
    variant: str = "delivery",
    edges: tuple[Any, dict[str, Any]] | None = None,
    crypto_material: tuple[np.ndarray, np.ndarray] | None = None,
    extra_metrics: Mapping[str, Any] | None = None,
) -> GateReport:
    """Score `out` against `plate` (see the module docstring). `edges` is `gbuffer.from_packet`'s result and
    `crypto_material` the material Cryptomatte, when the caller already has them (they are read otherwise)."""
    plate16 = _u16(plate, "plate")
    out16 = _u16(out, "out")
    if plate16.shape != out16.shape:
        raise ValueError(f"plate {plate16.shape} and output {out16.shape} must be the same size")
    h, w = plate16.shape[:2]
    m = None
    if mask is not None:
        m = np.asarray(mask)
        m = (m > 0.5) if np.issubdtype(m.dtype, np.floating) else m.astype(bool)
        if m.ndim == 3:
            m = m[..., 0]
        if m.shape != (h, w):
            raise ValueError(f"mask {m.shape} does not match the plate {(h, w)}")
    root, manifest = _packet(packet)
    tier = str(tier or manifest.get("tier") or "A")
    plate8, out8 = to_uint8(plate16), to_uint8(out16)
    checks = _Checks()
    metrics: dict[str, Any] = {}
    notes: list[str] = []

    # registration (the engine's pixels against the plate, before compositing)
    reg = _registration(plate16, out16, m, registration)
    metrics["registration"] = reg
    checks.add("registration.residual_px", reg.get("residual_px"))
    checks.add("registration.shift_px", reg.get("shift_px"))

    # outside the mask: bit identity and the feather band
    full = np.ones((h, w), dtype=bool) if m is None else m
    om = iddiff.outside_mask_check(plate16, out16, full)
    om["mask_share"] = float(full.mean())
    metrics["outside_mask"] = om
    checks.add("outside_mask.de00", om["de00_max_outside"])
    checks.add("feather_band.de00_p99", om["feather_band_p99"])
    if m is None:
        notes.append("outside_mask: no mask, the whole frame is editable")

    # geometry: G-buffer recall and invention flags (Tier A)
    if tier == "A" and root is not None:
        emap, ctx = edges if edges is not None else gbuffer.from_packet(root, manifest=manifest or None)
        zw = gbuffer.resample_z(ctx["z"], emap.width, emap.height)
        weights = coc.weights(zw, ctx["manifest"])
        rec = gbuffer.edge_recall(
            emap, out8, weights, r=2, plate_rgb=plate8, ids=ctx["ids"], legend=legend or ctx.get("legend")
        )
        metrics["gbuffer"] = {k: v for k, v in rec.items() if k not in ("canny",)}
        checks.add(gbuffer.METRIC_ID, rec.get("recall_r2"), variant=variant)
        notes.append(f"gbuffer: {rec.get('backend')} detector at {emap.width} px, r = 2, {variant} bound")
        inv = invention.report(
            out8, emap, ctx["ids"], legend or ctx.get("legend"), plate_rgb=plate8, tier="A"
        )
        metrics["invention"] = inv
        metrics["invention_flags"] = inv["count"]
        if inv["count"] is None:
            checks.add(invention.METRIC_ID, None, status="advisory")
            notes.append(f"invention: skipped ({inv['reason']})")
        else:
            checks.add(invention.METRIC_ID, inv["count"])
    else:
        notes.append(
            "gbuffer and invention: tier C has no G-buffer (image-only agreement and depth are advisory)"
        )

    # per-material colour, by Cryptomatte id, held on the materials the edit may not touch
    if tier == "A" and root is not None and legend:
        if crypto_material is None:
            from plate_pixels.exr import read_cryptomatte

            exr = root / manifest["files"]["beauty_exr"]["path"]
            crypto_material = read_cryptomatte(exr, manifest, "material")
        ranks, cov = crypto_material
        per = material_de.per_material(plate8, out8, ranks, cov, legend)
        locked, edited = _locked_materials(per, np.asarray(ranks), np.asarray(cov), m)
        metrics["material"] = {"per_id": per, "locked": locked, "edited": edited}
        if locked:
            worst_median = max(per[c]["de00_median"] for c in locked)
            worst_p90 = max(per[c]["de00_p90"] for c in locked)
            metrics["material"]["worst_locked"] = {"de00_median": worst_median, "de00_p90": worst_p90}
            checks.add("material.de00_median", worst_median)
            checks.add("material.de00_p90", worst_p90)
        else:
            notes.append("material: no locked material is large enough to measure")

    # texture and tone bands (never upsampled to be measured)
    tw = TEXTURE_WIDTH if w >= TEXTURE_WIDTH else None
    st = texture.stats(out8, width=tw)
    band = texture.band_check(st)
    metrics["texture"] = {**st, **{k: band[k] for k in ("verdict", "reasons", "advisory_reasons")}}
    for c in band["checks"]:
        checks.add(c["metric_id"], c["value"], status=c["status"])
    notes.extend(f"texture: {n}" for n in band["notes"])
    tn = tone.tone_check({"luma": st["luma_mean"], "contrast": st["luma_std"], "sat": st["sat_mean"]})
    metrics["tone"] = {"luma": st["luma_mean"], "contrast": st["luma_std"], "sat": st["sat_mean"]}
    for c in tn["checks"]:
        checks.add(c["metric_id"], c["value"], status=c["status"])

    # image-only edge agreement (triage)
    ew = EDGE_AGREEMENT_WIDTH if w >= EDGE_AGREEMENT_WIDTH else None
    agree, _overlay = align.edge_agreement(plate8, out8, width=ew)
    metrics["image_edge_agreement"] = agree
    checks.add("image_edge_agreement", agree)

    # depth agreement (Tier C, with a depth backend)
    if tier == "C":
        if depth_fn is None:
            notes.append("depth agreement: skipped, no depth backend configured")
            metrics["depth_agreement"] = None
        else:
            from plate_gates import depth_agreement

            region = None if m is None else ~m if (~m).sum() >= 3 else None
            metrics["depth_agreement"] = depth_agreement.compare(depth_fn(plate8), depth_fn(out8), region)
            notes.append("depth agreement: advisory, no threshold in the set yet")

    if extra_metrics:
        metrics.update(extra_metrics)
    ref = engine if isinstance(engine, EngineRef) or engine is None else EngineRef(**dict(engine))
    return GateReport(
        engine=ref,
        tier=tier,
        metrics=metrics,
        checks=checks.items,
        backend_notes=notes,
    )


def score_of(report: GateReport | Mapping[str, Any]) -> float:
    """A rank for best-of-N siblings: geometry recall (else image agreement), less one per failed check."""
    data = report.model_dump(mode="json") if isinstance(report, GateReport) else dict(report)
    metrics = data.get("metrics") or {}
    base = _finite((metrics.get("gbuffer") or {}).get("recall_r2"))
    if base is None:
        base = _finite(metrics.get("image_edge_agreement")) or 0.0
    failed = sum(1 for c in data.get("checks") or [] if c.get("status") == "fail")
    return round(base - failed, 6)
