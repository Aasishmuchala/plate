"""The Recolour node: change one material's colour with the render's own passes (T31's recomposition, as a node).

    prepare    the material (`params.material`, an `m:` id of the packet's legend) and the target colour
               (`params.target`: `RAL 7016`, `7016`, `#383E42`, `{lab: [L, a, b]}` or `[L, a, b]`; the UI's
               `targetHex` is the fallback), resolved to a Lab at quote time so the run is keyed on the number
    normalize  recolour.apply on the EXR elements (raw lighting, GI, diffuse filter, reflections when the packet
               has them), weighted by the material's Cryptomatte coverage; the result goes back through the
               packet's own colour pipeline (OCIO or legacy gamma) to display, and is laid onto the display
               master as a DELTA: master + (display(recoloured) - display(original)) wherever the material has
               coverage, the master itself everywhere else. Outside the material the output is the plate bit
               for bit, even when the master carries VFB corrections the linear EXR does not.
    gate       scores the stored PNG: the outside is identical (hard), the material has pixels (hard), and the
               method decides the verdict: `recomposition` passes with the recorded expected dE00 (<= 1.0);
               `ratio` (no raw passes, expected <= 2.5), `degraded` (beauty only, <= 4.0) and `metal` (specular
               energy above 30% of the beauty: tinted reflections are out of scope) are advisory.

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


def prepare(q: QuoteContext) -> Prepared:
    packet_id = q.plate_packet_id()
    pv = q.packet(packet_id)
    if pv.tier != "A" or "beauty_exr" not in pv.files:
        raise PipelineError(
            f"packet {packet_id} is tier {pv.tier}: Recolour needs the EXR elements of a Tier A packet",
            code="tier_required",
        )
    material = q.param("material", "material_id", "materialId")
    if not isinstance(material, str) or not re.fullmatch(r"m:[0-9a-fA-F]{8}", material):
        raise PipelineError(
            "Recolour needs params.material: an m:<8 hex> material id from the legend", code="bad_params"
        )
    material = material.lower()
    entry = legend_entry(pv.legend, material)
    if entry is None:
        raise PipelineError(f"material {material} is not in packet {packet_id}'s legend", code="unknown_id")
    raw_target = q.param("target", "ral", "hex", "targetHex")
    if raw_target is None:
        raw_target = q.param("lab")
    if raw_target is None:
        raise PipelineError(
            "Recolour needs params.target (a RAL code, a hex colour or a Lab)", code="bad_params"
        )
    target = parse_target(raw_target)
    source_lab = q.param("source_lab", "sourceLab")
    if source_lab is not None:
        source_lab = parse_target(list(source_lab))["lab"]
    method = q.param("method")
    if method is not None and method not in ("auto", "recomposition", "ratio", "degraded"):
        raise PipelineError(
            "Recolour params.method is auto, recomposition, ratio or degraded", code="bad_params"
        )
    present = [e["element"] for e in pv.files["beauty_exr"]["elements"]]
    if "beauty" not in present:
        raise PipelineError(f"packet {packet_id} has no beauty element", code="elements_missing")
    has_albedo = source_lab is not None or any(entry.get(k) for k in LEGEND_ALBEDO_KEYS)
    if "diffuse_filter" not in present and not has_albedo:
        raise PipelineError(
            "no source albedo: the packet has no diffuse_filter element and the legend entry carries no "
            "albedo; give params.source_lab",
            code="no_source_albedo",
        )
    spec = {
        "material": material,
        "target": target,
        "source_lab": source_lab,
        "method": None if method in (None, "auto") else method,
        "elements": [e for e in ELEMENTS if e in present],
        "plate": pv.plate_hash,
        "exr": pv.files["beauty_exr"]["hash"]["hex"],
    }
    return Prepared(spec, packet_id)


def compute(pv: Any, spec: Mapping[str, Any]) -> tuple[dict[str, Any], np.ndarray, dict[str, np.ndarray]]:
    """recolour.apply on the packet's elements. Returns (result, material weight (H, W), elements)."""
    from plate_gates import recolour
    from plate_pixels import exr

    exr_path = pv.root / pv.files["beauty_exr"]["path"]
    elements = exr.read_elements(exr_path, pv.manifest, list(spec["elements"]))
    share = material_share(pv, spec["material"])
    res = recolour.apply(
        elements,
        share,
        spec["target"]["lab"],
        legend_entry(pv.legend, spec["material"]),
        source_lab=spec.get("source_lab"),
        method=spec.get("method"),
        K=pv.manifest["camera"],
    )
    return res, share, elements


def summary(res: Mapping[str, Any]) -> dict[str, Any]:
    """The recolour.apply result without its pixels (JSON, what `normalized` and the report keep)."""
    return {k: v for k, v in res.items() if k != "rgb_linear"}


def preview(c: RunContext) -> dict[str, Any]:
    """Which method the packet's elements allow and the error to expect, without making a picture."""
    res, _share, _elements = compute(c.packet(), c.spec)
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
    return plain({k: res.get(k) for k in keep} | {"target": c.spec["target"], "material": c.spec["material"]})


def normalize(c: RunContext) -> dict[str, Any]:
    spec = c.spec
    pv = c.packet()
    res, share, elements = compute(pv, spec)
    pipeline = c.runner.colour(pv.manifest)
    beauty = np.asarray(elements["beauty"][..., :3], dtype=np.float32)
    disp_new = pipeline.rederive_display(res["rgb_linear"])
    disp_old = pipeline.rederive_display(beauty)
    master16 = pv.plate16()
    master8 = master16.astype(np.float32) / 257.0
    sel = share > 0.0
    out16 = master16.copy()
    if sel.any():
        out16[sel] = to_display_png16(master8[sel] + (disp_new[sel] - disp_old[sel]))
    return {
        "outputs": [c.put_png16(out16)],
        "recolour": summary(res),
        "colour": pipeline.describe(),
        "mask_px": int(sel.sum()),
    }


def gate(c: RunContext) -> Any:
    from plate_gates import recolour
    from plate_gates.report import Check

    spec, norm = c.spec, c.normalized
    pv = c.packet()
    info = norm["recolour"]
    out16 = c.read_png16(norm["outputs"][0])
    master16 = pv.plate16()
    share = material_share(pv, spec["material"])
    sel = share > 0.0
    changed = np.any(out16 != master16, axis=2)
    outside_changed = int(changed[~sel].sum())
    inside_changed = int(changed[sel].sum())
    mask = share >= 0.5
    moved = recolour.compare_display(
        out16.astype(np.float32) / 257.0, master16.astype(np.float32) / 257.0, mask
    )
    exact = info["method"] == "recomposition" and not info.get("metal")
    checks = [
        Check(
            metric_id="recolour.mask_px", value=float(int(sel.sum())), status="pass" if sel.any() else "fail"
        ),
        Check(
            metric_id="outside_mask.changed_px",
            value=float(outside_changed),
            status="pass" if outside_changed == 0 else "fail",
        ),
        Check(
            metric_id="recolour.expected_de00",
            value=finite_or_none(info.get("expected_de00")),
            status="pass" if exact else "advisory",
        ),
    ]
    notes = list(info.get("notes") or [])
    if spec["material"] in set(pv.policy.get("locked_ids") or []):
        notes.append(
            f"{spec['material']} is locked by the packet policy; Recolour changed it by the artist's request"
        )
    metrics = {
        "recolour": info,
        "outside_mask": {"identical_outside": outside_changed == 0, "changed_px": outside_changed},
        "inside_changed_px": inside_changed,
        "moved_de00_median": finite_or_none(moved.get("de00_median")),
        "moved_de00_of_means": finite_or_none(moved.get("de00_of_means")),
        "material": spec["material"],
        "target": spec["target"],
        "colour": norm.get("colour"),
    }
    return gate_report(c, tier=pv.tier, metrics=metrics, checks=checks, notes=notes)


def score(report: Any) -> float:
    exp = ((report.metrics or {}).get("recolour") or {}).get("expected_de00")
    return -float(exp) if isinstance(exp, (int, float)) else 0.0


__all__ = ["KIND", "gate", "legend_entry", "normalize", "parse_target", "prepare", "preview", "score"]
