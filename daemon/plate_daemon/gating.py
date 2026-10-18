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
    outside_mask.de00, feather_band.de00_p99          bit identity outside the mask (hard), drift in the feather
                                                      (advisory until calibration, T23, sets a bound: the
                                                      threshold stays hard in the set, the check is forced)
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

Every check carries the threshold, op and unit it was held to (`report.scored_check`). The report's
`flagged_points` are the boxes to look at first, in plate pixels: each invention flag, the three 128 px tiles with
the highest mean dE00 over the region of a missed outside_mask / feather_band / material metric (the pixels
outside the editable mask, the feather band inside it, the locked materials), and the image centre for a failed
registration shift ("camera drift").

Viewer overlays (router plan T109, verification gap G1). With an overlay sink (`overlay_sink=`, a block of
`overlays_to(sink)`, or the one `install` binds around `Pipeline.gate`: the run's project CAS) the gate also
stores what the viewer's `e` and `h` modes draw, and lists it under `metrics.overlays` (`OVERLAYS_SCHEMA`):

    edges_plate      the G-buffer reference edges the recall gate scored (Tier A), 8-bit grey PNG, 255 = edge
    edges_candidate  the output's Canny edges as the recall gate detected them, the same encoding
    de00_map         per-pixel CIEDE2000 between the plate and the output at the output's size, raw little-endian
                     binary16, one channel, rows top-first, no header (`ui/src/viewer/README.md`, R16F); 0 where
                     the output's pixel equals the plate's, which outside the mask is every pixel

Each entry is `{hash, format, media_type, width, height, ...}`. The edge masks are the bool maps edge recall
scored, at its working size: the output's size up to `gbuffer.WORKING_WIDTH`, that width (aspect kept) above
it; they are never resampled up (the viewer samples a mask over the image whatever its size). Tier C has no
G-buffer, so only the map is stored. Without a sink nothing is computed and the report has no `overlays` block,
which is also what a run gated before T109 has. A sink that fails is a note and an `error` in the block, never a
failed gate.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from plate_gates import align, coc, gbuffer, iddiff, invention, material_de, texture, thresholds, tone
from plate_gates.color import ciede2000, srgb_to_lab
from plate_gates.io import to_uint8
from plate_gates.report import Check, EngineRef, FlaggedPoint, GateReport, scored_check

__all__ = [
    "DE00_FORMAT",
    "DepthFn",
    "EDGE_FORMAT",
    "LOCKED_SHARE",
    "OVERLAYS_KEY",
    "OVERLAYS_SCHEMA",
    "OverlaySink",
    "TEXTURE_WIDTH",
    "EDGE_AGREEMENT_WIDTH",
    "TILE_PX",
    "TILES_PER_METRIC",
    "FEATHER_NOTE",
    "install",
    "overlays_to",
    "overlays_of",
    "run_gates",
    "score_of",
]

DepthFn = Callable[[np.ndarray], np.ndarray]

TEXTURE_WIDTH = 3840
EDGE_AGREEMENT_WIDTH = 1920
LOCKED_SHARE = 0.5  # a material with less than this share of its pixels inside the mask is locked
TILE_PX = 128  # flagged-point tiles
TILES_PER_METRIC = 3  # the worst tiles named per missed dE00 metric
FEATHER_NOTE = "feather band: advisory until calibration (T23) sets a bound"

OVERLAYS_KEY = "overlays"  # under the report's metrics
OVERLAYS_SCHEMA = 1
EDGE_FORMAT = "png8"  # 8-bit single-channel PNG, 255 = edge, 0 elsewhere
EDGE_VALUE = 255
DE00_FORMAT = "r16f"  # raw little-endian IEEE binary16, one channel, rows top-first, no header
DE00_EXT = "r16f"  # the CAS extension of the map (served as application/octet-stream)
DE00_ENCODING = "binary16 little-endian, 1 channel, rows top-first, no header"
INSTALLED_ATTR = "gate_overlays_installed"


class OverlaySink(Protocol):
    """Where the overlays go: a project CAS (`cas.CAS.put_bytes`) returns the BLAKE3 key of what it stored."""

    def put_bytes(self, b: bytes, ext: str | None = None) -> str: ...


_SINK: ContextVar[OverlaySink | None] = ContextVar("plate_gate_overlay_sink", default=None)


@contextmanager
def overlays_to(sink: OverlaySink | None) -> Iterator[OverlaySink | None]:
    """Every `run_gates` inside the block (on this thread) stores its overlays in `sink`."""
    token = _SINK.set(sink)
    try:
        yield sink
    finally:
        _SINK.reset(token)


def install(pipeline: Any) -> Any:
    """Let `pipeline.gate` (GATING, `Pipeline.gate(run, cas)`) store the viewer overlays in the run's project
    CAS (idempotent; returns the pipeline). Wraps that one method on this instance; the report it writes then
    carries `metrics.overlays`."""
    if getattr(pipeline, INSTALLED_ATTR, False):
        return pipeline
    inner = pipeline.gate

    def gate(run: Any, cas: Any) -> Any:
        with overlays_to(cas):
            return inner(run, cas)

    pipeline.gate = gate
    setattr(pipeline, INSTALLED_ATTR, True)
    return pipeline


def overlays_of(report: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The `metrics.overlays` block of a stored report (JSON), or None (gated before T109, or without a sink)."""
    metrics = (report or {}).get("metrics")
    block = metrics.get(OVERLAYS_KEY) if isinstance(metrics, Mapping) else None
    return dict(block) if isinstance(block, Mapping) and block.get("schema") == OVERLAYS_SCHEMA else None


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
        c = scored_check(
            self.ts,
            metric_id,
            _finite(value),
            status=status,
            variant=variant,
            backend=backend if thr.status_by_backend else None,
        )
        self.items.append(c)
        return c.status

    def misses(self, metric_id: str) -> bool:
        """True when the measured value is outside its registered bound, whatever the check's status was
        forced to (the advisory feather band still misses when it drifts)."""
        for c in self.items:
            if c.metric_id == metric_id and c.value is not None:
                return self.ts.check(metric_id, c.value)[0] != "pass"
        return False

    def failed(self, metric_id: str) -> bool:
        """True when a measured value scored `fail` (a metric that could not be measured does not count)."""
        return any(
            c.metric_id == metric_id and c.value is not None and c.status == "fail" for c in self.items
        )


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
) -> tuple[list[str], list[str], np.ndarray | None]:
    """(locked ids, edited ids, pixels of the locked ids) of the measured materials, by the share of their
    pixels inside the mask. The pixel union is None when nothing is locked."""
    if mask is None:
        return [], list(per), None
    if ranks.ndim == 2:
        ranks, cov = ranks[..., None], cov[..., None]
    locked, edited = [], []
    locked_px = None
    for cid in per:
        target = np.uint32(material_de.id_to_u32(cid))
        px = np.where(ranks == target, cov, 0.0).sum(axis=2) >= material_de.MASK_THRESHOLD
        n = int(px.sum())
        inside = int((px & mask).sum())
        if n and inside / n >= LOCKED_SHARE:
            edited.append(cid)
        else:
            locked.append(cid)
            locked_px = px if locked_px is None else locked_px | px
    return locked, edited, locked_px


# ---------------------------------------------------------------------------- flagged points


def _de00_px(a16: np.ndarray, b16: np.ndarray) -> np.ndarray:
    """CIEDE2000 between two (n, 3) uint16 sRGB pixel lists."""
    to8 = 255.0 / 65535.0
    return ciede2000(srgb_to_lab(a16.astype(np.float64) * to8), srgb_to_lab(b16.astype(np.float64) * to8))


def _worst_tiles(
    de: np.ndarray, region: np.ndarray, n: int = TILES_PER_METRIC, tile: int = TILE_PX
) -> list[tuple[int, int, int, int, float]]:
    """The `n` tiles (x, y, w, h, mean dE00) with the highest mean dE00 over `region`, on a `tile` px grid whose
    last row / column is clamped to the frame. Tiles with no region pixel, or a zero mean, are not named."""
    h, w = region.shape
    ys, xs = np.arange(0, h, tile), np.arange(0, w, tile)
    cnt = np.add.reduceat(np.add.reduceat(region.astype(np.int32), ys, axis=0), xs, axis=1)
    tot = np.add.reduceat(np.add.reduceat(de, ys, axis=0, dtype=np.float64), xs, axis=1)
    mean = np.where(cnt > 0, tot / np.maximum(cnt, 1), 0.0)
    out: list[tuple[int, int, int, int, float]] = []
    for flat in np.argsort(-mean, axis=None, kind="stable")[:n]:
        ty, tx = divmod(int(flat), mean.shape[1])
        if not mean[ty, tx] > 0:
            break
        x, y = int(xs[tx]), int(ys[ty])
        out.append((x, y, min(tile, w - x), min(tile, h - y), float(mean[ty, tx])))
    return out


def _de_points(
    plate16: np.ndarray,
    out16: np.ndarray,
    changed: np.ndarray,
    region: np.ndarray,
    metric_id: str,
    where: str,
) -> list[FlaggedPoint]:
    """The worst tiles of the dE00 between plate and output over `region` (computed on the changed pixels
    only; an unchanged pixel is 0)."""
    sel = region & changed
    de = np.zeros(region.shape, dtype=np.float32)
    if sel.any():
        de[sel] = _de00_px(plate16[sel], out16[sel])
    return [
        FlaggedPoint(x=x, y=y, w=tw, h=th, metric_id=metric_id, note=f"mean dE00 {mean:.2f} {where}")
        for x, y, tw, th, mean in _worst_tiles(de, region)
    ]


def _invention_points(inv: Mapping[str, Any], src_wh: tuple[int, int], w: int, h: int) -> list[FlaggedPoint]:
    """One point per invention flag: its bbox (at the edge map's working size) scaled to plate pixels."""
    sx, sy = w / float(src_wh[0]), h / float(src_wh[1])
    pts = []
    for f in inv.get("flags") or []:
        x0, y0, x1, y1 = f["bbox"]
        bx0, by0 = min(int(math.floor(x0 * sx)), w - 1), min(int(math.floor(y0 * sy)), h - 1)
        bx1, by1 = (
            min(max(int(math.ceil(x1 * sx)), bx0 + 1), w),
            min(max(int(math.ceil(y1 * sy)), by0 + 1), h),
        )
        what = f.get("name") or f.get("id")
        pts.append(
            FlaggedPoint(
                x=bx0,
                y=by0,
                w=bx1 - bx0,
                h=by1 - by0,
                metric_id=invention.METRIC_ID,
                note=f"invented edge, {f.get('length_px')} px, in {what} ({f.get('class')})",
            )
        )
    return pts


# ---------------------------------------------------------------------------- viewer overlays


def _de00_map(plate16: np.ndarray, out16: np.ndarray, changed: np.ndarray) -> np.ndarray:
    """Per-pixel CIEDE2000 (h, w) float32, computed on the changed pixels only (an unchanged pixel is exactly 0),
    in material_de's batched float32 CIEDE2000 (within 1e-4 of float64, far below binary16's step)."""
    de = np.zeros(changed.size, dtype=np.float32)
    idx = np.flatnonzero(changed)
    if idx.size:
        to8 = np.float32(255.0 / 65535.0)
        p, o = plate16.reshape(-1, 3), out16.reshape(-1, 3)
        de[idx] = material_de._de00(p[idx].astype(np.float32) * to8, o[idx].astype(np.float32) * to8)
    return de.reshape(changed.shape)


def _edge_entry(sink: OverlaySink, edges: np.ndarray, source: str) -> dict[str, Any]:
    h, w = edges.shape
    ok, buf = cv2.imencode(".png", np.where(edges, EDGE_VALUE, 0).astype(np.uint8))
    if not ok:
        raise RuntimeError("cv2 could not encode an edge mask")
    return {
        "hash": sink.put_bytes(bytes(buf), "png"),
        "format": EDGE_FORMAT,
        "media_type": "image/png",
        "width": int(w),
        "height": int(h),
        "edge_value": EDGE_VALUE,
        "edge_px": int(edges.sum()),
        "source": source,
    }


def _overlays(
    sink: OverlaySink,
    plate16: np.ndarray,
    out16: np.ndarray,
    changed: np.ndarray,
    edge_masks: tuple[np.ndarray, np.ndarray, str] | None,
    tier: str,
) -> dict[str, Any]:
    """The `metrics.overlays` block (module docstring): the edge masks and the dE00 map, stored in `sink`."""
    h, w = changed.shape
    block: dict[str, Any] = {"schema": OVERLAYS_SCHEMA, "width": int(w), "height": int(h)}
    notes: list[str] = []
    if edge_masks is None:
        block["edges_plate"] = block["edges_candidate"] = None
        notes.append(
            "edges: tier C has no G-buffer edge masks"
            if tier == "C"
            else "edges: no G-buffer edges were scored"
        )
    else:
        ref, cand, backend = edge_masks
        block["edges_plate"] = _edge_entry(sink, ref, "gbuffer structural edges (depth, normal, id)")
        block["edges_candidate"] = _edge_entry(sink, cand, backend)
        if ref.shape != (h, w):
            notes.append(f"edges: scored at {ref.shape[1]}x{ref.shape[0]}, never resampled up to {w}x{h}")
    de = _de00_map(plate16, out16, changed)
    block["de00_map"] = {
        "hash": sink.put_bytes(de.astype("<f2").tobytes(), DE00_EXT),
        "format": DE00_FORMAT,
        "media_type": "application/octet-stream",
        "encoding": DE00_ENCODING,
        "width": int(w),
        "height": int(h),
        "unit": "dE00",
        "scale": 1.0,  # stored value x scale = dE00
        "max": float(de.max()) if de.size else 0.0,
        "changed_px": int(changed.sum()),
    }
    block["notes"] = notes
    return block


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
    overlay_sink: OverlaySink | None = None,
) -> GateReport:
    """Score `out` against `plate` (see the module docstring). `edges` is `gbuffer.from_packet`'s result and
    `crypto_material` the material Cryptomatte, when the caller already has them (they are read otherwise).
    `overlay_sink` (else the one bound by `overlays_to()` / `install`) stores the viewer overlays."""
    sink = overlay_sink if overlay_sink is not None else _SINK.get()
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
    points: list[FlaggedPoint] = []
    locked_px: np.ndarray | None = None
    edge_masks: tuple[np.ndarray, np.ndarray, str] | None = None  # (reference, candidate, backend)

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
    checks.add("feather_band.de00_p99", om["feather_band_p99"], status="advisory")  # decision 35
    notes.append(FEATHER_NOTE)
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
        if sink is not None:  # the candidate side exactly as edge_recall detected it (its default detector)
            cand = gbuffer.test_edges(gbuffer.resample_rgb(out8, emap.width, emap.height))
            edge_masks = (emap.edges, cand, str(rec.get("backend") or "canny"))
        checks.add(gbuffer.METRIC_ID, rec.get("recall_r2"), variant=variant)
        notes.append(f"gbuffer: {rec.get('backend')} detector at {emap.width} px, r = 2, {variant} bound")
        inv = invention.report(
            out8, emap, ctx["ids"], legend or ctx.get("legend"), plate_rgb=plate8, tier="A"
        )
        metrics["invention"] = inv
        metrics["invention_flags"] = inv["count"]
        points.extend(_invention_points(inv, (emap.width, emap.height), w, h))
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
        locked, edited, locked_px = _locked_materials(per, np.asarray(ranks), np.asarray(cov), m)
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
    if depth_fn is not None:
        notes.append(f"depth agreement backend: {getattr(depth_fn, 'name', 'unknown')}")
        if tier != "C":
            notes.append("depth agreement: not scored on tier A, the G-buffer is the reference")

    # where to look first: invention boxes (above), the worst dE00 tiles of a missed metric, camera drift
    regions: list[tuple[str, np.ndarray, str]] = []
    if m is not None:
        if checks.misses("outside_mask.de00"):
            regions.append(("outside_mask.de00", ~m, "outside the mask"))
        if checks.misses("feather_band.de00_p99"):
            regions.append(("feather_band.de00_p99", iddiff.feather_band(m), "in the feather band"))
    if locked_px is not None and checks.misses("material.de00_p90"):
        regions.append(("material.de00_p90", locked_px, "on the locked materials"))
    changed = np.any(plate16 != out16, axis=-1) if regions or sink is not None else None
    if regions:
        for metric_id, region, where in regions:
            points.extend(_de_points(plate16, out16, changed, region, metric_id, where))
    if checks.failed("registration.shift_px"):
        bw, bh = min(TILE_PX, w), min(TILE_PX, h)
        points.append(
            FlaggedPoint(
                x=(w - bw) // 2,
                y=(h - bh) // 2,
                w=bw,
                h=bh,
                metric_id="registration.shift_px",
                note="camera drift",
            )
        )

    # what the viewer's `e` and `h` modes draw (T109), only with a sink
    if sink is not None and changed is not None:
        try:
            metrics[OVERLAYS_KEY] = _overlays(sink, plate16, out16, changed, edge_masks, tier)
        except Exception as e:  # a convenience for the viewer, never a reason to fail the gate
            metrics[OVERLAYS_KEY] = {"schema": OVERLAYS_SCHEMA, "error": f"{type(e).__name__}: {e}"[:300]}
            notes.append(f"overlays: not stored ({type(e).__name__})")

    if extra_metrics:
        metrics.update(extra_metrics)
    ref = engine if isinstance(engine, EngineRef) or engine is None else EngineRef(**dict(engine))
    return GateReport(
        engine=ref,
        tier=tier,
        metrics=metrics,
        checks=checks.items,
        flagged_points=points,
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
