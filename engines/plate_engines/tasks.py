"""Task vocabulary and the doctrine's forbidden tasks.

New for the PLATE monorepo (CLAUDE.md doctrine rules 1, 2, 4 and 5). TASKS is every task an engine manifest
may list in allowed_tasks; FORBIDDEN_TASKS can never be listed and the router refuses them outright.
"""

from __future__ import annotations

TASKS: frozenset[str] = frozenset(
    {
        "relight",
        "people",
        "vegetation",
        "sky",
        "rephotograph_close",
        "crop_pair_motion",
        "previs",
        "upscale_ai_layers",
        "recolour",
    }
)

# text_to_image: geometry first (rule 1). plate_upscale: the plate is never upscaled (rule 5).
# hero_wide_rephotograph: movie-grade is a render (rule 4). invent_camera_angle: source-view-only (rule 2).
FORBIDDEN_TASKS: frozenset[str] = frozenset(
    {"text_to_image", "plate_upscale", "hero_wide_rephotograph", "invent_camera_angle"}
)
