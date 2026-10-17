"""Invention flags: new long edges inside planar, single-id architecture regions of a Tier A packet.

BUILD_PLAN decision 14 ("invention flags 0 on architecture IDs") and the P0 failure list ("facades gain
windows"). The G-buffer says where the geometry's edges are; a planar wall, floor or pane has none inside its
own id. Any long image edge the output shows inside such a region, away from every geometry edge, is geometry
the generator made up: a window, a door, a joint line, a parapet. One flag per connected component; the
delivery bound is zero (thresholds.yaml `invention_flags`).

Per legend entry with `planar: true` and class in PLANAR_CLASSES:
  region   = rank-0 id mask eroded by erode_px (3) so anti-aliased borders never count
  edges    = gbuffer.test_edges(test) inside the region, minus every pixel within near_px (2) of a reference
             edge (the EdgeMap of gbuffer.structural_edges) and, when the plate is given, of a plate edge
  flags    = 8-connected components (gaps of gap_px bridged first so one scribble is one flag) whose pixel
             count is at least L_long = round(30 x width / 1500) (41 at 2048; thresholds long_edge_min_px)

"New" means new against the plate as well as the geometry: a planar id is not a flat colour. Floor tiles,
a shadow line, clouds seen through a pane (the pane owns rank 0 there) are long edges inside a planar id
that the render already shows; measured on fixtures/packets/room_a the display scores 7 such flags against
its own G-buffer alone and 0 once its own edges are subtracted, while a rectangle drawn on the wall is still
one flag. Without a plate the detector is the G-buffer-only rule of the plan (use it for synthetic vectors).

Tier C packets have no planar ids and no G-buffer: flags() returns None and report() says
"tier C: no planar ids". A Tier A packet whose legend marks no planar id of a scanned class (the sender rule:
architecture / ground / glazing at 0.5 % of the frame or more) is skipped the same way, with its own reason,
"tier A: legend marks no planar ids". Lettering inside signage is not scanned (signage is not planar-class
here and has its own OCR text-lock).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from . import gbuffer
from .masks import erode
from .thresholds import check as _check_threshold
from .thresholds import long_edge_min_px

__all__ = [
    "METRIC_ID",
    "NO_GBUFFER_REASON",
    "NO_PLANAR_REASON",
    "PLANAR_CLASSES",
    "TIER_C_REASON",
    "Flag",
    "InventionParams",
    "flags",
    "planar_entries",
    "report",
    "verdict",
]

METRIC_ID = "invention_flags"
PLANAR_CLASSES = ("architecture", "ground", "glazing")
TIER_C_REASON = "tier C: no planar ids"
NO_PLANAR_REASON = "tier A: legend marks no planar ids"
NO_GBUFFER_REASON = "tier A: no G-buffer edges or ids"


@dataclass(frozen=True)
class InventionParams:
    """Knobs of flags(); every one is copied into report()['params'].

    erode_px   shrink each id region so anti-aliased borders and the reference edge itself never count.
    near_px    test-edge pixels within this distance of a reference edge are the geometry, not an invention.
    min_len    component length (pixel count) that makes a flag; None = long_edge_min_px(working width).
    gap_px     bridge gaps up to this before labelling so a broken outline is one flag, not three.
    detector   gbuffer.test_edges detector ("rgb" or "fidelity_bench").
    """

    erode_px: int = 3
    near_px: int = 2
    min_len: int | None = None
    gap_px: int = 1
    detector: str = "rgb"


@dataclass(frozen=True)
class Flag:
    """One invented edge: legend id and class of the region, bbox (x0, y0, x1, y1; x1/y1 exclusive) at the
    working size, length_px = edge pixels in the component, n_edges = raw 8-connected pieces it bridges."""

    id: str
    class_: str
    bbox: tuple[int, int, int, int]
    length_px: int
    n_edges: int
    name: str = ""
    centroid: tuple[float, float] = (0.0, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "class": self.class_,
            "name": self.name,
            "bbox": list(self.bbox),
            "length_px": self.length_px,
            "n_edges": self.n_edges,
            "centroid": list(self.centroid),
        }


def _entries(legend: Any) -> list[Mapping[str, Any]]:
    if legend is None:
        return []
    if isinstance(legend, Mapping) and "entries" in legend:
        return list(legend["entries"])
    return list(legend)


def planar_entries(legend: Any) -> list[Mapping[str, Any]]:
    """Legend entries the detector scans: object kind, planar true, class in PLANAR_CLASSES."""
    out = []
    for e in _entries(legend):
        if e.get("kind", "object") != "object":
            continue
        if not bool(e.get("planar", False)):
            continue
        if e.get("class") not in PLANAR_CLASSES:
            continue
        out.append(e)
    return out


def _components(edges: np.ndarray, gap_px: int) -> tuple[int, np.ndarray]:
    """Label the edge pixels with gaps of gap_px bridged: labels are computed on the dilated map and read
    back on the raw pixels (label 0 = none)."""
    if gap_px > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * gap_px + 1, 2 * gap_px + 1))
        bridged = cv2.dilate(edges.astype(np.uint8), k) > 0
    else:
        bridged = edges
    n, lab = cv2.connectedComponents(bridged.astype(np.uint8), connectivity=8)
    lab = np.where(edges, lab, 0)
    return n, lab


def flags(
    test_rgb: np.ndarray,
    ref_edges: gbuffer.EdgeMap | None,
    ids: np.ndarray | None,
    legend: Any,
    width: int = gbuffer.WORKING_WIDTH,
    *,
    plate_rgb: np.ndarray | None = None,
    params: InventionParams | None = None,
    tier: str = "A",
) -> list[Flag] | None:
    """Invention flags of `test_rgb` against a packet's reference edges (module doc).

    The working size is the EdgeMap's (gbuffer.structural_edges already applied the no-upsample rule to
    `width`); `test_rgb`, `plate_rgb` and `ids` are brought to it (area / nearest). `plate_rgb` (the packet's
    display master) removes the plate's own edges from the search as well. Returns None for Tier C, when there
    is no EdgeMap or no ids, or when the legend has no planar entry of a scanned class; report() turns that
    into the reason string. Flags are sorted by length, longest first.
    """
    p = params or InventionParams()
    if tier != "A" or ref_edges is None or ids is None:
        return None
    entries = planar_entries(legend)
    if not entries:
        return None
    h, w = ref_edges.shape
    rgb = gbuffer.resample_rgb(test_rgb, w, h)
    idw = gbuffer.resample_ids(ids, w, h)
    if idw.shape != (h, w):
        raise ValueError(
            f"ids {np.asarray(ids).shape[:2]} and the EdgeMap {ref_edges.shape} disagree in aspect"
        )
    et = gbuffer.test_edges(rgb, p.detector)
    known = ref_edges.edges
    if plate_rgb is not None:
        known = known | gbuffer.test_edges(gbuffer.resample_rgb(plate_rgb, w, h), p.detector)
    if p.near_px > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * p.near_px + 1, 2 * p.near_px + 1))
        known = cv2.dilate(known.astype(np.uint8), k) > 0
    et = et & ~known
    min_len = int(p.min_len) if p.min_len is not None else long_edge_min_px(w)
    found: list[Flag] = []
    for e in entries:
        idv = np.uint32(gbuffer.parse_id(e["id"]))
        region = erode(idw == idv, p.erode_px)
        inside = et & region
        if not inside.any():
            continue
        n, lab = _components(inside, p.gap_px)
        if n <= 1:
            continue
        counts = np.bincount(lab.ravel(), minlength=n)
        for label in range(1, n):
            length = int(counts[label])
            if length < min_len:
                continue
            pix = lab == label
            ys, xs = np.nonzero(pix)
            n_raw = cv2.connectedComponents(pix.astype(np.uint8), connectivity=8)[0] - 1
            found.append(
                Flag(
                    id=str(e["id"]),
                    class_=str(e.get("class", "other")),
                    bbox=(int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1),
                    length_px=length,
                    n_edges=int(n_raw),
                    name=str(e.get("name", "")),
                    centroid=(float(xs.mean()), float(ys.mean())),
                )
            )
    found.sort(key=lambda f: (-f.length_px, f.id, f.bbox))
    return found


def verdict(count: int | None) -> tuple[str, float]:
    """('pass' | 'fail' | 'advisory', bound) of a flag count against thresholds.yaml invention_flags (= 0).
    None (skipped) is advisory."""
    if count is None:
        return "advisory", 0.0
    status, thr = _check_threshold(METRIC_ID, float(count))
    return status, float(thr.value)


def report(
    test_rgb: np.ndarray,
    ref_edges: gbuffer.EdgeMap | None,
    ids: np.ndarray | None,
    legend: Any,
    width: int = gbuffer.WORKING_WIDTH,
    *,
    plate_rgb: np.ndarray | None = None,
    params: InventionParams | None = None,
    tier: str = "A",
) -> dict[str, Any]:
    """flags() as a JSON-friendly gate record: {metric_id, count, flags, skipped, reason, status, params}.
    `count` is None when skipped; `reason` says why: TIER_C_REASON for a Tier C packet, NO_PLANAR_REASON for a
    Tier A legend without planar ids of a scanned class, NO_GBUFFER_REASON for a Tier A call with no edges or ids."""
    p = params or InventionParams()
    found = flags(test_rgb, ref_edges, ids, legend, width, plate_rgb=plate_rgb, params=p, tier=tier)
    w = ref_edges.width if ref_edges is not None else None
    min_len = int(p.min_len) if p.min_len is not None else (long_edge_min_px(w) if w else None)
    skipped = found is None
    count = None if skipped else len(found)
    status, bound = verdict(count)
    reason = None
    if skipped:
        if tier != "A":
            reason = TIER_C_REASON
        elif not planar_entries(legend):
            reason = NO_PLANAR_REASON
        else:
            reason = NO_GBUFFER_REASON
    return {
        "metric_id": METRIC_ID,
        "count": count,
        "flags": [] if skipped else [f.to_dict() for f in found],
        "skipped": skipped,
        "reason": reason,
        "status": status,
        "bound": bound,
        "params": {
            "width": w,
            "erode_px": p.erode_px,
            "near_px": p.near_px,
            "min_len": min_len,
            "gap_px": p.gap_px,
            "detector": p.detector,
            "plate_subtracted": plate_rgb is not None,
            "classes": list(PLANAR_CLASSES),
            "scanned_ids": [str(e["id"]) for e in planar_entries(legend)] if not skipped else [],
        },
    }
