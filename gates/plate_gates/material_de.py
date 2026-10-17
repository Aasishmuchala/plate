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

One pass over the frame, not one per id (router plan T67: the 4K gate run). The ranks are mapped once to a
label image (which legend id each pixel belongs to); raw counts, bounding boxes and the erosion come from that
image (np.bincount, scipy.ndimage.find_objects, one min/max filter pair), and only pixels near a shared
(0.5 + 0.5) membership are eroded per id. The plate's Lab is computed once per pixel (the sRGB curve read from an
8-bit table) and summed per id with np.bincount; the output's Lab only where its pixels differ from the plate.
The isolated blur is still per material, but only near pixels that changed: a pixel whose blur window (radius at
most 4 sigma + 1) holds no changed pixel of its material blurs to the same value in the plate and the output, so
its dE00 is exactly 0 and is never computed, and a sparse id is blurred cell by cell (_TILE px cells) instead of
over its whole bounding box. The blurred pixels that differ are scored in one batched CIEDE2000 in float32
(within 1e-5 dE00 of float64); median and p90 are numpy's (linear interpolation) over those values plus the
exact zeros. Batches run on up to 8 threads (numpy and OpenCV release the GIL) and are summed in batch order, so
the numbers do not depend on the thread count. A composite, bit-identical to the plate outside its mask, costs
little more than its edited materials; a whole-frame edit costs one Lab + CIEDE2000 pass over the measured
pixels. The per-id implementation this replaced is frozen in gates/tests/test_perf_4k.py, which holds this
module to its numbers within 0.05 dE00 (they agree to about 1e-5).

Thresholds: material.de00_median <= 2 and material.de00_p90 <= 5 (thresholds.yaml, hard).
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import cv2
import numpy as np
from scipy import ndimage

from .color import _D, _M, _WHITE, ciede2000
from .masks import erode

__all__ = ["per_material", "legend_entries", "id_to_u32", "SIGMA", "ERODE_PX", "MIN_PX"]

SIGMA = 6.0
ERODE_PX = 3
MIN_PX = 100
MASK_THRESHOLD = 0.5

_ID_RE = re.compile(r"^[a-z]:[0-9a-fA-F]{1,8}$")

# color.srgb_to_lab's constants: the sRGB decoding curve tabulated for the 256 8-bit codes (the same float64
# values), and the RGB -> XYZ / white matrix as one product
_CODES = np.arange(256, dtype=np.float64) / 255.0
_LIN8 = np.where(_CODES <= 0.04045, _CODES / 12.92, ((_CODES + 0.055) / 1.055) ** 2.4)
_M_W = (_M / _WHITE[:, None]).T
_M_W32 = _M_W.astype(np.float32)
_D3 = _D**3
_SLOPE = 1 / (3 * _D * _D)
_CHUNK = 1 << 20  # pixels per batch: bounds the float64 temporaries at 4K
_THREADS = max(1, min(8, os.cpu_count() or 1))  # numpy releases the GIL inside each batch
_TILE = 256  # cell size of the isolated blur over a sparse id

Box = tuple[slice, slice]


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


def _lab8(rgb8: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """color.srgb_to_lab of (N, 3) uint8 as three float64 planes (L, a, b), the sRGB curve looked up."""
    t = _LIN8[rgb8] @ _M_W
    f = np.where(t > _D3, np.cbrt(t), t * _SLOPE + 4 / 29)
    return 116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])


def _lab_f32(rgb: np.ndarray) -> np.ndarray:
    """color.srgb_to_lab of (N, 3) float 0..255 values (the blurred images) in float32."""
    x = np.asarray(rgb, dtype=np.float32) * np.float32(1 / 255)
    lin = np.where(
        x <= 0.04045, x / np.float32(12.92), ((x + np.float32(0.055)) / np.float32(1.055)) ** np.float32(2.4)
    )
    t = lin @ _M_W32
    f = np.where(t > np.float32(_D3), np.cbrt(t), t * np.float32(_SLOPE) + np.float32(4 / 29))
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], -1)


def _pmap(fn: Callable[[Any], Any], items: Iterable[Any]) -> list[Any]:
    """[fn(i) for i in items] on a few threads, results in input order (so every sum is deterministic)."""
    items = list(items)
    if len(items) < 2 or _THREADS < 2:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(min(_THREADS, len(items))) as pool:
        return list(pool.map(fn, items))


def _lab_sums(
    rgb_flat: np.ndarray,
    bins: np.ndarray,
    size: int,
    idx: np.ndarray | None = None,
    minus: np.ndarray | None = None,
) -> np.ndarray:
    """(size, 3): per-bin sums of the Lab of the (N, 3) uint8 pixels rgb_flat[idx] (all of them when idx is None),
    less the Lab of minus[idx] when given; `bins` holds one bin per summed pixel."""

    def part(s: int) -> np.ndarray:
        take = slice(s, s + _CHUNK) if idx is None else idx[s : s + _CHUNK]
        lab = _lab8(rgb_flat[take])
        if minus is not None:
            lab = tuple(a - b for a, b in zip(lab, _lab8(minus[take]), strict=True))
        b = bins[s : s + _CHUNK]
        return np.stack([np.bincount(b, weights=lab[c], minlength=size) for c in range(3)], -1)

    parts = _pmap(part, range(0, bins.size, _CHUNK))
    return np.sum(parts, axis=0) if parts else np.zeros((size, 3))


def _de00(bp: np.ndarray, bt: np.ndarray) -> np.ndarray:
    """CIEDE2000 between two (N, 3) float 0..255 RGB arrays, in batches."""
    parts = _pmap(
        lambda s: ciede2000(_lab_f32(bp[s : s + _CHUNK]), _lab_f32(bt[s : s + _CHUNK])),
        range(0, len(bp), _CHUNK),
    )
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


# ---------------------------------------------------------------------------- the label image


def _labels(
    ranks: np.ndarray, cov: np.ndarray, uids: np.ndarray, threshold: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Which wanted id each pixel belongs to: (lab, extra_pix, extra_lab).

    `lab` (H*W,) int32 is the index into `uids` of the pixel's id (coverage summed over the ranks >= threshold),
    -1 for none. A pixel can belong to two ids (0.5 + 0.5); its second membership is a pair (extra_pix,
    extra_lab). A pixel where one rank alone hits a wanted id (nearly all) is settled by that rank's coverage;
    pixels where two ranks hit get the summed-coverage rule of the per-id test, on their own.
    """
    h, w, levels = ranks.shape
    n, k = h * w, uids.size
    if not levels:
        return np.full(n, -1, np.int32), np.zeros(0, np.int64), np.zeros(0, np.int32)
    ranks2, cov2 = ranks.reshape(n, levels), cov.reshape(n, levels)
    live = cov2 != 0  # zero coverage adds nothing to any id's sum; one read of the interleaved coverage
    # the first rank, which holds nearly every pixel's id: settled without index arrays over the frame
    tot = cov2[:, 0].copy()
    r0 = ranks2[:, 0].copy()
    pos = np.minimum(np.searchsorted(uids, r0), k - 1)
    lab = np.where((uids[pos] == r0) & live[:, 0], pos, -1).astype(np.int32)
    multi = np.zeros(n, bool)
    for lv in range(1, levels):
        idx = np.flatnonzero(live[:, lv])
        if not idx.size:
            continue
        rv = ranks2[idx, lv]
        pos = np.minimum(np.searchsorted(uids, rv), k - 1)
        hit = uids[pos] == rv
        idx, pos = idx[hit], pos[hit]
        if not idx.size:
            continue
        taken = lab[idx] >= 0
        multi[idx[taken]] = True
        free = ~taken
        lab[idx[free]] = pos[free]
        tot[idx[free]] = cov2[idx[free], lv]
    lab[(lab >= 0) & ~multi & ~(tot >= threshold)] = -1

    extra_pix = np.zeros(0, np.int64)
    extra_lab = np.zeros(0, np.int32)
    sub = np.flatnonzero(multi)
    if sub.size:
        r_s, c_s = ranks2[sub], cov2[sub]
        pos_s = np.minimum(np.searchsorted(uids, r_s), k - 1)
        hit_s = (uids[pos_s] == r_s) & (c_s != 0)
        pix_parts, lab_parts = [], []
        for lv in range(levels):
            first = hit_s[:, lv].copy()  # the first rank holding an id stands for it
            for prev in range(lv):
                first &= ~(hit_s[:, prev] & (r_s[:, prev] == r_s[:, lv]))
            if not first.any():
                continue
            r_f, c_f = r_s[first], c_s[first]
            summed = np.where(r_f == r_f[:, lv : lv + 1], c_f, np.float32(0.0)).sum(axis=1)
            ok = summed >= threshold
            pix_parts.append(sub[first][ok])
            lab_parts.append(pos_s[first, lv][ok].astype(np.int32))
        lab[sub] = -1
        if pix_parts:
            pix, labs = np.concatenate(pix_parts), np.concatenate(lab_parts)
            first_pix, at = np.unique(pix, return_index=True)
            lab[first_pix] = labs[at]
            rest = np.ones(pix.size, bool)
            rest[at] = False
            extra_pix, extra_lab = pix[rest], labs[rest]
    return lab, extra_pix, extra_lab


def _grow(box: Box | None, ys: np.ndarray, xs: np.ndarray) -> Box | None:
    """The bounding box `box` (None: empty) grown to hold the pixels (ys, xs)."""
    if not ys.size:
        return box
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    if box is not None:
        y0, y1 = min(y0, box[0].start), max(y1, box[0].stop)
        x0, x1 = min(x0, box[1].start), max(x1, box[1].stop)
    return slice(y0, y1), slice(x0, x1)


def _expand(box: Box, by: int, h: int, w: int) -> Box:
    return (
        slice(max(box[0].start - by, 0), min(box[0].stop + by, h)),
        slice(max(box[1].start - by, 0), min(box[1].stop + by, w)),
    )


def _clip(box: Box, to: Box) -> Box | None:
    y0, y1 = max(box[0].start, to[0].start), min(box[0].stop, to[0].stop)
    x0, x1 = max(box[1].start, to[1].start), min(box[1].stop, to[1].stop)
    return None if y0 >= y1 or x0 >= x1 else (slice(y0, y1), slice(x0, x1))


def _scatter(mask: np.ndarray, px: np.ndarray, box: Box, w: int) -> None:
    """Set the flat frame pixels `px` (frame width w) that fall inside the crop `box` in its mask."""
    ys, xs = px // w - box[0].start, px % w - box[1].start
    inside = (ys >= 0) & (ys < mask.shape[0]) & (xs >= 0) & (xs < mask.shape[1])
    mask[ys[inside], xs[inside]] = True


def _offset(box: Box, origin: Box) -> Box:
    """`box`, given in the coordinates of the crop `origin`, in frame coordinates."""
    oy, ox = origin[0].start, origin[1].start
    return slice(box[0].start + oy, box[0].stop + oy), slice(box[1].start + ox, box[1].stop + ox)


def _blocks(mask: np.ndarray, tile: int) -> list[Box]:
    """Boxes covering every True pixel of `mask`: the cells of a tile x tile grid that hold one, merged along
    each row of cells. A mask spread over most of its cells is one box (the blur pad would cost more than the
    empty cells it skips); a sparse one, say an id with a few pixels in each corner, is blurred cell by cell."""
    hh, ww = mask.shape
    ny, nx = -(-hh // tile), -(-ww // tile)
    if ny * nx <= 1:
        return [(slice(0, hh), slice(0, ww))]
    grid = np.zeros((ny * tile, nx * tile), bool)
    grid[:hh, :ww] = mask
    occupied = grid.reshape(ny, tile, nx, tile).any(axis=(1, 3))
    if occupied.mean() >= 0.75:
        return [(slice(0, hh), slice(0, ww))]
    boxes = []
    for r in np.flatnonzero(occupied.any(axis=1)).tolist():
        edges = np.flatnonzero(np.diff(np.concatenate([[0], occupied[r].astype(np.int8), [0]])))
        for c0, c1 in zip(edges[0::2].tolist(), edges[1::2].tolist(), strict=True):
            boxes.append((slice(r * tile, min((r + 1) * tile, hh)), slice(c0 * tile, min(c1 * tile, ww))))
    return boxes


def _inner(box: Box, outer: Box) -> Box:
    """`box` in the coordinates of the crop `outer` (which holds it)."""
    oy, ox = outer[0].start, outer[1].start
    return slice(box[0].start - oy, box[0].stop - oy), slice(box[1].start - ox, box[1].stop - ox)


def _median_p90(vals: np.ndarray, n: int) -> tuple[float, float]:
    """np.median and np.percentile(.., 90) of `vals` padded with exact zeros (all dE00 >= 0) to `n` values."""
    v = np.asarray(vals, dtype=np.float64)
    z = n - v.size
    mid = n // 2
    vi = (n - 1) * (90 / 100)  # numpy's 'linear' method: virtual index (n - 1) q, then its two-sided lerp
    lo = math.floor(vi)
    hi = min(lo + 1, n - 1)
    kth = sorted({i - z for i in (mid - 1 + n % 2, mid, lo, hi) if i >= z})
    part = np.partition(v, kth) if kth else v

    def at(i: int) -> float:
        return 0.0 if i < z else float(part[i - z])

    median = at(mid) if n % 2 else (at(mid - 1) + at(mid)) / 2.0
    a, b = at(lo), at(hi)
    g = vi - lo
    p90 = b - (b - a) * (1 - g) if g >= 0.5 else a + (b - a) * g
    return median, p90


# ---------------------------------------------------------------------------- the gate


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
    (kind=None scores every entry). `targets`: {id: (L, a, b)}. `threshold` must be above 0.
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

    chosen: dict[str, int] = {}  # legend order, each id string once
    for entry in legend_entries(legend):
        if kind is not None and entry.get("kind") not in (None, kind):
            continue
        cid = str(entry["id"])
        if cid not in chosen:
            chosen[cid] = id_to_u32(cid)
    if not chosen:
        return {}
    uids = np.unique(np.array(list(chosen.values()), dtype=np.uint32))
    k = uids.size
    ranks = ranks.astype(np.uint32, copy=False)
    h, w = plate.shape[:2]
    n = h * w
    # the float Gaussian kernel radius is at most this, so a crop this much wider blurs like the full frame
    pad = int(np.ceil(4.0 * sigma)) + 1

    # raw membership, in row bands: counts and bounding boxes per id
    bands = [(y, min(y + max(_CHUNK // w, 1), h)) for y in range(0, h, max(_CHUNK // w, 1))]
    found = _pmap(lambda b: _labels(ranks[b[0] : b[1]], cov[b[0] : b[1]], uids, threshold), bands)
    lab = np.concatenate([f[0] for f in found])
    extra_pix = np.concatenate([f[1] + b[0] * w for f, b in zip(found, bands, strict=True)])
    extra_lab = np.concatenate([f[2] for f in found])
    lab2d = lab.reshape(h, w)
    lab1 = lab2d + 1  # label + 1, 0 for none: the form bincount, find_objects and the min/max filters take
    raw_n = np.bincount(lab1.reshape(-1), minlength=k + 1)[1:] + np.bincount(extra_lab, minlength=k)
    raw_box: list[Box | None] = list(ndimage.find_objects(lab1, max_label=k))
    extra_of: dict[int, np.ndarray] = {}
    for j in np.unique(extra_lab).tolist():
        extra_of[j] = extra_pix[extra_lab == j]
        raw_box[j] = _grow(raw_box[j], extra_of[j] // w, extra_of[j] % w)

    def raw_on(j: int, box: Box) -> np.ndarray:
        r = lab2d[box] == j
        if j in extra_of:
            _scatter(r, extra_of[j], box, w)
        return r

    # eroded membership: a pixel stays when every pixel within erode_px holds its id (the frame edge does not
    # cut). The label image settles every pixel but those near a second membership; around those an id's raw
    # mask is eroded on its own and the pixels it adds are kept as a list per id.
    e = max(int(erode_px), 0)
    if e:  # eid: the eroded label + 1, 0 for none
        kern = np.ones((2 * e + 1, 2 * e + 1), np.uint8)
        small = lab1.astype(np.uint16 if k < 65535 else np.float32)
        lo, hi = cv2.erode(small, kern), cv2.dilate(small, kern)
        eid = np.where(lo == hi, lo, 0)
        eid = eid if eid.dtype == np.uint16 else eid.astype(np.int32)
    else:
        eid = lab1
    n_px = np.bincount(eid.reshape(-1), minlength=k + 1)[1:]
    gain_of: dict[int, np.ndarray] = {}
    for j, px in extra_of.items():
        if raw_n[j] < min_px:
            continue
        near = _expand(_grow(None, px // w, px % w), e, h, w)
        crop = _expand(near, e + 1, h, w)  # the erosion's context, so `near` erodes as in the full frame
        inner = _inner(near, crop)
        got = np.zeros((crop[0].stop - crop[0].start, crop[1].stop - crop[1].start), bool)
        got[inner] = erode(raw_on(j, crop), erode_px)[inner] & (eid[crop][inner] != j + 1)
        ys, xs = np.nonzero(got)
        if ys.size:
            gain_of[j] = (ys + crop[0].start).astype(np.int64) * w + (xs + crop[1].start)
            n_px[j] += ys.size
    keep = (raw_n >= min_px) & (n_px >= min_px) & (n_px > 0)

    # the plate's Lab summed per id over its measured pixels, in batches (bin k collects the rest); the
    # output's is the plate's plus the change at the pixels that differ
    bins = np.concatenate([[k], np.where(keep, np.arange(k), k)]).astype(np.intp)[eid]
    flat_bins = bins.reshape(-1)
    gained = [j for j in gain_of if keep[j]]
    sh_pix = np.concatenate([np.zeros(0, np.int64)] + [gain_of[j] for j in gained])
    sh_bin = np.concatenate([np.zeros(0, np.intp)] + [np.full(gain_of[j].size, j, np.intp) for j in gained])
    plate_flat, test_flat = plate.reshape(n, 3), test.reshape(n, 3)
    sums_p = _lab_sums(plate_flat, flat_bins, k + 1) + _lab_sums(plate_flat, sh_bin, k + 1, idx=sh_pix)
    changed = (
        (plate[..., 0] != test[..., 0]) | (plate[..., 1] != test[..., 1]) | (plate[..., 2] != test[..., 2])
    )
    flat_changed = changed.reshape(-1)
    moved = np.flatnonzero(flat_changed & (flat_bins < k))
    sel = flat_changed[sh_pix]
    sums_t = (
        sums_p
        + _lab_sums(test_flat, flat_bins[moved], k + 1, idx=moved, minus=plate_flat)
        + _lab_sums(test_flat, sh_bin[sel], k + 1, idx=sh_pix[sel], minus=plate_flat)
    )
    sums_p, sums_t = sums_p[:k], sums_t[:k]

    # dE00 where it can be above 0: within the blur radius of a pixel that changed. The blurred plate and output
    # are gathered at those pixels (per id and block, on threads), then scored in one batched CIEDE2000.
    pairs: list[tuple[int, np.ndarray, np.ndarray]] = []
    if (moved.size or sel.any()) and isolate:
        changed_box = list(ndimage.find_objects(np.where(changed, lab1, 0), max_label=k))
        moved_extra = flat_changed[extra_pix]
        for j in np.unique(extra_lab[moved_extra]).tolist():
            px = extra_pix[moved_extra & (extra_lab == j)]
            changed_box[j] = _grow(changed_box[j], px // w, px % w)

        def measured(j: int, box: Box) -> np.ndarray:
            m = eid[box] == j + 1
            if j in gain_of:
                _scatter(m, gain_of[j], box, w)
            return m

        tasks: list[tuple[int, Box]] = []
        for j in np.flatnonzero(keep).tolist():
            reach = None if changed_box[j] is None else _clip(_expand(changed_box[j], pad, h, w), raw_box[j])
            if reach is not None:
                m = measured(j, reach)
                tasks += [(j, _offset(blk, reach)) for blk in _blocks(m, _TILE) if m[blk].any()]

        def blurred(task: tuple[int, Box]) -> tuple[int, np.ndarray, np.ndarray]:
            j, at = task
            crop = _expand(at, pad, h, w)
            q = np.nonzero(measured(j, at))
            q = (q[0] + at[0].start - crop[0].start, q[1] + at[1].start - crop[1].start)
            weight = raw_on(j, crop).astype(np.float32)
            den = np.maximum(cv2.GaussianBlur(weight, (0, 0), sigma)[q], 1e-6)[:, None]
            bp = cv2.GaussianBlur(plate[crop].astype(np.float32) * weight[..., None], (0, 0), sigma)[q] / den
            bt = cv2.GaussianBlur(test[crop].astype(np.float32) * weight[..., None], (0, 0), sigma)[q] / den
            diff = (bp[:, 0] != bt[:, 0]) | (bp[:, 1] != bt[:, 1]) | (bp[:, 2] != bt[:, 2])
            return j, bp[diff], bt[diff]

        pairs = [r for r in _pmap(blurred, tasks) if len(r[1])]
    elif moved.size or sel.any():
        bp = cv2.GaussianBlur(plate.astype(np.float32), (0, 0), sigma).reshape(n, 3)
        bt = cv2.GaussianBlur(test.astype(np.float32), (0, 0), sigma).reshape(n, 3)
        diff = (bp[:, 0] != bt[:, 0]) | (bp[:, 1] != bt[:, 1]) | (bp[:, 2] != bt[:, 2])
        base, sh_sel = np.flatnonzero(diff & (flat_bins < k)), diff[sh_pix]
        idx = np.concatenate([base, sh_pix[sh_sel]])
        lab_d = np.concatenate([flat_bins[base], sh_bin[sh_sel]])
        order = np.argsort(lab_d.astype(np.int16 if k < 32767 else np.intp), kind="stable")
        cuts = np.searchsorted(lab_d[order], np.arange(k + 1))
        pairs = [
            (j, bp[idx[order[cuts[j] : cuts[j + 1]]]], bt[idx[order[cuts[j] : cuts[j + 1]]]])
            for j in range(k)
            if cuts[j + 1] > cuts[j]
        ]
    de_of: dict[int, list[np.ndarray]] = {}
    if pairs:
        de = _de00(np.concatenate([p[1] for p in pairs]), np.concatenate([p[2] for p in pairs]))
        at = 0
        for j, a, _ in pairs:
            de_of.setdefault(j, []).append(de[at : at + len(a)])
            at += len(a)

    label_of = {int(u): i for i, u in enumerate(uids.tolist())}
    out: dict[str, dict] = {}
    for cid, u in chosen.items():
        j = label_of[u]
        if not keep[j]:
            continue
        count = int(n_px[j])
        median, p90 = _median_p90(np.concatenate(de_of[j]) if j in de_of else np.zeros(0), count)
        mean_plate = sums_p[j] / count
        mean_test = sums_t[j] / count
        row: dict[str, Any] = {
            "de00_median": median,
            "de00_p90": p90,
            "n_px": count,
            "lab_mean_plate": [float(v) for v in mean_plate],
            "lab_mean_test": [float(v) for v in mean_test],
        }
        goal = want.get(cid.lower())
        if goal is not None:
            row["target_de"] = float(ciede2000(mean_test[None, :], goal[None, :])[0])
        out[cid] = row
    return out
