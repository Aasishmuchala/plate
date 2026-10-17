"""Render-it router math (BUILD_PLAN decision 15, router plan T28): the pre-spend numbers that say
"render it in V-Ray" before a generator is asked to invent geometry.

Three pure functions over packet geometry, no images:

- :func:`detail_resolvability` -- expected detail size in pixels, ``D(p) = fx * s / Z(p)`` with ``s`` the
  class's characteristic detail size (leaf 0.06 m, brick 0.065 m, sett 0.10 m, fabric 0.02 m) in scene
  units. Median over the pixels of the textured classes. ``D < 8`` px: V-Ray only (AI for people and patches by
  region); 8 to 24 px: AI by region; ``>= 24`` px: a whole-frame re-photograph may be allowed.
- :func:`disocclusion_budget` -- the fraction of end-frame pixels the start frame never saw (outside its frame,
  behind it, or hidden behind nearer geometry), from the two Z buffers and the two camera poses, plus the orbit
  angle. ``<= 2 %``: AI may interpolate; 2 to 10 %: only with rendered start and end frames plus a clay motion
  reference; ``> 10 %`` or orbit over 30 degrees: render the move.
- :func:`route_move` -- the two numbers plus the packet's path crossings folded into one
  :class:`RouterVerdict` whose ``mode`` is advisory (P1) or blocking (P3).

Everything is in the ``plate-cam-v0`` convention (PACKET.md 3.3): ``world_from_camera`` 4x4 row-major with
column vectors, the camera looks down its local -Z with +Y up, planar depth along the view axis, pixel centres
at +0.5 from the top-left corner. ``plate_spec.project_point`` is the scalar reference of the projection used
vectorised here. Tier C (relative depth) cannot feed either function: callers report ``unknown`` and a human
decides (PACKET.md section 11).

The thresholds live here as constants with their provenance rather than in ``thresholds.yaml`` so this module
stays importable without the YAML loader; registering them under a new threshold-set id is a one-line change
when the P3 calibration fixes the numbers.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, computed_field

__all__ = [
    "DETAIL_M",
    "GROUND_SETT_M",
    "TEXTURED_CLASSES",
    "D_VRAY_ONLY_PX",
    "D_WHOLE_FRAME_PX",
    "DISOCC_AI_MAX",
    "DISOCC_CLAY_MAX",
    "ORBIT_MAX_DEG",
    "OCCLUSION_REL_TOL",
    "OCCLUSION_ABS_TOL",
    "DetailVerdict",
    "MoveVerdict",
    "RouterMode",
    "RouterVerdict",
    "detail_size_m",
    "detail_resolvability",
    "camera_forward",
    "orbit_angle_deg",
    "disocclusion_budget",
    "router_mode",
    "route_move",
    "load_motion",
]

# --------------------------------------------------------------------------- thresholds (decision 15)

# Characteristic detail size per textured class, metres. BUILD_PLAN decision 15: leaf 0.06, brick 0.065,
# sett 0.10; furniture fabric weave 0.02 added by the router plan (T28). A legend entry's own `detail_size_m`
# wins over the class default.
DETAIL_M: dict[str, float] = {"vegetation": 0.06, "ground": 0.065, "furniture": 0.02}
GROUND_SETT_M = 0.10  # ground whose material (or node) name contains "sett": paving setts, not brick
TEXTURED_CLASSES = frozenset(DETAIL_M)

D_VRAY_ONLY_PX = 8.0  # D below this: V-Ray only (AI for people and patches by region)
D_WHOLE_FRAME_PX = 24.0  # D at or above this: whole-frame re-photograph allowed for close/medium frames

DISOCC_AI_MAX = 0.02  # fraction <= 2 %: AI may interpolate between the endpoints
DISOCC_CLAY_MAX = 0.10  # 2 to 10 %: rendered start and end frames plus a clay motion reference
ORBIT_MAX_DEG = 30.0  # over this the move is rendered regardless of the fraction

# Occlusion test tolerance: a reprojected end pixel counts as hidden when its depth in the start camera exceeds
# the start Z sampled there by more than 1 % of that Z plus 0.5 % of its own depth (bilinear resampling and
# anti-aliased silhouettes otherwise flag the surface against itself).
OCCLUSION_REL_TOL = 0.01
OCCLUSION_ABS_TOL = 0.005

DetailVerdict = Literal["vray_only", "ai_by_region", "whole_frame_ok", "unknown"]
MoveVerdict = Literal["ai_interpolate", "rendered_endpoints_plus_clay", "render_move", "unknown"]
RouterMode = Literal["advisory", "blocking"]

ROUTER_MODE_ENV = "PLATE_ROUTER_MODE"


class RouterVerdict(BaseModel):
    """What :func:`route_move` returns. ``blocks`` is the one bit the Move policy (T43) acts on; it is a
    computed field, so a ``model_dump()`` carries it and still round-trips through ``model_validate``."""

    model_config = ConfigDict(extra="ignore")

    mode: RouterMode
    verdict: MoveVerdict
    numbers: dict[str, Any]
    explanation: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blocks(self) -> bool:
        return self.mode == "blocking" and self.verdict == "render_move"


# --------------------------------------------------------------------------- small geometry helpers


def _mat(m: Any, shape: tuple[int, int], what: str) -> np.ndarray:
    a = np.asarray(m, dtype=np.float64)
    if a.shape != shape:
        raise ValueError(f"{what}: expected a {shape[0]}x{shape[1]} matrix, got shape {a.shape}")
    return a


def _pose(cam: Any) -> np.ndarray:
    """4x4 world_from_camera from a camera block, a camera_frames entry or a bare matrix."""
    if isinstance(cam, Mapping):
        if "world_from_camera" not in cam:
            raise ValueError("camera mapping has no 'world_from_camera'")
        return _mat(cam["world_from_camera"], (4, 4), "world_from_camera")
    return _mat(cam, (4, 4), "world_from_camera")


def _intrinsics(K: Any) -> tuple[float, float, float, float]:
    """(fx, fy, cx, cy) from a 3x3 K or an ``intrinsics_px`` mapping."""
    if isinstance(K, Mapping):
        if "K" in K:
            return _intrinsics(K["K"])
        fx = float(K["fx"])
        fy = float(K.get("fy", fx))
        cx, cy = (float(v) for v in K["principal_point"])
        return fx, fy, cx, cy
    k = _mat(K, (3, 3), "K")
    return float(k[0, 0]), float(k[1, 1]), float(k[0, 2]), float(k[1, 2])


def _meters_per_unit(units: Any) -> float:
    if isinstance(units, Mapping):
        mpu = float(units["meters_per_unit"])
    else:
        mpu = float(units)
    if not mpu > 0:
        raise ValueError(f"meters_per_unit must be > 0, got {mpu}")
    return mpu


def _valid_depth(z: np.ndarray, bg_value: float | None) -> np.ndarray:
    """Finite, positive and not the background sentinel (matched with a relative tolerance: V-Ray and the
    fixtures write 1e10 as float32)."""
    ok = np.isfinite(z) & (z > 0)
    if bg_value is not None and math.isfinite(bg_value):
        ok &= ~np.isclose(z, bg_value, rtol=1e-3, atol=0.0)
        if bg_value > 0:
            ok &= z < bg_value
    return ok


def camera_forward(cam: Any) -> np.ndarray:
    """Unit forward axis in world space: the camera's local -Z (third column of R, negated)."""
    m = _pose(cam)
    f = -m[:3, 2]
    n = float(np.linalg.norm(f))
    if n == 0:
        raise ValueError("degenerate camera rotation")
    return f / n


def _angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    c = float(np.dot(a, b) / (na * nb))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def orbit_angle_deg(cam_start: Any, cam_end: Any, pivot: Sequence[float] | None = None) -> dict[str, float]:
    """Orbit angle between two poses: the angle between the forward axes plus, when a look-at pivot is given,
    the angle the two camera centres subtend at it. Returns ``{orbit_deg, forward_deg, pivot_deg}``."""
    ms, me = _pose(cam_start), _pose(cam_end)
    forward = _angle_deg(camera_forward(ms), camera_forward(me))
    pivot_deg = 0.0
    if pivot is not None:
        p = np.asarray(pivot, dtype=np.float64).reshape(3)
        pivot_deg = _angle_deg(ms[:3, 3] - p, me[:3, 3] - p)
    return {"orbit_deg": forward + pivot_deg, "forward_deg": forward, "pivot_deg": pivot_deg}


# --------------------------------------------------------------------------- detail resolvability


def _legend_entries(legend: Any) -> list[Mapping[str, Any]]:
    if isinstance(legend, Mapping):
        if "entries" in legend:
            return list(legend["entries"])
        return list(legend.values())  # {id: entry}
    return list(legend)


def _id_int(legend_id: str | int) -> int:
    if isinstance(legend_id, (int, np.integer)):
        return int(legend_id) & 0xFFFFFFFF
    text = str(legend_id).strip().lower()
    if ":" in text:
        text = text.split(":", 1)[1]
    return int(text, 16)


def detail_size_m(entry: Mapping[str, Any], detail_m: Mapping[str, float] | None = None) -> float | None:
    """Characteristic detail size in metres for one legend entry, or None when the class is not textured.

    Order: the entry's own ``detail_size_m`` > the ``sett`` rule for ground (material or node name contains
    "sett") > the class default in ``detail_m`` (merged over :data:`DETAIL_M`).
    """
    own = entry.get("detail_size_m")
    if own is not None:
        return float(own)
    table = {**DETAIL_M, **(detail_m or {})}
    cls = entry.get("class")
    if cls not in table:
        return None
    if cls == "ground":
        names = f"{entry.get('material') or ''} {entry.get('name') or ''}".lower()
        if "sett" in names:
            return GROUND_SETT_M
    return float(table[cls])


def _detail_verdict(d: float | None) -> DetailVerdict:
    if d is None or not math.isfinite(d):
        return "unknown"
    if d < D_VRAY_ONLY_PX:
        return "vray_only"
    if d < D_WHOLE_FRAME_PX:
        return "ai_by_region"
    return "whole_frame_ok"


def detail_resolvability(
    z: np.ndarray,
    ids: np.ndarray,
    legend: Any,
    K: Any,
    units: Any,
    detail_m: Mapping[str, float] | None = None,
    *,
    bg_value: float | None = None,
) -> dict[str, Any]:
    """Expected detail size in pixels over the textured classes of a still.

    ``z`` is the planar depth (H, W) in scene units, ``ids`` the rank-0 Cryptomatte object ids (H, W) uint32
    (an (H, W, levels) ranks array is accepted and rank 0 taken), ``legend`` the ``id_legend.json`` document
    (or its entries), ``K`` a 3x3 or the ``intrinsics_px`` block, ``units`` the manifest ``units`` block (or
    ``meters_per_unit``). ``detail_m`` overrides class sizes (metres).

    Returns ``{D_px_median, per_class: {class: D_median}, per_id: {legend id: D_median}, verdict, n_pixels,
    fx, meters_per_unit}``. With no textured pixel the median is None and the verdict ``unknown``.
    """
    z = np.asarray(z, dtype=np.float64)
    if z.ndim == 3 and z.shape[2] == 1:
        z = z[..., 0]
    ids = np.asarray(ids)
    if ids.ndim == 3:
        ids = ids[..., 0]
    if z.ndim != 2 or ids.shape != z.shape:
        raise ValueError(f"z {z.shape} and ids {ids.shape} must be 2-D arrays of the same shape")
    ids = ids.astype(np.uint32, copy=False)
    fx = _intrinsics(K)[0]
    mpu = _meters_per_unit(units)
    valid = _valid_depth(z, bg_value)

    by_class: dict[str, list[np.ndarray]] = {}
    per_id: dict[str, float | None] = {}
    all_d: list[np.ndarray] = []
    for entry in _legend_entries(legend):
        if entry.get("kind", "object") != "object":
            continue
        s_m = detail_size_m(entry, detail_m)
        if s_m is None:
            continue
        sel = valid & (ids == np.uint32(_id_int(entry["id"])))
        if not sel.any():
            continue
        d = fx * (s_m / mpu) / z[sel]
        per_id[str(entry["id"])] = float(np.median(d))
        by_class.setdefault(str(entry["class"]), []).append(d)
        all_d.append(d)

    per_class = {cls: float(np.median(np.concatenate(parts))) for cls, parts in sorted(by_class.items())}
    n = int(sum(d.size for d in all_d))
    median = float(np.median(np.concatenate(all_d))) if n else None
    return {
        "D_px_median": median,
        "per_class": per_class,
        "per_id": per_id,
        "verdict": _detail_verdict(median),
        "n_pixels": n,
        "fx": fx,
        "meters_per_unit": mpu,
    }


# --------------------------------------------------------------------------- disocclusion budget


def _bilinear_valid(z: np.ndarray, valid: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Bilinear sample of ``z`` at render-frame positions (u, v) (pixel centres at +0.5), weights renormalised
    over the valid neighbours; +inf where no neighbour is valid (nothing in front)."""
    h, w = z.shape
    x = u - 0.5
    y = v - 0.5
    x0 = np.floor(x)
    y0 = np.floor(y)
    fx = x - x0
    fy = y - y0
    x0 = x0.astype(np.int64)
    y0 = y0.astype(np.int64)
    acc = np.zeros(u.shape, dtype=np.float64)
    wsum = np.zeros(u.shape, dtype=np.float64)
    for dy, wy in ((0, 1.0 - fy), (1, fy)):
        yy = np.clip(y0 + dy, 0, h - 1)
        for dx, wx in ((0, 1.0 - fx), (1, fx)):
            xx = np.clip(x0 + dx, 0, w - 1)
            wgt = wx * wy * valid[yy, xx]
            acc += wgt * np.where(valid[yy, xx], z[yy, xx], 0.0)
            wsum += wgt
    out = np.full(u.shape, np.inf, dtype=np.float64)
    nz = wsum > 0
    out[nz] = acc[nz] / wsum[nz]
    return out


def _planar(z: np.ndarray, depth_kind: str, fx: float, fy: float, cx: float, cy: float) -> np.ndarray:
    """Planar depth from the stored buffer: identity for ``planar``, divide by the ray length for ``radial``."""
    if depth_kind == "planar":
        return z
    if depth_kind != "radial":
        raise ValueError(f"depth_kind must be 'planar' or 'radial', not {depth_kind!r}")
    h, w = z.shape
    u = (np.arange(w, dtype=np.float64) + 0.5 - cx) / fx
    v = (np.arange(h, dtype=np.float64) + 0.5 - cy) / fy
    ray = np.sqrt(1.0 + u[None, :] ** 2 + v[:, None] ** 2)
    return z / ray


def _move_verdict(fraction: float | None, orbit_deg: float, crossings: int = 0) -> MoveVerdict:
    if crossings > 0 or orbit_deg > ORBIT_MAX_DEG:
        return "render_move"
    if fraction is None or not math.isfinite(fraction):
        return "unknown"
    if fraction > DISOCC_CLAY_MAX:
        return "render_move"
    if fraction > DISOCC_AI_MAX:
        return "rendered_endpoints_plus_clay"
    return "ai_interpolate"


def disocclusion_budget(
    z_start: np.ndarray,
    cam_start: Any,
    z_end: np.ndarray,
    cam_end: Any,
    K: Any,
    bg_value: float | None,
    *,
    pivot: Sequence[float] | None = None,
    depth_kind: str = "planar",
    K_start: Any = None,
    rel_tol: float = OCCLUSION_REL_TOL,
    abs_tol: float = OCCLUSION_ABS_TOL,
) -> dict[str, Any]:
    """Fraction of end-frame pixels the start frame did not see, and the orbit angle.

    Every end pixel with valid depth is back-projected to world with ``cam_end`` and projected into
    ``cam_start``. It is invisible when it lands outside the start frame, behind the start camera, or is
    occluded: ``depth_in_start > Z_start(bilinear at the landing point) * (1 + rel_tol) + abs_tol *
    depth_in_start``. ``K`` is the intrinsics of the sequence frames (``K_start`` if the start frame differs);
    ``bg_value`` the depth sentinel for empty pixels (None: only non-finite / non-positive depth is empty);
    ``depth_kind`` ``planar`` or ``radial`` (PACKET.md 4); ``pivot`` an optional look-at point in world units.

    Returns ``{fraction, orbit_deg, verdict, n_valid, n_invisible, outside, behind, occluded, forward_deg,
    pivot_deg}``. ``fraction`` is None (verdict ``unknown``) when the end frame has no valid depth.
    """
    zs = np.asarray(z_start, dtype=np.float64)
    ze = np.asarray(z_end, dtype=np.float64)
    if zs.ndim == 3 and zs.shape[2] == 1:
        zs = zs[..., 0]
    if ze.ndim == 3 and ze.shape[2] == 1:
        ze = ze[..., 0]
    if zs.ndim != 2 or ze.ndim != 2:
        raise ValueError("z_start and z_end must be 2-D depth buffers")
    fx, fy, cx, cy = _intrinsics(K)
    fxs, fys, cxs, cys = _intrinsics(K_start) if K_start is not None else (fx, fy, cx, cy)
    ms, me = _pose(cam_start), _pose(cam_end)
    angles = orbit_angle_deg(ms, me, pivot)

    valid_s = _valid_depth(zs, bg_value)
    valid_e = _valid_depth(ze, bg_value)
    zs_p = np.where(valid_s, _planar(zs, depth_kind, fxs, fys, cxs, cys), np.inf)
    ze_p = _planar(ze, depth_kind, fx, fy, cx, cy)

    n_valid = int(valid_e.sum())
    base = {
        "orbit_deg": angles["orbit_deg"],
        "forward_deg": angles["forward_deg"],
        "pivot_deg": angles["pivot_deg"],
        "n_valid": n_valid,
        "n_invisible": 0,
        "outside": 0,
        "behind": 0,
        "occluded": 0,
    }
    if n_valid == 0:
        return {"fraction": None, "verdict": _move_verdict(None, angles["orbit_deg"]), **base}

    rows, cols = np.nonzero(valid_e)
    d = ze_p[rows, cols]
    u = cols.astype(np.float64) + 0.5
    v = rows.astype(np.float64) + 0.5
    # back-project in the end camera (planar depth along -Z), then to world
    pc = np.stack([(u - cx) / fx * d, -(v - cy) / fy * d, -d], axis=0)  # (3, N)
    pw = me[:3, :3] @ pc + me[:3, 3:4]
    # into the start camera: camera_from_world = [R^T | -R^T t]
    q = ms[:3, :3].T @ (pw - ms[:3, 3:4])
    depth_s = -q[2]
    behind = depth_s <= 0
    safe = np.where(behind, 1.0, depth_s)
    us = cxs + fxs * q[0] / safe
    vs = cys - fys * q[1] / safe
    hs, ws = zs.shape
    outside = ~behind & ((us < 0) | (us >= ws) | (vs < 0) | (vs >= hs))
    inside = ~behind & ~outside
    occluded = np.zeros(d.shape, dtype=bool)
    if inside.any():
        z_at = _bilinear_valid(zs_p, valid_s, us[inside], vs[inside])
        ds = depth_s[inside]
        occluded[inside] = ds > z_at * (1.0 + rel_tol) + abs_tol * ds
    invisible = behind | outside | occluded
    n_inv = int(invisible.sum())
    fraction = n_inv / n_valid
    base.update(
        n_invisible=n_inv, outside=int(outside.sum()), behind=int(behind.sum()), occluded=int(occluded.sum())
    )
    return {"fraction": fraction, "verdict": _move_verdict(fraction, angles["orbit_deg"]), **base}


# --------------------------------------------------------------------------- route_move


def router_mode(mode: str | None = None) -> RouterMode:
    """advisory (P1) or blocking (P3). Explicit argument > ``PLATE_ROUTER_MODE`` > advisory. The daemon's
    settings pass the project's mode explicitly; the environment variable is the seam for the CLI and tests."""
    value = (mode or os.environ.get(ROUTER_MODE_ENV) or "advisory").strip().lower()
    if value not in ("advisory", "blocking"):
        raise ValueError(f"router mode must be 'advisory' or 'blocking', not {value!r}")
    return value  # type: ignore[return-value]


def _crossing_list(crossings: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in crossings or ():
        if isinstance(c, Mapping):
            out.append({k: c[k] for k in ("frame", "kind", "note") if k in c})
        else:
            out.append({"kind": str(c)})
    return out


def route_move(
    packet_motion: Mapping[str, Any],
    z_seq_first: np.ndarray,
    z_seq_last: np.ndarray,
    crossings: Sequence[Any] | None = None,
    *,
    mode: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> RouterVerdict:
    """Fold the disocclusion budget, the orbit angle and the path crossings into one verdict.

    ``packet_motion`` carries the move geometry: ``cam_start`` and ``cam_end`` (``world_from_camera`` 4x4 or a
    camera block), ``K`` (sequence-frame intrinsics), ``bg_value``; optional ``pivot``, ``depth_kind``,
    ``crossings`` (the manifest's ``motion.path_crossings``; the ``crossings`` argument is appended) and
    ``packet_id``. ``detail`` is an optional :func:`detail_resolvability` result of the hero still, reported
    in ``numbers`` for the verdict card (it does not change the move verdict). Use :func:`load_motion` to read
    all of it from a motion packet folder.

    Any crossing of kind ``threshold`` (or any other kind: a room change or an occluder is a cut for the
    generator too) makes the verdict ``render_move``.
    """
    for key in ("cam_start", "cam_end", "K"):
        if key not in packet_motion:
            raise ValueError(f"packet_motion is missing {key!r}")
    all_crossings = _crossing_list(packet_motion.get("crossings")) + _crossing_list(crossings)
    budget = disocclusion_budget(
        z_seq_first,
        packet_motion["cam_start"],
        z_seq_last,
        packet_motion["cam_end"],
        packet_motion["K"],
        packet_motion.get("bg_value"),
        pivot=packet_motion.get("pivot"),
        depth_kind=str(packet_motion.get("depth_kind") or "planar"),
    )
    verdict = _move_verdict(budget["fraction"], budget["orbit_deg"], len(all_crossings))
    numbers: dict[str, Any] = {
        "disocclusion_fraction": budget["fraction"],
        "orbit_deg": budget["orbit_deg"],
        "forward_deg": budget["forward_deg"],
        "pivot_deg": budget["pivot_deg"],
        "n_valid": budget["n_valid"],
        "n_invisible": budget["n_invisible"],
        "outside": budget["outside"],
        "behind": budget["behind"],
        "occluded": budget["occluded"],
        "crossings": all_crossings,
        "thresholds": {
            "disocclusion_ai_max": DISOCC_AI_MAX,
            "disocclusion_clay_max": DISOCC_CLAY_MAX,
            "orbit_max_deg": ORBIT_MAX_DEG,
        },
    }
    if packet_motion.get("packet_id"):
        numbers["packet_id"] = packet_motion["packet_id"]
    if detail is not None:
        numbers["D_px_median"] = detail.get("D_px_median")
        numbers["detail_verdict"] = detail.get("verdict")
        numbers["detail_per_class"] = dict(detail.get("per_class") or {})

    frac = budget["fraction"]
    frac_text = (
        "no valid depth in the end frame"
        if frac is None
        else f"{100.0 * frac:.1f} % of end-frame pixels unseen by the start frame"
    )
    reasons: list[str] = []
    if all_crossings:
        kinds = ", ".join(sorted({c.get("kind", "?") for c in all_crossings}))
        reasons.append(f"{len(all_crossings)} path crossing(s) ({kinds})")
    if budget["orbit_deg"] > ORBIT_MAX_DEG:
        reasons.append(f"orbit {budget['orbit_deg']:.1f} deg > {ORBIT_MAX_DEG:g}")
    if frac is not None and frac > DISOCC_CLAY_MAX:
        reasons.append(f"disocclusion {100.0 * frac:.1f} % > {100.0 * DISOCC_CLAY_MAX:g} %")
    if verdict == "render_move":
        action = "render the move in Vantage/V-Ray; AI for people only"
    elif verdict == "rendered_endpoints_plus_clay":
        action = "AI may move only between rendered start and end frames with the clay motion reference"
    elif verdict == "ai_interpolate":
        action = "AI may interpolate between the endpoints"
    else:
        action = "cannot measure; a human decides"
    why = f" because {'; '.join(reasons)}" if reasons else ""
    explanation = f"{frac_text}, orbit {budget['orbit_deg']:.1f} deg: {action}{why} ({router_mode(mode)})."
    return RouterVerdict(mode=router_mode(mode), verdict=verdict, numbers=numbers, explanation=explanation)


# --------------------------------------------------------------------------- packet loader


def load_motion(packet_dir: str | Path) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    """Read a motion packet folder into ``(packet_motion, z_first, z_last)`` for :func:`route_move`.

    Cameras come from ``camera_frames.json`` (first and last frame), ``K`` from the manifest scaled to the
    sequence resolution (PACKET.md 4: ``s = w_seq / W`` on both axes), ``bg_value`` and ``depth_kind`` from the
    beauty EXR's ``zdepth`` element, crossings from ``motion.path_crossings``. The Z frames are read through
    ``plate_pixels.exr.read_elements`` with the same manifest (the sequence files use the beauty layer names).
    """
    from plate_pixels.exr import read_elements  # gates stay import-light; pixels pulls in OpenEXR

    root = Path(packet_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("kind") != "motion":
        raise ValueError(f"{root}: not a motion packet (kind={manifest.get('kind')!r})")
    frames_name = manifest["camera"].get("frames_file", "camera_frames")
    frames_path = root / manifest["files"][frames_name]["path"]
    frames_doc = json.loads(frames_path.read_text(encoding="utf-8"))
    frames = sorted(frames_doc["frames"], key=lambda f: int(f["frame"]))
    if not frames:
        raise ValueError(f"{frames_path}: no frames")
    z_seq = manifest["files"]["z_seq"]
    first, last = int(z_seq["first"]), int(z_seq["last"])
    pattern = z_seq["pattern"]
    z_first = read_elements(root / (pattern % first), manifest, ["zdepth"])["zdepth"][..., 0]
    z_last = read_elements(root / (pattern % last), manifest, ["zdepth"])["zdepth"][..., 0]

    cam = manifest["camera"]
    seq = manifest["motion"]["sequence_resolution"]
    s = float(seq["width"]) / float(cam["resolution"]["width"])
    k = np.asarray(cam["intrinsics_px"]["K"], dtype=np.float64)
    k_seq = k.copy()
    k_seq[0, :] *= s
    k_seq[1, :] *= s
    zd = next((e for e in manifest["files"]["beauty_exr"]["elements"] if e["element"] == "zdepth"), {})
    by_frame = {int(f["frame"]): f for f in frames}
    cam_start = by_frame.get(first, frames[0])
    cam_end = by_frame.get(last, frames[-1])
    packet_motion: dict[str, Any] = {
        "packet_id": manifest.get("packet_id"),
        "cam_start": cam_start["world_from_camera"],
        "cam_end": cam_end["world_from_camera"],
        "K": k_seq.tolist(),
        "bg_value": zd.get("background_value"),
        "depth_kind": zd.get("depth_kind", "planar"),
        "crossings": list(manifest["motion"].get("path_crossings") or []),
        "frames": (first, last),
        "fps": manifest["camera"]["frames"].get("fps"),
    }
    return packet_motion, z_first, z_last
