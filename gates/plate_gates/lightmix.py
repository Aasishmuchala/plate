"""LightMix: per-light gains from non-negative least squares on V-Ray light selects.

A light-select pass holds the scene lit by ONE light (or light group), in linear RGB. Rendered light adds, so a
mood is a weighted sum  sum_i g_i * L_i  with g_i >= 0 (a negative light does not exist). solve() finds the
gains that bring that sum closest to a target image (a reference grade, a dusk plate, a client sketch):

    min_g || sum_i g_i L_i - T ||_2   subject to  g >= 0

scipy.optimize.nnls runs on a seeded random sample of at most 4096 pixels inside the mask (three rows per pixel),
the solution is clipped to max_gain, and the residual is then measured on EVERY valid masked pixel with the
clipped gains, so the reported number is the error of the recomposite the artist will actually see. No generator
is involved: this is numeric, and recompose() is plain arithmetic on the passes (the doctrine: lighting changes
come from light selects or a V-Ray re-render, never from an image model).

Everything is linear light. Convert display-referred pixels first (plate_pixels colour core); nothing here
applies a transfer curve.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence

import numpy as np
from scipy.optimize import nnls

__all__ = ["solve", "recompose", "mood_presets", "preset_gains", "SAMPLE_PIXELS"]

SAMPLE_PIXELS = 4096

# Initial guesses for the gain sliders, NOT solver output: key = light-select name or fnmatch pattern (case
# insensitive, first match in declaration order wins), value = starting gain. solve() never reads these.
mood_presets: dict[str, dict[str, float]] = {
    "dusk": {"sun": 0.15, "sky": 0.6, "interior_*": 3.0},
}


def _as_bool(mask: np.ndarray) -> np.ndarray:
    """Any mask encoding -> bool (H, W). uint8: > 127 (or > 0 for a 0/1 mask); other ints: > 0; floats: >= 0.5."""
    m = np.asarray(mask)
    if m.ndim == 3:
        m = m[..., 0]
    if m.dtype == bool:
        return m
    if m.dtype == np.uint8:
        return m > 127 if m.max(initial=0) > 1 else m > 0
    if np.issubdtype(m.dtype, np.floating):
        return m >= 0.5
    return m > 0


def _check_images(light_selects: Mapping[str, np.ndarray]) -> tuple[int, int]:
    if not light_selects:
        raise ValueError("light_selects is empty: LightMix needs at least one light-select pass")
    shapes = {}
    for name, img in light_selects.items():
        a = np.asarray(img)
        if a.ndim != 3 or a.shape[2] != 3:
            raise ValueError(f"light select {name!r} must be linear (H, W, 3), got shape {a.shape}")
        shapes[name] = a.shape
    first = next(iter(shapes.values()))
    bad = {n: s for n, s in shapes.items() if s != first}
    if bad:
        raise ValueError(f"light selects differ in shape from {first}: {bad}")
    return first[0], first[1]


def _validity(
    light_selects: Mapping[str, np.ndarray], target: np.ndarray, mask: np.ndarray | None, hw: tuple[int, int]
) -> np.ndarray:
    """Flat bool (H*W,): inside the mask and finite in the target and in every pass (a stray inf is dropped)."""
    ok = np.isfinite(target).all(axis=2)
    for img in light_selects.values():
        ok &= np.isfinite(np.asarray(img)).all(axis=2)
    if mask is not None:
        m = _as_bool(mask)
        if m.shape != hw:
            raise ValueError(f"mask shape {m.shape} does not match the images {hw}")
        ok &= m
    return ok.reshape(-1)


def solve(
    light_selects: Mapping[str, np.ndarray],
    target_rgb_linear: np.ndarray,
    mask: np.ndarray | None = None,
    max_gain: float = 8.0,
    *,
    seed: int = 0,
    sample: int = SAMPLE_PIXELS,
) -> dict:
    """Non-negative gains for the light selects that best reproduce target_rgb_linear.

    light_selects: name -> linear float (H, W, 3); target_rgb_linear: linear (H, W, 3); mask: optional, bool, 0/1 or
    0..255 uint8, or a float mask >= 0.5 (only pixels inside it count). Returns
    {"gains": {name: float}, "residual_rms": float, "clipped": [names whose unclipped gain exceeded max_gain]}.
    All values are plain Python types (JSON-ready for project.json). The pixel sample is seeded, so the same
    inputs always give the same answer. Raises ValueError on empty input, shape mismatch or an empty mask.
    """
    if not max_gain > 0 or not np.isfinite(max_gain):
        raise ValueError(f"max_gain must be a positive finite number, got {max_gain!r}")
    if sample < 1:
        raise ValueError(f"sample must be >= 1, got {sample!r}")
    hw = _check_images(light_selects)
    target = np.asarray(target_rgb_linear)
    if target.shape != (*hw, 3):
        raise ValueError(f"target must be linear (H, W, 3) = {(*hw, 3)}, got shape {target.shape}")

    names = list(light_selects)
    flat = {n: np.asarray(light_selects[n]).reshape(-1, 3) for n in names}
    t_flat = target.reshape(-1, 3)

    valid = np.flatnonzero(_validity(light_selects, target, mask, hw))
    if valid.size == 0:
        raise ValueError("no valid pixels: the mask is empty or every masked pixel is non-finite")
    if valid.size > sample:
        idx = np.sort(np.random.default_rng(seed).choice(valid, size=sample, replace=False))
    else:
        idx = valid

    # One column per light, three rows (R, G, B) per sampled pixel; float64 for the solver.
    a = np.stack([flat[n][idx].astype(np.float64).reshape(-1) for n in names], axis=1)
    b = t_flat[idx].astype(np.float64).reshape(-1)
    raw, _ = nnls(a, b, maxiter=max(50 * len(names), 1000))

    clipped = [n for n, g in zip(names, raw, strict=True) if g > max_gain]
    gains = {n: float(min(max(g, 0.0), max_gain)) for n, g in zip(names, raw, strict=True)}

    # Honest residual: every valid masked pixel, the gains the artist will get (after the clip).
    acc = np.zeros((valid.size, 3), np.float64)
    for n in names:
        acc += gains[n] * flat[n][valid].astype(np.float64)
    resid = acc - t_flat[valid].astype(np.float64)
    return {
        "gains": gains,
        "residual_rms": float(np.sqrt(np.mean(resid * resid))),
        "clipped": clipped,
    }


def recompose(light_selects: Mapping[str, np.ndarray], gains: Mapping[str, float]) -> np.ndarray:
    """sum_i gains[i] * light_selects[i] -> linear float32 (H, W, 3).

    A light with no entry in gains contributes nothing; a gain for an unknown light, a negative or a non-finite
    gain raises ValueError (a typo or a slider bug must not silently relight the frame).
    """
    _check_images(light_selects)
    unknown = sorted(set(gains) - set(light_selects))
    if unknown:
        raise ValueError(f"gains for unknown light selects: {unknown}")
    for n, g in gains.items():
        if not np.isfinite(g) or g < 0:
            raise ValueError(f"gain for {n!r} must be finite and >= 0, got {g!r}")
    first = np.asarray(next(iter(light_selects.values())))
    out = np.zeros(first.shape, np.float64)
    for n, g in gains.items():
        out += float(g) * np.asarray(light_selects[n], dtype=np.float64)
    return out.astype(np.float32)


def preset_gains(
    preset: str | Mapping[str, float], names: Sequence[str], default: float = 1.0
) -> dict[str, float]:
    """Expand a mood preset (a key of mood_presets, or a pattern -> gain mapping) onto real light-select names.

    Each name takes the gain of the first pattern that fnmatch-matches it (case insensitive); unmatched lights
    get `default`. These are starting positions for the sliders and warm starts for review, not a solution.
    """
    table = mood_presets[preset] if isinstance(preset, str) else preset
    out: dict[str, float] = {}
    for n in names:
        out[n] = float(default)
        for pat, g in table.items():
            if fnmatch.fnmatchcase(n.lower(), pat.lower()):
                out[n] = float(g)
                break
    return out
