"""The LightMix node: relight a frame by weighting its light-select passes (T32's NNLS, as a node).

    light selects   one linear RGB pass per light: the `light_select` elements of the beauty EXR (one entry per
                    light, `light_name` required) and `x_light_<name>` extension files (an opt-in pass written
                    to its own EXR next to the packet; PACKET.md 4.1). A name found twice is an error.
    prepare         the lights (all of them, in a fixed order), the gains the artist set (`params.gains`: a number
                    or an `[r, g, b]` vector per light, each >= 0), a mood preset (`params.preset`, initial
                    guesses only) or a target picture (`params.targetHash` / `target_hash`, a CAS PNG: the
                    display picture to match) the gains are solved for
    normalize       gains: solved by lightmix.solve against the target in linear light (the packet's own colour
                    pipeline undoes the display transform when it can, the sRGB curve otherwise; the solver
                    returns one number per light), else the given gains (a light with no entry keeps the preset's
                    gain, else 1.0), else the preset, else 1.0 each. A vector gain scales that light's red,
                    green and blue separately; `max_gain` clips each channel. The frame is
                    sum_i g_i L_i (`keep_base: true` adds the part of the beauty the selects do not carry:
                    beauty - sum_i L_i, e.g. emissives and reflections of the environment), out through the
                    colour pipeline to a PNG16
    gate            on the stored PNG: right size, finite, in range; gains non-negative by construction; the
                    NNLS residual and any gain that hit `max_gain` are advisory numbers, not failures
    preview         the lights, the gains the spec would use and, per light, whether the gain is a `scalar` or
                    an `rgb` vector (`channels`)

Lighting changes come from the render's own light selects or a V-Ray re-render, never from an image model.
`verify-on-box`: whether V-Ray 7's light selects sum to the beauty minus a base (so `keep_base` is the right
default) is a box question; the node reports `sum_vs_beauty_rms` so the artist can see it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import numpy as np

from ..pipeline import PacketView, PipelineError
from .common import Prepared, QuoteContext, RunContext, finite_or_none, gate_report, pick, to_display_png16

KIND = "LightMix"
X_PREFIX = "x_light_"
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,60}$")
DEFAULT_MAX_GAIN = 8.0
RESIDUAL_ADVISORY = 0.05  # linear RMS; above it the lights cannot make the target and the artist should look


def light_names(pv: PacketView) -> list[str]:
    """The packet's light-select names, in a stable order: EXR elements first (manifest order), then `x_light_*`
    files (by name). Reads no pixels."""
    names: list[str] = []
    exr_ref = pv.files.get("beauty_exr") or {}
    for el in exr_ref.get("elements") or []:
        if el.get("element") == "light_select" and el.get("light_name"):
            names.append(str(el["light_name"]))
    for role in sorted(pv.files):
        ref = pv.files[role]
        if role.startswith(X_PREFIX) and ref.get("kind") == "file" and ref.get("format") == "exr":
            names.append(role[len(X_PREFIX) :])
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise PipelineError(f"light select names repeat in packet {pv.packet_id}: {dup}", code="bad_packet")
    return names


def load_light_selects(pv: PacketView) -> dict[str, np.ndarray]:
    """{name: linear float32 (H, W, 3)} for every light select of the packet."""
    from plate_pixels import exr

    out: dict[str, np.ndarray] = {}
    exr_ref = pv.files.get("beauty_exr") or {}
    wanted = [e for e in exr_ref.get("elements") or [] if e.get("element") == "light_select"]
    if wanted:
        got = exr.read_elements(pv.root / exr_ref["path"], pv.manifest, ["light_select"])
        for el in wanted:
            out[str(el["light_name"])] = np.ascontiguousarray(
                got[exr.element_key(el)][..., :3], dtype=np.float32
            )
    for role in sorted(pv.files):
        ref = pv.files[role]
        if not (role.startswith(X_PREFIX) and ref.get("kind") == "file" and ref.get("format") == "exr"):
            continue
        name = role[len(X_PREFIX) :]
        fake = {
            "files": {
                "beauty_exr": {
                    "elements": [
                        {
                            "element": "light_select",
                            "light_name": name,
                            "layer": "RGBA",
                            "channels": ["R", "G", "B"],
                        }
                    ]
                }
            }
        }
        got = exr.read_elements(pv.root / ref["path"], fake, ["light_select"])
        out[name] = np.ascontiguousarray(
            got[exr.element_key(fake["files"]["beauty_exr"]["elements"][0])], dtype=np.float32
        )
    return out


def _gain_in(name: str, g: Any) -> float | list[float]:
    """One gain from params: a finite number >= 0, or an `[r, g, b]` list of three of them."""
    bad = PipelineError(
        f"LightMix gain for {name!r} must be a finite number >= 0 or an [r, g, b] vector of three, got {g!r}",
        code="bad_params",
    )
    if isinstance(g, (list, tuple)):
        if len(g) != 3:
            raise bad
        parts: list[float] = []
        for c in g:
            f = finite_or_none(c)
            if f is None or f < 0:
                raise bad
            parts.append(f)
        return parts
    f = finite_or_none(g)
    if f is None or f < 0:
        raise bad
    return f


def _gains_in(raw: Any, names: list[str]) -> dict[str, float | list[float]] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PipelineError(
            "LightMix params.gains must map light names to numbers or [r, g, b] vectors", code="bad_params"
        )
    unknown = sorted(set(raw) - set(names))
    if unknown:
        raise PipelineError(
            f"LightMix params.gains names unknown lights {unknown}; the packet has {names}", code="bad_params"
        )
    return {str(name): _gain_in(str(name), g) for name, g in raw.items()}


def prepare(q: QuoteContext) -> Prepared:
    from plate_gates import lightmix

    packet_id = q.plate_packet_id()
    pv = q.packet(packet_id)
    if pv.tier != "A" or "beauty_exr" not in pv.files:
        raise PipelineError(
            f"packet {packet_id} is tier {pv.tier}: LightMix needs a Tier A packet", code="tier_required"
        )
    names = light_names(pv)
    if not names:
        raise PipelineError(
            f"packet {packet_id} carries no light selects (light_select elements or x_light_* files): "
            "render them with the shot or use the Light node",
            code="no_light_selects",
        )
    preset = q.param("preset")
    if preset is not None and preset not in lightmix.mood_presets:
        raise PipelineError(
            f"unknown mood preset {preset!r}; known: {sorted(lightmix.mood_presets)}", code="bad_params"
        )
    target = pick(q.params, "target_hash", "targetHash")
    if target is not None:
        q.need_asset(target, "LightMix target")
    max_gain = finite_or_none(q.param("max_gain", "maxGain", default=DEFAULT_MAX_GAIN))
    if max_gain is None or max_gain <= 0:
        raise PipelineError("LightMix params.max_gain must be a positive number", code="bad_params")
    spec = {
        "lights": names,
        "gains": _gains_in(q.param("gains"), names),
        "preset": preset,
        "target_hash": target,
        "max_gain": max_gain,
        "keep_base": bool(pick(q.params, "keep_base", "keepBase", default=False)),
        "seed": int(q.param("seed", default=0)),
        "plate": pv.plate_hash,
        "exr": pv.files["beauty_exr"]["hash"]["hex"],
    }
    return Prepared(spec, packet_id)


def target_linear(c_pipeline: Any, png16: np.ndarray) -> np.ndarray:
    """A display picture (uint16 sRGB-encoded PNG) -> scene-linear float32, through the packet's colour pipeline
    when its view transform is invertible, else the analytic sRGB curve (the PNG is a picture, not a master)."""
    from plate_pixels.ocio import ColourError, srgb_eotf

    display8 = png16.astype(np.float32) / 257.0
    if c_pipeline is not None and c_pipeline.invertible:
        try:
            return np.asarray(c_pipeline.display_to_linear(display8), dtype=np.float32)
        except ColourError:
            pass
    return np.asarray(srgb_eotf(display8 / 255.0), dtype=np.float32)


def _components(g: float | list[float]) -> list[float]:
    return [float(c) for c in g] if isinstance(g, (list, tuple)) else [float(g)]


def _clip(g: float | list[float], max_gain: float) -> float | list[float]:
    """The gain held to `max_gain`, each channel of a vector on its own."""
    if isinstance(g, (list, tuple)):
        return [float(min(c, max_gain)) for c in g]
    return float(min(g, max_gain))


def gain_channels(gains: Mapping[str, Any]) -> dict[str, str]:
    """{light: "scalar" | "rgb"}: whether each gain is one number or a per-channel vector."""
    from plate_gates import lightmix

    return {n: lightmix.gain_channels(g) for n, g in gains.items()}


def resolve_gains(
    spec: Mapping[str, Any], selects: Mapping[str, np.ndarray], target_lin: np.ndarray | None
) -> dict[str, Any]:
    """The gains to apply and where they came from: {gains, mode, residual_rms?, clipped}."""
    from plate_gates import lightmix

    names = list(spec["lights"])
    if target_lin is not None:
        solved = lightmix.solve(
            {n: selects[n] for n in names}, target_lin, None, spec["max_gain"], seed=spec.get("seed", 0)
        )
        return {
            "gains": solved["gains"],
            "mode": "solve",
            "residual_rms": solved["residual_rms"],
            "clipped": solved["clipped"],
        }
    base = lightmix.preset_gains(spec["preset"], names) if spec.get("preset") else dict.fromkeys(names, 1.0)
    mode = "preset" if spec.get("preset") else "unity"
    if spec.get("gains"):
        base.update(spec["gains"])
        mode = "explicit"
    max_gain = spec["max_gain"]
    clipped = [n for n, g in base.items() if max(_components(g)) > max_gain]
    gains = {n: _clip(g, max_gain) for n, g in base.items()}
    return {"gains": gains, "mode": mode, "residual_rms": None, "clipped": clipped}


def _base(pv: PacketView, selects: Mapping[str, np.ndarray], names: list[str]) -> tuple[np.ndarray, float]:
    """What the light selects do not carry: (beauty - sum of the selects at gain 1, its RMS). Linear float64."""
    from plate_gates import lightmix
    from plate_pixels import exr

    beauty = exr.read_elements(pv.root / pv.files["beauty_exr"]["path"], pv.manifest, ["beauty"])["beauty"]
    unity = lightmix.recompose({n: selects[n] for n in names}, dict.fromkeys(names, 1.0)).astype(np.float64)
    base = beauty[..., :3].astype(np.float64) - unity
    return base, float(np.sqrt(np.mean(base * base)))


def _gains(
    c: RunContext, pv: PacketView, selects: Mapping[str, np.ndarray]
) -> tuple[dict[str, Any], np.ndarray, float]:
    """(chosen gains, base, base RMS) for the spec; a target picture is matched after taking the base off when
    the spec keeps it."""
    spec = c.spec
    base, base_rms = _base(pv, selects, list(spec["lights"]))
    target = None
    if spec.get("target_hash"):
        png = c.read_png16(spec["target_hash"])
        if png.shape[:2] != pv.plate16().shape[:2]:
            raise PipelineError(
                f"the target picture is {png.shape[1]} x {png.shape[0]}, the packet "
                f"{pv.plate16().shape[1]} x {pv.plate16().shape[0]}",
                code="size_mismatch",
            )
        target = target_linear(c.runner.colour(pv.manifest), png)
        if spec.get("keep_base"):
            target = target - base.astype(np.float32)
    return resolve_gains(spec, selects, target), base, base_rms


def preview(c: RunContext) -> dict[str, Any]:
    """The packet's lights and the gains the spec would use: `{}` learns the light selects (unity or the preset),
    a target picture seeds them by NNLS. Nothing is rendered or stored."""
    pv = c.packet()
    chosen, _base_px, base_rms = _gains(c, pv, load_light_selects(pv))
    return {
        "lights": list(c.spec["lights"]),
        "gains": chosen["gains"],
        "channels": gain_channels(chosen["gains"]),
        "mode": chosen["mode"],
        "residual_rms": finite_or_none(chosen["residual_rms"]),
        "clipped": chosen["clipped"],
        "sum_vs_beauty_rms": finite_or_none(base_rms),
    }


def normalize(c: RunContext) -> dict[str, Any]:
    from plate_gates import lightmix

    spec = c.spec
    pv = c.packet()
    pipeline = c.runner.colour(pv.manifest)
    selects = load_light_selects(pv)
    names = list(spec["lights"])
    missing = [n for n in names if n not in selects]
    if missing:
        raise PipelineError(
            f"light selects vanished from packet {pv.packet_id}: {missing}", code="bad_packet"
        )
    chosen, base, base_rms = _gains(c, pv, selects)
    mixed = lightmix.recompose({n: selects[n] for n in names}, chosen["gains"]).astype(np.float64)
    if spec.get("keep_base"):
        mixed = mixed + base
    display = pipeline.rederive_display(np.maximum(mixed, 0.0).astype(np.float32))
    return {
        "outputs": [c.put_png16(to_display_png16(display))],
        "lightmix": {
            "gains": chosen["gains"],
            "channels": gain_channels(chosen["gains"]),
            "mode": chosen["mode"],
            "residual_rms": finite_or_none(chosen["residual_rms"]),
            "clipped": chosen["clipped"],
            "max_gain": spec["max_gain"],
            "lights": names,
            "keep_base": bool(spec.get("keep_base")),
            "sum_vs_beauty_rms": finite_or_none(base_rms),
        },
        "colour": pipeline.describe(),
    }


def gate(c: RunContext) -> Any:
    from plate_gates.report import Check

    norm = c.normalized
    pv = c.packet()
    info = norm["lightmix"]
    out = c.read_png16(norm["outputs"][0])
    plate = pv.plate16()
    same_size = out.shape == plate.shape
    in_range = bool(np.isfinite(out.astype(np.float64)).all())
    negative = [n for n, g in info["gains"].items() if min(_components(g)) < 0]
    residual = info.get("residual_rms")
    checks = [
        Check(
            metric_id="lightmix.output_valid",
            value=1.0 if same_size and in_range else 0.0,
            status="pass" if same_size and in_range else "fail",
        ),
        Check(
            metric_id="lightmix.gains_nonneg",
            value=float(len(negative)),
            status="pass" if not negative else "fail",
        ),
        Check(
            metric_id="lightmix.clipped",
            value=float(len(info["clipped"])),
            status="pass" if not info["clipped"] else "advisory",
        ),
    ]
    if residual is not None:
        checks.append(
            Check(
                metric_id="lightmix.residual_rms",
                value=residual,
                status="pass" if residual <= RESIDUAL_ADVISORY else "advisory",
            )
        )
    notes: list[str] = []
    if info["clipped"]:
        notes.append(f"gains hit max_gain {info['max_gain']:g}: {', '.join(info['clipped'])}")
    return gate_report(
        c, tier=pv.tier, metrics={"lightmix": info, "colour": norm.get("colour")}, checks=checks, notes=notes
    )


def score(report: Any) -> float:
    r = ((report.metrics or {}).get("lightmix") or {}).get("residual_rms")
    return -float(r) if isinstance(r, (int, float)) else 0.0


__all__ = [
    "KIND",
    "gain_channels",
    "gate",
    "light_names",
    "load_light_selects",
    "normalize",
    "prepare",
    "preview",
    "resolve_gains",
    "score",
    "target_linear",
]
