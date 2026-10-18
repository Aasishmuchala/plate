"""Tests for the Runway schema tables (router plan T117).

Pure data and pure functions: no network, no fixtures. Every vector below is pinned by the plan; the research tables
(image_to_video.md sections 4, 5 and 8, other_endpoints.md section 1, pricing_terms.md section 2) are the source.
Doctrine checked here: an unknown key never reaches a closed schema, and an off-ratio frame is refused rather than
cropped by the vendor (a crop would move a crop pair's landing).
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from plate_engines.adapters import runway_schema as rs

KEYS = [
    "seedance2_5",
    "seedance2_5/text_to_video",
    "kling3.0_pro",
    "klingO3_4k",
    "veo3.1",
    "gen4.5",
]


def test_constants():
    assert rs.RUNWAY_BASE == "https://api.dev.runwayml.com/v1"
    assert rs.RUNWAY_HOST == "api.dev.runwayml.com"
    assert rs.RUNWAY_BASE.startswith(f"https://{rs.RUNWAY_HOST}/")
    assert rs.RUNWAY_VERSION == "2024-11-06"
    assert rs.KEY_ENV == "RUNWAYML_API_SECRET"
    assert rs.DATA_URI_MAX_CHARS == 5_242_880
    assert rs.USD_PER_CREDIT == 0.01
    assert rs.DOCUMENTED_OUTPUT_HOST == "dnznrvs05pmza.cloudfront.net"
    assert rs.ASPECT_TOLERANCE == 0.025


def test_models_table_has_exactly_the_named_entries():
    # the plan's line says "7 keys" but names six: five models plus Seedance 2.5 text_to_video
    assert sorted(rs.MODELS) == sorted(KEYS)


def test_model_spec_is_frozen():
    spec = rs.MODELS["veo3.1"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.closed = False  # type: ignore[misc]


@pytest.mark.parametrize("key", KEYS)
def test_every_spec_fields_contain_model(key):
    assert "model" in rs.MODELS[key].fields


@pytest.mark.parametrize("key", KEYS)
def test_spec_for_roundtrips_the_key(key):
    spec = rs.MODELS[key]
    assert rs.spec_for(spec.model_id, spec.endpoint) is spec
    assert rs.spec_for(key) is spec  # a full key is accepted as is


@pytest.mark.parametrize("key", KEYS)
def test_check_body_accepts_allowlist_and_rejects_resolution(key):
    spec = rs.MODELS[key]
    body = {k: "x" for k in spec.fields}
    rs.check_body(spec.model_id, spec.endpoint, body)
    for k in spec.fields:  # every single allowed key alone is fine too
        rs.check_body(spec.model_id, spec.endpoint, {k: 1})
    with pytest.raises(ValueError, match="resolution"):
        rs.check_body(spec.model_id, spec.endpoint, {**body, "resolution": "4K"})


def test_check_body_names_every_offending_key():
    with pytest.raises(ValueError) as exc:
        rs.check_body("klingO3_4k", "image_to_video", {"model": "klingO3_4k", "seed": 1, "cfgScale": 0.5})
    assert "cfgScale" in str(exc.value) and "seed" in str(exc.value)


def test_no_requested_model_has_resolution_or_unlisted_fields():
    for spec in rs.MODELS.values():
        assert "resolution" not in spec.fields
    assert "seed" not in rs.MODELS["klingO3_4k"].fields and "seed" not in rs.MODELS["kling3.0_pro"].fields
    assert "cfgScale" in rs.MODELS["kling3.0_pro"].fields and "cfgScale" not in rs.MODELS["klingO3_4k"].fields
    assert "audio" not in rs.MODELS["gen4.5"].fields
    assert (
        "draft" in rs.MODELS["seedance2_5"].fields
        and "draftTaskId" in rs.MODELS["seedance2_5/text_to_video"].fields
    )


def test_spec_for_unknown_lists_known_ids():
    with pytest.raises(KeyError) as exc:
        rs.spec_for("gen4_turbo")
    assert "klingO3_4k" in str(exc.value)
    with pytest.raises(KeyError):
        rs.spec_for("gen4.5", "text_to_video")  # Gen-4.5 on text_to_video is not a PLATE route


@pytest.mark.parametrize(
    ("key", "keyframes", "references", "references_max"),
    [
        ("seedance2_5", "first_last", "promptImage_unpositioned", None),
        ("seedance2_5/text_to_video", "none", "text_to_video_references", 30),
        ("kling3.0_pro", "first_last", "none", None),
        ("klingO3_4k", "first_last", "references_field", 7),
        ("veo3.1", "first_last", "none", None),
        ("gen4.5", "first_only", "none", None),
    ],
)
def test_keyframe_and_reference_rules(key, keyframes, references, references_max):
    spec = rs.MODELS[key]
    assert (spec.keyframes, spec.references, spec.references_max) == (keyframes, references, references_max)


def test_capability_flags():
    m = rs.MODELS
    assert [m[k].has_seed for k in KEYS] == [True, True, False, False, True, True]
    assert [m[k].has_audio for k in KEYS] == [True, True, True, True, True, False]
    assert [m[k].has_negative_prompt for k in KEYS] == [False, False, False, False, True, False]
    assert [m[k].has_cfg_scale for k in KEYS] == [False, False, True, False, False, False]
    assert [m[k].has_draft for k in KEYS] == [True, True, False, False, False, False]
    assert [m[k].closed for k in KEYS] == [True, True, True, True, True, False]
    assert m["seedance2_5/text_to_video"].reference_videos_max == 10
    assert m["seedance2_5"].reference_videos_max == 0


def test_durations_and_prompt_limits():
    m = rs.MODELS
    assert m["seedance2_5"].durations == tuple(range(4, 31))
    assert m["kling3.0_pro"].durations == m["klingO3_4k"].durations == tuple(range(3, 16))
    assert m["veo3.1"].durations == (4, 6, 8)
    assert m["gen4.5"].durations == tuple(range(2, 11))
    assert (m["seedance2_5"].prompt_min, m["seedance2_5"].prompt_max, m["seedance2_5"].prompt_required) == (
        1,
        15000,
        False,
    )
    assert (m["klingO3_4k"].prompt_min, m["klingO3_4k"].prompt_max) == (2, 2500)
    assert (m["veo3.1"].prompt_min, m["veo3.1"].prompt_max, m["veo3.1"].prompt_required) == (1, 1000, False)
    assert (m["gen4.5"].prompt_min, m["gen4.5"].prompt_max, m["gen4.5"].prompt_required) == (1, 1000, True)


def test_ratio_tables():
    m = rs.MODELS
    assert list(m["seedance2_5"].ratios) == ["480P", "720P", "1080P"]
    assert m["seedance2_5"].ratios["480P"] == (
        "992:432",
        "854:480",
        "752:560",
        "640:640",
        "560:752",
        "480:854",
    )
    assert m["seedance2_5"].ratios["720P"] == (
        "1470:630",
        "1280:720",
        "1112:834",
        "960:960",
        "834:1112",
        "720:1280",
    )
    assert m["seedance2_5"].ratios["1080P"] == (
        "2206:946",
        "1920:1080",
        "1664:1248",
        "1440:1440",
        "1248:1664",
        "1080:1920",
    )
    assert m["seedance2_5/text_to_video"].ratios == m["seedance2_5"].ratios
    assert m["kling3.0_pro"].ratios == {"1080P": ("1920:1080", "1080:1920", "1440:1440")}
    assert m["klingO3_4k"].ratios == {"4K": ("3840:2160", "2160:3840", "2880:2880")}
    assert m["veo3.1"].ratios == {"720P": ("1280:720", "720:1280"), "1080P": ("1920:1080", "1080:1920")}
    assert m["gen4.5"].ratios == {
        "all": ("1280:720", "720:1280", "1104:832", "960:960", "832:1104", "1584:672")
    }
    assert sum(len(v) for v in m["seedance2_5"].ratios.values()) == 18


def test_aspect():
    assert aspect_close("16:9", 16 / 9)
    assert aspect_close("2206:946", 2206 / 946)
    for bad in ("adaptive", "16x9", "0:5", "5:0", "1:2:3", ""):
        with pytest.raises(ValueError):
            rs.aspect(bad)


def aspect_close(ratio: str, expected: float) -> bool:
    return math.isclose(rs.aspect(ratio), expected)


@pytest.mark.parametrize(
    ("model", "w", "h", "tier", "expected"),
    [
        ("klingO3_4k", 1920, 1080, None, "3840:2160"),
        ("klingO3_4k", 1080, 1920, None, "2160:3840"),
        ("klingO3_4k", 1000, 1000, None, "2880:2880"),
        ("klingO3_4k", 1920, 804, None, None),
        ("klingO3_4k", 1920, 1080, "4k", "3840:2160"),
        ("seedance2_5", 1920, 804, "1080P", "2206:946"),
        ("seedance2_5", 1920, 1080, "480P", "854:480"),
        ("seedance2_5", 3840, 2160, "1080P", "1920:1080"),
        ("seedance2_5", 1080, 1350, "1080P", None),
        ("veo3.1", 1920, 1080, "720p", "1280:720"),
        ("veo3.1", 1920, 1080, "1080P", "1920:1080"),
        ("gen4.5", 1920, 1080, None, "1280:720"),
        ("gen4.5", 2400, 1000, None, "1584:672"),
        ("kling3.0_pro", 1920, 1080, None, "1920:1080"),
    ],
)
def test_ratio_for_vectors(model, w, h, tier, expected):
    assert rs.ratio_for(model, w, h, tier) == expected


def test_ratio_for_without_tier_walks_every_tuple_in_table_order():
    # no tier on Seedance: the 480P tuple comes first, so a 16:9 frame lands on the 480p ratio (callers pass the tier)
    assert rs.ratio_for("seedance2_5", 1920, 1080) == "854:480"
    # a tier the model does not have falls back to every tuple
    assert rs.ratio_for("kling3.0_pro", 1920, 1080, "4K") == "1920:1080"
    assert rs.ratio_for("gen4.5", 1920, 1080, "1080P") == "1280:720"


def test_ratio_for_tolerance_edge():
    # 2.388 vs 2.332 is 2.4 %: inside; 2.45 would be 5 %: outside
    assert rs.ratio_for("seedance2_5", 1920, 804, "1080P") == "2206:946"
    assert rs.ratio_for("seedance2_5", 2450, 1000, "1080P") is None


def test_ratio_for_rejects_degenerate_frame():
    with pytest.raises(ValueError):
        rs.ratio_for("klingO3_4k", 0, 1080)


@pytest.mark.parametrize(
    ("model", "name", "tier", "expected"),
    [
        ("seedance2_5", "21:9", "1080P", "2206:946"),
        ("seedance2_5", "4:3", "1080P", "1664:1248"),
        ("seedance2_5", "9:16", "480P", "480:854"),
        ("seedance2_5", "16:9", "720P", "1280:720"),
        ("seedance2_5", "1:1", "1080P", "1440:1440"),
        ("seedance2_5", "3:4", "1080P", "1248:1664"),
        ("klingO3_4k", "16:9", "4K", "3840:2160"),
        ("klingO3_4k", "21:9", "4K", None),
        ("veo3.1", "9:16", "1080P", "1080:1920"),
        ("gen4.5", "4:3", None, "1104:832"),
    ],
)
def test_named_ratio_for_vectors(model, name, tier, expected):
    assert rs.named_ratio_for(model, name, tier) == expected


def test_named_ratio_for_adaptive_and_unknown():
    assert rs.named_ratio_for("seedance2_5", "adaptive", "1080P") is None
    with pytest.raises(ValueError, match="2:1"):
        rs.named_ratio_for("seedance2_5", "2:1", "1080P")


@pytest.mark.parametrize(
    ("model", "seconds", "expected"),
    [
        ("veo3.1", 5, 6),
        ("veo3.1", 7, 8),
        ("veo3.1", 8.0, 8),
        ("veo3.1", 9, 8),
        ("veo3.1", 3, 4),
        ("veo3.1", 4, 4),
        ("klingO3_4k", 1, 3),
        ("klingO3_4k", 15.2, 15),
        ("klingO3_4k", 5, 5),
        ("kling3.0_pro", 2.1, 3),
        ("seedance2_5", 3.1, 4),
        ("seedance2_5", 30, 30),
        ("seedance2_5", 4.2, 5),
        ("gen4.5", 0.5, 2),
        ("gen4.5", 10.1, 10),
    ],
)
def test_snap_duration_vectors(model, seconds, expected):
    assert rs.snap_duration(model, seconds) == expected


def test_snap_duration_is_always_legal_and_never_short_below_the_cap():
    for key in KEYS:
        spec = rs.MODELS[key]
        for tenths in range(0, 400):
            s = tenths / 10
            got = rs.snap_duration(spec.model_id, s)
            assert got in spec.durations
            if s <= max(spec.durations):
                assert got >= s


def test_snap_duration_rejects_non_finite():
    for bad in (math.inf, -math.inf, math.nan):
        with pytest.raises(ValueError):
            rs.snap_duration("veo3.1", bad)


def test_usd_per_second_vectors():
    assert rs.usd_per_second("klingO3_4k") == 0.43
    assert rs.usd_per_second("klingO3_4k", audio=True) == 0.43  # same rate whether or not audio is on
    assert rs.usd_per_second("kling3.0_pro") == 0.12
    assert rs.usd_per_second("kling3.0_pro", audio=True) == 0.17
    assert rs.usd_per_second("veo3.1") == 0.20
    assert rs.usd_per_second("veo3.1", audio=True) == 0.40
    assert rs.usd_per_second("seedance2_5", resolution="1080p") == 0.68
    assert rs.usd_per_second("seedance2_5", resolution="720P") == 0.30
    assert rs.usd_per_second("seedance2_5", resolution="480p") == 0.20
    assert rs.usd_per_second("seedance2_5/text_to_video", resolution="1080P") == 0.68
    assert rs.usd_per_second("gen4.5") == 0.12


def test_usd_per_second_seedance_without_tier_never_under_prices():
    assert rs.usd_per_second("seedance2_5") == 0.68
    with pytest.raises(ValueError, match="4K"):
        rs.usd_per_second("seedance2_5", resolution="4K")


def test_quote_arithmetic_matches_the_plan_examples():
    # T118 quotes: kling-o3-4k 5 s 2.15, kling-v3-pro 5 s 0.60, veo 6 s no audio 1.20 / audio 2.40, gen-4.5 5 s 0.60
    assert round(rs.usd_per_second("klingO3_4k") * 5, 2) == 2.15
    assert round(rs.usd_per_second("kling3.0_pro") * 5, 2) == 0.60
    assert round(rs.usd_per_second("veo3.1") * rs.snap_duration("veo3.1", 5), 2) == 1.20
    assert round(rs.usd_per_second("veo3.1", audio=True) * rs.snap_duration("veo3.1", 5), 2) == 2.40
    assert round(rs.usd_per_second("seedance2_5", resolution="1080P") * 15, 2) == 10.20
    assert round(rs.usd_per_second("gen4.5") * 5, 2) == 0.60
    # 480P 4 s is 0.80, which is also the floor
    assert (
        round(rs.usd_per_second("seedance2_5", resolution="480P") * 4, 2)
        == rs.minimum_usd("seedance2_5")
        == 0.80
    )


def test_minimum_usd():
    assert rs.minimum_usd("seedance2_5") == 0.80
    assert rs.minimum_usd("seedance2_5/text_to_video") == 0.80
    for k in ("kling3.0_pro", "klingO3_4k", "veo3.1", "gen4.5"):
        assert rs.minimum_usd(k) == 0.0


def test_data_uri_fits_vectors():
    assert rs.data_uri_fits(3_900_000, "image/png") is True
    assert rs.data_uri_fits(0, "image/png") is True
    assert rs.data_uri_fits(5_000_000, "image/png") is False


def test_data_uri_fits_boundary_follows_the_formula():
    # prefix "data:image/png;base64," is 22 chars; 4 * ceil(n / 3) + 22 <= 5,242,880 holds up to n = 3,932,142
    assert len("data:image/png;base64,") == 22
    assert rs.data_uri_fits(3_932_142, "image/png") is True
    assert rs.data_uri_fits(3_932_143, "image/png") is False
    # a longer mime type moves the boundary down
    assert rs.data_uri_fits(3_932_142, "image/svg+xml") is False
    with pytest.raises(ValueError):
        rs.data_uri_fits(-1, "image/png")


def test_prompt_text_folds_negative_into_models_without_the_field():
    assert rs.prompt_text("klingO3_4k", "Camera holds.", "blur, text") == "Camera holds.\nAvoid: blur, text"
    assert rs.prompt_text("seedance2_5", "Camera holds.", "blur") == "Camera holds.\nAvoid: blur"
    assert rs.prompt_text("gen4.5", "Camera holds.", "blur") == "Camera holds.\nAvoid: blur"
    assert rs.prompt_text("kling3.0_pro", "Camera holds.", "  ") == "Camera holds."
    assert rs.prompt_text("kling3.0_pro", "Camera holds.", None) == "Camera holds."


def test_prompt_text_leaves_veo_alone_because_negative_prompt_exists():
    assert rs.prompt_text("veo3.1", "Camera holds.", "blur") == "Camera holds."


def test_prompt_text_empty_rules():
    with pytest.raises(ValueError, match="requires a prompt"):
        rs.prompt_text("gen4.5", "", "")
    with pytest.raises(ValueError, match="requires a prompt"):
        rs.prompt_text("gen4.5", "   ", None)
    # an optional prompt may be empty: the caller omits promptText
    assert rs.prompt_text("klingO3_4k", "", "") == ""
    assert rs.prompt_text("veo3.1", "", "blur") == ""
    # a lone negative on a model that folds it becomes the whole prompt rather than a dangling newline
    assert rs.prompt_text("klingO3_4k", "", "blur") == "Avoid: blur"
    # gen4.5 with only a negative is a non-empty prompt after folding
    assert rs.prompt_text("gen4.5", "", "blur") == "Avoid: blur"


def test_prompt_text_length_caps():
    ok = "a" * 2500
    assert rs.prompt_text("klingO3_4k", ok, "") == ok
    with pytest.raises(ValueError, match="2501"):
        rs.prompt_text("klingO3_4k", "a" * 2501, "")
    with pytest.raises(ValueError, match="2506"):
        rs.prompt_text("kling3.0_pro", "a" * 2494, "blur")  # the folded line counts toward the cap
    with pytest.raises(ValueError, match="1001"):
        rs.prompt_text("veo3.1", "a" * 1001, "")
    assert len(rs.prompt_text("seedance2_5", "a" * 15000, "")) == 15000
    with pytest.raises(ValueError, match="15001"):
        rs.prompt_text("seedance2_5", "a" * 15001, "")
    with pytest.raises(ValueError, match="minimum"):
        rs.prompt_text("klingO3_4k", "a", "")  # Kling's prompt floor is 2


def test_prompt_text_counts_utf16_code_units():
    # an astral character is two UTF-16 code units: 501 of them is 1002 units against Veo's 1000
    with pytest.raises(ValueError, match="1002"):
        rs.prompt_text("veo3.1", "\U0001f600" * 501, "")
    assert rs.prompt_text("veo3.1", "\U0001f600" * 500, "") == "\U0001f600" * 500


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("SAFETY.INPUT.IMAGE", "refused"),
        ("SAFETY.INPUT.TEXT", "refused"),
        ("SAFETY.OUTPUT.VIDEO", "refused"),
        ("INPUT_PREPROCESSING.SAFETY.TEXT", "refused"),
        ("INTERNAL.BAD_OUTPUT.CODE01", "bad_output"),
        ("INTERNAL.BAD_OUTPUT.01", "bad_output"),
        ("ASSET.INVALID", "input"),
        ("INPUT_PREPROCESSING.VALIDATION", "input"),
        ("INPUT_VALIDATION.RATIO", "input"),
        ("DEPRECATED_MODEL", "input"),
        ("QUOTA", "input"),
        ("QUOTA.EXCEEDED", "input"),
        ("FORBIDDEN", "input"),
        ("THIRD_PARTY.UNAVAILABLE", "transient"),
        ("INPUT_PREPROCESSING.INTERNAL", "transient"),
        ("INTERNAL", "transient"),
        ("INTERNAL.SOMETHING_NEW", "transient"),
        ("SOMETHING.UNKNOWN", "transient"),
        (None, "transient"),
        ("", "transient"),
    ],
)
def test_classify_failure_vectors(code, expected):
    assert rs.classify_failure(code) == expected


def test_classify_failure_is_case_insensitive():
    assert rs.classify_failure("safety.input.image") == "refused"


def test_failure_prefixes_order_is_decision_66():
    assert rs.FAILURE_PREFIXES == (
        ("SAFETY.", "refused"),
        ("INPUT_PREPROCESSING.SAFETY.", "refused"),
        ("INTERNAL.BAD_OUTPUT.", "bad_output"),
        ("ASSET.", "input"),
        ("INPUT_PREPROCESSING.VALIDATION", "input"),
        ("INPUT_VALIDATION.", "input"),
        ("DEPRECATED_MODEL", "input"),
        ("QUOTA", "input"),
        ("FORBIDDEN", "input"),
        ("THIRD_PARTY.", "transient"),
        ("INPUT_PREPROCESSING.INTERNAL", "transient"),
        ("INTERNAL", "transient"),
    )
    # every prefix classifies to its own kind, i.e. no earlier prefix shadows a later one
    for prefix, kind in rs.FAILURE_PREFIXES:
        assert rs.classify_failure(prefix + "X" if prefix.endswith(".") else prefix) == kind
