"""Depth agreement for Tier C (no G-buffer): does the output keep the plate's depth structure?

Tier C packets have no Z pass, so the hard G-buffer edge-recall gate cannot run. What can run is a comparison of
two monocular depth estimates, one of the plate and one of the output, from the pixels worker's depth backend.
Monocular depth is relative (unknown scale and shift per image), so the comparison is scale-and-shift
invariant: fit `z_est ~ a * z_ref + b` by least squares on the valid pixels, then score the residual.

    compare(z_ref, z_est, mask=None) -> {scale, shift, abs_rel, delta_1, edge_f1, ...}

    scale, shift    the least-squares a and b
    abs_rel         median |a*z_ref + b - z_est| / z_est   (relative error, median so a few wild pixels do not dominate)
    delta_1         fraction of valid pixels whose aligned prediction is within 1.25x of z_est, either way
    edge_f1         F1 of the depth-edge maps at a 2 px tolerance; an edge is a relative depth jump over 2%
                    ("the 2% rule") between a pixel and its right or lower neighbour, measured after the
                    alignment so both maps are in z_est's units. Invented or erased depth discontinuities
                    (a window punched into a wall, a hedge flattened) move this number.
    corr            Pearson correlation of the valid z_ref and z_est (0.0 when either is constant). The fit
                    alone cannot see a collapsed estimate: a flat or unrelated z_est is fitted by a ~ 0 and
                    scores a deceptively good abs_rel. Below MIN_CORR the alignment is meaningless, so edge_f1
                    then compares the raw maps (each in its own units) and a lost structure shows as a low F1.

The thresholds are ADVISORY: image-only depth agreement is triage, never a delivery gate (router plan
decision 3). They are not in thresholds.yaml yet; a calibrated set would add them there.
"""

from __future__ import annotations

import numpy as np

from .masks import as_bool
from .structure import tol_metrics

__all__ = ["compare", "depth_edges", "EDGE_REL", "EDGE_TOL_PX", "DELTA_1_RATIO", "MIN_CORR"]

EDGE_REL = 0.02  # the 2% rule: a relative depth jump above this is a depth edge
EDGE_TOL_PX = 2  # edge maps agree within this many pixels
DELTA_1_RATIO = 1.25
BACKGROUND = 1e9  # renderers write ~1e10 for "no surface"; anything at or above this is not a depth
MIN_VALID_PX = 3
MIN_CORR = 0.2  # below this the least-squares alignment says nothing (flat or unrelated estimate)


def depth_edges(z: np.ndarray, valid: np.ndarray | None = None, rel: float = EDGE_REL) -> np.ndarray:
    """bool (H, W) depth-edge map: True where the relative jump to the right or lower neighbour exceeds `rel`.

    Both pixels of a pair must be valid (finite, positive, and inside `valid` when given); the edge is marked on
    the pixel whose right or lower neighbour differs, so it is one pixel thick, the same convention as the
    analytic edge map of fixtures.synthetic.make_depth_scene.
    """
    z = np.asarray(z, dtype=np.float64)
    ok = np.isfinite(z) & (z > 0) & (z < BACKGROUND)
    if valid is not None:
        ok &= as_bool(valid)
    zz = np.where(ok, z, 1.0)  # keeps the divisions finite; pairs touching an invalid pixel are dropped
    edges = np.zeros(z.shape, dtype=bool)
    a, b = zz[:, :-1], zz[:, 1:]  # right neighbour
    edges[:, :-1] |= (np.abs(a - b) / np.minimum(a, b) > rel) & ok[:, :-1] & ok[:, 1:]
    a, b = zz[:-1, :], zz[1:, :]  # lower neighbour
    edges[:-1, :] |= (np.abs(a - b) / np.minimum(a, b) > rel) & ok[:-1, :] & ok[1:, :]
    return edges


def _f1(precision: float, recall: float) -> float:
    return 0.0 if precision + recall <= 0 else 2.0 * precision * recall / (precision + recall)


def compare(z_ref: np.ndarray, z_est: np.ndarray, mask: np.ndarray | None = None) -> dict:
    """Scale-and-shift-invariant agreement of `z_est` with `z_ref` (both (H, W) depth, any positive unit).

    `mask` (any encoding masks.as_bool takes) restricts the comparison, for instance to the pixels the edit
    left alone. Non-finite, non-positive and background (>= 1e9) pixels are ignored. Raises ValueError when the
    shapes differ or fewer than 3 valid pixels remain. Returns floats: scale, shift, abs_rel, delta_1, edge_f1,
    corr, plus edge_precision / edge_recall (the two halves of edge_f1) and n_valid.
    """
    zr = np.asarray(z_ref, dtype=np.float64)
    ze = np.asarray(z_est, dtype=np.float64)
    if zr.ndim != 2 or zr.shape != ze.shape:
        raise ValueError(f"z_ref and z_est must be 2-D arrays of one shape, got {zr.shape} and {ze.shape}")
    ok = np.isfinite(zr) & np.isfinite(ze) & (zr < BACKGROUND) & (ze > 0) & (ze < BACKGROUND)
    if mask is not None:
        m = as_bool(mask)
        if m.shape != zr.shape:
            raise ValueError(f"mask shape {m.shape} does not match the depth maps {zr.shape}")
        ok &= m
    n = int(ok.sum())
    if n < MIN_VALID_PX:
        raise ValueError(f"only {n} valid pixels to fit a scale and shift (need {MIN_VALID_PX})")

    x, y = zr[ok], ze[ok]
    xm, ym = x.mean(), y.mean()
    var = ((x - xm) ** 2).mean()
    var_y = ((y - ym) ** 2).mean()
    if var <= 1e-12 * max(1.0, xm * xm):  # a constant reference has no slope to fit
        a, b = 0.0, float(ym)
    else:
        a = float(((x - xm) * (y - ym)).mean() / var)
        b = float(ym - a * xm)
    flat_y = var_y <= 1e-12 * max(1.0, ym * ym)
    corr = 0.0 if (flat_y or var <= 1e-12 * max(1.0, xm * xm)) else float(a * np.sqrt(var / var_y))

    pred = a * zr + b
    p, e = pred[ok], ze[ok]
    abs_rel = float(np.median(np.abs(p - e) / e))
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(p > 0, np.maximum(p / e, e / np.where(p > 0, p, 1.0)), np.inf)
    delta_1 = float((ratio < DELTA_1_RATIO).mean())

    edges_ref = depth_edges(pred if corr >= MIN_CORR else zr, ok)
    edges_est = depth_edges(ze, ok)
    if not edges_ref.any() and not edges_est.any():
        precision = recall = f1 = 1.0  # two flat surfaces agree that nothing is an edge
    else:
        t = tol_metrics(edges_ref, edges_est, EDGE_TOL_PX)
        precision, recall = float(t["precision"]), float(t["recall"])
        f1 = _f1(precision, recall)
    return {
        "scale": a,
        "shift": b,
        "abs_rel": abs_rel,
        "delta_1": delta_1,
        "edge_f1": float(f1),
        "corr": corr,
        "edge_precision": precision,
        "edge_recall": recall,
        "n_valid": n,
    }
