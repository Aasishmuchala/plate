"""The Atmosphere node: Koschmieder depth fog from the packet's own Z (decision 48, router plan T93).

    prepare    Tier A only (Tier C, or a packet without a zdepth element: 422 `no_z`). Params: `visibility_m`
               (default 800, 20..20000), `colour` (`[r, g, b]` linear, or null: the mean linear colour of the
               lowest 10 % band of the SKY-class pixels, else [0.75, 0.78, 0.82]), `sky_fog` (default 0.6, 0..1),
               `height_falloff_m` (null: off).
    normalize  beta = 3.912 / visibility_m (the distance at which contrast falls to 2 %), per pixel
               f = 1 - exp(-beta Z_m) where Z is finite, > 0 and not the renderer background, and f = sky_fog
               where it is not or the pixel is a SKY id (blended by the SKY ids' Cryptomatte coverage, so the
               anti-aliased skyline does not step). With `height_falloff_m` and a `world_position` element, beta
               is multiplied by exp(-max(z_world - z_cam, 0) / height_falloff_m): fog thins above the camera
               (without the element the falloff is off and the report says so). In linear light (srgb_eotf of the
               display master): out = lerp(lin, colour, f), back through srgb_oetf to a PNG16.
    gate       the still gate on the whole frame (any pixel may change): G-buffer structure hard (fog must not wash
               out the architecture's edges), `material.*` advisory, texture and tone advisory (fog lowers contrast
               by design), invention flags advisory (fog is a per-pixel function of the plate and Z: it can lift a
               plate edge over the detector's threshold but cannot draw geometry).

Z is planar depth in scene units (`units.meters_per_unit` makes it metres), the quantity the packet carries; at
the angles of a still the difference from ray distance is under the slider's resolution.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..pipeline import PipelineError
from .common import Prepared, QuoteContext, RunContext, finite_or_none, pick, plain
from .comp_node import put_mask, still_gates
from .comp_node import score as _score

KIND = "Atmosphere"
KOSCHMIEDER = 3.912  # -ln(0.02): the contrast threshold of the meteorological visibility
DEFAULT_VISIBILITY_M = 800.0
VISIBILITY_RANGE = (20.0, 20000.0)
DEFAULT_SKY_FOG = 0.6
DEFAULT_COLOUR = (0.75, 0.78, 0.82)
SKY_BAND = 0.10  # the lowest share of the sky's pixels whose mean colour is the default fog colour
MIN_SKY_PX = 16


def sky_alpha(pv: Any) -> np.ndarray:
    """Summed object-Cryptomatte coverage of the legend's SKY-class ids, float32 (H, W) in [0, 1]; zeros when
    the packet has no Cryptomatte or no sky id."""
    from plate_gates.material_de import id_to_u32

    pair = pv.crypto("object")
    shape = pv.plate16().shape[:2]
    if pair is None:
        return np.zeros(shape, dtype=np.float32)
    ranks, cov = pair
    ids = sky_ids(pv)
    alpha = np.zeros(shape, dtype=np.float64)
    for cid in ids:
        alpha += np.where(ranks == np.uint32(id_to_u32(cid)), cov, 0.0).sum(axis=2)
    return np.clip(alpha, 0.0, 1.0).astype(np.float32)


def sky_ids(pv: Any) -> list[str]:
    """The object ids whose legend class is `sky`."""
    legend = pv.legend if isinstance(pv.legend, dict) else {}
    entries = legend.get("entries") or []
    return [
        str(e["id"]).lower()
        for e in entries
        if str(e.get("class", "")).lower() == "sky" and str(e.get("id", "")).lower().startswith("o:")
    ]


def world_up(camera: Any) -> np.ndarray:
    """The world's up axis as a unit vector, from the camera convention (`world_up`, default +Z)."""
    text = str(((camera or {}).get("convention") or {}).get("world_up") or "+Z").strip().upper()
    sign = -1.0 if text.startswith("-") else 1.0
    axis = {"X": 0, "Y": 1, "Z": 2}.get(text.lstrip("+-"), 2)
    up = np.zeros(3)
    up[axis] = sign
    return up


def fog_fraction(
    z_m: np.ndarray,
    beta: float | np.ndarray,
    sky_fog: float,
    sky: np.ndarray | None = None,
    valid: np.ndarray | None = None,
) -> np.ndarray:
    """f per pixel: 1 - exp(-beta Z) on usable depth, `sky_fog` elsewhere, blended to `sky_fog` by sky coverage."""
    z = np.asarray(z_m, dtype=np.float64)
    ok = (
        np.isfinite(z) & (z > 0)
        if valid is None
        else (np.asarray(valid, dtype=bool) & np.isfinite(z) & (z > 0))
    )
    f = np.where(ok, 1.0 - np.exp(-np.asarray(beta, dtype=np.float64) * np.where(ok, z, 0.0)), sky_fog)
    if sky is not None:
        a = np.clip(np.asarray(sky, dtype=np.float64), 0.0, 1.0)
        f = f * (1.0 - a) + sky_fog * a
    return f


def apply_fog(lin: np.ndarray, f: np.ndarray, colour: Any) -> np.ndarray:
    """lerp(lin, colour, f) in linear light."""
    c = np.asarray(colour, dtype=np.float64).reshape(1, 1, 3)
    lin = np.asarray(lin, dtype=np.float64)
    return lin + (c - lin) * np.asarray(f, dtype=np.float64)[..., None]


def fog16(src16: np.ndarray, f: np.ndarray, colour: Any) -> np.ndarray:
    from plate_pixels.ocio import srgb_eotf, srgb_oetf

    lin = srgb_eotf(np.asarray(src16, dtype=np.float64) / 65535.0)
    return np.clip(np.rint(srgb_oetf(apply_fog(lin, f, colour)) * 65535.0), 0, 65535).astype(np.uint16)


def horizon_colour(plate16: np.ndarray, alpha: np.ndarray) -> tuple[list[float], str]:
    """(linear colour, source): the mean of the lowest 10 % of the sky's pixels (nearest the horizon)."""
    from plate_pixels.ocio import srgb_eotf

    sky = alpha >= 0.5
    if int(sky.sum()) < MIN_SKY_PX:
        return list(DEFAULT_COLOUR), "default"
    ys = np.nonzero(sky)[0]
    cut = np.quantile(ys, 1.0 - SKY_BAND)
    band = sky & (np.arange(sky.shape[0])[:, None] >= cut)
    lin = srgb_eotf(plate16[band].astype(np.float64) / 65535.0)
    return [float(v) for v in lin.mean(axis=0)], "sky_band"


def prepare(q: QuoteContext) -> Prepared:
    packet_id = q.plate_packet_id()
    pv = q.packet(packet_id)
    elements = {e.get("element") for e in ((pv.files.get("beauty_exr") or {}).get("elements") or [])}
    if pv.tier != "A" or "zdepth" not in elements:
        raise PipelineError(
            f"packet {packet_id} is tier {pv.tier} with no Z: Atmosphere needs a Tier A packet's zdepth",
            code="no_z",
        )
    vis = finite_or_none(
        pick(q.params, "visibility_m", "visibilityM", "visibility", default=DEFAULT_VISIBILITY_M)
    )
    if vis is None or not VISIBILITY_RANGE[0] <= vis <= VISIBILITY_RANGE[1]:
        raise PipelineError(
            f"Atmosphere params.visibility_m is {VISIBILITY_RANGE[0]:g}..{VISIBILITY_RANGE[1]:g} metres",
            code="bad_params",
        )
    raw_colour = pick(q.params, "colour", "color")
    colour = None
    if raw_colour is not None:
        if not isinstance(raw_colour, (list, tuple)) or len(raw_colour) != 3:
            raise PipelineError(
                "Atmosphere params.colour is [r, g, b] in linear light, or null", code="bad_params"
            )
        colour = [finite_or_none(v) for v in raw_colour]
        if any(v is None or v < 0 or v > 1 for v in colour):
            raise PipelineError(
                "Atmosphere params.colour channels are linear values in 0..1", code="bad_params"
            )
    sky_fog = finite_or_none(pick(q.params, "sky_fog", "skyFog", default=DEFAULT_SKY_FOG))
    if sky_fog is None or not 0.0 <= sky_fog <= 1.0:
        raise PipelineError("Atmosphere params.sky_fog is 0..1", code="bad_params")
    raw_falloff = pick(q.params, "height_falloff_m", "heightFalloffM")
    falloff = None
    if raw_falloff is not None:
        falloff = finite_or_none(raw_falloff)
        if falloff is None or falloff <= 0:
            raise PipelineError(
                "Atmosphere params.height_falloff_m is a positive number of metres, or null",
                code="bad_params",
            )
    spec = {
        "packet": packet_id,
        "plate": pv.plate_hash,
        "exr": pv.files["beauty_exr"]["hash"]["hex"],
        "visibility_m": vis,
        "colour": colour,
        "sky_fog": sky_fog,
        "height_falloff_m": falloff,
    }
    return Prepared(spec, packet_id)


def compute(c: RunContext) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """(out16, f, info) for the spec; nothing stored."""
    spec = c.spec
    pv = c.packet()
    view = c.runner.view(pv)
    plate16 = pv.plate16()
    mpu = float((pv.manifest.get("units") or {}).get("meters_per_unit") or 1.0)
    z_m = view.z.astype(np.float64) * mpu
    valid = view.valid()
    alpha = sky_alpha(pv)
    beta: float | np.ndarray = KOSCHMIEDER / spec["visibility_m"]
    notes: list[str] = []
    falloff_applied = False
    if spec.get("height_falloff_m"):
        if view.world_position is not None:
            up = world_up(pv.manifest["camera"])
            cam = np.asarray(pv.manifest["camera"]["world_from_camera"], dtype=np.float64)[:3, 3]
            height = view.world_position[..., :3].astype(np.float64) @ up - float(cam @ up)
            above = np.maximum(height * mpu, 0.0)
            beta = beta * np.exp(-above / float(spec["height_falloff_m"]))
            falloff_applied = True
        else:
            notes.append("height falloff: the packet has no world_position element, so the fog is uniform")
    f = fog_fraction(z_m, beta, spec["sky_fog"], alpha, valid)
    if spec.get("colour") is not None:
        colour, colour_source = list(spec["colour"]), "params"
    else:
        colour, colour_source = horizon_colour(plate16, alpha)
    out16 = fog16(plate16, f, colour)
    info = {
        "visibility_m": spec["visibility_m"],
        "beta_per_m": KOSCHMIEDER / spec["visibility_m"],
        "colour": colour,
        "colour_source": colour_source,
        "sky_fog": spec["sky_fog"],
        "height_falloff_m": spec.get("height_falloff_m"),
        "height_falloff_applied": falloff_applied,
        "sky_px": int((alpha > 0).sum()),
        "f": {
            "min": float(f.min()),
            "median": float(np.median(f)),
            "max": float(f.max()),
        },
        "notes": notes,
    }
    return out16, f, info


def normalize(c: RunContext) -> dict[str, Any]:
    out16, _f, info = compute(c)
    return {
        "outputs": [c.put_png16(out16)],
        "atmosphere": info,
        "mask_key": put_mask(c, np.ones(out16.shape[:2], dtype=bool)),
        "mask_px": int(out16.shape[0] * out16.shape[1]),
    }


def preview(c: RunContext) -> dict[str, Any]:
    _out16, f, info = compute(c)
    return plain({**info, "f_p90": float(np.quantile(f, 0.9))})


def gate(c: RunContext) -> Any:
    norm = c.normalized
    pv = c.packet()
    out16 = c.read_png16(norm["outputs"][0])
    info = norm.get("atmosphere") or {}
    return still_gates(
        c,
        pv,
        out16,
        None,
        advisory=("material.", "texture.", "tone.", "invention_flags"),
        notes=[
            "atmosphere: the whole frame may change; material dE00, texture and tone are advisory (fog moves "
            "them by design), G-buffer structure stays hard",
            "atmosphere: invention flags are scored against the plate and advisory: fog is a per-pixel function "
            "of the plate and its Z, so a flag is a plate edge the lift carried over the detector's threshold",
            *(info.get("notes") or []),
        ],
        metrics={"atmosphere": info},
        material_advisory=True,
    )


score = _score

__all__ = [
    "KIND",
    "KOSCHMIEDER",
    "apply_fog",
    "fog16",
    "fog_fraction",
    "gate",
    "horizon_colour",
    "normalize",
    "prepare",
    "preview",
    "score",
    "sky_alpha",
    "sky_ids",
    "world_up",
]
