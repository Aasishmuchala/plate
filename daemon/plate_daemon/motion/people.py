"""The People node: a person from STAFFAGE, placed in an approved still and held behind the packet's geometry.

    run(project, node, packet, approved_still, *, client=None, stage_dir=None, ...) -> PeopleResult

The flow, in the order money is at stake:

1. Custody first. STAFFAGE is a custody D hop (its generator calls leave through Atlas Cloud), so the run must
   allow D: an NDA project, or any project whose grades (project custody intersected with the packet's
   `policy.custody`) exclude D, is refused with `CustodyViolation` before a file is written, unless STAFFAGE is
   configured `local_only` (`staffage_client.StaffageSettings`, an operator assertion that its generator runs on
   this box; the route is then C0). Nothing is sent on a refused run.
2. Everything that can be wrong is checked before STAFFAGE is called: the node's parameters, the doctrine
   (`retake` invents a camera view and is refused here), the packet's Z, and the person's depth.
3. The approved still goes out as the egress transform makes it (`plate_pixels.egress.prepare`: 8-bit sRGB PNG,
   every metadata chunk stripped, never upsampled) under an opaque ULID file name in `stage_dir`, which must be
   under the user's home because STAFFAGE reads by path. STAFFAGE gets the file, a film, a shot and the job; the
   job is polled by id until it is done (never resubmitted).
4. STAFFAGE returns `platePath` (the scene with the person in it) and `mattePath` (the person's alpha), as
   stills or as video (a frame is taken). The plate is restored onto the still's grid and registered on the
   ring outside the person (`plate_pixels.ingress.restore`), tone-matched on that ring, and the matte follows
   the same size map and warp.
5. Occlusion by the packet's Z. The person is a billboard at `person_depth` metres from the camera (the node's
   `personDepthM`, else the ground point back-projected onto the ground plane fitted from Z, else Z at the foot):
   a pixel the packet puts nearer than `person_depth - tol` hides the person (alpha 0), a farther one shows
   her. The shadow STAFFAGE drew on the ground is kept as a darkening of the still, and is hidden only where
   something is nearer than the ground itself.
6. The composite lands only where the person or her shadow is: every other pixel is the approved still, copied
   by slicing (outside-mask dE00 = 0 by construction). The gate mask is `editable_ids ∪ people`: the people
   footprint grown by the gate's 12 px collar, so a halo around the person fails the feather-band check, and
   the report is `gating.run_gates` as for any still (registration measured on the ring outside the person,
   outside-mask enforced).

Z units: the packet's `units.meters_per_unit` turns Z into metres; a radial `depth_kind` is converted to planar;
background (non-finite, non-positive, or at the manifest's background value) occludes nothing. A Tier C packet
has no Z: pass `depth_m` (metres, plate-sized or resampled) or the run is refused, because an unoccluded person
over a plate with foreground is the failure this node exists to prevent.

The matte, the person plate and the sent still go into the project's CAS; the caller owns the ledger, the run
state and the quote (a STAFFAGE take costs about USD 0.10 per second of shot).

`run` is `plan_take` (steps 1 and 2, and the egress PNG of step 3: nothing leaves) then `start_take` (the staging and
the STAFFAGE job, polled to its end) then the still composite. People in motion (`plate_daemon.people_video`, T105)
uses the first two as they are and reads the whole take (`read_layer_frames`) instead of one frame of it.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from plate_gates.composite import match_to_ring
from plate_gates.iddiff import BAND_PX
from plate_pixels import egress as pixels_egress
from plate_pixels import ingress
from plate_pixels.icc import strip_metadata_png
from plate_pixels.register import warp_to_plate

from ..config import app_home
from ..custody import CustodyViolation, require_route
from ..egress import EgressRun, LeakBlocked, new_opaque_id
from ..gating import run_gates
from ..outbound_log import OutboundLog
from .staffage_client import (
    PERSON_MODES,
    ROUTE_ID,
    StaffageClient,
    StaffageSettings,
    build_person_prompt,
    leak_hit,
)

__all__ = [
    "BG_Z",
    "CustodyViolation",
    "GroundPlane",
    "PeopleError",
    "PeopleParams",
    "PeopleResult",
    "Placed",
    "TakePlan",
    "accept_take",
    "composite_person",
    "fit_ground_plane",
    "layer_fps",
    "occluder_mask",
    "occlusion_weight",
    "parse_params",
    "plan_take",
    "read_layer_frames",
    "resolve_person_depth",
    "run",
    "shadow_layer",
    "start_take",
]

BG_Z = 1e9  # packet Z at or above this (V-Ray's 1e10 background) is "no geometry"
OCC_TOL_MIN_M = 0.05  # a person is not a plane: ignore geometry within 5 cm or 2% of her depth
OCC_TOL_REL = 0.02
FOOT_WINDOW = 3  # half window of the median Z read at the foot pixel
GROUND_AGREE_REL = 0.05  # the ground plane and Z at the foot agree within 5%
GROUND_MIN_PX = 150
GROUND_MAX_HORIZON_DEG = 10.0
GROUND_SAMPLE_PX = 600_000
PERSON_HEIGHT_M = 1.7  # for the framing sentence only
MIN_DEPTH_M, MAX_DEPTH_M = 0.2, 500.0
SHADOW_MIN_DARK = 0.04
SHADOW_CAP = 0.85
MATTE_EPS = 0.004
ANY_SIZE = {"sizes": {"auto": True}}  # STAFFAGE takes and returns any size: the still goes at native size
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
DEPTH_SOURCES = ("node", "ground_plane", "z_at_foot")
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)


class PeopleError(ValueError):
    """The People node cannot run as asked. `code` is machine-readable, `status` the HTTP status a route uses."""

    def __init__(self, message: str, *, code: str = "people", status: int = 422, **extra: Any):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


# ---------------------------------------------------------------------------- node parameters


def _node_params(node: Any) -> dict[str, Any]:
    if not isinstance(node, Mapping):
        raise PeopleError(f"the People node must be a mapping, got {type(node).__name__}", code="node")
    params = node.get("params")
    if not isinstance(params, Mapping):
        data = node.get("data")
        params = data.get("params") if isinstance(data, Mapping) else None
    if isinstance(params, Mapping):
        return dict(params)
    return {} if ("id" in node or "kind" in node or "data" in node) else dict(node)


def _first(params: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if params.get(name) is not None:
            return params[name]
    return None


def _text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PeopleError(f"{name} must be text, got {type(value).__name__}", code="params")
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class PeopleParams:
    """The People node's parameters. The editor (T47) writes `cast`, `behavior`, `placement` {x, y} in plate pixels,
    `light`, `personDepthM`, `depthSource` ('z' | 'manual') and `mode`; snake_case spellings are accepted too."""

    mode: str
    foot: tuple[float, float]
    behavior: str | None
    cast: str | None
    light: str | None
    prompt: str | None
    person_depth_m: float | None
    depth_source: str
    film_id: str | None
    shot_id: str | None
    frame: int | str
    shadow: bool
    shadow_reach_px: int | None = None


def parse_params(node: Any) -> PeopleParams:
    p = _node_params(node)
    mode = _text(_first(p, "mode"), "mode") or "i2v"
    if mode not in PERSON_MODES:
        raise PeopleError(f"mode {mode!r}: expected one of {list(PERSON_MODES)}", code="mode")
    if mode == "retake":
        raise PeopleError(
            "retake makes a new camera view with no matte, which is an invented angle and cannot be composited; "
            "the People node places a person in the approved view (use i2v or wan-edit), and a new view belongs "
            "to the Motion node's Move policy",
            code="invented_camera",
        )
    placement = _first(p, "placement")
    if not isinstance(placement, Mapping) or not all(
        isinstance(placement.get(k), (int, float)) and math.isfinite(placement[k]) for k in ("x", "y")
    ):
        raise PeopleError(
            "placement must be {x, y}: the pixel of the plate where the feet touch the ground",
            code="placement",
        )
    depth = _first(p, "personDepthM", "person_depth_m", "personDepth")
    if depth is not None:
        if isinstance(depth, bool) or not isinstance(depth, (int, float)) or not math.isfinite(depth):
            raise PeopleError(f"personDepthM must be a number of metres, got {depth!r}", code="bad_depth")
        if not MIN_DEPTH_M <= float(depth) <= MAX_DEPTH_M:
            raise PeopleError(
                f"personDepthM {depth} m is outside {MIN_DEPTH_M} to {MAX_DEPTH_M} m", code="bad_depth"
            )
        depth = float(depth)
    prompt = _text(_first(p, "prompt"), "prompt")
    behavior = _text(_first(p, "behavior", "behaviour"), "behavior")
    if prompt is None and behavior is None:
        raise PeopleError("the node needs a behavior (what the person does) or a full prompt", code="params")
    film_id = _text(_first(p, "filmId", "film_id"), "filmId")
    shot_id = _text(_first(p, "shotId", "shot_id"), "shotId")
    if shot_id is not None and film_id is None:
        raise PeopleError("shotId needs the filmId it belongs to", code="params")
    frame = _first(p, "frame")
    if frame is None:
        frame = "mid"
    elif isinstance(frame, str):
        if frame not in ("mid", "first", "last") and not frame.isdigit():
            raise PeopleError(
                f"frame {frame!r}: expected 'first', 'mid', 'last' or a frame number", code="params"
            )
        frame = int(frame) if frame.isdigit() else frame
    elif isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
        raise PeopleError(
            f"frame {frame!r}: expected 'first', 'mid', 'last' or a frame number", code="params"
        )
    source = _text(_first(p, "depthSource", "depth_source"), "depthSource") or "manual"
    reach = _first(p, "shadowReachPx", "shadow_reach_px")
    if reach is not None and (
        isinstance(reach, bool) or not isinstance(reach, (int, float)) or not 4 <= reach <= 400
    ):
        raise PeopleError(f"shadowReachPx must be 4 to 400 pixels, got {reach!r}", code="params")
    return PeopleParams(
        mode=mode,
        foot=(float(placement["x"]), float(placement["y"])),
        behavior=behavior,
        cast=_text(_first(p, "cast", "cast_id", "castId"), "cast"),
        light=_text(_first(p, "light"), "light"),
        prompt=prompt,
        person_depth_m=depth,
        depth_source=source,
        film_id=film_id,
        shot_id=shot_id,
        frame=frame,
        shadow=p.get("shadow") is not False,
        shadow_reach_px=None if reach is None else int(reach),
    )


# ---------------------------------------------------------------------------- camera and the packet's Z


@dataclass(frozen=True)
class Cam:
    """plate-cam-v0 at the plate's pixel size, in metres: the camera looks down its local -Z, +Y up, +X right."""

    R: np.ndarray  # (3, 3) world_from_camera rotation
    t: np.ndarray  # (3,) camera position, metres
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int

    def ray(self, u: float, v: float) -> np.ndarray:
        """World direction of the pixel (u, v) scaled so that `t + s * ray` has planar depth `s`."""
        return self.R @ np.array([(u - self.cx) / self.fx, -(v - self.cy) / self.fy, -1.0])


def camera_model(camera: Mapping[str, Any], meters_per_unit: float, width: int, height: int) -> Cam:
    try:
        m = np.asarray(camera["world_from_camera"], float)
        k = camera["intrinsics_px"]
        fx, fy = float(k["fx"]), float(k["fy"])
        cx, cy = (float(c) for c in k["principal_point"])
    except (KeyError, TypeError, ValueError) as e:
        raise PeopleError(f"the packet camera is unreadable ({type(e).__name__}: {e})", code="camera") from e
    if m.shape != (4, 4):
        raise PeopleError("camera.world_from_camera must be 4x4", code="camera")
    res = camera.get("resolution") or {}
    rw, rh = res.get("width"), res.get("height")
    sx = width / rw if rw else 1.0
    sy = height / rh if rh else 1.0
    return Cam(
        R=m[:3, :3].copy(),
        t=m[:3, 3] * meters_per_unit,
        fx=fx * sx,
        fy=fy * sy,
        cx=cx * sx,
        cy=cy * sy,
        width=width,
        height=height,
    )


def _meters_per_unit(manifest: Mapping[str, Any], notes: list[str]) -> float:
    mpu = (manifest.get("units") or {}).get("meters_per_unit")
    if isinstance(mpu, (int, float)) and mpu > 0:
        return float(mpu)
    notes.append("units: relative (no meters_per_unit); Z and camera are read as metres")
    return 1.0


def _zdepth_entry(manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    elements = ((manifest.get("files") or {}).get("beauty_exr") or {}).get("elements") or []
    return next((e for e in elements if e.get("element") == "zdepth"), None)


def packet_z(packet: Any, shape: tuple[int, int], notes: list[str]) -> tuple[np.ndarray, Mapping[str, Any]]:
    """The packet's Z in scene units at `shape` (H, W), nearest-resampled, with the manifest's zdepth entry."""
    manifest = getattr(packet, "manifest", None) or {}
    root = getattr(packet, "root", None)
    entry = _zdepth_entry(manifest)
    if root is None or entry is None:
        raise PeopleError(
            "the packet has no Z (a Tier C plate or a packet without a zdepth element): pass depth_m, or "
            "the person cannot be placed behind foreground geometry",
            code="no_depth",
        )
    if entry.get("normalized"):
        raise PeopleError("the packet's zdepth is normalised, so it has no metric scale", code="no_depth")
    from plate_pixels.exr import read_elements

    exr = Path(root) / manifest["files"]["beauty_exr"]["path"]
    z = read_elements(exr, manifest, ["zdepth"])["zdepth"][..., 0]
    if z.shape != shape:
        was = z.shape
        z = cv2.resize(z, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)
        notes.append(f"depth: packet Z resampled from {was[::-1]} to the still's {shape[::-1]} (nearest)")
    return np.asarray(z, np.float32), entry


def depth_metres(
    z: np.ndarray, entry: Mapping[str, Any] | None, cam: Cam, meters_per_unit: float
) -> np.ndarray:
    """Planar depth in metres; background and unusable pixels become +inf (they occlude nothing)."""
    z = np.asarray(z, np.float64) * meters_per_unit
    bg = entry.get("background_value") if entry else None
    bad = ~np.isfinite(z) | (z <= 0) | (z >= BG_Z * meters_per_unit)
    if isinstance(bg, (int, float)) and bg > 0:
        bad |= z >= float(bg) * meters_per_unit * 0.999
    if entry and entry.get("depth_kind") == "radial":
        xn = (np.arange(z.shape[1]) + 0.5 - cam.cx) / cam.fx
        yn = -((np.arange(z.shape[0]) + 0.5 - cam.cy) / cam.fy)
        z = z / np.sqrt(xn[None, :] ** 2 + yn[:, None] ** 2 + 1.0)
    return np.where(bad, np.inf, z).astype(np.float32)


# ---------------------------------------------------------------------------- the ground plane


@dataclass(frozen=True)
class GroundPlane:
    """The level ground (world Z = `height_m`, metres) fitted from the packet's Z."""

    height_m: float
    n_px: int
    share: float

    def to_dict(self) -> dict[str, float]:
        return {"height_m": self.height_m, "n_px": self.n_px, "share": self.share}


def fit_ground_plane(z_m: np.ndarray, cam: Cam, *, max_px: int = GROUND_SAMPLE_PX) -> GroundPlane | None:
    """The dominant level surface below the camera, or None when Z has no ground worth the name.

    The scene is Z-up (`units.up_axis`), so the ground is a plane of constant world Z. Z is back-projected to
    world points on a subsampled grid; a point is a ground candidate when its local surface normal is within
    10 degrees of vertical (cross product of its grid neighbours, no depth edge) and it lies below the camera;
    the plane is the 2 cm height bin that most candidates share, refined to the median of its members. Walls
    are vertical and ceilings are above the camera, so neither can win.
    """
    h, w = z_m.shape
    stride = max(1, math.ceil(math.sqrt(h * w / max_px)))
    zs = z_m[::stride, ::stride].astype(np.float64)
    sh, sw = zs.shape
    if sh < 3 or sw < 3:
        return None
    valid = np.isfinite(zs) & (zs > 0)
    zz = np.where(valid, zs, 0.0)
    u = (np.arange(sw) * stride + 0.5 - cam.cx) / cam.fx
    v = -((np.arange(sh) * stride + 0.5 - cam.cy) / cam.fy)
    pts = np.stack([u[None, :] * zz, v[:, None] * zz, -zz], axis=-1) @ cam.R.T + cam.t
    du = pts[:-1, 1:] - pts[:-1, :-1]
    dv = pts[1:, :-1] - pts[:-1, :-1]
    n = np.cross(du, dv)
    nl = np.linalg.norm(n, axis=-1)
    z0 = zz[:-1, :-1]
    ok = valid[:-1, :-1] & valid[:-1, 1:] & valid[1:, :-1] & (nl > 1e-12)
    ok &= (np.abs(zz[:-1, 1:] - z0) < 0.15 * z0) & (np.abs(zz[1:, :-1] - z0) < 0.15 * z0)
    level = ok & (np.abs(n[..., 2]) > math.cos(math.radians(GROUND_MAX_HORIZON_DEG)) * nl)
    heights = pts[:-1, :-1, 2]
    cand = level & (heights < cam.t[2] - 0.05) & (heights > cam.t[2] - 100.0)
    vals = heights[cand]
    n_valid = int(valid.sum())
    if vals.size < max(GROUND_MIN_PX // (stride * stride), 20) or n_valid == 0:
        return None
    lo = float(vals.min())
    bins = np.arange(lo, float(vals.max()) + 0.04, 0.02)
    counts, edges = np.histogram(vals, bins=bins)
    peak = float(edges[int(np.argmax(counts))] + 0.01)
    members = vals[np.abs(vals - peak) <= 0.03]
    n_px = int(members.size) * stride * stride
    if n_px < GROUND_MIN_PX or members.size / n_valid < 0.0005:
        return None
    return GroundPlane(height_m=float(np.median(members)), n_px=n_px, share=float(members.size / n_valid))


def ground_depth_at(cam: Cam, plane: GroundPlane, u: float, v: float) -> float | None:
    """Planar depth of the ground point under pixel (u, v), or None for a ray that never meets the ground."""
    d = cam.ray(u, v)
    if abs(d[2]) < 1e-9:
        return None
    s = (plane.height_m - cam.t[2]) / d[2]
    return float(s) if s > 0 else None


def ground_depth_map(cam: Cam, plane: GroundPlane, shape: tuple[int, int]) -> np.ndarray:
    """Planar depth of the ground at every pixel (+inf where the ray never meets it)."""
    h, w = shape
    xn = (np.arange(w) + 0.5 - cam.cx) / cam.fx
    yn = -((np.arange(h) + 0.5 - cam.cy) / cam.fy)
    dz = cam.R[2, 0] * xn[None, :] + cam.R[2, 1] * yn[:, None] - cam.R[2, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        s = (plane.height_m - cam.t[2]) / dz
    return np.where(np.isfinite(s) & (s > 0), s, np.inf).astype(np.float32)


def _z_at(z_m: np.ndarray, x: float, y: float) -> float | None:
    h, w = z_m.shape
    xi, yi = int(math.floor(x)), int(math.floor(y))
    win = z_m[
        max(0, yi - FOOT_WINDOW) : min(h, yi + FOOT_WINDOW + 1),
        max(0, xi - FOOT_WINDOW) : min(w, xi + FOOT_WINDOW + 1),
    ]
    finite = win[np.isfinite(win)]
    return float(np.median(finite)) if finite.size else None


def resolve_person_depth(
    params: PeopleParams, z_m: np.ndarray, cam: Cam, notes: list[str]
) -> tuple[float, str, GroundPlane | None]:
    """(depth in metres, source, ground plane). The node's own `personDepthM` wins (the editor fills it from Z
    at the ground point and the user may overwrite it). Else the foot pixel is back-projected onto the ground
    plane fitted from Z; if the plane disagrees with Z at the foot by more than 5% (a step, a podium) Z wins;
    with neither, the run is refused."""
    h, w = z_m.shape
    x, y = params.foot
    if not (0 <= x < w and 0 <= y < h):
        raise PeopleError(f"placement ({x:g}, {y:g}) is outside the {w} x {h} plate", code="placement")
    plane = fit_ground_plane(z_m, cam)
    if params.person_depth_m is not None:
        return params.person_depth_m, "node", plane
    d_plane = ground_depth_at(cam, plane, x, y) if plane is not None else None
    d_z = _z_at(z_m, x, y)
    if d_plane is not None and (d_z is None or abs(d_plane - d_z) <= GROUND_AGREE_REL * max(d_z, 1.0)):
        return d_plane, "ground_plane", plane
    if d_z is not None:
        if d_plane is not None:
            notes.append(
                f"depth: Z at the foot ({d_z:.2f} m) disagrees with the ground plane ({d_plane:.2f} m); using Z"
            )
        elif plane is None:
            notes.append("depth: no ground plane could be fitted from Z; using Z at the foot")
        return d_z, "z_at_foot", plane
    raise PeopleError(
        f"no depth at the ground point ({x:g}, {y:g}): the packet's Z has nothing there; set personDepthM",
        code="no_depth",
    )


# ---------------------------------------------------------------------------- occlusion and the composite


def occluder_mask(z_m: np.ndarray, person_depth_m: float, tol_m: float | None = None) -> np.ndarray:
    """bool (H, W): the packet puts geometry nearer than the person here (Z < depth - tol). +inf never occludes."""
    tol = max(OCC_TOL_MIN_M, OCC_TOL_REL * person_depth_m) if tol_m is None else tol_m
    with np.errstate(invalid="ignore"):
        return np.asarray(z_m < (person_depth_m - tol))


def _soft(mask: np.ndarray, sigma: float = 0.8) -> np.ndarray:
    """A 0..1 weight for a bool mask: grown one pixel and blurred so its edge is anti-aliased; the interior is
    exactly 1 and everything beyond 2 px is exactly 0."""
    if not mask.any():
        return np.zeros(mask.shape, np.float32)
    grown = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8))
    s = cv2.GaussianBlur(grown.astype(np.float32), (0, 0), sigma)
    s[s > 0.999] = 1.0
    s[s < 0.001] = 0.0
    return s


def shadow_reach_px(height: int) -> int:
    """How far from the matte a shadow is looked for: 10% of the frame height (8 to 200 px). A shadow STAFFAGE
    draws beyond it is dropped; the node's `shadowReachPx` widens the search."""
    return int(min(200, max(8, round(0.10 * height))))


def shadow_strength(base_f: np.ndarray, person_f: np.ndarray, matte: np.ndarray, reach_px: int) -> np.ndarray:
    """The ground shadow STAFFAGE drew, as a darkening of the base: where the person plate is darker than the base
    in the band around the matte (outside the matte and a 2 px halo, within `reach_px`), strength = 1 - ratio."""
    solid = matte > 0.05
    if not solid.any():
        return np.zeros(matte.shape, np.float32)
    dist = cv2.distanceTransform((~solid).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    band = (dist > 2.0) & (dist <= float(reach_px))
    ratio = (person_f @ LUMA) / np.maximum(base_f @ LUMA, 0.02)
    dark = np.clip(1.0 - ratio, 0.0, 1.0)
    s = np.where(band & (dark > SHADOW_MIN_DARK), dark, 0.0).astype(np.float32)
    s = cv2.GaussianBlur(s, (0, 0), 1.5)
    s[~((dist > 2.0) & (dist <= float(reach_px) + 2.0))] = 0.0
    return np.clip(s, 0.0, SHADOW_CAP)


def occlusion_weight(z_m: np.ndarray, person_depth_m: float) -> np.ndarray:
    """float32 (H, W): how much of the person shows at each pixel (1 where nothing is nearer than her, 0 behind
    the packet's nearer geometry, anti-aliased on the occluder's edge). The person's alpha is her matte times this."""
    return (1.0 - _soft(occluder_mask(z_m, person_depth_m))).astype(np.float32)


def shadow_layer(
    base16: np.ndarray,
    person16: np.ndarray,
    matte: np.ndarray,
    z_m: np.ndarray,
    person_depth_m: float,
    *,
    foot_y: float | None = None,
    cam: Cam | None = None,
    plane: GroundPlane | None = None,
    reach_px: int | None = None,
) -> np.ndarray:
    """float32 (H, W): the ground shadow STAFFAGE drew (`shadow_strength`), hidden where the packet puts something
    nearer than the ground the shadow falls on (the ground plane's depth, else the person's depth at or above the
    foot row)."""
    h = base16.shape[0]
    base_f = base16.astype(np.float32) / 65535.0
    person_f = person16.astype(np.float32) / 65535.0
    s = shadow_strength(base_f, person_f, matte, reach_px or shadow_reach_px(h))
    if not s.any():
        return s
    if plane is not None and cam is not None:
        ground = ground_depth_map(cam, plane, matte.shape)
        ground = np.where(np.isfinite(ground), ground, person_depth_m)
    else:
        # without a ground plane, geometry nearer than the person counts only at or above the foot row
        # (the ground in front of her lies below it and is always "nearer")
        ground = np.full(matte.shape, person_depth_m, np.float32)
        if foot_y is not None:
            ground[int(math.ceil(foot_y)) :] = -np.inf
    with np.errstate(invalid="ignore"):
        hidden = np.asarray(z_m < (ground - np.maximum(OCC_TOL_MIN_M, OCC_TOL_REL * ground)))
    return (s * (1.0 - _soft(hidden))).astype(np.float32)


@dataclass
class Placed:
    """The pure composite: `out16` differs from the base only where `footprint` is True."""

    out16: np.ndarray
    alpha: np.ndarray  # float32, the person's alpha after occlusion
    occluded: np.ndarray  # bool, matte pixels (> 0) hidden by the packet's geometry
    shadow: np.ndarray  # float32, shadow strength applied
    footprint: np.ndarray  # bool, pixels the composite may change


def composite_person(
    base16: np.ndarray,
    person16: np.ndarray,
    matte: np.ndarray,
    z_m: np.ndarray,
    person_depth_m: float,
    *,
    foot_y: float | None = None,
    cam: Cam | None = None,
    plane: GroundPlane | None = None,
    shadow: bool = True,
    reach_px: int | None = None,
) -> Placed:
    """Put the person (`person16`, alpha `matte`) into `base16` behind whatever the packet's Z (`z_m`, metres,
    +inf = nothing) puts nearer than `person_depth_m`. All arrays share one (H, W)."""
    h, w = base16.shape[:2]
    if person16.shape != base16.shape or matte.shape != (h, w) or z_m.shape != (h, w):
        raise PeopleError(
            f"shape mismatch: base {base16.shape}, person {person16.shape}, matte {matte.shape}, Z {z_m.shape}",
            code="shape",
        )
    base_f = base16.astype(np.float32) / 65535.0
    person_f = person16.astype(np.float32) / 65535.0
    matte = np.where(matte < MATTE_EPS, 0.0, np.clip(matte, 0.0, 1.0)).astype(np.float32)

    alpha = matte * occlusion_weight(z_m, person_depth_m)
    occluded = (matte > 0) & (alpha <= 0)

    s = np.zeros((h, w), np.float32)
    if shadow:
        s = shadow_layer(
            base16,
            person16,
            matte,
            z_m,
            person_depth_m,
            foot_y=foot_y,
            cam=cam,
            plane=plane,
            reach_px=reach_px,
        )

    out_f = base_f * (1.0 - s[..., None])
    out_f = out_f * (1.0 - alpha[..., None]) + person_f * alpha[..., None]
    footprint = (alpha > 0) | (s > 0)
    out16 = base16.copy()
    out16[footprint] = np.clip(np.rint(out_f[footprint] * 65535.0), 0, 65535).astype(np.uint16)
    return Placed(out16=out16, alpha=alpha, occluded=occluded, shadow=s, footprint=footprint)


# ---------------------------------------------------------------------------- reading what STAFFAGE returns


def _decode(path: Path) -> np.ndarray:
    """cv2 image from a path (np.fromfile: cv2.imread cannot open non-ASCII Windows paths)."""
    img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise PeopleError(f"STAFFAGE's output {path.name} is not a readable image", code="staffage_output")
    return img


def _video_frame(path: Path, frame: int | str) -> np.ndarray:
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise PeopleError(
                f"STAFFAGE's output {path.name} is not a readable video", code="staffage_output"
            )
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        idx = (
            {"first": 0, "mid": n // 2, "last": n - 1}.get(frame, frame) if isinstance(frame, str) else frame
        )
        idx = int(min(max(idx, 0), n - 1))
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, img = cap.read()
        if not ok:
            raise PeopleError(f"could not read frame {idx} of {path.name}", code="staffage_output")
        return img
    finally:
        cap.release()


def read_layer(path: Path, frame: int | str) -> np.ndarray:
    """STAFFAGE output (an image, or a video from which `frame` is taken) as a cv2 BGR(A)/gray array."""
    if not path.is_file():
        raise PeopleError(
            f"STAFFAGE reported {path.name}, which cannot be read from here (it hands files over by path)",
            code="staffage_output",
        )
    return _decode(path) if path.suffix.lower() in IMAGE_SUFFIXES else _video_frame(path, frame)


def read_layer_frames(path: Path) -> Iterator[np.ndarray]:
    """Every frame of a STAFFAGE output in order, as cv2 BGR(A)/gray arrays (an image is one frame). One frame
    is decoded at a time, so a 4K take never sits in memory whole."""
    path = Path(path)
    if not path.is_file():
        raise PeopleError(
            f"STAFFAGE reported {path.name}, which cannot be read from here (it hands files over by path)",
            code="staffage_output",
        )
    if path.suffix.lower() in IMAGE_SUFFIXES:
        yield _decode(path)
        return
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise PeopleError(
                f"STAFFAGE's output {path.name} is not a readable video", code="staffage_output"
            )
        n = 0
        while True:
            ok, img = cap.read()
            if not ok or img is None:
                break
            n += 1
            yield img
        if n == 0:
            raise PeopleError(f"STAFFAGE's output {path.name} has no frames", code="staffage_output")
    finally:
        cap.release()


def layer_fps(path: Path) -> float | None:
    """The frame rate of a STAFFAGE video output, or None for an image (or a container that reports none)."""
    path = Path(path)
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return None
    cap = cv2.VideoCapture(str(path))
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS)) if cap.isOpened() else 0.0
    finally:
        cap.release()
    return fps if math.isfinite(fps) and fps > 0 else None


def to_rgb(img: np.ndarray) -> np.ndarray:
    """RGB array (uint8 or uint16) from a cv2 array: gray is replicated, alpha dropped, BGR flipped."""
    if img.ndim == 2:
        img = np.repeat(img[..., None], 3, axis=2)
    elif img.shape[2] == 4:
        img = img[..., :3]
    if img.dtype not in (np.uint8, np.uint16):
        img = np.clip(np.rint(img.astype(np.float32) * 255.0), 0, 255).astype(np.uint8)
    return np.ascontiguousarray(img[..., ::-1])


def to_matte(img: np.ndarray) -> np.ndarray:
    """float32 (H, W) in [0, 1]: the alpha channel of a 4-channel image, else the first channel (a gray pha)."""
    plane = img if img.ndim == 2 else (img[..., 3] if img.shape[2] == 4 else img[..., 0])
    if plane.dtype == np.uint8:
        return plane.astype(np.float32) / 255.0
    if plane.dtype == np.uint16:
        return plane.astype(np.float32) / 65535.0
    return np.clip(plane.astype(np.float32), 0.0, 1.0)


def undo_sizemap(img: np.ndarray, sm: pixels_egress.SizeMap) -> np.ndarray:
    """Map a layer STAFFAGE returned (matte or plate) back onto the plate grid by the size map, the geometry half of
    `ingress.restore` (centre-crop to the sent aspect, resample to the padded rectangle, drop the pad)."""
    h, w = img.shape[:2]
    aspect = sm.dst[0] / sm.dst[1]
    if abs((w / h) / aspect - 1.0) > 1e-3:
        if w / h > aspect:
            nw = max(1, round(h * aspect))
            left = (w - nw) // 2
            img = img[:, left : left + nw]
        else:
            nh = max(1, round(w / aspect))
            top = (h - nh) // 2
            img = img[top : top + nh]
        h, w = img.shape[:2]
    pw, ph = sm.padded_size
    if (w, h) != (pw, ph):
        interp = cv2.INTER_AREA if max(pw / w, ph / h) < 0.8 else cv2.INTER_LINEAR
        img = cv2.resize(img, (pw, ph), interpolation=interp)
    left, top, _, _ = sm.pad
    cw, ch = sm.content_size
    return np.ascontiguousarray(img[top : top + ch, left : left + cw])


def _png16(rgb16: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(rgb16[..., ::-1]))
    if not ok:
        raise PeopleError("could not encode the composite as a 16-bit PNG", code="encode")
    return bytes(buf)


def _png8(plane: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.clip(np.rint(plane * 255.0), 0, 255).astype(np.uint8))
    if not ok:
        raise PeopleError("could not encode the matte as a PNG", code="encode")
    return bytes(buf)


def _as_u16(img: Any, name: str) -> np.ndarray:
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] != 3:
        raise PeopleError(f"{name} must be an RGB array (h, w, 3), got {a.shape}", code="bad_still")
    if a.dtype == np.uint16:
        return np.ascontiguousarray(a)
    if a.dtype == np.uint8:
        return a.astype(np.uint16) * np.uint16(257)
    if np.issubdtype(a.dtype, np.floating):
        return np.clip(np.rint(a * 65535.0), 0, 65535).astype(np.uint16)
    raise PeopleError(f"{name} must be uint8, uint16 or float in [0, 1], got {a.dtype}", code="bad_still")


# ---------------------------------------------------------------------------- the run


@dataclass
class PeopleResult:
    rgb16: np.ndarray
    region: np.ndarray  # bool: the gate mask, editable_ids ∪ people footprint (+ the gate's collar)
    footprint: np.ndarray  # bool: where the composite differs from the still
    alpha: np.ndarray
    occluded: np.ndarray
    shadow: np.ndarray
    person_depth_m: float
    depth_source: str
    ground_plane: GroundPlane | None
    grade: str
    run_id: str
    job: dict[str, Any]
    registration: dict[str, Any]
    report: Any  # plate_gates.report.GateReport (None when the gate was skipped)
    staged: Path
    assets: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str | None:
        return None if self.report is None else self.report.verdict

    def summary(self) -> dict[str, Any]:
        """JSON-safe facts for the ledger and the run's `outputs`."""
        return {
            "run_id": self.run_id,
            "job_id": self.job.get("id"),
            "model": self.job.get("model"),
            "est_cost_usd": self.job.get("estCostUsd"),
            "grade": self.grade,
            "person_depth_m": round(self.person_depth_m, 4),
            "depth_source": self.depth_source,
            "ground_plane": None if self.ground_plane is None else self.ground_plane.to_dict(),
            "footprint_px": int(self.footprint.sum()),
            "occluded_px": int(self.occluded.sum()),
            "verdict": self.verdict,
            "assets": dict(self.assets),
            "notes": list(self.notes),
        }


def _editable_mask(packet: Any, shape: tuple[int, int], notes: list[str]) -> np.ndarray:
    """The union of the packet's editable ids (empty when the packet has no ids to read)."""
    mask = np.zeros(shape, bool)
    ids_fn = getattr(packet, "editable_ids", None)
    mask_fn = getattr(packet, "id_mask", None)
    if not callable(ids_fn) or not callable(mask_fn):
        return mask
    try:
        for cid in ids_fn():
            m = np.asarray(mask_fn(cid), bool)
            if m.shape == shape:
                mask |= m
    except (ValueError, LookupError, OSError) as e:
        notes.append(
            f"editable ids not read ({type(e).__name__}: {e}); the gate mask is the people footprint"
        )
    return mask


def _framing_sentence(params: PeopleParams, depth_m: float, cam: Cam) -> str:
    x, y = params.foot
    frac = min(1.0, max(0.02, PERSON_HEIGHT_M * cam.fy / depth_m / cam.height))
    return (
        f"standing about {depth_m:.1f} m from the camera, feet on the ground at {100 * x / cam.width:.0f}% "
        f"across and {100 * y / cam.height:.0f}% down the frame, full body about {100 * frac:.0f}% of the frame "
        "height"
    )


def _find_cast(film: Mapping[str, Any], wanted: str) -> str | None:
    for member in film.get("cast") or []:
        if (
            wanted in (member.get("id"), member.get("name"))
            or str(member.get("name", "")).casefold() == wanted.casefold()
        ):
            return str(member["id"])
    return None


@dataclass
class TakePlan:
    """What `plan_take` settled before anything leaves the box: the node's parameters, the custody grade of the
    route, the still and the egress PNG STAFFAGE will get (with its size map), the camera, the packet's Z in metres
    and where the person stands in it. Deterministic: planning the same node on the same still again gives the
    same plan (People in motion re-plans at NORMALIZING instead of storing Z)."""

    params: PeopleParams
    manifest: dict[str, Any]
    root: Any
    legend: Any
    run_id: str
    egress_run: EgressRun
    route: dict[str, Any]
    grade: str
    base16: np.ndarray
    cam: Cam
    z_m: np.ndarray
    person_depth_m: float
    depth_source: str
    ground_plane: GroundPlane | None
    png: bytes
    sizemap: pixels_egress.SizeMap
    notes: list[str] = field(default_factory=list)

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.base16.shape[0]), int(self.base16.shape[1]))


def plan_take(
    project: Any,
    node: Any,
    packet: Any,
    approved_still: Any,
    *,
    settings: StaffageSettings | None = None,
    run_id: str | None = None,
    depth_m: np.ndarray | None = None,
) -> TakePlan:
    """Steps 1 and 2 of the module docstring and the egress PNG of step 3: custody and the leak scan first, then
    the parameters, the still, the packet's Z and the person's depth. Nothing is written and nothing is sent."""
    params = parse_params(node)
    settings = settings or StaffageSettings.load()
    manifest = dict(getattr(packet, "manifest", None) or {})
    root = getattr(packet, "root", None)
    legend = getattr(packet, "legend", None)
    run_id = run_id or new_opaque_id()
    notes: list[str] = []

    # 1. custody, before anything else happens
    egress_run = EgressRun.for_project(project, run_id, packet=manifest, legend=legend, packet_dir=root)
    route = settings.route()
    grade = require_route(route, egress_run.allowed_grades, what="People node (STAFFAGE)")
    hit = leak_hit(egress_run, [params.behavior, params.prompt, params.cast, params.light])
    if hit is not None:
        raise LeakBlocked(f"the People node's text carries {hit!r}, which never leaves the box")

    # 2. everything that can be wrong, before anything is spent
    still = approved_still.get("rgb") if isinstance(approved_still, Mapping) else approved_still
    base16 = _as_u16(still, "approved_still")
    h, w = base16.shape[:2]
    mpu = _meters_per_unit(manifest, notes)
    cam = camera_model(manifest.get("camera") or {}, mpu, w, h)
    if depth_m is not None:
        z_m = np.asarray(depth_m, np.float32)
        if z_m.shape != (h, w):
            z_m = cv2.resize(z_m, (w, h), interpolation=cv2.INTER_NEAREST)
            notes.append("depth: the supplied depth was resampled to the still (nearest)")
        z_m = np.where(np.isfinite(z_m) & (z_m > 0), z_m, np.inf).astype(np.float32)
        depth_origin = "supplied"
    else:
        z_raw, entry = packet_z(packet, (h, w), notes)
        z_m = depth_metres(z_raw, entry, cam, mpu)
        depth_origin = "packet"
    notes.append(f"depth: {depth_origin} Z, {int(np.isfinite(z_m).sum())} of {h * w} px carry geometry")
    person_depth, depth_source, plane = resolve_person_depth(params, z_m, cam, notes)
    if not MIN_DEPTH_M <= person_depth <= MAX_DEPTH_M:
        raise PeopleError(
            f"person depth {person_depth:.2f} m is outside {MIN_DEPTH_M} to {MAX_DEPTH_M} m", code="bad_depth"
        )

    # 3. (first half) the still as egress makes it: 8-bit sRGB, metadata stripped, never upsampled
    png, sm = pixels_egress.prepare(base16, manifest, ANY_SIZE, mask=None, region_crop=False)
    png = strip_metadata_png(png)
    return TakePlan(
        params=params,
        manifest=manifest,
        root=root,
        legend=legend,
        run_id=run_id,
        egress_run=egress_run,
        route=route,
        grade=grade.value,
        base16=base16,
        cam=cam,
        z_m=z_m,
        person_depth_m=person_depth,
        depth_source=depth_source,
        ground_plane=plane,
        png=png,
        sizemap=sm,
        notes=notes,
    )


def start_take(
    plan: TakePlan,
    project: Any,
    *,
    client: StaffageClient | None = None,
    settings: StaffageSettings | None = None,
    stage_dir: Path | str | None = None,
    log: OutboundLog | None = None,
    poll_s: float = 5.0,
    timeout_s: float = 25 * 60.0,
    sleep: Callable[[float], None] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Step 3 (second half): the planned PNG is staged under an opaque name, STAFFAGE gets a film, the plate, a
    shot and the person job, and the job is polled by id to its end. Returns (the finished job, the staged
    still); the job names `platePath` and `mattePath`, stills or video."""
    params, cam = plan.params, plan.cam
    settings = settings or StaffageSettings.load()
    stage = Path(stage_dir) if stage_dir is not None else app_home() / "cache" / "staffage"
    stage.mkdir(parents=True, exist_ok=True)
    opaque = new_opaque_id()
    staged = stage / f"{opaque}.png"
    staged.write_bytes(plan.png)

    if log is None and hasattr(project, "root"):
        log = OutboundLog.for_project(project.root)
    client = client or StaffageClient(settings.base_url, timeout_s=settings.timeout_s)
    client.attach(run=plan.egress_run, route=plan.route, log=log)

    film_id = params.film_id
    cast_id: str | None = None
    if film_id is not None:
        film = client.get_film(film_id)
        if params.cast:
            cast_id = _find_cast(film, params.cast)
    else:
        film_id = str(client.create_film(opaque)["id"])
    camera = plan.manifest.get("camera") or {}
    client.register_plate(
        opaque,
        staged,
        lensMm=camera.get("focal_length_mm"),
        cameraHeightM=round(float(cam.t[2]), 3),
        lightDirection=params.light or "",
    )
    shot_id = params.shot_id
    if shot_id is None:
        media = client.ingest_media(staged)
        shot_id = str(client.add_shot(film_id, staged, media, name=opaque)["id"])
    placement = _framing_sentence(params, plan.person_depth_m, cam)
    prompt = params.prompt
    if prompt is None and cast_id is None:
        prompt = build_person_prompt(
            params.cast or "one person", params.behavior or "", placement, params.light
        )
    job = client.start_person(
        film_id,
        shot_id,
        castId=cast_id,
        behavior=params.behavior if cast_id else None,
        placement=placement if cast_id else None,
        light=params.light if cast_id else None,
        mode=params.mode,
        stillPath=staged,
        prompt=prompt,
    )
    job = client.wait_for_person(
        str(job["id"]), poll_s=poll_s, timeout_s=timeout_s, sleep=sleep or time.sleep
    )
    if not job.get("platePath") or not job.get("mattePath"):
        raise PeopleError(
            "STAFFAGE finished the job without a plate and a matte", code="staffage_output", job=job
        )
    return job, staged


def run(
    project: Any,
    node: Any,
    packet: Any,
    approved_still: Any,
    *,
    client: StaffageClient | None = None,
    settings: StaffageSettings | None = None,
    stage_dir: Path | str | None = None,
    run_id: str | None = None,
    depth_m: np.ndarray | None = None,
    log: OutboundLog | None = None,
    gate_fn: Callable[..., Any] | None = None,
    poll_s: float = 5.0,
    timeout_s: float = 25 * 60.0,
    sleep: Callable[[float], None] | None = None,
) -> PeopleResult:
    """Place a person in `approved_still` (see the module docstring). `project` is a `projects.Project`; `node` the
    canvas node (`params` per `parse_params`); `packet` a `pipeline.PacketView` or anything with `manifest`, `root`,
    `legend`, `editable_ids()` and `id_mask()`; `approved_still` an RGB array (uint8, uint16 or float) or a mapping
    with `rgb`. `depth_m` overrides the packet's Z (metres; background +inf or <= 0), the only source for Tier C."""
    settings = settings or StaffageSettings.load()
    plan = plan_take(project, node, packet, approved_still, settings=settings, run_id=run_id, depth_m=depth_m)
    job, staged = start_take(
        plan,
        project,
        client=client,
        settings=settings,
        stage_dir=stage_dir,
        log=log,
        poll_s=poll_s,
        timeout_s=timeout_s,
        sleep=sleep,
    )
    params, base16, notes, sm = plan.params, plan.base16, plan.notes, plan.sizemap
    manifest, root, legend, z_m = plan.manifest, plan.root, plan.legend, plan.z_m
    cam, plane = plan.cam, plan.ground_plane
    person_depth, depth_source = plan.person_depth_m, plan.depth_source
    h, w = plan.shape
    png, run_id = plan.png, plan.run_id

    # 4. its plate and matte, back onto the still's grid
    plate_layer = read_layer(Path(job["platePath"]), params.frame)
    matte_layer = read_layer(Path(job["mattePath"]), params.frame)
    matte0 = undo_sizemap(to_matte(matte_layer), sm)
    solid = matte0 > MATTE_EPS
    if not solid.any():
        raise PeopleError(
            "STAFFAGE's matte is empty: there is no person to place", code="empty_matte", job=job
        )
    reach = params.shadow_reach_px or shadow_reach_px(h)
    dist = cv2.distanceTransform((~solid).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    person16, rep = ingress.restore(to_rgb(plate_layer), sm, base16, ring_mask=dist > reach + 8)
    matte = matte0
    if rep.get("warp_applied"):
        warp = np.asarray(rep["registration"]["warp"], np.float32)
        matte = warp_to_plate(np.ascontiguousarray(matte0), warp, interpolation=cv2.INTER_LINEAR)
    if rep.get("camera_drift"):
        notes.append(f"registration: STAFFAGE moved the view by {rep['shift_px']:.1f} px (camera drift)")
    person16 = match_to_ring(base16, person16, dist <= reach, ring_px=24)

    # 5 and 6. occlusion by Z, the composite, the gate
    placed = composite_person(
        base16,
        person16,
        matte,
        z_m,
        person_depth,
        foot_y=params.foot[1],
        cam=cam,
        plane=plane,
        shadow=params.shadow,
        reach_px=reach,
    )
    collar = (
        cv2.distanceTransform((~placed.footprint).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
        <= BAND_PX
    )
    region = collar | _editable_mask(packet, (h, w), notes)

    packet_arg: dict[str, Any] = {"manifest": manifest}
    if root is not None:
        packet_arg["path"] = root
    report = (gate_fn or run_gates)(
        base16,
        placed.out16,
        packet_arg,
        legend,
        str(manifest.get("tier") or "A"),
        {"id": ROUTE_ID, "version": job.get("model")},
        mask=region,
        registration=rep,
    )

    assets: dict[str, str] = {}
    cas = project.cas() if callable(getattr(project, "cas", None)) else None
    if cas is not None:
        assets = {
            "sent_still": cas.put_bytes(png, "png"),
            "person_matte": cas.put_bytes(_png8(matte), "png"),
            "person_plate": cas.put_bytes(_png16(person16), "png"),
            "output": cas.put_bytes(_png16(placed.out16), "png"),
        }
    return PeopleResult(
        rgb16=placed.out16,
        region=region,
        footprint=placed.footprint,
        alpha=placed.alpha,
        occluded=placed.occluded,
        shadow=placed.shadow,
        person_depth_m=person_depth,
        depth_source=depth_source,
        ground_plane=plane,
        grade=plan.grade,
        run_id=run_id,
        job=job,
        registration=rep,
        report=report,
        staged=staged,
        assets=assets,
        notes=notes,
    )


def accept_take(client: StaffageClient, result: PeopleResult) -> dict[str, Any]:
    """After a human accepts the output in PLATE: tell STAFFAGE, so its takes ledger records the decision."""
    return client.accept(str(result.job["id"]))
