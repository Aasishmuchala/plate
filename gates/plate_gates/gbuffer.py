"""G-buffer structural edges (depth, normal, id discontinuities) and the edge-recall gate against an image.

This is the hard geometry gate of BUILD_PLAN decision 14: `gbuffer_edge_recall_r2 >= 0.90` for delivery
(0.85 preview), Tier A only. The reference edges come from the packet's own G-buffer, never from an image, so
the gate asks one question of the AI pixels: "is every edge the geometry has still there?" Image-only Canny
agreement (align.py) stays advisory.

Evaluation width. Everything is measured at a working width, 2048 by default; `ids` and `Z` are resampled by
nearest neighbour (never blended: a blended id or depth is a value that exists nowhere), normals and images by
area averaging. A source narrower than the working width is NOT upsampled: upsampling adds no edges and only
dilutes the pixel tolerance, so the working width is min(width, native) and the EdgeMap records both. The
tolerance `r` is in working-width pixels and is deliberately not rescaled (the report carries the width).

Edge rules, each on 4-neighbour pairs (p, q = right or below), the edge pixel being p:
  depth   |Z(p) - Z(q)| / min(Z) > depth_rel (0.02), both sides rendered (0 < Z < background_value), AND the
          pair is not explained by p's or q's own tangent plane. With camera-space normals and K the depth of q
          is predicted from p's plane, Z_q^ = Z_p (n_p . r_p) / (n_p . r_q), r the pixel ray; the residual
          min(|Z_q - Z_q^|, |Z_p - Z_p^|) / min(Z) must also exceed depth_rel. A floor or driveway seen at a
          grazing angle changes depth by more than 2 % per pixel without any discontinuity; the plane test
          only ever suppresses, so a wrong normal convention degrades to the raw rule, never past it.
  normal  angle between unit normals > normal_deg (20 deg), both sides rendered with a valid normal.
  id      rank-0 Cryptomatte object id differs (a transition to id 0, the empty background, counts: a
          roofline against the sky is an edge the output must keep).
Each kind is thinned to 1 px (Zhang-Suen) and the kinds are bit-or'ed: 1 depth, 2 normal, 4 id. Strength is
in [0, 1]: depth step / (10 x depth_rel), normal angle / 90 deg, id 1.0, max over kinds.

Weighting. `coc.weights` gives w = clip(1 - c_px / c_max, 0, 1) per pixel from the camera's thin-lens circle
of confusion; recall is sum(w . [ref & dilate_r(test)]) / sum(w) over reference edge pixels, so geometry the
render itself blurs away is not demanded of the output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import structure
from .io import to_uint8
from .thresholds import check as _check_threshold

__all__ = [
    "CANNY_HI",
    "CANNY_LO",
    "CANNY_SIGMA",
    "DETECTORS",
    "KIND_DEPTH",
    "KIND_ID",
    "KIND_NAMES",
    "KIND_NORMAL",
    "METRIC_ID",
    "WORKING_WIDTH",
    "EdgeMap",
    "EdgeParams",
    "class_map",
    "edge_recall",
    "from_packet",
    "gray_u8",
    "intrinsics",
    "legend_classes",
    "parse_id",
    "resample_ids",
    "resample_mask",
    "resample_normals",
    "resample_rgb",
    "resample_z",
    "structural_edges",
    "test_edges",
    "thin",
    "verdict",
    "working_size",
    "working_width",
]

KIND_DEPTH = 1
KIND_NORMAL = 2
KIND_ID = 4
KIND_NAMES = {"depth": KIND_DEPTH, "normal": KIND_NORMAL, "id": KIND_ID}
WORKING_WIDTH = 2048
BACKGROUND_VALUE = 1e10  # V-Ray and the fixture packets write this for "nothing rendered"
METRIC_ID = "gbuffer_edge_recall_r2"


# --------------------------------------------------------------------------- parameters and result


@dataclass(frozen=True)
class EdgeParams:
    """Knobs of structural_edges; every one is copied into EdgeMap.params for the gate report.

    width            working width (px); None = native. Never upsamples unless allow_upsample.
    depth_rel        relative depth step that makes a depth edge (0.02 = 2 %).
    normal_deg       angle between neighbouring normals that makes a normal edge.
    background_value Z at or above this is "nothing rendered" (manifest zdepth background_value).
    plane_test       use normals + K to suppress depth steps a tangent plane explains (see module doc).
    thin             Zhang-Suen each kind to 1 px.
    """

    width: int | None = WORKING_WIDTH
    depth_rel: float = 0.02
    normal_deg: float = 20.0
    background_value: float = BACKGROUND_VALUE
    plane_test: bool = True
    thin: bool = True
    allow_upsample: bool = False


@dataclass
class EdgeMap:
    """Reference edges at the working size: edges bool (H, W), kind uint8 bit-or, strength float32 in [0, 1]."""

    edges: np.ndarray
    kind: np.ndarray
    strength: np.ndarray
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        return int(self.edges.shape[0]), int(self.edges.shape[1])

    @property
    def width(self) -> int:
        return int(self.edges.shape[1])

    @property
    def height(self) -> int:
        return int(self.edges.shape[0])

    def by_kind(self, kind: int | str) -> np.ndarray:
        """bool mask of the edge pixels carrying one kind bit ('depth' | 'normal' | 'id' or the bit)."""
        bit = KIND_NAMES[kind] if isinstance(kind, str) else int(kind)
        return self.edges & ((self.kind & bit) != 0)

    def counts(self) -> dict[str, int]:
        return {
            "total": int(self.edges.sum()),
            **{k: int(self.by_kind(b).sum()) for k, b in KIND_NAMES.items()},
        }

    def to_report(self) -> dict[str, Any]:
        """JSON-friendly summary for a gate report: parameters, working size and per-kind counts."""
        return {
            "params": dict(self.params),
            "width": self.width,
            "height": self.height,
            "counts": self.counts(),
        }


# --------------------------------------------------------------------------- resampling


def working_width(native_w: int, width: int | None = WORKING_WIDTH, *, allow_upsample: bool = False) -> int:
    """The width edges are measured at: min(width, native) unless upsampling is explicitly allowed."""
    native_w = int(native_w)
    if width is None:
        return native_w
    width = int(width)
    if width <= 0:
        raise ValueError(f"width must be positive, got {width}")
    if width >= native_w and not allow_upsample:
        return native_w
    return width


def working_size(
    native_shape: Sequence[int], width: int | None = WORKING_WIDTH, *, allow_upsample: bool = False
):
    """(w, h) at the working width, aspect kept (height rounded, at least 1)."""
    nh, nw = int(native_shape[0]), int(native_shape[1])
    w = working_width(nw, width, allow_upsample=allow_upsample)
    if w == nw:
        return nw, nh
    return w, max(int(round(nh * w / nw)), 1)


def _nearest_index(n_src: int, n_dst: int) -> np.ndarray:
    """Source index of each destination pixel centre (pixel-centre convention, no half-pixel drift)."""
    return np.minimum(((np.arange(n_dst) + 0.5) * (n_src / n_dst)).astype(np.intp), n_src - 1)


def _nearest(a: np.ndarray, w: int, h: int) -> np.ndarray:
    if a.shape[0] == h and a.shape[1] == w:
        return a
    return a[_nearest_index(a.shape[0], h)][:, _nearest_index(a.shape[1], w)]


def resample_z(z: np.ndarray, w: int, h: int) -> np.ndarray:
    """Depth to (h, w) by nearest neighbour, float32 (H, W). A (H, W, 1) input is squeezed."""
    zz = np.asarray(z)
    if zz.ndim == 3 and zz.shape[2] == 1:
        zz = zz[..., 0]
    if zz.ndim != 2:
        raise ValueError(f"z must be (H, W) or (H, W, 1), got {zz.shape}")
    return np.ascontiguousarray(_nearest(zz, w, h)).astype(np.float32, copy=False)


def resample_ids(ids: np.ndarray, w: int, h: int) -> np.ndarray:
    """Rank-0 ids to (h, w) by nearest neighbour, uint32 (H, W). A (H, W, levels) ranks array uses rank 0."""
    ii = np.asarray(ids)
    if ii.ndim == 3:
        ii = ii[..., 0]
    if ii.ndim != 2:
        raise ValueError(f"ids must be (H, W) or (H, W, levels), got {ii.shape}")
    return np.ascontiguousarray(_nearest(ii, w, h)).astype(np.uint32, copy=False)


def resample_mask(mask: np.ndarray, w: int, h: int) -> np.ndarray:
    """bool mask to (h, w) by nearest neighbour."""
    return np.ascontiguousarray(_nearest(np.asarray(mask).astype(bool), w, h))


def _area_or_linear(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """cv2 resize: INTER_AREA when shrinking, INTER_LINEAR when growing (cv2's AREA degenerates to nearest on zoom)."""
    if img.shape[0] == h and img.shape[1] == w:
        return img
    interp = cv2.INTER_AREA if w < img.shape[1] else cv2.INTER_LINEAR
    return cv2.resize(img, (int(w), int(h)), interpolation=interp)


def resample_normals(normals: np.ndarray, w: int, h: int) -> np.ndarray:
    """Camera-space normals to (h, w): area average when shrinking, nearest when growing (a blended normal
    across a crease is a direction no surface has; shrinking averages within surfaces, which is what we want).
    Re-normalised where the norm is above 1e-6; zero vectors (background) stay zero. float32 (H, W, 3)."""
    n = np.asarray(normals, dtype=np.float32)
    if n.ndim != 3 or n.shape[2] != 3:
        raise ValueError(f"normals must be (H, W, 3), got {n.shape}")
    if n.shape[0] != h or n.shape[1] != w:
        n = (
            cv2.resize(n, (int(w), int(h)), interpolation=cv2.INTER_AREA)
            if w < n.shape[1]
            else _nearest(n, w, h)
        )
    norm = np.linalg.norm(n, axis=-1, keepdims=True)
    return np.where(norm > 1e-6, n / np.maximum(norm, 1e-6), 0.0).astype(np.float32)


def resample_rgb(rgb: np.ndarray, w: int, h: int) -> np.ndarray:
    """An image (uint8 / uint16 / float) to (h, w): area when shrinking, linear when growing; dtype kept."""
    img = np.asarray(rgb)
    if img.ndim == 2:
        img = img[..., None]
    out = _area_or_linear(np.ascontiguousarray(img), w, h)
    if out.ndim == 2:
        out = out[..., None]
    return out


def gray_u8(rgb: np.ndarray) -> np.ndarray:
    """RGB (uint8 / uint16 / float 0..1, 1 or 3+ channels) -> uint8 gray for Canny."""
    u8 = to_uint8(np.asarray(rgb))
    if u8.ndim == 2:
        return np.ascontiguousarray(u8)
    if u8.shape[2] == 1:
        return np.ascontiguousarray(u8[..., 0])
    return cv2.cvtColor(np.ascontiguousarray(u8[..., :3]), cv2.COLOR_RGB2GRAY)


# --------------------------------------------------------------------------- legend helpers


def parse_id(hex_id: str | int) -> int:
    """'o:0b411b0a', 'm:...', '0b411b0a', '0x0b411b0a' or an int -> the uint32 id value."""
    if isinstance(hex_id, (int, np.integer)):
        return int(hex_id) & 0xFFFFFFFF
    text = str(hex_id).strip().lower()
    if ":" in text:
        text = text.split(":", 1)[1]
    if text.startswith("0x"):
        text = text[2:]
    return int(text, 16)


def _entries(legend: Any) -> list[Mapping[str, Any]]:
    if legend is None:
        return []
    if isinstance(legend, Mapping) and "entries" in legend:
        return list(legend["entries"])
    if isinstance(legend, Mapping):  # {id: class} shorthand
        return [{"id": k, "class": v, "kind": "object"} for k, v in legend.items()]
    return list(legend)


def legend_classes(legend: Any, kind: str = "object") -> dict[int, str]:
    """{uint32 id: class} for the legend entries of one kind (id_legend.json dict, its entries, or {id: class})."""
    out: dict[int, str] = {}
    for e in _entries(legend):
        if e.get("kind", kind) != kind:
            continue
        out[parse_id(e["id"])] = str(e.get("class", "other"))
    return out


def class_map(ids: np.ndarray, legend: Any, kind: str = "object") -> tuple[np.ndarray, list[str]]:
    """Per-pixel class index (int16, -1 = unlabelled) and the class name list it indexes."""
    classes = legend_classes(legend, kind)
    names = sorted(set(classes.values()))
    index = {n: i for i, n in enumerate(names)}
    out = np.full(ids.shape, -1, dtype=np.int16)
    for idv, cls in classes.items():
        out[ids == np.uint32(idv)] = index[cls]
    return out, names


# --------------------------------------------------------------------------- intrinsics


def intrinsics(K: Any, scale: float = 1.0) -> tuple[float, float, float, float] | None:  # noqa: N803
    """(fx, fy, cx, cy) from a 3x3 matrix, a manifest intrinsics_px block, or a manifest camera block, scaled
    to the working width; None when K is None."""
    if K is None:
        return None
    if isinstance(K, Mapping):
        if "intrinsics_px" in K:
            K = K["intrinsics_px"]
        if "K" in K:
            m = np.asarray(K["K"], dtype=np.float64)
            return (
                float(m[0, 0]) * scale,
                float(m[1, 1]) * scale,
                float(m[0, 2]) * scale,
                float(m[1, 2]) * scale,
            )
        fx = float(K["fx"])
        fy = float(K.get("fy", fx))
        cx, cy = (float(v) for v in K["principal_point"])
        return fx * scale, fy * scale, cx * scale, cy * scale
    m = np.asarray(K, dtype=np.float64)
    if m.shape != (3, 3):
        raise ValueError(f"K must be 3x3, got {m.shape}")
    return float(m[0, 0]) * scale, float(m[1, 1]) * scale, float(m[0, 2]) * scale, float(m[1, 2]) * scale


# --------------------------------------------------------------------------- thinning


def thin(mask: np.ndarray, max_iter: int = 64) -> np.ndarray:
    """Zhang-Suen thinning of a bool edge map to 1 px (8-connected). End points and isolated pixels survive."""
    img = np.asarray(mask).astype(np.uint8)
    if not img.any():
        return img.astype(bool)
    for _ in range(max_iter):
        changed = False
        for step in (0, 1):
            p = np.pad(img, 1)
            p2 = p[:-2, 1:-1]
            p3 = p[:-2, 2:]
            p4 = p[1:-1, 2:]
            p5 = p[2:, 2:]
            p6 = p[2:, 1:-1]
            p7 = p[2:, :-2]
            p8 = p[1:-1, :-2]
            p9 = p[:-2, :-2]
            ring = (p2, p3, p4, p5, p6, p7, p8, p9, p2)
            b = sum(r.astype(np.int16) for r in ring[:8])
            a = sum(((ring[k] == 0) & (ring[k + 1] == 1)).astype(np.int16) for k in range(8))
            if step == 0:
                m = ((p2 & p4 & p6) == 0) & ((p4 & p6 & p8) == 0)
            else:
                m = ((p2 & p4 & p8) == 0) & ((p2 & p6 & p8) == 0)
            rem = (img == 1) & (b >= 2) & (b <= 6) & (a == 1) & m
            if rem.any():
                img[rem] = 0
                changed = True
        if not changed:
            break
    return img.astype(bool)


# --------------------------------------------------------------------------- structural edges


def _valid_depth(z: np.ndarray, background_value: float) -> np.ndarray:
    return np.isfinite(z) & (z > 0) & (z < background_value)


def _pair_edges(
    z: np.ndarray,
    normals: np.ndarray | None,
    ids: np.ndarray,
    rays: np.ndarray | None,
    p: EdgeParams,
    axis: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Edge tests for the 4-neighbour pairs along one axis (1 = right neighbour, 0 = lower neighbour).

    Returns (depth, normal, id, depth_strength, normal_strength), each (H, W) with the last row/column of the
    axis False / 0, the value sitting on p (the first pixel of the pair)."""
    h, w = z.shape
    if axis == 1:
        sp, sq = (slice(None), slice(0, w - 1)), (slice(None), slice(1, w))
    else:
        sp, sq = (slice(0, h - 1), slice(None)), (slice(1, h), slice(None))
    zp, zq = z[sp].astype(np.float64), z[sq].astype(np.float64)
    valid = _valid_depth(zp, p.background_value) & _valid_depth(zq, p.background_value)
    zmin = np.minimum(np.where(valid, zp, 1.0), np.where(valid, zq, 1.0))
    raw = np.where(valid, np.abs(zp - zq) / zmin, 0.0)
    depth = valid & (raw > p.depth_rel)
    d_strength = np.clip(raw / (10.0 * p.depth_rel), 0.0, 1.0)

    normal = np.zeros(zp.shape, dtype=bool)
    n_strength = np.zeros(zp.shape, dtype=np.float64)
    if normals is not None:
        npv, nqv = normals[sp].astype(np.float64), normals[sq].astype(np.float64)
        ok = valid & (np.linalg.norm(npv, axis=-1) > 0.5) & (np.linalg.norm(nqv, axis=-1) > 0.5)
        cosang = np.clip(np.sum(npv * nqv, axis=-1), -1.0, 1.0)
        ang = np.degrees(np.arccos(cosang))
        normal = ok & (ang > p.normal_deg)
        n_strength = np.where(ok, np.clip(ang / 90.0, 0.0, 1.0), 0.0)
        if p.plane_test and rays is not None:
            # predict each side's depth from the other side's tangent plane; keep the depth edge only when
            # neither plane explains the step (a grazing plane explains it; an occlusion boundary does not)
            rp, rq = rays[sp], rays[sq]
            dp_p, dp_q = np.sum(npv * rp, axis=-1), np.sum(npv * rq, axis=-1)
            dq_q, dq_p = np.sum(nqv * rq, axis=-1), np.sum(nqv * rp, axis=-1)
            eps = 1e-3
            use_p = ok & (np.abs(dp_p) > eps) & (np.abs(dp_q) > eps)
            use_q = ok & (np.abs(dq_q) > eps) & (np.abs(dq_p) > eps)
            pred_q = np.where(use_p, zp * dp_p / np.where(use_p, dp_q, 1.0), np.nan)
            pred_p = np.where(use_q, zq * dq_q / np.where(use_q, dq_p, 1.0), np.nan)
            res_q = np.where(use_p, np.abs(zq - pred_q) / zmin, np.inf)
            res_p = np.where(use_q, np.abs(zp - pred_p) / zmin, np.inf)
            residual = np.minimum(res_q, res_p)  # inf where no plane is usable -> raw rule decides
            depth &= residual > p.depth_rel

    idp, idq = ids[sp], ids[sq]
    ident = idp != idq

    def full(a: np.ndarray, fill: Any) -> np.ndarray:
        out = np.full((h, w), fill, dtype=a.dtype)
        out[sp] = a
        return out

    return (
        full(depth, False),
        full(normal, False),
        full(ident, False),
        full(d_strength, 0.0),
        full(n_strength, 0.0),
    )


def structural_edges(
    z: np.ndarray,
    normals_cam: np.ndarray | None,
    ids: np.ndarray | None,
    K: Any = None,  # noqa: N803
    params: EdgeParams | None = None,
) -> EdgeMap:
    """Reference edges of a Tier A packet from its depth, camera-space normals and rank-0 ids (module doc).

    Inputs are at native size; the result is at the working size (params.width, never upsampled by default).
    `normals_cam` or `ids` may be None (that kind is then absent). `K` is the 3x3 intrinsics, the manifest
    `intrinsics_px` block, or the whole camera block; it drives the tangent-plane depth test and is optional.
    """
    p = params or EdgeParams()
    zz = np.asarray(z)
    if zz.ndim == 3 and zz.shape[2] == 1:
        zz = zz[..., 0]
    if zz.ndim != 2:
        raise ValueError(f"z must be (H, W) or (H, W, 1), got {zz.shape}")
    nh, nw = zz.shape
    w, h = working_size((nh, nw), p.width, allow_upsample=p.allow_upsample)
    scale = w / nw
    zw = resample_z(zz, w, h)
    nw_ = resample_normals(normals_cam, w, h) if normals_cam is not None else None
    if ids is None:
        idw = np.zeros((h, w), dtype=np.uint32)
    else:
        idw = resample_ids(ids, w, h)
        if idw.shape != (h, w):
            raise ValueError(f"ids {np.asarray(ids).shape[:2]} and z {zz.shape} disagree in aspect")
    if nw_ is not None and nw_.shape[:2] != (h, w):
        raise ValueError(f"normals {np.asarray(normals_cam).shape[:2]} and z {zz.shape} disagree in aspect")

    intr = intrinsics(K, scale)
    rays = None
    if intr is not None and nw_ is not None and p.plane_test:
        fx, fy, cx, cy = intr
        u = (np.arange(w, dtype=np.float64) + 0.5 - cx) / fx
        v = -(np.arange(h, dtype=np.float64) + 0.5 - cy) / fy  # plate-cam-v0: +Y up, camera looks down -Z
        rays = np.empty((h, w, 3), dtype=np.float64)
        rays[..., 0] = u[None, :]
        rays[..., 1] = v[:, None]
        rays[..., 2] = -1.0

    depth = np.zeros((h, w), dtype=bool)
    normal = np.zeros((h, w), dtype=bool)
    ident = np.zeros((h, w), dtype=bool)
    d_str = np.zeros((h, w), dtype=np.float64)
    n_str = np.zeros((h, w), dtype=np.float64)
    for axis in (1, 0):
        d, n, i, ds, ns = _pair_edges(zw, nw_, idw, rays, p, axis)
        depth |= d
        normal |= n
        ident |= i
        d_str = np.maximum(d_str, ds)
        n_str = np.maximum(n_str, ns)
    if ids is None:
        ident[:] = False
    if p.thin:
        depth, normal, ident = thin(depth), thin(normal), thin(ident)

    kind = np.zeros((h, w), dtype=np.uint8)
    kind[depth] |= KIND_DEPTH
    kind[normal] |= KIND_NORMAL
    kind[ident] |= KIND_ID
    edges = kind != 0
    strength = np.zeros((h, w), dtype=np.float32)
    strength[depth] = np.maximum(strength[depth], d_str[depth])
    strength[normal] = np.maximum(strength[normal], n_str[normal])
    strength[ident] = 1.0
    record = {
        "width": w,
        "height": h,
        "width_native": int(nw),
        "height_native": int(nh),
        "scale": float(scale),
        "depth_rel": p.depth_rel,
        "normal_deg": p.normal_deg,
        "background_value": p.background_value,
        "plane_test": bool(rays is not None),
        "thin": p.thin,
        "has_normals": nw_ is not None,
        "has_ids": ids is not None,
        "resample": {"z": "nearest", "ids": "nearest", "normals": "area", "images": "area"},
        "kind_bits": dict(KIND_NAMES),
    }
    return EdgeMap(edges=edges, kind=kind, strength=strength, params=record)


# --------------------------------------------------------------------------- image edges (the test side)

CANNY_SIGMA = 1.2
CANNY_LO = 20
CANNY_HI = 50
DETECTORS = ("rgb", "fidelity_bench")


def test_edges(
    rgb: np.ndarray,
    detector: str = "rgb",
    *,
    sigma: float = CANNY_SIGMA,
    lo: float = CANNY_LO,
    hi: float = CANNY_HI,
) -> np.ndarray:
    """Image edges on the test side of the gate, bool (H, W).

    "rgb" (default): Gaussian blur sigma 1.2, then Canny on all three channels (the gradient is the largest
    over R, G, B) with FIXED hysteresis thresholds 20 / 50 on the L1 Sobel magnitude, i.e. a step of about
    12 levels after the blur. Fixed thresholds are deliberate: measured on fixtures/packets/room_a, grain
    (sigma 8) does not inflate recall (0.890 -> 0.900) while a sigma 3 blur is caught (0.656), whereas every
    per-image rule tried either inflates on grain (precision 0.2) or misses the blur. Colour matters: a
    window painted on a wall can be a chroma edge with under 10 levels of luma contrast.
    "fidelity_bench": structure.canny on gray with the median-adaptive thresholds of the original bench,
    kept for comparison with the historical numbers (it under-detects on renders: 0.53 self-recall).
    """
    if detector not in DETECTORS:
        raise ValueError(f"detector must be one of {DETECTORS}, not {detector!r}")
    if detector == "fidelity_bench":
        return structure.canny(gray_u8(rgb), sigma)
    u8 = to_uint8(np.asarray(rgb))
    if u8.ndim == 2:
        u8 = np.stack([u8] * 3, axis=-1)
    elif u8.shape[2] == 1:
        u8 = np.repeat(u8, 3, axis=2)
    u8 = np.ascontiguousarray(u8[..., :3])
    g = cv2.GaussianBlur(u8, (0, 0), float(sigma))
    return cv2.Canny(g, float(lo), float(hi)) > 0


def _backend_name(detector: str) -> str:
    if detector == "fidelity_bench":
        return "canny-gray(fidelity_bench median-adaptive, sigma 1.2) vs G-buffer edges"
    return f"canny-rgb(sigma {CANNY_SIGMA}, lo {CANNY_LO}, hi {CANNY_HI}) vs G-buffer edges"


# --------------------------------------------------------------------------- recall


def _ellipse(r: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * int(r) + 1, 2 * int(r) + 1))


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    if r <= 0:
        return mask.astype(bool)
    return cv2.dilate(mask.astype(np.uint8), _ellipse(r)) > 0


def _wrecall(sel: np.ndarray, hit: np.ndarray, w: np.ndarray) -> float | None:
    """Weighted share of `sel` pixels that are hit; None when sel carries no weight."""
    denom = float(w[sel].sum())
    if denom <= 0.0:
        return None
    return float(w[sel & hit].sum() / denom)


def edge_recall(
    ref_edges: EdgeMap,
    test_rgb: np.ndarray,
    weights: np.ndarray | None = None,
    r: int = 2,
    *,
    plate_rgb: np.ndarray | None = None,
    ids: np.ndarray | None = None,
    legend: Any = None,
    shift_rng: int = 8,
    detector: str = "rgb",
) -> dict[str, Any]:
    """How much of the G-buffer edge map the test image keeps.

    test_edges() on the test brought to the EdgeMap's size, dilated by `r` (ellipse (2r+1)^2);
    recall_r2 = sum(w . [ref & dil(test)]) / sum(w) over reference edge pixels, recall_r0 the same at r = 0,
    precision_r2 the (unweighted) share of test edge pixels within r of a reference edge.

    w is the product of two optional terms. `weights` is the circle-of-confusion weight (coc.weights, 1 where
    absent). `plate_rgb` adds visibility: a reference edge the plate itself does not show (same detector,
    within r) gets weight 0. A frameless glass pane against the sky, two coplanar walls of one material in
    shadow: geometry the render cannot show is not demanded of the output, and the plate scores 1.0 against its
    own G-buffer by construction. The report keeps the unfiltered numbers too: `recall_r2_all` (CoC only),
    `recall_r2_unweighted` (nothing) and `plate_visibility` (the weighted share of reference edges the plate
    shows; None without a plate).

    `recall_by_kind` splits by depth / normal / id; `recall_by_class` (needs `ids` + `legend`) attributes an
    edge pixel to every legend class touching its 3x3 neighbourhood, so the wall/shrub boundary counts for both
    architecture and vegetation. `best_shift` is structure.best_shift on the visible reference edges
    (shift_rng = 0 skips it). Weights at another size are nearest-resampled; a missing kind or class reports
    None rather than 0.
    """
    er = ref_edges.edges
    h, w = er.shape
    rgb = resample_rgb(test_rgb, w, h)
    et = test_edges(rgb, detector)
    r = int(r)
    dt = _dilate(et, r)
    dr = _dilate(er, r)
    if weights is None:
        wt = np.ones((h, w), dtype=np.float32)
    else:
        wt = np.asarray(weights, dtype=np.float32)
        if wt.ndim == 3:
            wt = wt[..., 0]
        if wt.shape != (h, w):
            wt = _nearest(wt, w, h)
        wt = np.clip(wt, 0.0, None)
    coc_weighted = bool((wt[er] < 1.0).any())
    if plate_rgb is None:
        vis = np.ones((h, w), dtype=bool)
    else:
        vis = _dilate(test_edges(resample_rgb(plate_rgb, w, h), detector), r)
    w_eff = wt * vis
    ones = np.ones((h, w), dtype=np.float32)
    n_ref = int(er.sum())
    n_test = int(et.sum())
    hit_r = er & dt
    hit_0 = er & et
    out: dict[str, Any] = {
        "recall_r2": _wrecall(er, hit_r, w_eff) if n_ref else 1.0,
        "recall_r0": _wrecall(er, hit_0, w_eff) if n_ref else 1.0,
        "recall_r2_all": _wrecall(er, hit_r, wt) if n_ref else 1.0,
        "recall_r2_unweighted": _wrecall(er, hit_r, ones) if n_ref else 1.0,
        "precision_r2": float((et & dr).sum() / n_test) if n_test else None,
        "weighted": bool(coc_weighted or plate_rgb is not None),
        "coc_weighted": coc_weighted,
        "visibility_weighted": plate_rgb is not None,
        "plate_visibility": (_wrecall(er, vis, wt) if n_ref else 1.0) if plate_rgb is not None else None,
        "n_ref": n_ref,
        "n_ref_weighted": float(w_eff[er].sum()),
        "n_ref_visible": int((er & vis).sum()) if plate_rgb is not None else n_ref,
        "n_test": n_test,
        "backend": _backend_name(detector),
        "detector": detector,
        "canny": {"sigma": CANNY_SIGMA, "lo": CANNY_LO, "hi": CANNY_HI}
        if detector == "rgb"
        else {"sigma": CANNY_SIGMA},
        "r": r,
        "width": int(w),
        "height": int(h),
        "metric_id": METRIC_ID,
    }
    out["recall_by_kind"] = {
        name: _wrecall(ref_edges.by_kind(bit), hit_r, w_eff) for name, bit in KIND_NAMES.items()
    }
    by_class: dict[str, float | None] = {}
    if ids is not None and legend is not None:
        idw = resample_ids(ids, w, h)
        cmap, names = class_map(idw, legend)
        cross = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
        for i, name in enumerate(names):
            touch = cv2.dilate((cmap == i).astype(np.uint8), cross) > 0
            by_class[name] = _wrecall(er & touch, hit_r, w_eff)
    out["recall_by_class"] = by_class
    er_vis = er & vis
    if shift_rng and er_vis.any() and n_test:
        out["best_shift"] = structure.best_shift(er_vis, et, rng=int(shift_rng), r=r)
    else:
        out["best_shift"] = None
    return out


def verdict(result: Mapping[str, Any], variant: str = "delivery") -> tuple[str, float]:
    """('pass' | 'fail' | 'advisory', bound) of an edge_recall result against thresholds.yaml
    gbuffer_edge_recall_r2 (variants delivery 0.90 / preview 0.85)."""
    value = result.get("recall_r2")
    if value is None:
        return "advisory", float("nan")
    status, thr = _check_threshold(METRIC_ID, float(value), variant=variant)
    return status, float(thr.bound(variant))


# --------------------------------------------------------------------------- packet convenience


def from_packet(
    packet_dir: str | Path,
    params: EdgeParams | None = None,
    *,
    manifest: Mapping[str, Any] | None = None,
) -> tuple[EdgeMap, dict[str, Any]]:
    """Reference edges straight from a Tier A packet folder (manifest.json + beauty.exr).

    Returns (EdgeMap, ctx) with ctx = {manifest, legend, z, normals, ids (rank 0), ranks, coverage, camera}
    at native size so the caller can derive weights (coc.weights on resample_z(z, map.width, map.height)) and
    class recalls. The EXR reader is imported lazily (plate_pixels pulls in OpenEXR).
    """
    import json

    from plate_pixels.exr import read_cryptomatte, read_elements  # gates stay import-light

    root = Path(packet_dir)
    if manifest is None:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("tier") != "A":
        raise ValueError(f"packet {root.name} is tier {manifest.get('tier')!r}: G-buffer edges need tier A")
    exr = root / manifest["files"]["beauty_exr"]["path"]
    elements = {e["element"]: e for e in manifest["files"]["beauty_exr"]["elements"]}
    want = ["zdepth"] + (["normals"] if "normals" in elements else [])
    el = read_elements(exr, manifest, want)
    z = el["zdepth"][..., 0]
    normals = el.get("normals")
    ranks, coverage = read_cryptomatte(exr, manifest, "object")
    ids = ranks[..., 0]
    legend = None
    legend_ref = manifest.get("files", {}).get("id_legend")
    if legend_ref:
        lp = root / legend_ref["path"]
        if lp.is_file():
            legend = json.loads(lp.read_text(encoding="utf-8"))
    p = params or EdgeParams()
    bg = elements["zdepth"].get("background_value")
    if bg is not None and p.background_value == BACKGROUND_VALUE and float(bg) != BACKGROUND_VALUE:
        p = EdgeParams(**{**p.__dict__, "background_value": float(bg)})
    emap = structural_edges(z, normals, ids, manifest["camera"], p)
    ctx = {
        "manifest": manifest,
        "legend": legend,
        "z": z,
        "normals": normals,
        "ids": ids,
        "ranks": ranks,
        "coverage": coverage,
        "camera": manifest["camera"],
        "units": manifest.get("units", {}),
    }
    return emap, ctx
