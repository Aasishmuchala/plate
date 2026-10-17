"""Router vectors from the plan (a)-(h) plus the custody, fallback and health edges around them."""

from __future__ import annotations

import pytest

from plate_engines.models import PriceSpec
from plate_engines.router import (
    GRADE_ORDER,
    NDA_MAX_GRADE,
    Health,
    Refusal,
    custody_filter,
    doctrine_filter,
    effective_grade,
    estimate_price,
    rank,
    route,
)


def priced(manifests, **usd_by_id):
    """Copy of the manifests with confirmed fake prices on the named engines."""
    out = dict(manifests)
    for engine_id, usd in usd_by_id.items():
        m = out[engine_id]
        out[engine_id] = m.model_copy(
            update={"price": PriceSpec(model="per_image", usd=usd, status="confirmed")}
        )
    return out


def engine_order(pairs):
    return [m.id for m, _ in pairs]


def relight_ranked(manifests, health, max_grade="D"):
    return rank(doctrine_filter(custody_filter(manifests, max_grade), "relight"), health)


def test_a_nda_project_relight_only_reaches_the_local_engine(manifests, health_up):
    assert NDA_MAX_GRADE == "C1"
    pairs = doctrine_filter(custody_filter(manifests, "C1"), "relight")
    assert engine_order(pairs) == ["qwen-image-edit-2509"]
    decision = route("relight", "C1", manifests, health_up)
    assert decision.engine_id == "qwen-image-edit-2509"
    assert (decision.provider, decision.custody_grade) == ("local", "C0")


def test_b_ranking_follows_the_fake_prices(manifests, health_up):
    cheap_nano = priced(manifests, **{"nano-banana-2.1": 0.04, "gpt-image-2.5": 0.08})
    order = engine_order(relight_ranked(cheap_nano, health_up))
    assert order.index("nano-banana-2.1") < order.index("gpt-image-2.5")

    cheap_gpt = priced(manifests, **{"nano-banana-2.1": 0.08, "gpt-image-2.5": 0.04})
    order = engine_order(relight_ranked(cheap_gpt, health_up))
    assert order.index("gpt-image-2.5") < order.index("nano-banana-2.1")


def test_b_latency_matters_when_prices_tie(manifests):
    flat = priced(manifests, **{"nano-banana-2.1": 0.05, "gpt-image-2.5": 0.05})
    health = {
        "gemini": Health(True, 900.0),
        "atlas": Health(True, 900.0),
        "openai": Health(True, 100.0),
        "bfl": Health(True, 100.0),
        "local": Health(True, 100.0),
    }
    order = engine_order(relight_ranked(flat, health))
    assert order.index("gpt-image-2.5") < order.index("nano-banana-2.1")


def test_b_unconfirmed_prices_sort_after_confirmed_ones(manifests, health_up):
    only_gpt_known = priced(manifests, **{"gpt-image-2.5": 9.99})
    order = engine_order(relight_ranked(only_gpt_known, health_up))
    assert order.index("gpt-image-2.5") < order.index("nano-banana-2.1")


def test_region_preference_breaks_ties(manifests, health_up):
    ms = {k: v for k, v in manifests.items() if k in ("nano-banana-2.1",)}
    pairs = custody_filter(ms, "D")
    assert [r.provider for _, r in pairs] == ["gemini", "atlas"]
    tweaked = []
    for m, r in pairs:
        tweaked.append((m, r.model_copy(update={"region": "asia-south1" if r.provider == "atlas" else "eu"})))
    ranked = rank(tweaked, health_up, prefer_region="asia-south1")
    assert [r.provider for _, r in ranked] == ["atlas", "gemini"]
    ranked = rank(tweaked, health_up, prefer_region="eu")
    assert [r.provider for _, r in ranked] == ["gemini", "atlas"]


def test_c_pinned_atlas_engine_on_an_nda_project_is_a_custody_refusal(manifests, health_up):
    with pytest.raises(Refusal) as exc:
        route("relight", "C1", manifests, health_up, engine_pin="seedream-5-pro")
    assert exc.value.code == "custody"
    text = str(exc.value).lower()
    assert "atlas" in text
    assert "public" in text


def test_pinned_engine_is_never_substituted(manifests, health_up):
    with pytest.raises(Refusal) as exc:
        route("relight", "C1", manifests, health_up, engine_pin="nano-banana-2.1")
    assert exc.value.code == "custody"  # gemini pending, atlas public: no silent swap to qwen
    assert route("relight", "D", manifests, health_up, engine_pin="nano-banana-2.1").engine_id == (
        "nano-banana-2.1"
    )


def test_pin_missing(manifests, health_up):
    with pytest.raises(Refusal) as exc:
        route("relight", "D", manifests, health_up, engine_pin="no-such-engine")
    assert exc.value.code == "pin_missing"


def test_pinned_engine_that_does_not_allow_the_task_is_doctrine(manifests, health_up):
    with pytest.raises(Refusal) as exc:
        route("previs", "D", manifests, health_up, engine_pin="nano-banana-2.1")
    assert exc.value.code == "doctrine"


@pytest.mark.parametrize(
    "task", ["text_to_image", "plate_upscale", "hero_wide_rephotograph", "invent_camera_angle"]
)
@pytest.mark.parametrize("grade", GRADE_ORDER)
def test_d_e_forbidden_tasks_are_doctrine_refusals(manifests, health_up, task, grade):
    with pytest.raises(Refusal) as exc:
        route(task, grade, manifests, health_up)
    assert exc.value.code == "doctrine"
    with pytest.raises(Refusal) as exc2:
        doctrine_filter(custody_filter(manifests, grade), task)
    assert exc2.value.code == "doctrine"


def test_unknown_and_unserved_tasks_are_doctrine_refusals(manifests, health_up):
    for task in ("make_coffee", "recolour"):  # recolour is numeric: no generator serves it
        with pytest.raises(Refusal) as exc:
            route(task, "D", manifests, health_up)
        assert exc.value.code == "doctrine"


def test_f_atlas_down_skips_kling_and_refuses_with_health(manifests, health_up):
    health = {**health_up, "atlas": Health(False)}
    ranked = rank(doctrine_filter(custody_filter(manifests, "D"), "crop_pair_motion"), health)
    assert "kling-o3-4k" not in engine_order(ranked)
    assert all(r.provider != "atlas" for _, r in ranked)
    with pytest.raises(Refusal) as exc:
        route("crop_pair_motion", "D", manifests, health, engine_pin="kling-o3-4k")
    assert exc.value.code == "health"


def test_f_everything_down_is_a_health_refusal(manifests, health_up):
    health = {p: Health(False) for p in health_up}
    with pytest.raises(Refusal) as exc:
        route("relight", "D", manifests, health)
    assert exc.value.code == "health"


def test_a_provider_without_a_health_entry_counts_as_down(manifests):
    with pytest.raises(Refusal) as exc:
        route("relight", "D", manifests, {})
    assert exc.value.code == "health"


def test_g_pending_c1_route_is_excluded_at_c1_and_included_at_d(manifests):
    at_c1 = {(m.id, r.provider) for m, r in custody_filter(manifests, "C1")}
    at_d = {(m.id, r.provider) for m, r in custody_filter(manifests, "D")}
    for key in [("gpt-image-2.5", "openai"), ("nano-banana-pro", "gemini"), ("veo-3.1", "vertex")]:
        assert key not in at_c1
        assert key in at_d
    assert at_c1 == {
        ("qwen-image-edit-2509", "local"),
        ("wan-2.2-vace", "local"),
        ("real-esrgan", "local"),
    }


def test_g_the_same_route_is_included_once_its_terms_are_confirmed(manifests):
    m = manifests["gpt-image-2.5"]
    r = m.routes[0]
    confirmed = r.model_copy(update={"custody_status": "confirmed", "terms_hash": "b" * 64})
    assert effective_grade(r) == "D"
    assert effective_grade(confirmed) == "C1"
    ms = {"gpt-image-2.5": m.model_copy(update={"routes": [confirmed]})}
    assert [x.provider for _, x in custody_filter(ms, "C1")] == ["openai"]
    assert custody_filter(ms, "C0") == []


def test_public_uploads_are_grade_d_even_if_a_route_claims_otherwise(manifests):
    atlas = next(r for r in manifests["kling-o3-4k"].routes if r.provider == "atlas")
    lying = atlas.model_copy(
        update={"custody_grade": "C1", "custody_status": "confirmed"}
    )  # bypasses validators
    assert effective_grade(lying) == "D"


@pytest.mark.parametrize("grade", ["C0", "C1", "C2"])
def test_atlas_is_never_routable_below_grade_d(manifests, health_up, grade):
    assert all(r.provider != "atlas" for _, r in custody_filter(manifests, grade))
    for task in ("relight", "crop_pair_motion", "previs", "upscale_ai_layers"):
        try:
            assert route(task, grade, manifests, health_up).provider != "atlas"
        except Refusal as refusal:
            assert refusal.code in {"custody", "doctrine"}


def test_nda_video_has_no_cloud_path(manifests, health_up):
    with pytest.raises(Refusal) as exc:
        route("crop_pair_motion", "C1", manifests, health_up)
    assert exc.value.code == "custody"  # allowed by engines, removed by custody: not a doctrine problem
    assert "public" in str(exc.value)
    assert route("previs", "C1", manifests, health_up).engine_id == "wan-2.2-vace"


def test_same_model_fallback_only_when_the_primary_is_unavailable(manifests, health_up):
    pairs = doctrine_filter(custody_filter(manifests, "D"), "crop_pair_motion")
    up = rank(pairs, health_up)
    seedance_up = [r.provider for m, r in up if m.id == "seedance-2.5-r2v"]
    assert seedance_up == ["atlas"]  # byteplus / wavespeed held back while atlas is up

    health = {**health_up, "atlas": Health(False)}
    seedance_down = [r for m, r in rank(pairs, health) if m.id == "seedance-2.5-r2v"]
    assert {r.provider for r in seedance_down} == {"byteplus", "wavespeed"}
    assert all(r.same_model_fallback_of == "atlas" for r in seedance_down)

    decision = route("crop_pair_motion", "D", manifests, health, engine_pin="seedance-2.5-r2v")
    assert decision.engine_id == "seedance-2.5-r2v"
    assert decision.provider in {"byteplus", "wavespeed"}
    assert decision.fallback_of == "atlas"
    assert decision.custody_grade == "D"  # pending terms: treated as D


def test_decision_reports_unconfirmed_price_honestly(manifests, health_up):
    decision = route("crop_pair_motion", "D", manifests, health_up, engine_pin="kling-o3-4k")
    assert decision.price_status == "unconfirmed"
    assert decision.custody_status == "confirmed"
    assert decision.version == "unpinned"


def test_h_estimate_price(manifests):
    per_second = manifests["kling-o3-4k"].model_copy(
        update={"price": PriceSpec(model="per_second", usd=0.25, status="confirmed")}
    )
    per_mp = manifests["nano-banana-2.1"].model_copy(
        update={"price": PriceSpec(model="per_megapixel", usd=0.01, status="confirmed")}
    )
    flat = manifests["nano-banana-2.1"].model_copy(update={"price": PriceSpec(model="flat", usd=0.3)})
    per_image = manifests["nano-banana-2.1"].model_copy(
        update={"price": PriceSpec(model="per_image", usd=0.04)}
    )
    assert estimate_price(per_second, seconds=5) == pytest.approx(1.25)
    assert estimate_price(per_mp, megapixels=4) == pytest.approx(0.04)
    assert estimate_price(per_mp, megapixels=4, images=3) == pytest.approx(0.12)
    assert estimate_price(flat) == pytest.approx(0.3)
    assert estimate_price(per_image, images=4) == pytest.approx(0.16)
    with pytest.raises(ValueError, match="seconds"):
        estimate_price(per_second)
    with pytest.raises(ValueError, match="megapixels"):
        estimate_price(per_mp)


def test_bad_grade_is_a_value_error(manifests):
    with pytest.raises(ValueError, match="unknown custody grade"):
        custody_filter(manifests, "E")


def test_router_is_pure(manifests, health_up):
    before = {k: v.model_dump() for k, v in manifests.items()}
    route("relight", "D", manifests, health_up)
    route("relight", "C1", manifests, health_up)
    assert {k: v.model_dump() for k, v in manifests.items()} == before
