"""The Recolour node: change materials' colours with the render's own passes (T31's recomposition, as a node).

    prepare    the materials: `params.materials` is `[{material, target}, ...]` (1 to 8 entries, distinct `m:`
               ids of the packet's legend; an entry may also carry `source_lab` and `method`), and the old
               single pair `params.material` + `params.target` is a one-entry list. A target is `RAL 7016`,
               `7016`, `#383E42`, `{lab: [L, a, b]}` or `[L, a, b]` (the UI's `targetHex` is the fallback), resolved
               to a Lab at quote time so the run is keyed on the number. A top-level `method` is the default of
               the entries that name none.
    normalize  recolour.apply on the EXR elements per entry (raw lighting, GI, diffuse filter, reflections when
               the packet has them), weighted by the material's Cryptomatte coverage; each result goes back
               through the packet's own colour pipeline (OCIO or legacy gamma) to display, and is laid onto the
               display master as a DELTA: master + (display(recoloured) - display(original)) wherever the
               material has coverage, the master itself everywhere else. Several materials are the sum of their
               deltas, each as the single recolour of that material would store it: out = master + sum_i
               (single_i - master), clipped once, in 16-bit integers. `recolour.apply` has already weighted its
               result by the coverage, and the coverages of distinct materials never overlap, so one pass with
               two materials equals the two single recolours laid on one plate (also where one of them clips at
               white), and a one-entry list is the old single-material output bit for bit. Outside the union the
               output is the plate bit for bit, even when the master carries VFB corrections the linear EXR does
               not.
    gate       scores the stored PNG: the outside of the union is identical (hard), every material has pixels
               (hard), and each material's method decides its verdict: `recomposition` passes with the recorded
               expected dE00 (<= 1.0); `ratio` (no raw passes, expected <= 2.5), `degraded` (beauty only,
               <= 4.0) and `metal` (specular energy above 30% of the beauty: tinted reflections are out of
               scope) are advisory. The worst material decides the headline checks and `metrics.recolour`.

No generator: the colour comes from the render's own light, never from an image model. A locked material may
be recoloured (that is what this node is for: a RAL panel), and the report says so.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import numpy as np

from ..pipeline import PipelineError
from .common import (
    Prepared,
    QuoteContext,
    RunContext,
    finite_or_none,
    gate_report,
    material_share,
    pick,
    plain,
    to_display_png16,
)

KIND = "Recolour"
# The EXR elements recolour.apply can use. Anything else in the packet stays in the beauty untouched.
ELEMENTS = (
    "beauty",
    "diffuse_filter",
    "raw_lighting",
    "raw_gi",
    "reflection",
    "refraction",
    "specular",
    "self_illumination",
    "normals",
)
RAL_RE = re.compile(r"^(?:ral)?\s*(\d{4})$", re.IGNORECASE)
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
LEGEND_ALBEDO_KEYS = ("albedo_lab", "albedo_srgb", "base_srgb", "ral")


def parse_target(value: Any) -> dict[str, Any]:
    """A target colour in any of the forms the UI and the API use -> {kind, label, lab: [L, a, b]}."""
    from plate_gates import recolour

    try:
        if isinstance(value, Mapping):
            if value.get("lab") is not None:
                return parse_target(list(value["lab"])) | {"label": str(value.get("label") or "lab")}
            if value.get("ral") is not None:
                return parse_target(f"RAL {value['ral']}")
            if value.get("hex") is not None:
                return parse_target(str(value["hex"]))
        elif isinstance(value, (list, tuple)):
            lab = [float(v) for v in value]
            if len(lab) != 3 or not all(np.isfinite(lab)):
                raise ValueError("a Lab target is three finite numbers")
            return {"kind": "lab", "label": "lab", "lab": lab}
        elif isinstance(value, str):
            text = value.strip()
            if HEX_RE.match(text):
                return {
                    "kind": "hex",
                    "label": text.upper(),
                    "lab": [float(v) for v in recolour.hex_lab(text)],
                }
            m = RAL_RE.match(text)
            if m:
                return {
                    "kind": "ral",
                    "label": f"RAL {m.group(1)}",
                    "lab": [float(v) for v in recolour.ral_lab(m.group(1))],
                }
    except KeyError as e:
        raise PipelineError(str(e).strip("'\""), code="unknown_ral") from e
    except (TypeError, ValueError) as e:
        raise PipelineError(f"cannot read the recolour target {value!r}: {e}", code="bad_target") from e
    raise PipelineError(
        f"cannot read the recolour target {value!r}: use 'RAL 7016', '#383E42' or a Lab [L, a, b]",
        code="bad_target",
    )


def legend_entry(legend: Any, material: str) -> dict[str, Any] | None:
    entries = (legend or {}).get("entries", []) if isinstance(legend, Mapping) else []
    return next((e for e in entries if str(e.get("id", "")).lower() == material.lower()), None)


MAX_MATERIALS = 8
MATERIAL_RE = re.compile(r"m:[0-9a-fA-F]{8}")
METHODS = ("auto", "recomposition", "ratio", "degraded")


def _raw_entries(q: QuoteContext) -> tuple[list[Any], bool]:
    """The request's materials as written: (entries, listed). The request's params come before the node's saved
    ones, and within one source `materials` comes before a single `material`; the single pair is read the way
    `QuoteContext.param` reads it (each of its fields from the request, else the node)."""
    for source in (q.overlay, q.node_params):
        listed = pick(source, "materials")
        if listed is not None:
            if not isinstance(listed, (list, tuple)):
                raise PipelineError(
                    "Recolour params.materials is a list of {material, target} entries", code="bad_params"
                )
            return list(listed), True
        if pick(source, "material", "material_id", "materialId") is not None:
            break
    raw_target = q.param("target", "ral", "hex", "targetHex")
    if raw_target is None:
        raw_target = q.param("lab")
    pair = {
        "material": q.param("material", "material_id", "materialId"),
        "target": raw_target,
        "source_lab": q.param("source_lab", "sourceLab"),
        "method": q.param("method"),
    }
    return [pair], False


def _entry(
    raw: Mapping[str, Any],
    where: str,
    pv: Any,
    packet_id: str,
    present: list[str],
    default_method: Any,
) -> dict[str, Any]:
    """One validated entry: {material, target, source_lab, method}. `where` names it in a refusal."""
    material = pick(raw, "material", "material_id", "materialId")
    if not isinstance(material, str) or not MATERIAL_RE.fullmatch(material):
        raise PipelineError(
            f"Recolour needs {where}.material: an m:<8 hex> material id from the legend", code="bad_params"
        )
    material = material.lower()
    entry = legend_entry(pv.legend, material)
    if entry is None:
        raise PipelineError(f"material {material} is not in packet {packet_id}'s legend", code="unknown_id")
    raw_target = pick(raw, "target", "ral", "hex", "targetHex")
    if raw_target is None:
        raw_target = pick(raw, "lab")
    if raw_target is None:
        raise PipelineError(
            f"Recolour needs {where}.target (a RAL code, a hex colour or a Lab)", code="bad_params"
        )
    target = parse_target(raw_target)
    source_lab = pick(raw, "source_lab", "sourceLab")
    if source_lab is not None:
        source_lab = parse_target(list(source_lab))["lab"]
    method = pick(raw, "method", default=default_method)
    if method is not None and method not in METHODS:
        raise PipelineError(
            f"Recolour {where}.method is auto, recomposition, ratio or degraded", code="bad_params"
        )
    if "beauty" not in present:
        raise PipelineError(f"packet {packet_id} has no beauty element", code="elements_missing")
    has_albedo = source_lab is not None or any(entry.get(k) for k in LEGEND_ALBEDO_KEYS)
    if "diffuse_filter" not in present and not has_albedo:
        raise PipelineError(
            f"no source albedo for {material}: the packet has no diffuse_filter element and the legend entry "
            f"carries no albedo; give {where}.source_lab",
            code="no_source_albedo",
        )
    return {
        "material": material,
        "target": target,
        "source_lab": source_lab,
        "method": None if method in (None, "auto") else method,
    }


def prepare(q: QuoteContext) -> Prepared:
    packet_id = q.plate_packet_id()
    pv = q.packet(packet_id)
    if pv.tier != "A" or "beauty_exr" not in pv.files:
        raise PipelineError(
            f"packet {packet_id} is tier {pv.tier}: Recolour needs the EXR elements of a Tier A packet",
            code="tier_required",
        )
    raws, listed = _raw_entries(q)
    if not 1 <= len(raws) <= MAX_MATERIALS:
        raise PipelineError(
            f"Recolour params.materials holds 1 to {MAX_MATERIALS} entries, not {len(raws)}",
            code="bad_params",
        )
    seen: set[str] = set()
    for i, raw in enumerate(raws):
        if not isinstance(raw, Mapping):
            raise PipelineError(
                f"Recolour params.materials[{i}] is not a {{material, target}} entry", code="bad_params"
            )
        mid = pick(raw, "material", "material_id", "materialId")
        if isinstance(mid, str) and MATERIAL_RE.fullmatch(mid):
            if mid.lower() in seen:
                raise PipelineError(
                    f"Recolour params.materials names {mid.lower()} twice: each material appears once",
                    code="bad_params",
                )
            seen.add(mid.lower())
    if listed and q.param("source_lab", "sourceLab") is not None:
        q.notes.append("params.source_lab is ignored with params.materials; give source_lab on each entry")
    present = [e["element"] for e in pv.files["beauty_exr"]["elements"]]
    default_method = q.param("method")
    entries = [
        _entry(raw, f"params.materials[{i}]" if listed else "params", pv, packet_id, present, default_method)
        for i, raw in enumerate(raws)
    ]
    spec = {
        "materials": entries,
        "elements": [e for e in ELEMENTS if e in present],
        "plate": pv.plate_hash,
        "exr": pv.files["beauty_exr"]["hash"]["hex"],
    }
    return Prepared(spec, packet_id)


def spec_entries(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The spec's materials. A run quoted before T90 holds one `material` + `target` pair; it reads as one entry."""
    listed = spec.get("materials")
    if listed:
        return [dict(e) for e in listed]
    return [
        {
            "material": spec["material"],
            "target": spec["target"],
            "source_lab": spec.get("source_lab"),
            "method": spec.get("method"),
        }
    ]


def compute(
    pv: Any, spec: Mapping[str, Any]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any], np.ndarray]], dict[str, np.ndarray]]:
    """recolour.apply per entry on the packet's elements (read once).

    Returns ([(entry, result, material weight (H, W))], elements)."""
    from plate_gates import recolour
    from plate_pixels import exr

    exr_path = pv.root / pv.files["beauty_exr"]["path"]
    elements = exr.read_elements(exr_path, pv.manifest, list(spec["elements"]))
    results = []
    for entry in spec_entries(spec):
        share = material_share(pv, entry["material"])
        res = recolour.apply(
            elements,
            share,
            entry["target"]["lab"],
            legend_entry(pv.legend, entry["material"]),
            source_lab=entry.get("source_lab"),
            method=entry.get("method"),
            K=pv.manifest["camera"],
        )
        results.append((entry, res, share))
    return results, elements


def summary(res: Mapping[str, Any]) -> dict[str, Any]:
    """The recolour.apply result without its pixels (JSON, what `normalized` and the report keep)."""
    return {k: v for k, v in res.items() if k != "rgb_linear"}


def is_exact(info: Mapping[str, Any]) -> bool:
    """Does the recorded method meet the hard bound (recomposition, and not a metal)?"""
    return info.get("method") == "recomposition" and not info.get("metal")


def worst_index(infos: list[Mapping[str, Any]]) -> int:
    """The material that decides the verdict: one that misses the bound before one that meets it, then the larger
    expected dE00; the first of equals."""

    def key(i: int) -> tuple[bool, float]:
        info = infos[i]
        expected = finite_or_none(info.get("expected_de00"))
        return (not is_exact(info), expected if expected is not None else 0.0)

    return max(range(len(infos)), key=lambda i: (key(i), -i))


def preview(c: RunContext) -> dict[str, Any]:
    """Which method the packet's elements allow and the error to expect for each material, without making a
    picture. The headline keys are the worst material's; `materials` holds one row per entry."""
    results, _elements = compute(c.packet(), c.spec)
    keep = (
        "method",
        "expected_de00",
        "degraded",
        "metal",
        "specular_share",
        "n_px",
        "elements_used",
        "notes",
    )
    rows = [
        plain({k: res.get(k) for k in keep} | {"target": entry["target"], "material": entry["material"]})
        for entry, res, _share in results
    ]
    return plain(rows[worst_index([res for _e, res, _s in results])] | {"materials": rows})


def normalize(c: RunContext) -> dict[str, Any]:
    spec = c.spec
    pv = c.packet()
    results, elements = compute(pv, spec)
    pipeline = c.runner.colour(pv.manifest)
    beauty = np.asarray(elements["beauty"][..., :3], dtype=np.float32)
    disp_old = pipeline.rederive_display(beauty)
    master16 = pv.plate16()
    master8 = master16.astype(np.float32) / 257.0
    union = np.zeros(master16.shape[:2], dtype=bool)
    total = np.zeros(master16.shape, dtype=np.int32)  # sum_i (single_i - master), 16-bit counts
    for _e, res, share in results:
        sel = share > 0.0
        if not sel.any():
            continue
        disp_new = pipeline.rederive_display(res["rgb_linear"])
        # exactly what a recolour of this material alone stores on these pixels
        single = to_display_png16(master8[sel] + (disp_new[sel] - disp_old[sel]))
        total[sel] += single.astype(np.int32) - master16[sel].astype(np.int32)
        union |= sel
    out16 = master16.copy()
    if union.any():
        out16[union] = np.clip(master16[union].astype(np.int32) + total[union], 0, 65535).astype(np.uint16)
    infos = [summary(res) for _e, res, _s in results]
    return {
        "outputs": [c.put_png16(out16)],
        "recolour": infos[worst_index(infos)],
        "recolours": [
            {"material": entry["material"]} | info
            for (entry, _r, _s), info in zip(results, infos, strict=True)
        ],
        "materials": [
            {
                "material": entry["material"],
                "target": entry["target"],
                "method": info["method"],
                "expected_de00": info["expected_de00"],
            }
            for (entry, _r, _s), info in zip(results, infos, strict=True)
        ],
        "colour": pipeline.describe(),
        "mask_px": int(union.sum()),
    }


def gate(c: RunContext) -> Any:
    from plate_gates import recolour, thresholds
    from plate_gates.report import Check, scored_check

    spec, norm = c.spec, c.normalized
    pv = c.packet()
    entries = spec_entries(spec)
    infos = [
        {k: v for k, v in i.items() if k != "material"} for i in norm.get("recolours") or [norm["recolour"]]
    ]
    out16 = c.read_png16(norm["outputs"][0])
    master16 = pv.plate16()
    changed = np.any(out16 != master16, axis=2)
    union = np.zeros(changed.shape, dtype=bool)
    rows: dict[str, dict[str, Any]] = {}
    locked = set(pv.policy.get("locked_ids") or [])
    notes: list[str] = []
    for entry, info in zip(entries, infos, strict=True):
        mid = entry["material"]
        share = material_share(pv, mid)
        sel = share > 0.0
        union |= sel
        moved = recolour.compare_display(
            out16.astype(np.float32) / 257.0, master16.astype(np.float32) / 257.0, share >= 0.5
        )
        rows[mid] = {
            "material": mid,
            "target": entry["target"],
            "method": info.get("method"),
            "expected_de00": finite_or_none(info.get("expected_de00")),
            "exact": is_exact(info),
            "metal": bool(info.get("metal")),
            "mask_px": int(sel.sum()),
            "inside_changed_px": int(changed[sel].sum()),
            "moved_de00_median": finite_or_none(moved.get("de00_median")),
            "moved_de00_of_means": finite_or_none(moved.get("de00_of_means")),
        }
        for n in info.get("notes") or []:
            notes.append(n if len(entries) == 1 else f"{mid}: {n}")
        if mid in locked:
            notes.append(f"{mid} is locked by the packet policy; Recolour changed it by the artist's request")
    outside_changed = int(changed[~union].sum())
    inside_changed = int(changed[union].sum())
    w = worst_index(infos)
    worst, worst_entry = infos[w], entries[w]
    worst_row = rows[worst_entry["material"]]
    min_px = min(r["mask_px"] for r in rows.values())
    all_exact = all(r["exact"] for r in rows.values())
    checks = [
        Check(metric_id="recolour.mask_px", value=float(min_px), status="pass" if min_px > 0 else "fail"),
        Check(
            metric_id="outside_mask.changed_px",
            value=float(outside_changed),
            status="pass" if outside_changed == 0 else "fail",
        ),
        Check(
            metric_id="recolour.expected_de00",
            value=finite_or_none(worst.get("expected_de00")),
            status="pass" if all_exact else "advisory",
        ),
        # The registered bound of the worst material's method; the value is the method's expected dE00 (no
        # re-render exists at run time to measure against: validate_against_rerender does that once).
        scored_check(
            thresholds.load(),
            "recolour.de00_median",
            finite_or_none(worst.get("expected_de00")),
            status="pass" if all_exact else "advisory",
            variant=worst.get("method"),
        ),
    ]
    metrics = {
        "recolour": worst,
        "per_material": rows,
        "outside_mask": {"identical_outside": outside_changed == 0, "changed_px": outside_changed},
        "inside_changed_px": inside_changed,
        "moved_de00_median": worst_row["moved_de00_median"],
        "moved_de00_of_means": worst_row["moved_de00_of_means"],
        "material": worst_entry["material"],
        "target": worst_entry["target"],
        "colour": norm.get("colour"),
    }
    return gate_report(c, tier=pv.tier, metrics=metrics, checks=checks, notes=notes)


def score(report: Any) -> float:
    exp = ((report.metrics or {}).get("recolour") or {}).get("expected_de00")
    return -float(exp) if isinstance(exp, (int, float)) else 0.0


__all__ = [
    "KIND",
    "gate",
    "legend_entry",
    "normalize",
    "parse_target",
    "prepare",
    "preview",
    "score",
    "spec_entries",
]
