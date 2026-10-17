"""Camera-lock and region-motion gates for a clip, built on phase correlation of luma regions.

Consolidates couple-dining/clip_gate.py (first-vs-last-frame shift per static region) and the MOTION / CAMERA
blocks of car-entry/tools_v4_gate.py (mean abs luma diff per region over time). Regions are given in native clip
pixels; the old hard-coded 3840x2160 region tables are parameters now.

The verdict rule is fixed from clip_gate.py: the clip is "moved" when ANY textured region shifts by more than
tol_px (the original demanded ALL regions move, which passed a pan whenever one region sat still). The sampled,
multi-frame version is video.camera_lock_phasecorr; camera_lock here is its two-frame form (t0 vs t1).
"""

from __future__ import annotations

from pathlib import Path

from .register import phase_shift
from .video import Box, _lock, probe, region_motion

__all__ = ["phase_shift", "camera_lock", "region_motion"]


def camera_lock(
    clip: Path,
    regions: list[Box],
    t0: float = 0.0,
    t1: float | None = None,
    tol_px: int = 2,
) -> dict:
    """Phase-correlate each static region between t0 and t1 (default: 0.05 s before the end).

    verdict is "moved" when any textured region shifted by more than tol_px on either axis, "locked" when none
    did, "unknown" when every region is too flat to correlate. Shifts use phase_shift's sign convention.
    """
    clip = Path(clip)
    if not regions:
        raise ValueError("camera_lock needs at least one region")
    if t1 is None:
        t1 = max(probe(clip)["duration_s"] - 0.05, 0.0)
    return _lock(clip, list(regions), [t0, t1], tol_px)
