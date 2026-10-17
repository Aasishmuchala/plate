"""Tone band: mean luma, contrast (luma std) and mean saturation of a still against the house band.

The band (luma 100-120, contrast 52-60, saturation 55-65 at 3840 wide) is advisory: it interpolates the Aparna
gate/street frames and flags a still that has drifted far from the project's look. Numbers come from
thresholds.yaml (tone.luma / tone.contrast / tone.sat); the statistics are the same ones texture.stats
reports, computed on the same resized luma.
"""

from __future__ import annotations

import numpy as np

from . import thresholds
from .texture import prepare_rgb, stats

__all__ = ["tone_stats", "tone_check"]

_IDS = {"luma": "tone.luma", "contrast": "tone.contrast", "sat": "tone.sat"}


def tone_stats(rgb: np.ndarray, width: int | None = 3840) -> dict:
    """{'luma', 'contrast', 'sat'} of an RGB image (uint8, uint16 or float 0..1), resized to `width` first."""
    s = stats(prepare_rgb(rgb), width=width)
    return {"luma": s["luma_mean"], "contrast": s["luma_std"], "sat": s["sat_mean"]}


def tone_check(tone: dict) -> dict:
    """Score a tone_stats dict against the band: {'verdict', 'checks': [{metric_id, value, status}]}.

    Every tone threshold is advisory, so the verdict is 'pass' or 'advisory', never 'fail'.
    """
    checks = []
    for key, metric_id in _IDS.items():
        value = float(tone[key])
        status, _ = thresholds.check(metric_id, value)
        checks.append({"metric_id": metric_id, "value": value, "status": status})
    verdict = "advisory" if any(c["status"] != "pass" for c in checks) else "pass"
    return {"verdict": verdict, "checks": checks}
