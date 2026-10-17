"""Recolour by element recomposition (RAL / swatch): change one material's albedo with the render's own passes.

    apply(elements, mask, target_lab, legend_entry=None, ...) -> {rgb_linear, method, expected_de00, ...}

Model (V-Ray, linear light; the Blender fixtures repack Cycles passes to the same semantics):

    beauty = diffuse_filter * raw_lighting + diffuse_filter * raw_gi
           + reflection + refraction + specular + self_illumination

`diffuse_filter` is the material's diffuse colour as the renderer actually applies it: the nominal albedo `rho`
dimmed by the energy-conserving Fresnel term of the reflective layer (`df = rho * (1 - F)`; a chrome VRayMtl has
a black diffuse filter, and the fixture panels measure `df / rho = 0.952` for Blender's F0 of 0.048). The raw
lighting and raw GI passes are pre-albedo, so the diffuse term is `df * (raw_lighting + raw_gi)` and everything
else in the beauty is kept as it is.

Recolouring material m to a target albedo `rho'` (linear RGB from the RAL Lab through D65 sRGB) inside m's mask,
with `r = rho' / rho` per channel:

    recomposition   beauty' = beauty + (df' - df) * (raw_lighting + raw_gi),  df' = df * r
                    needs diffuse_filter, raw_lighting, raw_gi; reflections untouched; expected dE00 <= 1.0
    ratio           beauty' = beauty + D * (r - 1),  D = beauty - (reflection + refraction + specular + self_illumination)
                    raw passes absent, at least one non-diffuse element present; expected dE00 <= 2.5
    degraded        beauty' = beauty * r   (the reflections get tinted too)
                    nothing but the beauty (and the diffuse filter for the source estimate); `degraded: true`,
                    expected dE00 <= 4.0

`df' = df * r` rather than the bare `rho'` of the planning note: scaling keeps the per-pixel dimming and any
texture modulation of the diffuse filter, while substituting `rho'` makes the panel `1 / (1 - F)` (about 5%)
too bright (measured 0.57 against 0.18 dE00 on the fixture, see docs/gates/recolour.md).

The source albedo `rho` is, in order: `source_lab` (the swatch the material carries today), the legend entry's
`albedo_lab` / `albedo_srgb` / `ral`, or an estimate from the diffuse filter inside the mask: the per-channel
median of `df / (1 - F)` with Schlick's F at `f0` (0.04, a plain dielectric) using the camera-space normals and
the intrinsics when both are given, else a constant `1 - f0`. Reflection passes are kept as they are (tinted
reflections of coloured metals are out of scope); `metal: true` flags a specular (or reflection) energy above
30% of the beauty inside the mask.

Masks are the Cryptomatte coverage (float 0..1, soft edges) or a boolean; pixels of weight 0 are copied from
the beauty bit for bit. Display values come from the packet's OCIO pipeline (plate_pixels.ocio, T3) and the
re-render harness `validate_against_rerender` scores dE00 in display space with the per-material convention
of material_de (sigma-blurred inside the mask, so Monte-Carlo noise is not counted as a colour error) next to
the raw per-pixel numbers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .color import ciede2000, hex_to_rgb8, lab_to_srgb, srgb_to_lab
from .gbuffer import intrinsics
from .masks import erode

__all__ = [
    "EXPECTED_DE00",
    "F0_DEFAULT",
    "METAL_SHARE",
    "NON_DIFFUSE",
    "RAL_SRGB",
    "RecolourError",
    "apply",
    "compare_display",
    "estimate_source_albedo",
    "hex_lab",
    "lab_to_linear",
    "linear_to_lab",
    "load_packet",
    "ral_lab",
    "to_display",
    "validate_against_rerender",
]

METHODS = ("recomposition", "ratio", "degraded")
EXPECTED_DE00: dict[str, float] = {"recomposition": 1.0, "ratio": 2.5, "degraded": 4.0}
METAL_SHARE = 0.3
F0_DEFAULT = 0.04
NON_DIFFUSE = ("reflection", "refraction", "specular", "self_illumination")
ALBEDO_FLOOR = 1e-4  # a black source albedo has no colour to scale; the ratio is capped by this floor
MIN_ESTIMATE_PX = 64

# Published sRGB approximations of common archviz RAL codes (the official RAL Lab values are licensed; a job
# enters the swatch's measured Lab or the hex the material was built from). 7016 and 3020 are the fixture panels.
RAL_SRGB: dict[str, str] = {
    "1015": "#E6D2B5",
    "1023": "#F7B500",
    "3000": "#A72920",
    "3020": "#CC0605",
    "5002": "#00387B",
    "6005": "#114232",
    "6018": "#57A639",
    "7016": "#383E42",
    "7021": "#2F3234",
    "7024": "#45494E",
    "7035": "#CBD0CC",
    "7037": "#7A7B7A",
    "9005": "#0E0E10",
    "9010": "#F1ECE1",
    "9016": "#F1F0EA",
}


class RecolourError(RuntimeError):
    """The elements at hand cannot support the requested recolour (or the harness inputs do not match)."""


# --------------------------------------------------------------------------- colour helpers


def _srgb_oetf(lin: np.ndarray) -> np.ndarray:
    v = np.clip(np.asarray(lin, dtype=np.float64), 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1.0 / 2.4) - 0.055)


def _srgb_eotf(enc: np.ndarray) -> np.ndarray:
    v = np.clip(np.asarray(enc, dtype=np.float64), 0.0, 1.0)
    return np.where(v <= 0.04045, v / 12.92, np.power((v + 0.055) / 1.055, 2.4))


def lab_to_linear(lab: Sequence[float] | np.ndarray) -> np.ndarray:
    """CIE Lab D65 (..., 3) -> linear Rec.709/sRGB RGB (..., 3) float64 in [0, 1] (the albedo the renderer uses)."""
    return _srgb_eotf(lab_to_srgb(np.asarray(lab, dtype=np.float64)) / 255.0)


def linear_to_lab(lin: Sequence[float] | np.ndarray) -> np.ndarray:
    """Linear Rec.709/sRGB RGB (..., 3) -> CIE Lab D65 (..., 3) float64."""
    return srgb_to_lab(_srgb_oetf(np.asarray(lin, dtype=np.float64)) * 255.0)


def hex_lab(hex_srgb: str) -> np.ndarray:
    """'#RRGGBB' (sRGB) -> Lab (3,)."""
    return srgb_to_lab(hex_to_rgb8(hex_srgb))[0]


def ral_lab(code: str | int) -> np.ndarray:
    """RAL classic code ('7016', 'RAL 7016', 7016) -> Lab (3,) from RAL_SRGB; KeyError for an unknown code."""
    key = str(code).upper().replace("RAL", "").strip()
    try:
        return hex_lab(RAL_SRGB[key])
    except KeyError:
        raise KeyError(f"RAL {key} is not in the built-in table; pass a measured Lab or hex") from None


# --------------------------------------------------------------------------- inputs


def _rgb3(a: np.ndarray, name: str) -> np.ndarray:
    a = np.asarray(a, dtype=np.float32)
    if a.ndim != 3 or a.shape[2] < 3:
        raise ValueError(f"element {name!r} must be (H, W, >=3), got {a.shape}")
    return a[..., :3]


def _weight(mask: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Mask -> float32 weight (H, W) in [0, 1]: a boolean mask is 0/1, a float mask is a coverage."""
    m = np.asarray(mask)
    if m.ndim == 3 and m.shape[2] == 1:
        m = m[..., 0]
    if m.shape != shape:
        raise ValueError(f"mask shape {m.shape} does not match the image {shape}")
    if m.dtype == bool:
        return m.astype(np.float32)
    return np.clip(m.astype(np.float32), 0.0, 1.0)


def _view_cos(normals: np.ndarray, K: Any, shape: tuple[int, int]) -> np.ndarray | None:  # noqa: N803
    """cos of the angle between the camera-space normal and the direction to the camera (plate-cam-v0: pixel
    centres at +0.5, camera looks down -Z, +Y up); None when K or the normals are unusable."""
    k = intrinsics(K)
    if k is None:
        return None
    n = np.asarray(normals, dtype=np.float64)
    if n.ndim != 3 or n.shape[2] != 3 or n.shape[:2] != shape:
        return None
    fx, fy, cx, cy = k
    h, w = shape
    ys, xs = np.mgrid[0:h, 0:w]
    d = np.stack([(xs + 0.5 - cx) / fx, -(ys + 0.5 - cy) / fy, -np.ones((h, w))], axis=-1)
    d /= np.linalg.norm(d, axis=-1, keepdims=True)
    n = n / np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-6)
    return np.clip(-(n * d).sum(axis=-1), 0.0, 1.0)


def _legend_albedo(entry: Mapping[str, Any] | None) -> tuple[np.ndarray | None, str | None]:
    """A source albedo the legend entry carries: albedo_lab, albedo_srgb / base_srgb (hex) or ral."""
    if not entry:
        return None, None
    if entry.get("albedo_lab") is not None:
        return lab_to_linear(entry["albedo_lab"]), "legend:albedo_lab"
    for key in ("albedo_srgb", "base_srgb"):
        if entry.get(key):
            return lab_to_linear(hex_lab(str(entry[key]))), f"legend:{key}"
    if entry.get("ral"):
        return lab_to_linear(ral_lab(entry["ral"])), "legend:ral"
    return None, None


def estimate_source_albedo(
    diffuse_filter: np.ndarray,
    weight: np.ndarray,
    *,
    normals: np.ndarray | None = None,
    K: Any = None,  # noqa: N803
    f0: float | None = F0_DEFAULT,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Nominal albedo of the material under `weight` from its diffuse filter: the per-channel median of
    `df / (1 - F)` over the core pixels (weight >= 0.5, eroded 2 px when enough remain). F is Schlick's Fresnel at
    `f0` with the per-pixel view angle when `normals` and `K` are given, else the constant `f0`; `f0=None`
    returns the plain median (no Fresnel correction). Returns (rho (3,), info)."""
    df = _rgb3(diffuse_filter, "diffuse_filter")
    core = weight >= 0.5
    if int(core.sum()) >= 4 * MIN_ESTIMATE_PX:
        eroded = erode(core, 2)
        if int(eroded.sum()) >= MIN_ESTIMATE_PX:
            core = eroded
    n_px = int(core.sum())
    if n_px == 0:
        raise RecolourError("the mask selects no pixel to estimate the source albedo from")
    info: dict[str, Any] = {"n_px": n_px, "fresnel_f0": f0, "fresnel": "none"}
    if f0 is None:
        dim = np.ones(df.shape[:2], dtype=np.float64)
    else:
        cos = _view_cos(normals, K, df.shape[:2]) if normals is not None else None
        if cos is None:
            dim = np.full(df.shape[:2], 1.0 - f0, dtype=np.float64)
            info["fresnel"] = "constant"
        else:
            fres = f0 + (1.0 - f0) * (1.0 - cos) ** 5
            dim = np.maximum(1.0 - fres, 1e-3)
            info["fresnel"] = "schlick_per_pixel"
            info["cos_theta_median"] = float(np.median(cos[core]))
    rho = np.median(df[core].astype(np.float64) / dim[core][:, None], axis=0)
    info["rho"] = [float(v) for v in rho]
    return rho, info


# --------------------------------------------------------------------------- the recolour


def _pick_method(elements: Mapping[str, np.ndarray], method: str | None) -> tuple[str, list[str]]:
    have = set(elements)
    non_diffuse = [e for e in NON_DIFFUSE if e in have]
    can_recompose = {"diffuse_filter", "raw_lighting", "raw_gi"} <= have
    can_ratio = bool(non_diffuse)
    if method in (None, "auto"):
        if can_recompose:
            return "recomposition", ["beauty", "diffuse_filter", "raw_lighting", "raw_gi"]
        if can_ratio:
            return "ratio", ["beauty", *non_diffuse]
        return "degraded", ["beauty"]
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS} or 'auto', not {method!r}")
    if method == "recomposition":
        if not can_recompose:
            raise RecolourError("recomposition needs diffuse_filter, raw_lighting and raw_gi")
        return method, ["beauty", "diffuse_filter", "raw_lighting", "raw_gi"]
    if method == "ratio":
        if not can_ratio:
            raise RecolourError(f"ratio needs at least one of {NON_DIFFUSE}")
        return method, ["beauty", *non_diffuse]
    return "degraded", ["beauty"]


def apply(
    elements: Mapping[str, np.ndarray],
    mask: np.ndarray,
    target_lab: Sequence[float] | np.ndarray,
    legend_entry: Mapping[str, Any] | None = None,
    *,
    source_lab: Sequence[float] | np.ndarray | None = None,
    method: str | None = None,
    K: Any = None,  # noqa: N803
    f0: float | None = F0_DEFAULT,
    metal_share: float = METAL_SHARE,
) -> dict[str, Any]:
    """Recolour the material under `mask` to `target_lab`; see the module docstring for the three methods.

    `elements` is what plate_pixels.exr.read_elements returns (keys beauty, diffuse_filter, raw_lighting, raw_gi,
    reflection, ..., normals; linear float32 (H, W, C)). `mask` is a bool mask or a float coverage (H, W).
    `method` forces a path ('recomposition' | 'ratio' | 'degraded'; None/'auto' picks the best the elements allow).
    `K` (manifest camera block or 3x3) enables the per-pixel Fresnel view angle for the source estimate.

    Returns a dict: rgb_linear (H, W, 3) float32 (pixels of weight 0 are the beauty bit for bit), method,
    expected_de00, degraded, metal, specular_share, source_albedo_linear, source_albedo_origin,
    target_albedo_linear, target_lab, ratio, n_px, elements_used, fresnel, notes, legend (id/name/material).
    """
    if "beauty" not in elements:
        raise RecolourError("elements has no 'beauty'")
    beauty = _rgb3(elements["beauty"], "beauty")
    shape = beauty.shape[:2]
    weight = _weight(mask, shape)
    sel = weight > 0.0
    n_px = int(sel.sum())
    notes: list[str] = []
    if n_px == 0:
        notes.append("empty mask: nothing recoloured")

    chosen, used = _pick_method(elements, method)
    for name in used:
        if name != "beauty":
            _rgb3(elements[name], name)
            if elements[name].shape[:2] != shape:
                raise ValueError(
                    f"element {name!r} shape {elements[name].shape[:2]} differs from beauty {shape}"
                )

    # ----- source albedo
    origin: str
    fresnel: dict[str, Any] = {"fresnel": "n/a", "fresnel_f0": None}
    if source_lab is not None:
        rho = lab_to_linear(source_lab)
        origin = "given"
    else:
        rho_legend, origin_legend = _legend_albedo(legend_entry)
        if rho_legend is not None:
            rho, origin = rho_legend, str(origin_legend)
        elif "diffuse_filter" in elements and n_px:
            rho, fresnel = estimate_source_albedo(
                elements["diffuse_filter"], weight, normals=elements.get("normals"), K=K, f0=f0
            )
            origin = "estimated"
        elif n_px == 0:
            rho, origin = np.ones(3), "none"
        else:
            raise RecolourError(
                "no source albedo: pass source_lab, a legend albedo, or a diffuse_filter element"
            )
    target = lab_to_linear(target_lab)
    if np.any(rho < ALBEDO_FLOOR):
        notes.append(f"source albedo below {ALBEDO_FLOOR} in a channel: ratio capped by the floor")
    ratio = (target / np.maximum(rho, ALBEDO_FLOOR)).astype(np.float32)

    # ----- the diffuse share to rescale
    if chosen == "recomposition":
        df = _rgb3(elements["diffuse_filter"], "diffuse_filter")
        light = _rgb3(elements["raw_lighting"], "raw_lighting") + _rgb3(elements["raw_gi"], "raw_gi")
        delta = df * (ratio - 1.0) * light
    elif chosen == "ratio":
        share = beauty.copy()
        for name in used[1:]:
            share -= _rgb3(elements[name], name)
        delta = np.maximum(share, 0.0) * (ratio - 1.0)
        notes.append("ratio: raw lighting absent; the diffuse share is beauty minus " + ", ".join(used[1:]))
    else:
        delta = beauty * (ratio - 1.0)
        notes.append(
            "degraded: no raw lighting and no reflection element; reflections are tinted with the albedo"
        )

    out = beauty.copy()
    if n_px:
        out[sel] = np.maximum(beauty[sel] + weight[sel][:, None] * delta[sel], 0.0)

    # ----- metal flag from the specular (or reflection) energy inside the mask
    spec_name = "specular" if "specular" in elements else "reflection" if "reflection" in elements else None
    specular_share: float | None = None
    metal = False
    if spec_name is not None and n_px:
        spec = _rgb3(elements[spec_name], spec_name)
        denom = float((beauty[sel] * weight[sel][:, None]).sum())
        specular_share = float((spec[sel] * weight[sel][:, None]).sum() / denom) if denom > 0 else 0.0
        metal = specular_share > metal_share
        if metal:
            notes.append(
                f"metal: {spec_name} carries {specular_share:.2f} of the beauty inside the mask (> {metal_share}); "
                "tinted reflections of coloured metals are out of scope, expect a larger error"
            )
    elif n_px:
        notes.append("no specular or reflection element: the metal check is unavailable")

    legend = {
        k: legend_entry.get(k) for k in ("id", "name", "material") if legend_entry and k in legend_entry
    }
    return {
        "rgb_linear": out,
        "method": chosen,
        "expected_de00": EXPECTED_DE00[chosen],
        "degraded": chosen == "degraded",
        "metal": metal,
        "specular_share": specular_share,
        "source_albedo_linear": [float(v) for v in rho],
        "source_albedo_origin": origin,
        "target_albedo_linear": [float(v) for v in target],
        "target_lab": [float(v) for v in np.asarray(target_lab, dtype=np.float64)],
        "ratio": [float(v) for v in ratio],
        "n_px": n_px,
        "elements_used": used,
        "fresnel": fresnel,
        "notes": notes,
        "legend": legend,
    }


# --------------------------------------------------------------------------- display + comparison


def to_display(rgb_linear: np.ndarray, manifest: Mapping[str, Any]) -> np.ndarray:
    """Scene-linear (..., 3) -> display RGB float32 on 0..255 through the packet's colour pipeline (T3)."""
    from plate_pixels.ocio import Pipeline  # OCIO is a pixels dependency; keep the gates import light

    return Pipeline.from_manifest(manifest).rederive_display(rgb_linear)


def _blur_lab(rgb8: np.ndarray, weight: np.ndarray, sigma: float) -> np.ndarray:
    """Lab of the sigma-blurred display image, the blur normalised to the masked pixels (material_de convention)."""
    f = np.asarray(rgb8, dtype=np.float32)
    num = cv2.GaussianBlur(f * weight[..., None], (0, 0), sigma)
    den = cv2.GaussianBlur(weight, (0, 0), sigma)
    return srgb_to_lab(num / np.maximum(den, 1e-6)[..., None])


def compare_display(
    out8: np.ndarray,
    ref8: np.ndarray,
    mask: np.ndarray,
    *,
    sigma: float = 6.0,
    erode_px: int = 3,
) -> dict[str, Any]:
    """dE00 between two display images (RGB on 0..255) inside `mask` (bool): the sigma-blurred per-material
    numbers (de00_median / de00_p90, blur normalised inside the mask), the raw per-pixel ones (de00_median_px /
    de00_p90_px) and the dE00 of the two mean Lab values; scored on the mask eroded `erode_px`."""
    out8 = np.asarray(out8, dtype=np.float32)
    ref8 = np.asarray(ref8, dtype=np.float32)
    if out8.shape != ref8.shape or out8.ndim != 3 or out8.shape[2] != 3:
        raise ValueError(f"display images must be the same (H, W, 3), got {out8.shape} and {ref8.shape}")
    raw = np.asarray(mask, dtype=bool)
    if raw.shape != out8.shape[:2]:
        raise ValueError(f"mask shape {raw.shape} does not match the image {out8.shape[:2]}")
    scored = erode(raw, erode_px) if erode_px > 0 else raw
    if int(scored.sum()) == 0:
        scored = raw
    n_px = int(scored.sum())
    if n_px == 0:
        return {"n_px": 0, "sigma": sigma, "erode_px": erode_px}
    lab_out = srgb_to_lab(out8)
    lab_ref = srgb_to_lab(ref8)
    de_px = ciede2000(lab_out, lab_ref)[scored]
    weight = raw.astype(np.float32)
    de_blur = ciede2000(_blur_lab(out8, weight, sigma), _blur_lab(ref8, weight, sigma))[scored]
    mean_out = lab_out[scored].mean(axis=0)
    mean_ref = lab_ref[scored].mean(axis=0)
    return {
        "de00_median": float(np.median(de_blur)),
        "de00_p90": float(np.percentile(de_blur, 90)),
        "de00_median_px": float(np.median(de_px)),
        "de00_p90_px": float(np.percentile(de_px, 90)),
        "de00_of_means": float(ciede2000(mean_out[None, :], mean_ref[None, :])[0]),
        "lab_mean_out": [float(v) for v in mean_out],
        "lab_mean_ref": [float(v) for v in mean_ref],
        "n_px": n_px,
        "sigma": sigma,
        "erode_px": erode_px,
    }


# --------------------------------------------------------------------------- re-render validation harness


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_packet(packet_dir: str | Path, elements: Sequence[str] | None = None) -> dict[str, Any]:
    """Manifest, legend, the EXR elements and the display master of a Shot Packet folder.

    Returns {dir, manifest, legend, elements, display8 (float32 0..255 or None), packet_id}."""
    from plate_pixels import exr, imageio

    d = Path(packet_dir)
    manifest = _read_json(d / "manifest.json")
    files = manifest["files"]
    legend = _read_json(d / files["id_legend"]["path"]) if "id_legend" in files else {"entries": []}
    exr_path = d / files["beauty_exr"]["path"]
    els = exr.read_elements(exr_path, manifest, elements)
    display8 = None
    if "display_tiff16" in files:
        img = imageio.read(d / files["display_tiff16"]["path"])
        display8 = np.asarray(img.rgb, dtype=np.float32) * 255.0
    return {
        "dir": d,
        "manifest": manifest,
        "legend": legend,
        "elements": els,
        "display8": display8,
        "packet_id": manifest.get("packet_id"),
        "exr_path": exr_path,
    }


def _select(
    packet: Mapping[str, Any], select_id: str
) -> tuple[np.ndarray, np.ndarray, dict[str, Any] | None]:
    """(bool mask, coverage weight, legend entry) for an id like 'o:735a8655' or 'm:...' (bare hex = object)."""
    from plate_pixels import exr

    which = "material" if str(select_id).lower().startswith("m:") else "object"
    ranks, cov = exr.read_cryptomatte(packet["exr_path"], packet["manifest"], which)
    target = np.uint32(exr._parse_id(select_id))
    share = np.where(ranks == target, cov, 0.0).sum(axis=2).astype(np.float32)
    mask = share >= 0.5
    entry = next((e for e in packet["legend"].get("entries", []) if e.get("id") == select_id), None)
    return mask, np.clip(share, 0.0, 1.0), entry


def _same_view(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    ca, cb = a["camera"], b["camera"]
    if (
        ca["resolution"]["width"] != cb["resolution"]["width"]
        or ca["resolution"]["height"] != cb["resolution"]["height"]
    ):
        return False
    ma = np.asarray(ca["world_from_camera"], dtype=np.float64)
    mb = np.asarray(cb["world_from_camera"], dtype=np.float64)
    return bool(np.allclose(ma, mb, atol=1e-5)) and abs(ca["focal_length_mm"] - cb["focal_length_mm"]) < 1e-6


def validate_against_rerender(
    packet_dir: str | Path,
    rerender_dir: str | Path,
    select_id: str,
    target_lab: Sequence[float] | np.ndarray,
    *,
    source_lab: Sequence[float] | np.ndarray | None = None,
    methods: Sequence[str] = METHODS,
    sigma: float = 6.0,
    erode_px: int = 3,
    f0: float | None = F0_DEFAULT,
) -> dict[str, Any]:
    """Recolour `select_id` in `packet_dir` to `target_lab` with each method and score it against the display
    master of `rerender_dir` (the same camera re-rendered with the material changed) inside the mask.

    The re-render must be the same view (resolution, camera matrix, focal length): the harness validates a
    recolour, it never invents an angle. Per method: the apply() summary (without pixels), compare_display
    numbers, `passes` (sigma-blurred de00_median <= expected_de00) and `outside_identical` (pixels outside the
    mask equal the source beauty bit for bit). `before` is the dE00 of the untouched source against the re-render,
    the size of the colour move the recolour had to make."""
    src = load_packet(packet_dir)
    ref = load_packet(rerender_dir, elements=["beauty"])
    if ref["display8"] is None:
        raise RecolourError(f"{rerender_dir} has no display_tiff16 master to validate against")
    if not _same_view(src["manifest"], ref["manifest"]):
        raise RecolourError(
            "the re-render is not the same view as the packet (camera, focal length or resolution differ)"
        )
    mask, share, entry = _select(src, select_id)
    if int(mask.sum()) == 0:
        raise RecolourError(f"{select_id} covers no pixel in {packet_dir}")
    beauty = _rgb3(src["elements"]["beauty"], "beauty")
    display_src = to_display(beauty, src["manifest"])
    from plate_pixels.ocio import Pipeline

    pipeline = Pipeline.from_manifest(src["manifest"])
    report: dict[str, Any] = {
        "packet_id": src["packet_id"],
        "rerender_packet_id": ref["packet_id"],
        "select_id": select_id,
        "legend": {k: entry.get(k) for k in ("id", "name", "material")} if entry else None,
        "target_lab": [float(v) for v in np.asarray(target_lab, dtype=np.float64)],
        "source_lab": None
        if source_lab is None
        else [float(v) for v in np.asarray(source_lab, dtype=np.float64)],
        "pipeline": pipeline.describe(),
        "n_px": int(mask.sum()),
        "before": compare_display(display_src, ref["display8"], mask, sigma=sigma, erode_px=erode_px),
        "methods": {},
    }
    for name in methods:
        try:
            res = apply(
                src["elements"],
                share,
                target_lab,
                entry,
                source_lab=source_lab,
                method=name,
                K=src["manifest"]["camera"],
                f0=f0,
            )
        except RecolourError as exc:
            report["methods"][name] = {"error": str(exc)}
            continue
        out = res.pop("rgb_linear")
        outside = share <= 0.0
        res["outside_identical"] = bool(np.array_equal(out[outside], beauty[outside]))
        res.update(
            compare_display(
                pipeline.rederive_display(out), ref["display8"], mask, sigma=sigma, erode_px=erode_px
            )
        )
        res["passes"] = bool(res["de00_median"] <= res["expected_de00"])
        report["methods"][name] = res
    return report
