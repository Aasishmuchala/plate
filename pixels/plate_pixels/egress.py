"""Egress transform: the plate as a vendor sees it, and the size map that undoes it (BUILD_PLAN decision 3).

``prepare`` turns the display master (or a region of it) into an 8-bit sRGB PNG at an engine-legal size and
returns the ``SizeMap`` that ``plate_pixels.ingress.restore`` inverts. Rules, all enforced here:

- PNG only, 8-bit sRGB, the house ICC profile embedded, every metadata chunk stripped. Never JPEG. The 16-bit
  master is quantised per pixel to the floor/ceil code triple nearest in CIEDE2000 (``quantise8``), not by
  rounding each channel: rounding R, G and B independently can push a near-neutral 0.5 to 1.0 dE00 off hue,
  which alone breaks the identity round trip (BUILD_PLAN P0 exit: dE00 p99 <= 0.5).
- Pad, never crop, never reflect. A tier A overscan frame already has the engine aspect and goes as is; any
  other mismatch is padded to the nearest legal aspect with real plate pixels where the frame has them (region
  crops) and ``cv2.BORDER_REPLICATE`` beyond the frame edge. Replicated pixels are recorded as the pad mask.
- Never upsample the plate. Downsampling to a preset uses ``cv2.INTER_AREA``; when the preset is larger than
  the frame the frame goes at native size and the engine's output is mapped back by ingress.
- Region crop: the mask's bounding box plus 25% context on each side, clamped to the frame.

The plate is display-referred (the VFB TIFF16 master, or a tier C client plate): uint8, uint16 or float in
[0, 1]. A float plate above 1.0 is scene-linear and is refused: egress never tone-maps.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import icc as _icc
from .imageio import atomic_target
from .sizes import EngineSize, legal_sizes, pad_dims, pick_size

__all__ = [
    "REGION_CONTEXT",
    "QUANTISERS",
    "EgressError",
    "SizeMap",
    "encode_png8",
    "engine_size_of",
    "prepare",
    "quantise8",
    "region_box",
]

REGION_CONTEXT = 0.25
_FLOAT_HEADROOM = 1e-3  # display floats may overshoot 1.0 by rounding; more than this is scene-linear
QUANTISERS = ("de00", "round")

# sRGB -> CIE Lab D65, the same Lindbloom matrix and white point as plate_gates.color (the gates measure with it).
_M_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
_WHITE = np.array([0.95047, 1.0, 1.08883])
_MW = _M_XYZ / _WHITE[:, None]  # linear RGB -> XYZ / white
_LAB_D = 6 / 29
_LAB_A = np.array([[0.0, 116.0, 0.0], [500.0, -500.0, 0.0], [0.0, 200.0, -200.0]])  # f(t) -> (L, a, b)
_QUANT_CHUNK = 1 << 18  # pixels per block: bounds the (N, 3, 3) Jacobians to ~20 MB
_CODE_SNAP = 1e-3  # 8-bit code units; a 16-bit step is 1/257 = 0.0039, float32 error on [0, 1] about 1.5e-5


class EgressError(ValueError):
    """The plate, mask or manifest cannot be sent as asked (refused, never silently adjusted)."""


def _pair(v: Any) -> tuple[int, int]:
    a, b = v
    return int(a), int(b)


def _quad(v: Any) -> tuple[int, int, int, int]:
    a, b, c, d = v
    return int(a), int(b), int(c), int(d)


@dataclass
class SizeMap:
    """How the sent PNG relates to the plate. All rectangles are plate pixels, origin top-left, half-open.

    ``content`` is the plate rectangle that was sent: ``region`` for a region crop, else the whole frame
    ``src``. ``pad`` (left, top, right, bottom) grows ``content`` to the padded rectangle, which is then
    resampled by ``scale`` (<= 1) to ``dst``. ``synthetic`` is the part of ``pad`` that lies outside the frame
    and was edge-replicated (always equal to ``pad`` for a whole frame). ``mode`` is ``pick_size``'s
    (``exact`` | ``pad`` | ``arbitrary``) and ``engine_size`` the size to request from the vendor.
    ``pad_mode``: ``edge_extend`` when any pixel was replicated, ``overscan`` when every sent pixel is a real
    plate pixel and some of them are context (tier A overscan, region-crop context), ``none`` otherwise.
    """

    src: tuple[int, int]
    pad: tuple[int, int, int, int]
    scale: float
    dst: tuple[int, int]
    region: tuple[int, int, int, int] | None
    pad_mode: str
    pad_mask_path: str | None = None
    mode: str = "exact"
    engine_size: dict[str, Any] = field(default_factory=dict)
    synthetic: tuple[int, int, int, int] = (0, 0, 0, 0)
    note: str = ""
    quantise: str = "de00"

    def __post_init__(self) -> None:
        self.src = _pair(self.src)
        self.pad = _quad(self.pad)
        self.dst = _pair(self.dst)
        self.region = None if self.region is None else _quad(self.region)
        self.synthetic = _quad(self.synthetic)
        self.scale = float(self.scale)
        if self.pad_mode not in ("overscan", "edge_extend", "none"):
            raise ValueError(f"pad_mode {self.pad_mode!r}")
        if self.mode not in ("exact", "pad", "arbitrary"):
            raise ValueError(f"mode {self.mode!r}")
        if self.quantise not in QUANTISERS:
            raise ValueError(f"quantise {self.quantise!r}")
        if min(self.pad) < 0 or any(s > p for s, p in zip(self.synthetic, self.pad, strict=True)):
            raise ValueError("pad must be >= 0 and synthetic must lie inside pad")

    # -- geometry ---------------------------------------------------------------------------------------

    @property
    def content(self) -> tuple[int, int, int, int]:
        return self.region if self.region is not None else (0, 0, self.src[0], self.src[1])

    @property
    def content_size(self) -> tuple[int, int]:
        x0, y0, x1, y1 = self.content
        return x1 - x0, y1 - y0

    @property
    def padded_size(self) -> tuple[int, int]:
        cw, ch = self.content_size
        left, top, right, bottom = self.pad
        return cw + left + right, ch + top + bottom

    @property
    def padded_rect(self) -> tuple[int, int, int, int]:
        """The padded rectangle in plate coordinates (may extend past the frame by ``synthetic``)."""
        x0, y0, x1, y1 = self.content
        left, top, right, bottom = self.pad
        return x0 - left, y0 - top, x1 + right, y1 + bottom

    def pad_mask(self) -> np.ndarray:
        """uint8 (dst_h, dst_w): 255 where the sent pixel is edge-replicated, 0 where it is a plate pixel."""
        pw, ph = self.padded_size
        m = np.zeros((ph, pw), np.uint8)
        left, top, right, bottom = self.synthetic
        if top:
            m[:top] = 255
        if bottom:
            m[ph - bottom :] = 255
        if left:
            m[:, :left] = 255
        if right:
            m[:, pw - right :] = 255
        if (pw, ph) != self.dst:
            m = cv2.resize(m, self.dst, interpolation=cv2.INTER_NEAREST)
        return m

    # -- serialisation ----------------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "src": list(self.src),
            "pad": list(self.pad),
            "scale": self.scale,
            "dst": list(self.dst),
            "region": None if self.region is None else list(self.region),
            "pad_mode": self.pad_mode,
            "pad_mask_path": self.pad_mask_path,
            "mode": self.mode,
            "engine_size": dict(self.engine_size),
            "synthetic": list(self.synthetic),
            "note": self.note,
            "quantise": self.quantise,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> SizeMap:
        return cls(
            src=d["src"],
            pad=d["pad"],
            scale=d["scale"],
            dst=d["dst"],
            region=d.get("region"),
            pad_mode=d["pad_mode"],
            pad_mask_path=d.get("pad_mask_path"),
            mode=d.get("mode", "exact"),
            engine_size=dict(d.get("engine_size") or {}),
            synthetic=d.get("synthetic", (0, 0, 0, 0)),
            note=d.get("note", ""),
            quantise=d.get("quantise", "de00"),
        )


# --------------------------------------------------------------------------- helpers


def _as_display_float(plate: np.ndarray) -> np.ndarray:
    """(H, W, 3) float32 in [0, 1] from a uint8 / uint16 / float display image (gray and RGBA accepted)."""
    a = np.asarray(plate)
    if a.ndim == 2:
        a = a[..., None]
    if a.ndim != 3 or a.shape[2] not in (1, 3, 4):
        raise EgressError(f"plate must be (H, W, 3), got shape {a.shape}")
    if a.shape[2] == 1:
        a = np.repeat(a, 3, axis=2)
    a = a[..., :3]
    if a.dtype == np.uint8:
        return a.astype(np.float32) / 255.0
    if a.dtype == np.uint16:
        return a.astype(np.float32) / 65535.0
    if np.issubdtype(a.dtype, np.floating):
        f = a.astype(np.float32)
        if not np.isfinite(f).all():
            raise EgressError("plate has NaN or infinite pixels")
        if f.max(initial=0.0) > 1.0 + _FLOAT_HEADROOM or f.min(initial=0.0) < -_FLOAT_HEADROOM:
            raise EgressError(
                "plate floats leave [0, 1]: that is scene-linear data; egress takes the display master"
            )
        return np.clip(f, 0.0, 1.0)
    raise EgressError(f"unsupported plate dtype {a.dtype}")


def _quantise_block(v: np.ndarray) -> np.ndarray:
    """(N, 3) display floats in [0, 1] -> (N, 3) uint8 codes: of the 8 floor/ceil triples, the one nearest in
    CIEDE2000. The distance is ΔE00 linearised at the source colour (the steps are under one code, so first
    order is exact enough to rank the candidates): Lab' = diag(1, 1 + G, 1) Lab, then the S_L, S_C, S_H and
    R_T terms of Sharma/Wu/Dalal 2005 as a per-pixel quadratic form in (ΔL, Δa', Δb)."""
    c = v * 255.0
    # A channel that already sits on a code (8-bit-sourced 16-bit masters; float32 rounding leaves ~1e-5) has
    # floor == ceil: it is never traded a whole code to offset another channel.
    near = np.round(c)
    c = np.where(np.abs(c - near) <= _CODE_SNAP, near, c)
    lo = np.clip(np.floor(c), 0.0, 255.0)
    hi = np.clip(np.ceil(c), 0.0, 255.0)
    # d(Lab')/d(code) per channel: sRGB EOTF slope, then XYZ, then the Lab f(t) slope.
    knee = v <= 0.04045
    base = (v + 0.055) / 1.055
    lin = np.where(knee, v / 12.92, base**2.4)
    slope = np.where(knee, 1.0 / 12.92, (2.4 / 1.055) * base**1.4) / 255.0
    t = lin @ _MW.T
    cr = np.cbrt(t)
    small = t <= _LAB_D**3
    ft = np.where(small, t / (3 * _LAB_D * _LAB_D) + 4 / 29, cr)
    fslope = np.where(small, 1.0 / (3 * _LAB_D * _LAB_D), 1.0 / (3.0 * np.maximum(cr, 1e-12) ** 2))
    L = 116.0 * ft[:, 1] - 16.0
    a = 500.0 * (ft[:, 0] - ft[:, 1])
    b = 200.0 * (ft[:, 1] - ft[:, 2])
    c7 = np.hypot(a, b) ** 7
    g1 = 1.0 + 0.5 * (1.0 - np.sqrt(c7 / (c7 + 25.0**7)))
    ap = g1 * a
    cp = np.hypot(ap, b)
    jac = (_LAB_A[None] * fslope[:, None, :]) @ _MW * slope[:, None, :]
    jac[:, 1, :] *= g1[:, None]
    # Metric at the source colour; hue direction (ua, ub) is arbitrary for a neutral, where S_C = S_H = 1, R_T = 0.
    chromatic = cp > 1e-9
    denom = np.where(chromatic, cp, 1.0)
    ua = np.where(chromatic, ap / denom, 1.0)
    ub = np.where(chromatic, b / denom, 0.0)
    hp = np.degrees(np.arctan2(b, ap)) % 360.0
    tt = (
        1
        - 0.17 * np.cos(np.radians(hp - 30))
        + 0.24 * np.cos(np.radians(2 * hp))
        + 0.32 * np.cos(np.radians(3 * hp + 6))
        - 0.20 * np.cos(np.radians(4 * hp - 63))
    )
    sl = 1 + 0.015 * (L - 50) ** 2 / np.sqrt(20 + (L - 50) ** 2)
    sc = 1 + 0.045 * cp
    sh = 1 + 0.015 * cp * tt
    cp7 = cp**7
    rt = -np.sin(np.radians(60 * np.exp(-(((hp - 275) / 25) ** 2)))) * 2 * np.sqrt(cp7 / (cp7 + 25.0**7))
    ic2, ih2, r = 1 / sc**2, 1 / sh**2, rt / (sc * sh)
    uaa, ubb, uab = ua * ua, ub * ub, ua * ub
    q_ll = 1 / sl**2
    q_aa = uaa * ic2 + ubb * ih2 - r * uab
    q_bb = ubb * ic2 + uaa * ih2 + r * uab
    q_ab = 2 * uab * (ic2 - ih2) + r * (uaa - ubb)
    col_lo = jac * (lo - c)[:, None, :]  # column i: the Lab' step of taking the floor of channel i
    col_hi = jac * (hi - c)[:, None, :]
    best = np.full(v.shape[0], np.inf)
    pick = np.zeros(v.shape[0], np.int64)
    for k in range(8):  # bit i set = ceil channel i; k = 0 (all floors) wins ties, so exact codes stay put
        y = sum(col_hi[:, :, i] if (k >> i) & 1 else col_lo[:, :, i] for i in range(3))
        dl, da, db = y[:, 0], y[:, 1], y[:, 2]
        cost = q_ll * dl * dl + q_aa * da * da + q_bb * db * db + q_ab * da * db
        better = cost < best
        best = np.where(better, cost, best)
        pick = np.where(better, k, pick)
    ceil = ((pick[:, None] >> np.arange(3)[None, :]) & 1).astype(bool)
    return np.where(ceil, hi, lo).astype(np.uint8)


def quantise8(rgb: np.ndarray, method: str = "de00") -> np.ndarray:
    """uint8 (H, W, 3) from display floats in [0, 1] (or uint16, uint8). ``de00``: per pixel, the floor/ceil code
    triple nearest the source in CIEDE2000 (under one code from it on every channel; a channel already on a code
    keeps it); ``round``: each channel rounded on its own. Deterministic, no dither."""
    if method not in QUANTISERS:
        raise EgressError(f"quantise must be one of {QUANTISERS}, got {method!r}")
    a = _as_display_float(rgb).astype(np.float64)
    if method == "round":
        return np.round(a * 255.0).astype(np.uint8)
    flat = a.reshape(-1, 3)
    out = np.empty(flat.shape, np.uint8)
    for start in range(0, flat.shape[0], _QUANT_CHUNK):
        out[start : start + _QUANT_CHUNK] = _quantise_block(flat[start : start + _QUANT_CHUNK])
    return out.reshape(a.shape)


def encode_png8(rgb: np.ndarray, quantise: str = "de00") -> bytes:
    """8-bit RGB PNG bytes with the house sRGB ICC profile and nothing else (no text, time or EXIF chunks).
    A uint8 RGB array is written as is; anything deeper goes through ``quantise8``."""
    a = np.asarray(rgb)
    is_rgb8 = a.dtype == np.uint8 and a.ndim == 3 and a.shape[2] == 3
    rgb8 = a if is_rgb8 else quantise8(a, quantise)
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(rgb8[..., ::-1]))
    if not ok:
        raise EgressError("cv2 could not encode the egress PNG")
    data = _icc.strip_metadata_png(bytes(buf))
    return _icc.embed_png_icc(data, _icc.SRGB_ICC, name="sRGB")


def region_box(
    mask: np.ndarray, frame: tuple[int, int] | None = None, context: float = REGION_CONTEXT
) -> tuple[int, int, int, int]:
    """Bounding box (x0, y0, x1, y1), half-open, of the mask grown by ``context`` x its size on each side and
    clamped to the frame (W, H). Raises EgressError for an empty mask."""
    m = np.asarray(mask)
    if m.ndim == 3:
        m = m[..., 0]
    if m.ndim != 2:
        raise EgressError(f"mask must be (H, W), got shape {m.shape}")
    H, W = m.shape
    if frame is not None and tuple(frame) != (W, H):
        raise EgressError(f"mask is {W} x {H}, frame is {frame[0]} x {frame[1]}")
    ys, xs = np.nonzero(m > (0.5 if np.issubdtype(m.dtype, np.floating) else 0))
    if xs.size == 0:
        raise EgressError("region crop needs a non-empty mask")
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    gx = int(round(context * (x1 - x0)))
    gy = int(round(context * (y1 - y0)))
    return max(0, x0 - gx), max(0, y0 - gy), min(W, x1 + gx), min(H, y1 + gy)


def _split_pad(total: int, avail_lo: int, avail_hi: int) -> tuple[int, int, int, int]:
    """Split ``total`` pad pixels across the low and high sides, preferring real context over replication.

    Returns (lo, hi, synthetic_lo, synthetic_hi). Centred when both sides have room (or neither does).
    """
    lo = total // 2
    hi = total - lo
    if lo > avail_lo:
        hi, lo = hi + (lo - avail_lo), avail_lo
    if hi > avail_hi:
        lo, hi = lo + (hi - avail_hi), avail_hi
    if lo > avail_lo:  # neither side has room for all of it: replicate the remainder, centred
        spill = (lo - avail_lo) + (hi - avail_hi if hi > avail_hi else 0)
        lo, hi = avail_lo + spill // 2, avail_hi + (spill - spill // 2)
    return lo, hi, max(0, lo - avail_lo), max(0, hi - avail_hi)


def _extract(plate: np.ndarray, rect: tuple[int, int, int, int]) -> np.ndarray:
    """plate[rect] where rect may run past the frame; the outside is edge-replicated (never reflected)."""
    H, W = plate.shape[:2]
    x0, y0, x1, y1 = rect
    ix0, iy0, ix1, iy1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    inner = plate[iy0:iy1, ix0:ix1]
    top, bottom, left, right = iy0 - y0, y1 - iy1, ix0 - x0, x1 - ix1
    if top or bottom or left or right:
        inner = cv2.copyMakeBorder(inner, top, bottom, left, right, cv2.BORDER_REPLICATE)
    return np.ascontiguousarray(inner)


def _manifest_frame(manifest: Mapping[str, Any] | None, w: int, h: int) -> tuple[str | None, bool]:
    """(tier, has_overscan). Refuses a plate whose size is not the packet's rendered resolution."""
    if not manifest:
        return None, False
    cam = manifest.get("camera") or {}
    res = cam.get("resolution") or {}
    if res and (int(res.get("width", w)), int(res.get("height", h))) != (w, h):
        raise EgressError(
            f"plate is {w} x {h} but the packet rendered {res.get('width')} x {res.get('height')}: "
            "egress takes the packet's own master at its rendered resolution"
        )
    return manifest.get("tier"), bool(cam.get("overscan"))


def _write_pad_mask(sm: SizeMap, out_dir: Path) -> Path:
    digest = hashlib.sha256(sm.to_json().encode("utf-8")).hexdigest()[:16]
    path = Path(out_dir) / f"padmask_{digest}.png"
    ok, buf = cv2.imencode(".png", sm.pad_mask())
    if not ok:
        raise EgressError("cv2 could not encode the pad mask")
    with atomic_target(path) as part:
        part.write_bytes(_icc.strip_metadata_png(bytes(buf)))
    return path


# --------------------------------------------------------------------------- prepare


def prepare(
    plate_rgb16: np.ndarray,
    manifest: Mapping[str, Any] | None,
    engine_manifest: Any,
    mask: np.ndarray | None = None,
    region_crop: bool = False,
    *,
    pad_mask_dir: str | Path | None = None,
    allow_unverified_sizes: bool = False,
    quantise: str = "de00",
) -> tuple[bytes, SizeMap]:
    """Return ``(png_bytes, SizeMap)`` for sending ``plate_rgb16`` (H, W, 3) to the engine.

    ``manifest`` is the packet manifest (dict) or None; ``engine_manifest`` anything ``legal_sizes`` reads.
    ``mask`` (H, W) is the editable mask; with ``region_crop=True`` only its context box is sent.
    ``pad_mask_dir``: when given and pixels were replicated, the pad mask PNG is written there and its path
    recorded on the SizeMap (``SizeMap.pad_mask()`` rebuilds it either way). ``quantise``: how the 16-bit
    pixels become 8-bit codes (``quantise8``; recorded on the SizeMap).
    """
    if quantise not in QUANTISERS:
        raise EgressError(f"quantise must be one of {QUANTISERS}, got {quantise!r}")
    plate = _as_display_float(plate_rgb16)
    H, W = plate.shape[:2]
    tier, has_overscan = _manifest_frame(manifest, W, H)
    legal = legal_sizes(engine_manifest, allow_unverified=allow_unverified_sizes)

    if region_crop:
        if mask is None:
            raise EgressError("region_crop needs the editable mask")
        region: tuple[int, int, int, int] | None = region_box(mask, (W, H))
    else:
        if mask is not None and np.asarray(mask).shape[:2] != (H, W):
            raise EgressError(f"mask shape {np.asarray(mask).shape[:2]} does not match the plate {(H, W)}")
        region = None
    x0, y0, x1, y1 = region if region is not None else (0, 0, W, H)
    cw, ch = x1 - x0, y1 - y0

    size, mode = pick_size(cw, ch, legal)
    if mode == "pad":
        pw, ph = pad_dims(cw, ch, size.aspect)
    else:
        pw, ph = cw, ch
    # pick_size only sizes from the arbitrary envelope in mode "arbitrary", or "pad" when there are no presets.
    from_envelope = mode == "arbitrary" or (mode == "pad" and len(legal) == 0)
    if from_envelope and size.width >= pw and size.height >= ph:
        # An arbitrary-size engine with a minimum side: pad up to it (real context first), never upsample.
        pw, ph = size.width, size.height

    left, right, syn_l, syn_r = _split_pad(pw - cw, x0, W - x1)
    top, bottom, syn_t, syn_b = _split_pad(ph - ch, y0, H - y1)

    note = ""
    if size.width <= pw and size.height <= ph:
        dst = (size.width, size.height)
    else:
        dst = (pw, ph)
        note = "native_below_preset"
    if tier == "A" and has_overscan and region is None and mode != "exact":
        note = (note + ";" if note else "") + "overscan_aspect_not_legal_for_engine"
    scale = dst[0] / pw

    rect = (x0 - left, y0 - top, x1 + right, y1 + bottom)
    sent = _extract(plate, rect)
    if dst != (pw, ph):
        if dst[0] > pw or dst[1] > ph:  # unreachable by construction; the doctrine check stays explicit
            raise EgressError("egress would upsample the plate")
        sent = cv2.resize(sent, dst, interpolation=cv2.INTER_AREA)

    synthetic = (syn_l, syn_t, syn_r, syn_b)
    if any(synthetic):
        pad_mode = "edge_extend"
    elif (tier == "A" and has_overscan) or region is not None:
        pad_mode = "overscan"
    else:
        pad_mode = "none"

    sm = SizeMap(
        src=(W, H),
        pad=(left, top, right, bottom),
        scale=scale,
        dst=dst,
        region=region,
        pad_mode=pad_mode,
        mode=mode,
        engine_size=size.to_dict(),
        synthetic=synthetic,
        note=note,
        quantise=quantise,
    )
    if pad_mask_dir is not None and any(synthetic):
        sm.pad_mask_path = str(_write_pad_mask(sm, Path(pad_mask_dir)))
    return encode_png8(sent, quantise), sm


def engine_size_of(sizemap: SizeMap) -> EngineSize:
    """The EngineSize recorded on a SizeMap (what the adapter requests from the vendor)."""
    return EngineSize.from_dict(sizemap.engine_size)
