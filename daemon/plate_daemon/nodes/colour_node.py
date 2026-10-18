"""The Colour node: a grade in linear light inside a scope (decision 48, router plan T93).

    prepare    the source is the node wired into `image`: a Plate (or nothing) is the packet's display master,
               any other node is its take (comp_node.take; 422 `no_base` when it has none). The scope is
               `params.scope`: `mask` (the Mask node wired into `mask`: its ids, painted and segmented masks, or
               its output), `ids` (`params.ids`, legend ids) or `frame`; the default is `mask` when a Mask is
               wired, else `frame`. The grade: `ev` [-4, 4], `kelvin` [2000, 12000] or null, `tint` [-100, 100],
               `contrast` [-50, 50], `saturation` [-100, 100], `hue_deg` [-180, 180].
    normalize  in linear light (`srgb_eotf` of the 16-bit source, back through `srgb_oetf`):
                   exposure     lin *= 2 ** ev
                   white        Bradford adaptation from the neutral white to the target white: xy on the CIE
                                daylight locus for 4000 K and up, on the Planckian locus (Kang et al. 2002) below;
                                `tint` moves the target along the isotherm by tint/100 x 0.05 in CIE 1960 uv
                                (positive towards magenta, below the locus)
                   contrast     lin = 0.18 (lin / 0.18) ** (1 + contrast / 100)
                   saturation   luma + (lin - luma)(1 + saturation / 100), Rec.709 luma
                   hue          the (U, V) chroma plane rotated by hue_deg about the luma axis
               then out = where(scope, graded, source), no feather. `normalized.graded` is the grade as given,
               `mask_key` the scope, `mask_union_key` the scope with the source's own chain mask.
    gate       `outside_mask.changed_px == 0` against the source (hard); the still gate against the plate with the
               union as its mask: G-buffer structure and invention flags hard, `material.*` advisory (a grade
               moves materials by design, noted as such), the texture and tone bands advisory for the same reason.
               The invention flags count the edges the plate (and a source take) already shows under the same
               grade as known (comp_node.invention_against): a grade lifts the plate's weak edges over the
               detector's fixed threshold, which is not an invention; a step it draws across a wall is.

The neutral white is the daylight locus at 6500 K: the CIE formula puts 6500 K 0.0002 xy from the D65
chromaticity, so taking the formula's own point as the reference keeps kelvin 6500 / tint 0 the exact identity
and the slider continuous through it. A null kelvin keeps the temperature; a tint alone moves along 6500 K's
isotherm. A grade may touch a locked material (it is the artist's explicit scope); the report names the locked
ids the scope covers.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from ..pipeline import PipelineError
from .common import Prepared, QuoteContext, RunContext, cas_has, finite_or_none, pick, plain
from .comp_node import (
    chain_mask,
    changed_outside,
    connection,
    need_size,
    plate_wh,
    put_mask,
    read_mask,
    read_take,
    still_gates,
    take,
    wired,
)
from .comp_node import score as _score

KIND = "Colour"
NEUTRAL_K = 6500.0
TINT_DUV = 0.05  # |delta uv| at tint +-100
LUMA = np.array([0.2126, 0.7152, 0.0722])
KB, KR = 0.0722, 0.2126  # BT.709 chroma scales: U = (B - Y) / (2 (1 - KB)), V = (R - Y) / (2 (1 - KR))
RANGES: dict[str, tuple[float, float]] = {
    "ev": (-4.0, 4.0),
    "kelvin": (2000.0, 12000.0),
    "tint": (-100.0, 100.0),
    "contrast": (-50.0, 50.0),
    "saturation": (-100.0, 100.0),
    "hue_deg": (-180.0, 180.0),
}
ALIASES = {"hue_deg": ("hue_deg", "hueDeg", "hue")}
SCOPES = ("mask", "ids", "frame")
MASK_KEYS = (
    "maskHash",
    "samHash",
)  # a Mask node's painted and segmented CAS masks (pipeline.MASK_KEY_PARAMS)

# Linear Rec.709 (D65) <-> CIE XYZ, the same matrix as plate_gates.color; Bradford's cone response.
XYZ_FROM_RGB = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
RGB_FROM_XYZ = np.linalg.inv(XYZ_FROM_RGB)
BRADFORD = np.array(
    [
        [0.8951, 0.2664, -0.1614],
        [-0.7502, 1.7135, 0.0367],
        [0.0389, -0.0685, 1.0296],
    ]
)


# ---------------------------------------------------------------------------- white points


def _daylight_xy(t: float, branch_t: float) -> tuple[float, float]:
    """CIE daylight locus, 4000-25000 K (`branch_t` picks the polynomial, so a tangent stays on one piece)."""
    if branch_t <= 7000.0:
        x = -4.6070e9 / t**3 + 2.9678e6 / t**2 + 0.09911e3 / t + 0.244063
    else:
        x = -2.0064e9 / t**3 + 1.9018e6 / t**2 + 0.24748e3 / t + 0.237040
    return x, -3.000 * x * x + 2.870 * x - 0.275


def _planckian_xy(t: float, branch_t: float) -> tuple[float, float]:
    """Kang et al. (2002) cubic-spline Planckian locus, 1667-4000 K."""
    x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    if branch_t <= 2222.0:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    else:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    return x, y


def locus_xy(kelvin: float, at: float | None = None) -> tuple[float, float]:
    """xy of a colour temperature: the daylight locus from 4000 K, the Planckian locus below."""
    branch = kelvin if at is None else at
    return (_daylight_xy if branch >= 4000.0 else _planckian_xy)(kelvin, branch)


def xy_to_uv(x: float, y: float) -> tuple[float, float]:
    d = -2.0 * x + 12.0 * y + 3.0
    return 4.0 * x / d, 6.0 * y / d


def uv_to_xy(u: float, v: float) -> tuple[float, float]:
    d = 2.0 * u - 8.0 * v + 4.0
    return 3.0 * u / d, 2.0 * v / d


def white_xy(kelvin: float, tint: float = 0.0) -> tuple[float, float]:
    """The target white: the locus point of `kelvin`, moved tint/100 x 0.05 in uv along its isotherm (the
    normal to the locus there; positive tint lowers v, towards magenta)."""
    x, y = locus_xy(kelvin)
    if not tint:
        return x, y
    u, v = xy_to_uv(x, y)
    step = 1.0
    u0, v0 = xy_to_uv(*locus_xy(kelvin - step, at=kelvin))
    u1, v1 = xy_to_uv(*locus_xy(kelvin + step, at=kelvin))
    tu, tv = u1 - u0, v1 - v0
    norm = math.hypot(tu, tv)
    nu, nv = -tv / norm, tu / norm
    if nv > 0:
        nu, nv = -nu, -nv
    d = tint / 100.0 * TINT_DUV
    return uv_to_xy(u + d * nu, v + d * nv)


def _xyz(xy: tuple[float, float]) -> np.ndarray:
    x, y = xy
    return np.array([x / y, 1.0, (1.0 - x - y) / y])


def bradford(src_xy: tuple[float, float], dst_xy: tuple[float, float]) -> np.ndarray:
    """The XYZ -> XYZ von Kries adaptation in Bradford cone space that takes `src_xy` white to `dst_xy`."""
    gain = (BRADFORD @ _xyz(dst_xy)) / (BRADFORD @ _xyz(src_xy))
    return np.linalg.inv(BRADFORD) @ np.diag(gain) @ BRADFORD


def white_balance_matrix(kelvin: float | None, tint: float = 0.0) -> np.ndarray | None:
    """Linear Rec.709 -> linear Rec.709 for the white change, or None for none (kelvin null or 6500, tint 0)."""
    if (kelvin is None or kelvin == NEUTRAL_K) and not tint:
        return None
    src = locus_xy(NEUTRAL_K)
    dst = white_xy(NEUTRAL_K if kelvin is None else float(kelvin), float(tint))
    return RGB_FROM_XYZ @ bradford(src, dst) @ XYZ_FROM_RGB


# ---------------------------------------------------------------------------- the grade


def grade_linear(lin: np.ndarray, grade: Mapping[str, Any]) -> np.ndarray:
    """The grade on linear Rec.709 (..., 3), float64; a neutral parameter is skipped, so zeros are the input."""
    out = np.asarray(lin, dtype=np.float64)
    ev = float(grade.get("ev") or 0.0)
    if ev:
        out = out * (2.0**ev)
    wb = white_balance_matrix(grade.get("kelvin"), float(grade.get("tint") or 0.0))
    if wb is not None:
        out = out @ wb.T
    contrast = float(grade.get("contrast") or 0.0)
    if contrast:
        out = 0.18 * np.power(np.maximum(out, 0.0) / 0.18, 1.0 + contrast / 100.0)
    sat = float(grade.get("saturation") or 0.0)
    if sat:
        luma = (out @ LUMA)[..., None]
        out = luma + (out - luma) * (1.0 + sat / 100.0)
    hue = float(grade.get("hue_deg") or 0.0)
    if hue:
        y = out @ LUMA
        u = (out[..., 2] - y) / (2.0 * (1.0 - KB))
        v = (out[..., 0] - y) / (2.0 * (1.0 - KR))
        a = math.radians(hue)
        u, v = u * math.cos(a) - v * math.sin(a), u * math.sin(a) + v * math.cos(a)
        r = y + 2.0 * (1.0 - KR) * v
        b = y + 2.0 * (1.0 - KB) * u
        g = (y - LUMA[0] * r - LUMA[2] * b) / LUMA[1]
        out = np.stack([r, g, b], axis=-1)
    return out


def grade16(src16: np.ndarray, grade: Mapping[str, Any]) -> np.ndarray:
    """A 16-bit sRGB display picture graded in linear light, back to 16-bit sRGB (clipped to [0, 1])."""
    from plate_pixels.ocio import srgb_eotf, srgb_oetf

    lin = srgb_eotf(np.asarray(src16, dtype=np.float64) / 65535.0)
    enc = srgb_oetf(grade_linear(lin, grade))
    return np.clip(np.rint(enc * 65535.0), 0, 65535).astype(np.uint16)


# ---------------------------------------------------------------------------- prepare


def _grade_params(q: QuoteContext) -> dict[str, Any]:
    grade: dict[str, Any] = {}
    for name, (lo, hi) in RANGES.items():
        raw = q.param(*ALIASES.get(name, (name,)))
        if raw is None:
            grade[name] = None if name == "kelvin" else 0.0
            continue
        value = finite_or_none(raw)
        if value is None or isinstance(raw, bool) or not lo <= value <= hi:
            raise PipelineError(
                f"Colour params.{name} must be a number in [{lo:g}, {hi:g}], got {raw!r}", code="bad_params"
            )
        grade[name] = value
    return grade


def _check_ids(pv: Any, raw: Any, where: str) -> list[str]:
    """Legend ids for a scope, lower-cased: `o:` / `m:` ids of the packet's legend, `s:` segments of a Tier C
    packet (422 `bad_params` / `unknown_id`)."""
    if not isinstance(raw, (list, tuple)) or not all(isinstance(i, str) and i for i in raw):
        raise PipelineError(f"{where} is a list of legend ids", code="bad_params")
    known = {str(e.get("id", "")).lower() for e in ((pv.legend or {}).get("entries") or [])}
    ids = [i.strip().lower() for i in raw]
    unknown = [
        i for i in ids if not (i.startswith("s:") and pv.files.get("segments") is not None) and i not in known
    ]
    if unknown:
        raise PipelineError(f"ids {unknown} are not in packet {pv.packet_id}'s legend", code="unknown_id")
    return ids


def _mask_scope(q: QuoteContext, pv: Any, mask_node: Mapping[str, Any]) -> dict[str, Any]:
    """The wired Mask node as a scope: its ids and CAS masks (one union), else its output."""
    params = mask_node.get("params") if isinstance(mask_node.get("params"), Mapping) else {}
    raw_ids = params.get("ids")
    ids = _check_ids(pv, raw_ids, f"Mask node {mask_node['id']} params.ids") if raw_ids else []
    keys: list[str] = []
    for name in MASK_KEYS:
        key = params.get(name)
        if key is None or key == "":
            continue
        if not cas_has(q.cas, key):
            raise PipelineError(
                f"Mask node {mask_node['id']}: {name} is not in this project's CAS", code="mask_missing"
            )
        if key not in keys:
            keys.append(str(key))
    if not ids and not keys and cas_has(q.cas, mask_node.get("outputHash")):
        keys.append(str(mask_node["outputHash"]))
    if not ids and not keys:
        raise PipelineError(
            f"Mask node {mask_node['id']} has no ids, no painted mask and no segment mask", code="mask_empty"
        )
    shape = pv.plate16().shape[:2]
    for key in keys:
        read_mask(q.cas, key, shape)  # 422 mask_size when it is not a plate-sized grey PNG
    return {"kind": "mask", "node": str(mask_node["id"]), "ids": ids, "keys": keys}


def prepare(q: QuoteContext) -> Prepared:
    packet_id = q.plate_packet_id()
    pv = q.packet(packet_id)
    grade = _grade_params(q)
    sources = wired(q, "image")
    if len(sources) > 1:
        raise PipelineError(
            f"Colour {q.node_id} grades one picture, {len(sources)} are wired", code="bad_graph"
        )
    masks = [n for n in wired(q, "mask") if n.get("kind") == "Mask"]
    scope_kind = pick(q.params, "scope", default="mask" if masks else "frame")
    if scope_kind not in SCOPES:
        raise PipelineError("Colour params.scope is mask, ids or frame", code="bad_params")
    if scope_kind == "mask":
        if not masks:
            raise PipelineError(
                "Colour scope mask needs a Mask node wired into its mask input", code="no_mask"
            )
        scope = _mask_scope(q, pv, masks[0])
    elif scope_kind == "ids":
        raw = pick(q.params, "ids")
        if not raw:
            raise PipelineError("Colour scope ids needs params.ids, a list of legend ids", code="bad_params")
        scope = {"kind": "ids", "ids": _check_ids(pv, raw, "Colour params.ids"), "keys": []}
    else:
        scope = {"kind": "frame", "ids": [], "keys": []}
    with connection(q) as conn:
        if not sources or sources[0].get("kind") == "Plate":
            source: dict[str, Any] = {"kind": "plate", "hash": pv.plate_hash}
        else:
            source = {"kind": "take", **take(q, conn, sources[0], "a Colour grade")}
    if source["kind"] == "take":
        need_size(q, source["output"], plate_wh(pv), f"the source {source['node']}")
    spec = {"packet": packet_id, "plate": pv.plate_hash, "source": source, "scope": scope, "grade": grade}
    return Prepared(spec, packet_id)


# ---------------------------------------------------------------------------- run


def _source(c: RunContext, pv: Any) -> tuple[np.ndarray, dict[str, Any] | None]:
    plate16 = pv.plate16()
    entry = c.spec["source"] if c.spec["source"]["kind"] == "take" else None
    return (plate16 if entry is None else read_take(c, entry, plate16.shape, "the source")), entry


def scope_mask(c: RunContext, pv: Any, shape: tuple[int, int]) -> np.ndarray:
    scope = c.spec["scope"]
    if scope["kind"] == "frame":
        return np.ones(shape, dtype=bool)
    mask = np.zeros(shape, dtype=bool)
    for cid in scope.get("ids") or ():
        mask |= pv.id_mask(cid)
    for key in scope.get("keys") or ():
        mask |= read_mask(c.cas, key, shape)
    return mask


def _locked_in(pv: Any, mask: np.ndarray) -> list[str]:
    out = []
    for cid in pv.locked_ids():
        try:
            if bool((pv.id_mask(cid) & mask).any()):
                out.append(cid)
        except PipelineError:
            continue
    return out


def normalize(c: RunContext) -> dict[str, Any]:
    pv = c.packet()
    src16, entry = _source(c, pv)
    scope = scope_mask(c, pv, src16.shape[:2])
    graded = grade16(src16, c.spec["grade"])
    out16 = np.where(scope[..., None], graded, src16)
    union = scope | chain_mask(c, entry, src16, pv.plate16())
    return {
        "outputs": [c.put_png16(out16)],
        "graded": dict(c.spec["grade"]),
        "scope": {**c.spec["scope"], "px": int(scope.sum())},
        "source": c.spec["source"],
        "mask_key": put_mask(c, scope),
        "mask_union_key": put_mask(c, union),
        "changed_px": int(np.any(out16 != src16, axis=2).sum()),
    }


def preview(c: RunContext) -> dict[str, Any]:
    """The scope's size, the target white and the mean colour inside the scope before and after (Lab D65)."""
    from plate_gates.color import srgb_to_lab

    pv = c.packet()
    src16, _entry = _source(c, pv)
    scope = scope_mask(c, pv, src16.shape[:2])
    grade = c.spec["grade"]
    kelvin, tint = grade.get("kelvin"), float(grade.get("tint") or 0.0)
    before = after = None
    if scope.any():
        sel = src16[scope]
        before = srgb_to_lab(sel.astype(np.float64) / 257.0).mean(axis=0)
        after = srgb_to_lab(grade16(sel[None], grade)[0].astype(np.float64) / 257.0).mean(axis=0)
    return plain(
        {
            "scope": c.spec["scope"]["kind"],
            "px": int(scope.sum()),
            "coverage": float(scope.mean()),
            "white_xy": list(white_xy(NEUTRAL_K if kelvin is None else float(kelvin), tint)),
            "neutral_xy": list(locus_xy(NEUTRAL_K)),
            "mean_lab_before": before,
            "mean_lab_after": after,
            "locked_ids_in_scope": _locked_in(pv, scope),
        }
    )


def gate(c: RunContext) -> Any:
    norm = c.normalized
    pv = c.packet()
    src16, entry = _source(c, pv)
    out16 = c.read_png16(norm["outputs"][0])
    scope = read_mask(c.cas, norm["mask_key"], out16.shape[:2])
    union = read_mask(c.cas, norm["mask_union_key"], out16.shape[:2])
    frame = c.spec["scope"]["kind"] == "frame"
    grade = c.spec["grade"]
    # The edges the plate (and the source take) show under this same grade are not inventions of the grade.
    known = {"the plate under this grade": grade16(pv.plate16(), grade)}
    if entry is not None:
        known.update({"the source take": src16, "the source under this grade": grade16(src16, grade)})
    notes = ["colour: material dE00 is advisory (a grade moves materials by design); texture and tone too"]
    locked = _locked_in(pv, scope)
    if locked:
        notes.append(f"colour: the scope grades locked ids {locked} on the artist's say-so")
    return still_gates(
        c,
        pv,
        out16,
        None if frame else union,
        advisory=("material.", "texture.", "tone."),
        checks=[changed_outside(out16, src16, scope)],
        notes=notes,
        metrics={
            "colour": {"graded": norm.get("graded"), "scope": norm.get("scope"), "locked_in_scope": locked}
        },
        material_advisory=True,
        known=known,
    )


score = _score

__all__ = [
    "KIND",
    "bradford",
    "gate",
    "grade16",
    "grade_linear",
    "locus_xy",
    "normalize",
    "prepare",
    "preview",
    "score",
    "white_balance_matrix",
    "white_xy",
]
