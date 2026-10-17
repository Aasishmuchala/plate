"""The 13 shipped manifests load, are internally consistent and keep Atlas at grade D / public."""

from __future__ import annotations

import pytest

from plate_engines.loader import DEFAULT_MANIFEST_DIR
from plate_engines.tasks import FORBIDDEN_TASKS, TASKS

EXPECTED_IDS = {
    "nano-banana-2.1",
    "nano-banana-pro",
    "gpt-image-2.5",
    "seedream-5-pro",
    "flux-3-image",
    "qwen-image-edit-2509",
    "seedance-2.5-r2v",
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


def test_thirteen_manifests_load(manifests):
    assert set(manifests) == EXPECTED_IDS
    assert len(list(DEFAULT_MANIFEST_DIR.glob("*.yaml"))) == 13


def test_ids_match_file_names(manifests):
    for path in DEFAULT_MANIFEST_DIR.glob("*.yaml"):
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


def test_seedance_fallbacks_are_same_model_other_provider(manifests):
    by_provider = {r.provider: r for r in manifests["seedance-2.5-r2v"].routes}
    assert by_provider["atlas"].same_model_fallback_of is None
    assert by_provider["byteplus"].same_model_fallback_of == "atlas"
    assert by_provider["wavespeed"].same_model_fallback_of == "atlas"


def test_allowed_tasks_in_vocabulary_none_forbidden(manifests):
    for m in manifests.values():
        assert m.allowed_tasks, m.id
        for t in m.allowed_tasks:
            assert t in TASKS, (m.id, t)
            assert t not in FORBIDDEN_TASKS, (m.id, t)


def test_restricted_engines(manifests):
    assert manifests["wan-2.2-vace"].allowed_tasks == ["previs"]
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
