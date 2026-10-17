"""The 14 shipped manifests load, are internally consistent and keep Atlas at grade D / public."""

from __future__ import annotations

import re
from datetime import date

import pytest

from plate_engines.atlas import RESULT_HOSTS
from plate_engines.loader import DEFAULT_MANIFEST_DIR
from plate_engines.models import host_allowed
from plate_engines.tasks import FORBIDDEN_TASKS, TASKS


def _manifest_files():
    """Manifests only: "_"-prefixed files are overlays (prices.refresh writes _prices.yaml)."""
    return [p for p in DEFAULT_MANIFEST_DIR.glob("*.yaml") if not p.name.startswith("_")]


EXPECTED_IDS = {
    "nano-banana-2.1",
    "nano-banana-pro",
    "gpt-image-2.5",
    "seedream-5-pro",
    "flux-3-image",
    "qwen-image-edit-2509",
    "seedance-2.5-r2v",
    "seedance-2.5-i2v",
    "kling-o3-4k",
    "kling-v3-pro",
    "veo-3.1",
    "wan-2.2-vace",
    "magnific-precision-v2",
    "real-esrgan",
}
ATLAS_ENDPOINT = "https://api.atlascloud.ai/api/v1"


def routes(manifests):
    return [(m, r) for m in manifests.values() for r in m.routes]


def test_fourteen_manifests_load(manifests):
    assert set(manifests) == EXPECTED_IDS
    assert len(list(_manifest_files())) == 14
    assert (DEFAULT_MANIFEST_DIR / "seedance-2.5-i2v.yaml").is_file()


def test_ids_are_unique_hyphenated_and_match_their_file(manifests):
    files = sorted(p.stem for p in _manifest_files())
    assert len(files) == len(set(files)) == len(manifests)
    for engine_id in manifests:
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9.]+)+", engine_id), engine_id
        assert "_" not in engine_id


def test_ids_match_file_names(manifests):
    for path in _manifest_files():
        assert path.stem in manifests, path.name
        assert manifests[path.stem].id == path.stem


def test_versions_are_placeholders_not_pins(manifests):
    assert all(not m.pinned for m in manifests.values())


def test_atlas_routes_are_grade_d_public(manifests):
    atlas = [(m, r) for m, r in routes(manifests) if r.provider == "atlas"]
    assert {m.id for m, _ in atlas} == {
        "nano-banana-2.1",
        "seedream-5-pro",
        "seedance-2.5-r2v",
        "seedance-2.5-i2v",
        "kling-o3-4k",
        "kling-v3-pro",
    }
    for m, r in atlas:
        assert r.custody_grade == "D", m.id
        assert r.upload_visibility == "public", m.id
        assert r.endpoint == ATLAS_ENDPOINT, m.id


def test_local_routes_are_c0_confirmed_local(manifests):
    local = [(m, r) for m, r in routes(manifests) if r.provider == "local"]
    assert {m.id for m, _ in local} == {"qwen-image-edit-2509", "wan-2.2-vace", "real-esrgan"}
    for m, r in local:
        assert (r.custody_grade, r.custody_status, r.upload_visibility) == ("C0", "confirmed", "local"), m.id


def test_c1_candidates_are_pending_not_confirmed(manifests):
    c1 = [(m, r) for m, r in routes(manifests) if r.custody_grade == "C1"]
    assert {(m.id, r.provider) for m, r in c1} == {
        ("nano-banana-2.1", "gemini"),
        ("nano-banana-pro", "gemini"),
        ("gpt-image-2.5", "openai"),
        ("veo-3.1", "vertex"),
    }
    for m, r in c1:
        assert r.custody_status == "pending_written_terms", m.id
        assert r.terms_hash == "unknown", m.id


def test_no_unconfirmed_fact_is_dressed_as_confirmed(manifests):
    """Cloud prices are placeholders; only the local zero-fee prices are confirmed."""
    for m in manifests.values():
        is_local = all(r.provider == "local" for r in m.routes)
        assert (m.price.status == "confirmed") == is_local, m.id
        assert m.licence.vendor_terms_url == "unknown", m.id
        assert m.licence.training_opt_out is None, m.id


@pytest.mark.parametrize("engine_id", ["seedance-2.5-r2v", "seedance-2.5-i2v"])
def test_seedance_fallbacks_are_same_model_other_provider(manifests, engine_id):
    by_provider = {r.provider: r for r in manifests[engine_id].routes}
    assert set(by_provider) == {"atlas", "byteplus", "wavespeed"}
    assert by_provider["atlas"].same_model_fallback_of is None
    assert by_provider["byteplus"].same_model_fallback_of == "atlas"
    assert by_provider["wavespeed"].same_model_fallback_of == "atlas"
    for fallback in ("byteplus", "wavespeed"):
        assert (by_provider[fallback].custody_grade, by_provider[fallback].custody_status) == (
            "D",
            "pending_written_terms",
        )


def test_allowed_tasks_in_vocabulary_none_forbidden(manifests):
    for m in manifests.values():
        assert m.allowed_tasks, m.id
        for t in m.allowed_tasks:
            assert t in TASKS, (m.id, t)
            assert t not in FORBIDDEN_TASKS, (m.id, t)


def test_restricted_engines(manifests):
    assert manifests["wan-2.2-vace"].allowed_tasks == ["previs"]
    assert manifests["seedance-2.5-i2v"].allowed_tasks == ["crop_pair_motion"]
    assert manifests["seedance-2.5-r2v"].allowed_tasks == [
        "crop_pair_motion",
        "previs",
    ]  # T44 previs runs on r2v
    assert manifests["magnific-precision-v2"].allowed_tasks == ["upscale_ai_layers"]
    assert manifests["real-esrgan"].allowed_tasks == ["upscale_ai_layers"]
    assert manifests["magnific-precision-v2"].routes[0].custody_grade == "D"
    # no generator is asked to fix colour (doctrine rule 5)
    assert not any("recolour" in m.allowed_tasks for m in manifests.values())


def test_image_engines_require_a_source_image(manifests):
    """Geometry first: no manifest can run without an image or reference input."""
    for m in manifests.values():
        required = {f.type for f in m.inputs if f.required}
        assert required & {"image", "images"}, m.id


def test_manifests_are_frozen(manifests):
    with pytest.raises((TypeError, ValueError)):
        manifests["real-esrgan"].id = "other"


# ---- T7: the new manifest fields --------------------------------------------------------------------------


def cloud(manifests):
    return [m for m in manifests.values() if any(r.provider != "local" for r in m.routes)]


def test_every_route_has_custody_status_terms_hash_and_an_id(manifests):
    for m, r in routes(manifests):
        assert r.custody_grade in {"C0", "C1", "C2", "D"}, m.id
        assert r.custody_status in {"confirmed", "pending_written_terms"}, m.id
        assert r.terms_hash == "unknown" or len(r.terms_hash) == 64, m.id
        assert r.route_id == r.provider, m.id  # route_id defaults to the provider
        assert r.status == "active", m.id
    for m in manifests.values():
        assert len({r.route_id for r in m.routes}) == len(m.routes), m.id


def test_cloud_prices_are_positive_sourced_dated_and_unconfirmed(manifests):
    for m in cloud(manifests):
        assert m.price.usd > 0, m.id
        assert m.price.status == "unconfirmed", m.id
        assert m.price.as_of == date(2026, 10, 17), m.id
        assert m.price.source, m.id
        assert all(v > 0 for v in m.price.by.values()), m.id
    for m in manifests.values():
        if m not in cloud(manifests):
            assert m.price.usd == 0 and m.price.status == "confirmed", m.id


def test_prices_follow_the_dossier_figures(manifests):
    assert manifests["seedance-2.5-r2v"].price.usd == 0.10
    assert manifests["seedance-2.5-i2v"].price.usd == 0.10
    assert manifests["seedance-2.5-i2v"].price.by["base"] == 0.134
    kling = manifests["kling-o3-4k"].price
    assert (kling.model, kling.usd, kling.by["base"], kling.by["atlas_discounted"]) == (
        "per_second",
        0.357,
        0.357,
        0.0536,
    )
    assert manifests["kling-v3-pro"].price.model == "per_second"
    assert manifests["nano-banana-2.1"].price.by == {"1K": 0.034}


def test_per_second_engines_are_the_video_ones(manifests):
    for m in manifests.values():
        is_video = m.kind in {"video_i2v", "video_r2v", "video_startend", "local_video"}
        assert (m.price.model == "per_second") == is_video, m.id


def test_every_non_local_route_names_the_hosts_it_may_reach(manifests):
    for m, r in routes(manifests):
        if r.provider == "local":
            assert r.allowed_hosts == [] and r.max_inflight == 1, m.id
        else:
            assert r.allowed_hosts, (m.id, r.provider)
            assert all(h == h.lower() and "/" not in h and ":" not in h for h in r.allowed_hosts), m.id
        if r.endpoint.startswith("https://"):
            host = r.endpoint.split("/")[2]
            assert host_allowed(host, r.allowed_hosts), (m.id, host)


def test_atlas_routes_allow_exactly_the_atlas_result_hosts(manifests):
    for m, r in routes(manifests):
        if r.provider == "atlas":
            assert tuple(sorted(r.allowed_hosts)) == tuple(sorted(RESULT_HOSTS)), m.id
            assert r.model_slug is None or "/" in r.model_slug, m.id


def test_model_id_is_set_or_the_manifest_is_flagged_unverified(manifests):
    for m in manifests.values():
        assert m.model_id or "unverified_p0" in m.flags, m.id
        assert all(re.fullmatch(r"[a-z][a-z0-9_]*", f) for f in m.flags), m.id
    assert manifests["nano-banana-pro"].model_id == "gemini-3-pro-image"
    assert manifests["kling-o3-4k"].model_id == "kwaivgi/kling-video-o3-4k/image-to-video"
    assert manifests["kling-v3-pro"].model_id == "kwaivgi/kling-v3.0-pro/image-to-video"


def test_kinds_cover_the_new_literals(manifests):
    assert manifests["kling-o3-4k"].kind == manifests["kling-v3-pro"].kind == "video_startend"
    assert manifests["qwen-image-edit-2509"].kind == "local_image"
    assert manifests["wan-2.2-vace"].kind == "local_video"
    assert manifests["seedance-2.5-i2v"].kind == "video_i2v"
    assert manifests["seedance-2.5-r2v"].kind == "video_r2v"


def test_upscalers_never_touch_the_plate(manifests):
    upscalers = [m for m in manifests.values() if m.kind == "upscale"]
    assert {m.id for m in upscalers} == {"magnific-precision-v2", "real-esrgan"}
    assert all(m.plate_upscale_forbidden for m in upscalers)
    assert not any(m.plate_upscale_forbidden for m in manifests.values() if m.kind != "upscale")


def test_nano_banana_sizes_are_the_three_tiers(manifests):
    for engine_id in ("nano-banana-2.1", "nano-banana-pro"):
        m = manifests[engine_id]
        assert [x.label for x in m.sizes.legal] == ["1K", "2K", "4K"], engine_id
        size_param = next(p for p in m.params if p.name == "image_size")
        assert size_param.enum == ["1K", "2K", "4K"]
        aspect = next(p for p in m.params if p.name == "aspect_ratio")
        assert set(aspect.enum) == set(m.sizes.aspects)
    assert (
        "4:1" in manifests["nano-banana-2.1"].sizes.aspects
        and "4:1" not in manifests["nano-banana-pro"].sizes.aspects
    )
    assert (
        manifests["nano-banana-2.1"].model_id is None
        and "unverified_p0" in manifests["nano-banana-2.1"].flags
    )


def test_gpt_image_sizes_and_the_unverified_arbitrary_envelope(manifests):
    m = manifests["gpt-image-2.5"]
    assert [(x.w, x.h) for x in m.sizes.legal] == [(1024, 1024), (1536, 1024), (1024, 1536)]
    assert m.sizes.auto is True
    assert m.sizes.arbitrary is not None and "unverified_p0" in m.flags
    size = next(p for p in m.params if p.name == "size")
    assert size.enum == ["auto", "1024x1024", "1536x1024", "1024x1536"]
    assert next(p for p in m.params if p.name == "quality").enum == ["low", "medium", "high"]
    assert next(p for p in m.params if p.name == "input_fidelity").advanced is True
    assert "seed" not in {p.name for p in m.params}  # the Images API has no seed


def test_seedance_limits(manifests):
    for engine_id in ("seedance-2.5-r2v", "seedance-2.5-i2v"):
        m = manifests[engine_id]
        params = {p.name: p for p in m.params}
        assert params["duration"].range == [4, 30] and params["duration"].type == "int"
        assert params["resolution"].enum == ["480P", "720P", "1080P"]  # no 4K
        assert "adaptive" in params["ratio"].enum
        assert {"camera_fixed", "generate_audio", "return_last_frame", "seed"} <= set(params)
        assert (m.sizes.arbitrary.aspect_min, m.sizes.arbitrary.aspect_max) == (0.4, 2.5)
        assert (m.sizes.arbitrary.min_side, m.sizes.arbitrary.max_side) == (300, 6000)
    r2v = {f.name: f for f in manifests["seedance-2.5-r2v"].inputs}
    assert r2v["reference_images"].max_items == 30 and r2v["reference_images"].required
    assert r2v["reference_videos"].max_items == 10 and not r2v["reference_videos"].required
    i2v = {f.name: f for f in manifests["seedance-2.5-i2v"].inputs}
    assert set(i2v) == {"image", "prompt"}  # plain image-to-video: first frame, no reference arrays
    assert i2v["image"].required and all(f.max_items is None for f in i2v.values())


@pytest.mark.parametrize("engine_id", ["kling-o3-4k", "kling-v3-pro"])
def test_kling_start_end_schema(manifests, engine_id):
    m = manifests[engine_id]
    params = {p.name: p for p in m.params}
    assert params["duration"].range == [1, 15] and params["duration"].type == "int"
    assert {"negative_prompt", "cfg_scale"} <= set(params)
    inputs = {f.name: f for f in m.inputs}
    assert (
        inputs["image"].required and not inputs["image_tail"].required
    )  # vendor names: first frame, end pin
    assert inputs["elements"].type == "json"
    assert m.routes[0].model_slug == m.model_id


def test_previous_names_are_gone_where_the_vendor_uses_other_ones(manifests):
    for engine_id in ("seedance-2.5-r2v", "seedance-2.5-i2v", "kling-o3-4k", "kling-v3-pro", "veo-3.1"):
        assert "duration_s" not in {p.name for p in manifests[engine_id].params}, engine_id
    assert "reference_video" not in {f.name for f in manifests["seedance-2.5-r2v"].inputs}


def test_params_are_the_full_schema_with_advanced_flags(manifests):
    for m in manifests.values():
        names = [p.name for p in m.params]
        assert names and len(names) == len(set(names)), m.id
    assert any(p.advanced for p in manifests["nano-banana-2.1"].params)
    assert not next(p for p in manifests["seedance-2.5-i2v"].params if p.name == "duration").advanced


def test_magnific_and_veo_details(manifests):
    mag = manifests["magnific-precision-v2"]
    assert mag.plate_upscale_forbidden and next(p for p in mag.params if p.name == "scale").range == [2, 4]
    assert mag.routes[0].allowed_hosts == [".freepik.com"]
    veo = {p.name: p for p in manifests["veo-3.1"].params}
    assert veo["duration"].range == [4, 8] and veo["resolution"].enum == ["720p", "1080p"]
    assert veo["generate_audio"].default is False
