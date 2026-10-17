"""Per-material colour drift by Cryptomatte id: how far did each material move between the plate and the output?

    per_material(plate_rgb, test_rgb, ranks, coverage, legend, targets=None) -> {m_id: {...}}

For every material in the legend the pixels it covers (Cryptomatte coverage summed over the ranks, >= 0.5) are
eroded 3 px so edge blending does not count, and CIEDE2000 is measured between the plate and the output on
sigma=6 blurred 8-bit gamma RGB (the fidelity_bench convention: judge colour, not texture). Ids with fewer than
100 pixels left are skipped. Per material the result holds de00_median, de00_p90, n_px, the mean Lab of the
plate and of the output over the same pixels, and, when `targets` gives a Lab for that id (a hero frame from
the Set node), target_de: CIEDE2000 between the output's mean Lab and that target.

Blur isolation. A plain global blur lets a strongly edited neighbour bleed up to ~2 dE00 into an untouched
material across their shared border, so an edit to one material would flag its neighbours. By default (isolate=True)
each material is blurred only with its own pixels (normalised convolution: blur(img * m) / blur(m)), which keeps
"every other material is untouched" at exactly 0 dE00. isolate=False is the literal global blur of
fidelity_bench.compare.

Thresholds: material.de00_median <= 2 and material.de00_p90 <= 5 (thresholds.yaml, hard).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import cv2
import numpy as np

from .color import ciede2000, srgb_to_lab
from .masks import erode

__all__ = ["per_material", "legend_entries", "id_to_u32", "SIGMA", "ERODE_PX", "MIN_PX"]

SIGMA = 6.0
ERODE_PX = 3
MIN_PX = 100
MASK_THRESHOLD = 0.5

_ID_RE = re.compile(r"^[a-z]:[0-9a-fA-F]{1,8}$")


def _is_id(text: Any) -> bool:
    return isinstance(text, str) and _ID_RE.match(text) is not None


def id_to_u32(cid: str | int) -> int:
    """'m:3d8800df', '3d8800df' or an int -> the 32-bit Cryptomatte id value."""
    if isinstance(cid, (int, np.integer)):
        return int(cid) & 0xFFFFFFFF
    text = str(cid).strip().lower()
    if ":" in text:
        text = text.split(":", 1)[1]
    return int(text, 16)


def legend_entries(legend: Any) -> list[dict]:
    """Normalise a legend to a list of entry dicts, each with an "id".

    Accepts the id_legend.json dict ({"entries": [...]}), a list of entries, or a plain mapping from id to an
    entry dict (or to a name) or from a name to an id string.
    """
    if legend is None:
        return []
    if isinstance(legend, Mapping) and "entries" in legend:
        legend = legend["entries"]
    if isinstance(legend, Mapping):
        out = []
        for key, val in legend.items():
            if _is_id(key):
                entry = (
                    dict(val) if isinstance(val, Mapping) else {"name": val} if isinstance(val, str) else {}
                )
                entry.setdefault("id", key)
            elif _is_id(val):
                entry = {"id": val, "name": key}
            else:
                raise ValueError(f"legend mapping entry {key!r} holds neither an id key nor an id value")
            out.append(entry)
        return out
    if isinstance(legend, Sequence) and not isinstance(legend, (str, bytes)):
        for entry in legend:
            if not isinstance(entry, Mapping) or "id" not in entry:
                raise ValueError("every legend entry must be a mapping with an 'id'")
        return [dict(entry) for entry in legend]
    raise ValueError(f"cannot read a legend from {type(legend).__name__}")


def _rgb8(img: np.ndarray, name: str) -> np.ndarray:
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] != 3:
        raise ValueError(f"{name} must be an RGB array of shape (h, w, 3)")
    if a.dtype == np.uint8:
        return a
    if a.dtype == np.uint16:
        return np.clip(np.rint(a.astype(np.float64) / 257.0), 0, 255).astype(np.uint8)
    if np.issubdtype(a.dtype, np.floating):
        return np.clip(np.rint(a.astype(np.float64) * 255.0), 0, 255).astype(np.uint8)
    raise ValueError(f"{name} must be uint8, uint16 or float 0..1, got {a.dtype}")


def _blur_lab(rgb8: np.ndarray, weight: np.ndarray | None, sigma: float) -> np.ndarray:
    """Lab of the sigma-blurred image; with `weight` (0/1 float32) the blur is normalised to those pixels."""
    f = rgb8.astype(np.float32)
    if weight is None:
        blurred = cv2.GaussianBlur(f, (0, 0), sigma)
    else:
        num = cv2.GaussianBlur(f * weight[..., None], (0, 0), sigma)
        den = cv2.GaussianBlur(weight, (0, 0), sigma)
        blurred = num / np.maximum(den, 1e-6)[..., None]
    return srgb_to_lab(blurred)


def per_material(
    plate_rgb: np.ndarray,
    test_rgb: np.ndarray,
    crypto_material_ranks: np.ndarray,
    coverage: np.ndarray,
    legend: Any,
    targets: Mapping[str, Sequence[float]] | None = None,
    *,
    kind: str | None = "material",
    sigma: float = SIGMA,
    erode_px: int = ERODE_PX,
    min_px: int = MIN_PX,
    threshold: float = MASK_THRESHOLD,
    isolate: bool = True,
) -> dict[str, dict]:
    """Per-material CIEDE2000 between the plate and the output; see the module docstring.

    `plate_rgb` / `test_rgb`: same-size RGB (uint8 gamma; uint16 and float 0..1 are converted). `crypto_material_ranks`
    uint32 ids and `coverage` float, both (H, W, levels) as plate_pixels.exr.read_cryptomatte returns them (a
    2-D pair is read as one level). `legend`: see legend_entries; entries whose "kind" is not `kind` are skipped
    (kind=None scores every entry). `targets`: {id: (L, a, b)}.
    """
    plate = _rgb8(plate_rgb, "plate_rgb")
    test = _rgb8(test_rgb, "test_rgb")
    ranks = np.asarray(crypto_material_ranks)
    cov = np.asarray(coverage)
    if ranks.ndim == 2:
        ranks, cov = ranks[..., None], cov[..., None]
    if plate.shape != test.shape or ranks.shape[:2] != plate.shape[:2] or ranks.shape != cov.shape:
        raise ValueError(
            f"shape mismatch: plate {plate.shape}, test {test.shape}, ranks {ranks.shape}, coverage {cov.shape}"
        )
    want = {str(k).lower(): np.asarray(v, dtype=np.float64) for k, v in (targets or {}).items()}

    de_global = None
    if not isolate:
        de_global = ciede2000(_blur_lab(plate, None, sigma), _blur_lab(test, None, sigma))
    lab_plate = srgb_to_lab(plate)
    lab_test = srgb_to_lab(test)
    # the float Gaussian kernel radius: a crop then blurs like the full frame does
    pad = int(np.ceil(4.0 * sigma)) + 1
    h, w = plate.shape[:2]

    out: dict[str, dict] = {}
    for entry in legend_entries(legend):
        if kind is not None and entry.get("kind") not in (None, kind):
            continue
        cid = str(entry["id"])
        if cid in out:
            continue
        target = np.uint32(id_to_u32(cid))
        raw = np.where(ranks == target, cov, 0.0).sum(axis=2) >= threshold
        if int(raw.sum()) < min_px:
            continue
        m = erode(raw, erode_px)
        n_px = int(m.sum())
        if n_px < min_px:
            continue
        ys, xs = np.nonzero(raw)
        y0, y1 = max(int(ys.min()) - pad, 0), min(int(ys.max()) + 1 + pad, h)
        x0, x1 = max(int(xs.min()) - pad, 0), min(int(xs.max()) + 1 + pad, w)
        sl = (slice(y0, y1), slice(x0, x1))
        if isolate:
            weight = raw[sl].astype(np.float32)
            de = ciede2000(_blur_lab(plate[sl], weight, sigma), _blur_lab(test[sl], weight, sigma))
            de_m = de[m[sl]]
        else:
            de_m = de_global[m]
        mean_plate = lab_plate[m].mean(axis=0)
        mean_test = lab_test[m].mean(axis=0)
        row: dict[str, Any] = {
            "de00_median": float(np.median(de_m)),
            "de00_p90": float(np.percentile(de_m, 90)),
            "n_px": n_px,
            "lab_mean_plate": [float(v) for v in mean_plate],
            "lab_mean_test": [float(v) for v in mean_test],
        }
        goal = want.get(cid.lower())
        if goal is not None:
            row["target_de"] = float(ciede2000(mean_test[None, :], goal[None, :])[0])
        out[cid] = row
    return out
