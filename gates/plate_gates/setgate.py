"""The Set-consistency gate: one look across N cameras of a set, measured at world-point correspondences.

    run(hero_packet, hero_rgb, others) -> {targets, per_view, per_material, cross_view_p90, verdict, ...}
    numeric_correction(rgb, crypto_material, targets, offsets=None) -> rgb

The hero camera anchors the look: ``targets`` holds every material's mean Lab on the hero (the Set editor's
targets table). Each other view is tied to the hero by crossview.correspondences and scored with
crossview.delta_e; a material's cross-view number is the p90 of CIEDE2000 over its correspondences pooled across
the views, and ``cross_view_p90`` is the worst of them. The verdict holds that number to **p90 <= 3, hard**
(BUILD_PLAN P4 exit: "cross-view per-material dE00 p90 <= 3") for the materials the packet locks; materials in
the hero packet's ``policy.editable_ids`` (vegetation, people, sky, water: the classes AI regenerates per view,
so their content legitimately differs between cameras) are scored and reported but only advisory. A material
with fewer than ``min_n`` correspondences is reported unscored: a p90 over a handful of samples is not a
statistic. A view with fewer than ``min_corr`` correspondences is reported ``insufficient`` and makes the verdict
advisory rather than pass.

Correction. ``per_view[i].offsets`` is, per material, the mean Lab difference hero - view at the matched points:
the additive shift that moves the view's material onto the hero's. numeric_correction applies such a shift per
Cryptomatte id (weighted by the id's coverage so anti-aliased edges move proportionally, bit-identical outside
the ids it touches), clipped to +-8 L and +-6 a/b so a bad target can never restyle a material. The shift is
additive in Lab, so every pixel keeps its own chroma and lightness variation (texture, shading): only the
material's mean moves. Without ``offsets`` the shift is target - the view's own mean over the eroded id mask;
that is the fallback for a view that shares no surface with the hero, and it is a weaker estimate, because each
camera sees a different part of a material under different shading (on the fixture room those whole-mask means
differ by 2-10 dE00 on an identical render).

Measured on the fixture room (identical render, two cameras): matte wall p90 1.0, semi-gloss RAL panel 2.1,
people capsule 1.0-1.3; the leaf proxies 3-5 (a moving glossy highlight on spheres a few pixels wide at 640 px).
Fresnel makes any semi-gloss material view-dependent at grazing angles; such windows fail the patch checks and
are not correspondences, and the per-material ``n_dot_v_median`` says how frontally a material was seen.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np

from . import crossview, thresholds
from .color import ciede2000, lab_to_srgb, srgb_to_lab
from .crossview import PacketView, as_view
from .masks import erode
from .material_de import id_to_u32, per_material
from .report import Check, GateReport, verdict_of

__all__ = [
    "METRIC_ID",
    "P90_HARD",
    "MIN_N",
    "MIN_CORR",
    "CLIP_L",
    "CLIP_AB",
    "hero_targets",
    "run",
    "numeric_correction",
    "gate_report",
]

METRIC_ID = "set.cross_view_de00_p90"
P90_HARD = 3.0
MIN_N = 30
MIN_CORR = 50
CLIP_L = 8.0
CLIP_AB = 6.0
ERODE_PX = 3
MIN_PX = 100


# --------------------------------------------------------------------------- targets


def hero_targets(
    hero: PacketView, hero_rgb: np.ndarray, *, erode_px: int = ERODE_PX, min_px: int = MIN_PX
) -> dict[str, dict]:
    """Per-material mean Lab of the hero view over the eroded Cryptomatte mask (the material_de convention).

    {m_id: {lab: [L, a, b], n_px, name, class, editable}}; ids with fewer than ``min_px`` pixels are left out.
    """
    stats = per_material(
        hero_rgb, hero_rgb, hero.ranks, hero.coverage, hero.legend, erode_px=erode_px, min_px=min_px
    )
    entries = hero.material_entries()
    out: dict[str, dict] = {}
    for mid, row in stats.items():
        entry = entries.get(mid.lower(), {})
        out[mid.lower()] = {
            "lab": [float(v) for v in row["lab_mean_plate"]],
            "n_px": int(row["n_px"]),
            "name": entry.get("name"),
            "class": entry.get("class"),
            "editable": mid.lower() in hero.editable_ids,
        }
    return out


def _threshold() -> dict:
    """The committed threshold for METRIC_ID when thresholds.yaml carries it, else the module constant."""
    ts = thresholds.load()
    thr = ts.metrics.get(METRIC_ID)
    if thr is not None and thr.value is not None:
        return {
            "metric_id": METRIC_ID,
            "op": thr.op,
            "value": float(thr.value),
            "status": thr.status,
            "set_id": ts.id,
        }
    return {"metric_id": METRIC_ID, "op": "le", "value": P90_HARD, "status": "hard", "set_id": ts.id}


def _percentile(values: Sequence[float], q: float) -> float | None:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q)) if len(values) else None


# --------------------------------------------------------------------------- the gate


def run(
    hero_packet: Any,
    hero_rgb: np.ndarray,
    others: Iterable[tuple[Any, np.ndarray]],
    *,
    n: int = 2000,
    seed: int = 0,
    window: int = crossview.WINDOW,
    sigma: float = crossview.SIGMA,
    min_n: int = MIN_N,
    min_corr: int = MIN_CORR,
    p90_max: float | None = None,
    hard_ids: Iterable[str] | None = None,
    **corr_kwargs: Any,
) -> dict:
    """Score a set: the hero view against every other (packet, rgb); see the module docstring.

    ``hero_packet`` / packets: PacketView or packet folder; rgb images uint8, uint16 or float 0..1 at packet size.
    ``p90_max`` overrides the threshold (default: thresholds.yaml entry or 3.0). ``hard_ids`` names the
    materials the hard check covers (default: every material not in the hero packet's editable_ids).
    ``corr_kwargs`` go to crossview.correspondences (min_n_dot_v, z_tol, exclude_classes, ...).
    """
    hero = as_view(hero_packet)
    thr = _threshold()
    limit = float(thr["value"]) if p90_max is None else float(p90_max)
    targets = hero_targets(hero, hero_rgb)
    hard = (
        {str(i).lower() for i in hard_ids}
        if hard_ids is not None
        else {mid for mid in hero.material_entries() if mid not in hero.editable_ids}
    )

    per_view: list[dict] = []
    pooled: dict[str, dict[str, list]] = {}
    notes: list[str] = []
    for index, (packet, rgb) in enumerate(others):
        view = as_view(packet)
        corr = crossview.correspondences(hero, view, n=n, seed=seed, window=window, **corr_kwargs)
        de = crossview.delta_e(
            corr, hero_rgb, rgb, window, sigma=sigma, view_a=hero, view_b=view, return_values=True
        )
        own = {
            k.lower(): v for k, v in per_material(rgb, rgb, view.ranks, view.coverage, view.legend).items()
        }
        rows: dict[str, dict] = {}
        offsets: dict[str, list[float]] = {}
        for mid, stat in de["per_material"].items():
            values = stat.pop("de00")
            recs = [c for c in corr if str(c["id_m"]).lower() == mid]
            ndv = [min(float(c["n_dot_v_a"]), float(c["n_dot_v_b"])) for c in recs]
            rows[mid] = {
                **stat,
                "n_dot_v_median": float(np.median(ndv)) if ndv else None,
                "lab_mean": [float(v) for v in own[mid]["lab_mean_plate"]] if mid in own else None,
                "target_de": (
                    float(
                        ciede2000(
                            np.asarray(own[mid]["lab_mean_plate"])[None, :],
                            np.asarray(targets[mid]["lab"])[None, :],
                        )[0]
                    )
                    if mid in own and mid in targets
                    else None
                ),
            }
            offsets[mid] = [float(v) for v in stat["dlab_mean"]]
            bucket = pooled.setdefault(mid, {"de": [], "ndv": []})
            bucket["de"].extend(values)
            bucket["ndv"].extend(ndv)
        status = "ok" if len(corr) >= min_corr else "insufficient"
        if status == "insufficient":
            notes.append(f"view {index} ({view.packet_id}): {len(corr)} correspondences < {min_corr}")
        per_view.append(
            {
                "index": index,
                "packet_id": view.packet_id,
                "n_corr": len(corr),
                "source": de["source"],
                "status": status,
                "p90": de["overall_p90"],
                "median": de["overall_median"],
                "per_material": rows,
                "offsets": offsets,
            }
        )

    per_mat: dict[str, dict] = {}
    worst: float | None = None
    checks: list[Check] = []
    for mid, bucket in pooled.items():
        p90 = _percentile(bucket["de"], 90)
        scored = len(bucket["de"]) >= min_n
        row = {
            "de00_p90": p90,
            "de00_median": _percentile(bucket["de"], 50),
            "n": len(bucket["de"]),
            "n_dot_v_median": float(np.median(bucket["ndv"])) if bucket["ndv"] else None,
            "scored": scored,
            "hard": mid in hard,
            "name": targets.get(mid, {}).get("name") or hero.material_entries().get(mid, {}).get("name"),
        }
        if not scored:
            row["reason"] = f"n < {min_n}"
        elif mid in hard:
            worst = p90 if worst is None else max(worst, p90)
        else:
            row["reason"] = "editable material: advisory"
        per_mat[mid] = row
    if worst is not None:
        checks.append(
            Check(
                metric_id=METRIC_ID,
                value=worst,
                status="pass" if worst <= limit else ("fail" if thr["status"] == "hard" else "advisory"),
            )
        )
    elif pooled:
        notes.append("no locked material reached min_n correspondences: nothing scored")
    else:
        notes.append("no correspondences: nothing scored")
    advisory_misses = [
        mid for mid, r in per_mat.items() if r["scored"] and not r["hard"] and r["de00_p90"] > limit
    ]
    if advisory_misses:
        checks.append(
            Check(
                metric_id=f"{METRIC_ID}.editable",
                value=max(per_mat[m]["de00_p90"] for m in advisory_misses),
                status="advisory",
            )
        )
    verdict = verdict_of(checks)
    if verdict == "pass" and any(v["status"] == "insufficient" for v in per_view):
        verdict = "advisory"
    all_de = [d for b in pooled.values() for d in b["de"]]
    return {
        "hero": {"packet_id": hero.packet_id, "n_materials": len(targets)},
        "targets": targets,
        "per_view": per_view,
        "per_material": per_mat,
        "cross_view_p90": worst,
        "overall_p90": _percentile(all_de, 90),
        "n_corr": len(all_de),
        "threshold": {**thr, "value": limit},
        "checks": [c.model_dump() for c in checks],
        "verdict": verdict,
        "notes": notes,
    }


def gate_report(result: Mapping[str, Any], *, tier: str | None = "A") -> GateReport:
    """Wrap a run() result in the versioned GateReport envelope (checks and verdict carried over)."""
    checks = [Check(**c) for c in result.get("checks", [])]
    metrics = {
        "cross_view_p90": result.get("cross_view_p90"),
        "overall_p90": result.get("overall_p90"),
        "n_corr": result.get("n_corr"),
        "per_material": result.get("per_material"),
        "per_view": [
            {k: v for k, v in view.items() if k != "offsets"} for view in result.get("per_view", [])
        ],
        "targets": result.get("targets"),
        "threshold": result.get("threshold"),
    }
    return GateReport(
        threshold_set_id=result.get("threshold", {}).get("set_id", thresholds.load().id),
        tier=tier,
        metrics=metrics,
        checks=checks,
        backend_notes=list(result.get("notes", [])),
    )


# --------------------------------------------------------------------------- correction


def _as_share_source(crypto_material: Any) -> tuple[np.ndarray, np.ndarray]:
    """(ranks, coverage) from a PacketView, a (ranks, coverage) pair or a 2-D id image (coverage 1 where id != 0)."""
    if isinstance(crypto_material, PacketView):
        return crypto_material.ranks, crypto_material.coverage
    if isinstance(crypto_material, (tuple, list)) and len(crypto_material) == 2:
        ranks, cov = (np.asarray(crypto_material[0]), np.asarray(crypto_material[1]))
        if ranks.ndim == 2:
            ranks, cov = ranks[..., None], cov[..., None]
        if ranks.shape != cov.shape:
            raise ValueError(f"ranks {ranks.shape} and coverage {cov.shape} differ")
        return ranks, cov
    ids = np.asarray(crypto_material)
    if ids.ndim == 2:
        return ids[..., None].astype(np.uint32), (ids != 0)[..., None].astype(np.float32)
    raise TypeError("crypto_material must be a PacketView, a (ranks, coverage) pair or a 2-D id image")


def _to_float255(rgb: np.ndarray) -> np.ndarray:
    a = np.asarray(rgb)
    if a.ndim != 3 or a.shape[2] != 3:
        raise ValueError("rgb must be an RGB array of shape (h, w, 3)")
    if a.dtype == np.uint8:
        return a.astype(np.float64)
    if a.dtype == np.uint16:
        return a.astype(np.float64) / 257.0
    if np.issubdtype(a.dtype, np.floating):
        return a.astype(np.float64) * 255.0
    raise ValueError(f"rgb must be uint8, uint16 or float 0..1, got {a.dtype}")


def _from_float255(f: np.ndarray, like: np.ndarray) -> np.ndarray:
    if like.dtype == np.uint8:
        return np.clip(np.rint(f), 0, 255).astype(np.uint8)
    if like.dtype == np.uint16:
        return np.clip(np.rint(f * 257.0), 0, 65535).astype(np.uint16)
    return np.clip(f / 255.0, 0.0, 1.0).astype(like.dtype)


def _target_lab(value: Any) -> np.ndarray:
    lab = value["lab"] if isinstance(value, Mapping) else value
    arr = np.asarray(lab, dtype=np.float64).reshape(-1)
    if arr.size != 3:
        raise ValueError("a target must be [L, a, b] or {'lab': [L, a, b]}")
    return arr


def numeric_correction(
    rgb: np.ndarray,
    crypto_material: Any,
    targets: Mapping[str, Any],
    *,
    offsets: Mapping[str, Sequence[float]] | None = None,
    erode_px: int = ERODE_PX,
    min_px: int = MIN_PX,
    clip_l: float = CLIP_L,
    clip_ab: float = CLIP_AB,
    threshold: float = crossview.MASK_THRESHOLD,
) -> np.ndarray:
    """Per-id Lab mean shift toward ``targets`` ({id: [L, a, b]} or run()['targets']), clipped to +-clip_l L and
    +-clip_ab a/b, chroma-preserving (additive, every pixel keeps its own variation). See the module docstring.

    ``crypto_material``: (ranks, coverage) as read_cryptomatte returns them, a PacketView, or a 2-D id image.
    ``offsets``: {id: [dL, da, db]} measured at correspondences (run()['per_view'][i]['offsets']); when given for
    an id it is applied as is (after clipping) instead of target - own mean. Ids absent from both are untouched;
    the output is bit-identical to the input outside the touched ids and keeps the input dtype.
    """
    ranks, cov = _as_share_source(crypto_material)
    src = np.asarray(rgb)
    if ranks.shape[:2] != src.shape[:2]:
        raise ValueError(f"crypto {ranks.shape[:2]} and rgb {src.shape[:2]} differ in size")
    f = _to_float255(src)
    lab = srgb_to_lab(f)
    lab_out = lab.copy()
    touched = np.zeros(src.shape[:2], dtype=bool)
    want = {str(k).lower(): v for k, v in targets.items()}
    given = {str(k).lower(): np.asarray(v, dtype=np.float64).reshape(-1) for k, v in (offsets or {}).items()}
    for mid in set(want) | set(given):
        target = np.uint32(id_to_u32(mid))
        share = np.where(ranks == target, cov, 0.0).sum(axis=2)
        inside = share >= threshold
        if mid in given:
            delta = given[mid]
            if delta.size != 3:
                raise ValueError(f"offset for {mid} must be [dL, da, db]")
        else:
            region = erode(inside, erode_px)
            if int(region.sum()) < min_px:
                continue
            delta = _target_lab(want[mid]) - lab[region].mean(axis=0)
        delta = np.clip(delta, [-clip_l, -clip_ab, -clip_ab], [clip_l, clip_ab, clip_ab])
        w = np.clip(share, 0.0, 1.0)
        sel = w > 0
        if not sel.any():
            continue
        lab_out[sel] += w[sel][:, None] * delta[None, :]
        touched |= sel
    out = src.copy()
    if touched.any():
        out[touched] = _from_float255(lab_to_srgb(lab_out[touched]), src)
    return out
