"""Gate thresholds as data, keyed by a threshold-set id that every gate report carries.

Source: BUILD_PLAN.md section 3, decision 14 (~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md). Changing a
number means adding a NEW set id; a shipped report keeps pointing at the set it was scored against.
"""

from __future__ import annotations

DEFAULT_THRESHOLDS_ID = "bp-s4-d14-v0"

THRESHOLDS: dict[str, dict] = {
    "bp-s4-d14-v0": {
        # registration of the generated layer against the plate
        "registration_residual_px_max": 1.0,
        "shift_px_max": 8,
        # compositing feather band
        "feather_band_de00_p99_max": 1.0,
        # G-buffer edge agreement (long edges, tolerance r=2 px)
        "gbuffer_edge_recall_r2_delivery_min": 0.90,
        "gbuffer_edge_recall_r2_preview_min": 0.85,
        "gbuffer_eval_width": 2048,
        # invention detector
        "invention_flags_architecture_max": 0,
        # material and RAL colour
        "material_de00_median_max": 2,
        "material_de00_p90_max": 5,
        "ral_de00_max": 1,
        # texture band at 100%
        "texture_eval_width": 3840,
        "texture_lapvar_range": [300, 600],
        "texture_fine_ratio_range": [0.35, 0.45],
        # video
        "video_first_frame_max_255": 8,
        "video_last_frame_max_255": 14,
        "camera_lock_median_flow_px_max": 1,
        "camera_lock_eval_width": 960,
        "reprojection_edge_recall_min": 0.8,
    }
}
