"""Delivery formats: FCPXML 1.10, ProRes 4444, EXR sequences, HEVC with colour tags and the Vantage AV1
re-tag (T46).

    fcpxml.write(timeline, out)                 timeline for Resolve / Final Cut, masters + alpha layers
    video.prores4444(frames_or_mp4, out, fps)   ProRes 4444 .mov with alpha, BT.709 limited
    video.hevc(frames_or_mp4, out, fps)         HEVC Main 10 .mp4, hvc1, colour tags in three places
    video.exr_sequence(frames, out_pattern)     scene-linear half EXR with Rec.709 chromaticities
    video.av1_retag(src, out)                   lossless Vantage 3.3 colour re-tag

Every encode takes a keyword `provenance=guard.Provenance(...)`: previs and GATE_FAIL-without-override are
refused before any output exists, the lineage record is embedded in the file, and a `<file>.gate.json`
sidecar carries the lineage, the versioned gate report, the verdict, any override and the BLAKE3 of each file.
"""

from __future__ import annotations

from . import fcpxml, frames, guard, tools, video
from .guard import (
    DeliveryError,
    DeliveryRefused,
    Provenance,
    ToolMissing,
    authorize,
    read_sidecar,
    sidecar_path,
)
from .video import Delivered, av1_retag, exr_sequence, hevc, prores4444

__all__ = [
    "Delivered",
    "DeliveryError",
    "DeliveryRefused",
    "Provenance",
    "ToolMissing",
    "authorize",
    "av1_retag",
    "exr_sequence",
    "fcpxml",
    "frames",
    "guard",
    "hevc",
    "prores4444",
    "read_sidecar",
    "sidecar_path",
    "tools",
    "video",
]
