"""The Sky node: a reference sky placed inside the SKY Cryptomatte coverage, horizon from the packet camera
(decision 48, router plan T93).

    prepare    the packet (Tier C, or a Tier A packet without Cryptomatte: 422 `no_cryptomatte`; no SKY-class
               object id in the legend: 422 `no_sky`) and the sky picture: the Reference node wired into `sky`
               (its `params.referenceHash`), else `params.skyHash` (none: 422 `no_sky_reference`; not in the CAS:
               422 `reference_missing`). Params: `scale` (default 1.0), `horizon_frac` (default 0.5), `dx_px`
               (default 0), `tone_match` (default true).
    normalize  alpha = the summed coverage of the SKY-class object ids. The horizon row y_h is where the camera's
               level forward direction meets the frame: the world point t + 1e6 f_h through
               `plate_spec.project_point`, t the camera position, f_h its forward axis with the world-up component
               removed. The photo is resized to a width of plate width x scale (INTER_AREA when shrinking, Lanczos
               when enlarging: a sky photo may be resampled, a plate never is), translated so its row
               horizon_frac x height sits on y_h (centred, plus dx_px; bilinear, edges replicated), and with
               `tone_match` its mean Lab over alpha > 0.5 is moved to the plate's over the same pixels. Then
               out = plate (1 - alpha) + sky alpha in 16 bit, the plate itself wherever alpha is 0.
    gate       the still gate with mask alpha > 0 and a hard `outside_mask.changed_px == 0`; texture and tone are
               advisory (a new sky moves the frame's statistics by design).

The reference is an input of this node only: it is never an anchor (the camera is the packet's), never seen by a
vendor, and lands only where the render itself says there is sky.
"""

from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np

from ..pipeline import PipelineError
from .atmosphere_node import sky_alpha, sky_ids, world_up
from .common import Prepared, QuoteContext, RunContext, cas_has, finite_or_none, pick, plain
from .comp_node import changed_outside, png_size, put_mask, still_gates, wired
from .comp_node import score as _score

KIND = "Sky"
FAR = 1.0e6  # scene units to the horizon point
MAX_SCALE = 8.0
REFERENCE_KEYS = ("referenceHash", "reference_hash", "hash")


# ---------------------------------------------------------------------------- geometry


def horizon_y(camera: dict[str, Any]) -> float:
    """The row (continuous pixel coordinates, top-left corner origin) where the camera's level forward
    direction projects. 422 `no_horizon` for a camera looking straight up or down."""
    from plate_spec import project_point

    m = np.asarray(camera["world_from_camera"], dtype=np.float64)
    t, forward = m[:3, 3], -m[:3, 2]  # plate-cam-v0: the camera looks down its -Z axis
    up = world_up(camera)
    level = forward - float(forward @ up) * up
    norm = float(np.linalg.norm(level))
    if norm < 1e-6:
        raise PipelineError(
            "the camera looks straight up or down: there is no horizon to place a sky on", code="no_horizon"
        )
    _u, v, _depth = project_point(camera, (t + FAR * level / norm).tolist())
    return float(v)


def _lerp_axis(img: np.ndarray, s: np.ndarray, axis: int) -> np.ndarray:
    """Linear interpolation of `img` at fractional indices `s` along `axis`, edges replicated."""
    n = img.shape[axis]
    s = np.clip(np.asarray(s, dtype=np.float64), 0.0, n - 1.0)
    i0 = np.floor(s).astype(np.int64)
    i1 = np.minimum(i0 + 1, n - 1)
    shape = [1] * img.ndim
    shape[axis] = len(s)
    t = (s - i0).astype(np.float32).reshape(shape)
    a = np.take(img, i0, axis=axis)
    b = np.take(img, i1, axis=axis)
    return a + (b - a) * t


def resized(photo16: np.ndarray, width: int, scale: float) -> np.ndarray:
    """The photo at width = plate width x scale (aspect kept), float32 on the 16-bit scale."""
    h, w = photo16.shape[:2]
    w_r = max(1, int(round(width * scale)))
    h_r = max(1, int(round(h * w_r / w)))
    img = np.asarray(photo16, dtype=np.float32)
    if (w_r, h_r) == (w, h):
        return img
    interp = cv2.INTER_AREA if w_r < w else cv2.INTER_LANCZOS4
    return np.clip(cv2.resize(img, (w_r, h_r), interpolation=interp), 0.0, 65535.0)


def placement(
    width: int, photo_wh: tuple[int, int], y_h: float, horizon_frac: float, dx_px: float
) -> dict[str, float]:
    """Where the resized photo's index (0, 0) lands in plate index coordinates: plate (x, y) = photo (x, y) +
    (x0, y0), with photo row horizon_frac x height on the horizon row (both continuous, so the +0.5 pixel-centre
    offsets cancel)."""
    w_r, h_r = photo_wh
    return {"x0": (width - w_r) / 2.0 + dx_px, "y0": y_h - horizon_frac * h_r, "width": w_r, "height": h_r}


def place(
    photo: np.ndarray, shape: tuple[int, int], where: dict[str, float], box: tuple[int, int, int, int]
) -> np.ndarray:
    """The resized photo sampled at the plate pixels of `box` (y0, y1, x0, x1), float32 (y1-y0, x1-x0, 3)."""
    by0, by1, bx0, bx1 = box
    rows = _lerp_axis(photo, np.arange(by0, by1) - where["y0"], 0)
    return _lerp_axis(rows, np.arange(bx0, bx1) - where["x0"], 1)


def _bbox(sel: np.ndarray) -> tuple[int, int, int, int] | None:
    ys = np.nonzero(sel.any(axis=1))[0]
    xs = np.nonzero(sel.any(axis=0))[0]
    if not len(ys):
        return None
    return int(ys[0]), int(ys[-1]) + 1, int(xs[0]), int(xs[-1]) + 1


# ---------------------------------------------------------------------------- prepare


def _sky_hash(q: QuoteContext) -> tuple[str | None, str | None]:
    """(hash, Reference node id or None) of the sky picture."""
    refs = [n for n in wired(q, "sky") if n.get("kind") == "Reference"]
    if refs:
        node = refs[0]
        params = node.get("params") if isinstance(node.get("params"), dict) else {}
        key = pick(params, *REFERENCE_KEYS) or node.get("outputHash")
        if len(refs) > 1:
            q.notes.append(
                f"Sky {q.node_id}: {len(refs)} references wired; the first ({node['id']}) is the sky"
            )
        return (str(key) if key else None), str(node["id"])
    key = pick(q.params, "skyHash", "sky_hash")
    return (str(key) if key else None), None


def prepare(q: QuoteContext) -> Prepared:
    packet_id = q.plate_packet_id()
    pv = q.packet(packet_id)
    if pv.tier != "A" or "beauty_exr" not in pv.files or pv.crypto("object") is None:
        raise PipelineError(
            f"packet {packet_id} is tier {pv.tier} with no Cryptomatte: Sky places a sky only where the render "
            "says there is one",
            code="no_cryptomatte",
        )
    if not sky_ids(pv):
        raise PipelineError(f"packet {packet_id}'s legend has no sky-class object id", code="no_sky")
    key, ref_node = _sky_hash(q)
    if key is None:
        raise PipelineError(
            f"Sky {q.node_id} has no sky picture: wire a Reference into its sky input (or name params.skyHash)",
            code="no_sky_reference",
        )
    if not cas_has(q.cas, key):
        raise PipelineError(
            f"the sky picture {key[:12]} is not in this project's CAS", code="reference_missing"
        )
    if png_size(q.cas.read_bytes(key)) is None:
        raise PipelineError(f"the sky picture {key[:12]} is not a PNG", code="bad_image")
    scale = finite_or_none(pick(q.params, "scale", default=1.0))
    if scale is None or not 0.0 < scale <= MAX_SCALE:
        raise PipelineError(f"Sky params.scale is a number in (0, {MAX_SCALE:g}]", code="bad_params")
    frac = finite_or_none(pick(q.params, "horizon_frac", "horizonFrac", default=0.5))
    if frac is None or not 0.0 <= frac <= 1.0:
        raise PipelineError(
            "Sky params.horizon_frac is 0..1 (the photo row on the horizon)", code="bad_params"
        )
    dx = finite_or_none(pick(q.params, "dx_px", "dxPx", default=0.0))
    width = int(pv.plate16().shape[1])
    if dx is None or abs(dx) > MAX_SCALE * width:
        raise PipelineError("Sky params.dx_px is a finite pixel offset", code="bad_params")
    tone = pick(q.params, "tone_match", "toneMatch", default=True)
    if not isinstance(tone, bool):
        raise PipelineError("Sky params.tone_match is true or false", code="bad_params")
    spec = {
        "packet": packet_id,
        "plate": pv.plate_hash,
        "exr": pv.files["beauty_exr"]["hash"]["hex"],
        "sky": {"hash": key, "node": ref_node},
        "scale": scale,
        "horizon_frac": frac,
        "dx_px": dx,
        "tone_match": tone,
        "horizon_y": horizon_y(pv.manifest["camera"]),
    }
    return Prepared(spec, packet_id)


# ---------------------------------------------------------------------------- run


def compute(c: RunContext) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """(out16, alpha, info) for the spec; nothing stored."""
    from plate_gates.color import lab_to_srgb, srgb_to_lab

    spec = c.spec
    pv = c.packet()
    plate16 = pv.plate16()
    h, w = plate16.shape[:2]
    alpha = sky_alpha(pv)
    y_h = horizon_y(pv.manifest["camera"])
    photo = resized(c.read_png16(spec["sky"]["hash"]), w, float(spec["scale"]))
    where = placement(
        w, (photo.shape[1], photo.shape[0]), y_h, float(spec["horizon_frac"]), float(spec["dx_px"])
    )
    out16 = plate16.copy()
    shift = None
    box = _bbox(alpha > 0)
    if box is not None:
        by0, by1, bx0, bx1 = box
        sky = place(photo, (h, w), where, box).astype(np.float64)
        a = alpha[by0:by1, bx0:bx1].astype(np.float64)
        base = plate16[by0:by1, bx0:bx1].astype(np.float64)
        core = a > 0.5
        if spec["tone_match"] and core.any():
            lab_sky = srgb_to_lab(sky / 257.0)
            lab_plate = srgb_to_lab(base[core] / 257.0)
            shift = lab_plate.mean(axis=0) - lab_sky[core].mean(axis=0)
            sky = lab_to_srgb(lab_sky + shift) * 257.0
        mixed = np.rint(base * (1.0 - a[..., None]) + sky * a[..., None])
        inside = a > 0
        region = out16[by0:by1, bx0:bx1]
        region[inside] = np.clip(mixed, 0, 65535).astype(np.uint16)[inside]
    info = {
        "hash": spec["sky"]["hash"],
        "node": spec["sky"]["node"],
        "horizon_y": y_h,
        "placement": where,
        "scale": spec["scale"],
        "horizon_frac": spec["horizon_frac"],
        "dx_px": spec["dx_px"],
        "tone_match": spec["tone_match"],
        "tone_shift_lab": None if shift is None else [float(v) for v in shift],
        "alpha_px": int((alpha > 0).sum()),
        "alpha_full_px": int((alpha >= 1.0).sum()),
        "coverage": float(alpha.mean()),
        "pitch_deg": _pitch_deg(pv.manifest["camera"]),
    }
    return out16, alpha, info


def _pitch_deg(camera: dict[str, Any]) -> float:
    m = np.asarray(camera["world_from_camera"], dtype=np.float64)
    forward = -m[:3, 2] / np.linalg.norm(m[:3, 2])
    return math.degrees(math.asin(float(np.clip(forward @ world_up(camera), -1.0, 1.0))))


def normalize(c: RunContext) -> dict[str, Any]:
    out16, alpha, info = compute(c)
    return {
        "outputs": [c.put_png16(out16)],
        "sky": info,
        "mask_key": put_mask(c, alpha > 0),
        "mask_px": int((alpha > 0).sum()),
    }


def preview(c: RunContext) -> dict[str, Any]:
    _out16, _alpha, info = compute(c)
    return plain(info)


def gate(c: RunContext) -> Any:
    norm = c.normalized
    pv = c.packet()
    out16 = c.read_png16(norm["outputs"][0])
    inside = sky_alpha(pv) > 0
    return still_gates(
        c,
        pv,
        out16,
        inside,
        advisory=("texture.", "tone."),
        checks=[changed_outside(out16, pv.plate16(), inside)],
        notes=[
            "sky: placed inside the SKY Cryptomatte coverage; texture and tone are advisory (a new sky moves them)"
        ],
        metrics={"sky": norm.get("sky")},
    )


score = _score

__all__ = [
    "KIND",
    "gate",
    "horizon_y",
    "normalize",
    "place",
    "placement",
    "prepare",
    "preview",
    "resized",
    "score",
]
