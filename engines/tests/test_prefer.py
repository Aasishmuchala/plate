"""Wave 11 engines core: route-level price and floor, duration lists, the preference rule, Pin.route_id and
Prefs.prefer_provider. Every manifest here is synthetic (built through EngineManifest.model_validate)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plate_engines.loader import load_manifests
from plate_engines.models import EngineManifest, PriceSpec, Route
from plate_engines.prefer import (
    PREFERRED_VIDEO_PROVIDER,
    VIDEO_KINDS,
    clamp_to_legal,
    legal_durations,
    prefer_route,
)
from plate_engines.router import (
    KIND_FAMILIES,
    Health,
    Pin,
    Prefs,
    RetiredPin,
    choose,
    estimate_price,
)

ATLAS_PRICE = {"model": "per_second", "usd": 0.10}
RUNWAY_PRICE = {"model": "per_second", "usd": 0.20, "status": "confirmed", "minimum_usd": 0.80}


def route_doc(provider: str, host: str, **extra) -> dict:
    return {
        "provider": provider,
        "endpoint": f"https://{host}",
        "region": "us",
        "custody_grade": "D",
        "custody_status": "confirmed",
        "terms_hash": "unknown",
        "upload_visibility": "private" if provider == "runway" else "public",
        "allowed_hosts": [host],
        **extra,
    }


def manifest_doc(routes: list[dict], *, kind: str = "video_startend", rng=(1, 15), task="crop_pair_motion"):
    return {
        "id": "synthetic",
        "name": "Synthetic",
        "version": "unpinned",
        "pinned": False,
        "kind": kind,
        "inputs": [{"name": "image", "type": "image"}],
        "outputs": [{"name": "video", "type": "video"}],
        "params": [{"name": "duration", "type": "int", "default": int(rng[0]), "range": list(rng)}],
        "price": ATLAS_PRICE,
        "determinism": "nondeterministic",
        "licence": {"vendor_terms_url": "unknown"},
        "allowed_tasks": [task],
        "routes": routes,
        "model_id": "synthetic-model",
    }


def make(routes: list[dict], **kw) -> EngineManifest:
    return EngineManifest.model_validate(manifest_doc(routes, **kw))


def kling_like() -> EngineManifest:
    """atlas first, runway second with its own price and durations 3..15; the duration param spans 1..15."""
    return make(
        [
            route_doc("atlas", "api.atlas.test", model_slug="atlas/kling"),
            route_doc(
                "runway",
                "api.dev.runwayml.com",
                model_slug="klingO3_4k",
                price=RUNWAY_PRICE,
                durations=list(range(3, 16)),
            ),
        ]
    )


def veo_like() -> EngineManifest:
    return make(
        [
            route_doc("vertex", "vertex.test"),
            route_doc("runway", "api.dev.runwayml.com", model_slug="veo3.1", durations=[4, 6, 8]),
        ],
        rng=(4, 8),
    )


def providers(routes: list[Route]) -> list[str]:
    return [r.provider for r in routes]


def stub_ratio_for(model: str, w: int, h: int, tier: str | None):
    """Knows 16:9 only: (1920, 804) has no legal ratio."""
    return "3840:2160" if (w, h) == (1920, 1080) else None


# ---------------------------------------------------------------------------- constants


def test_constants():
    assert PREFERRED_VIDEO_PROVIDER == "runway"
    assert VIDEO_KINDS == KIND_FAMILIES["video"]


# ---------------------------------------------------------------------------- legal_durations / clamp_to_legal


def test_legal_durations_intersects_route_lists_with_the_param_range():
    m = kling_like()
    assert legal_durations(m) == list(range(3, 16))


def test_clamp_to_legal_on_the_kling_like_set():
    legal = legal_durations(kling_like())
    assert clamp_to_legal(0.4, legal, 1, 15) == 3
    assert clamp_to_legal(5.2, legal, 1, 15) == 6
    assert clamp_to_legal(15.9, legal, 1, 15) == 15
    assert clamp_to_legal(5.0, legal, 1, 15) == 5


def test_veo_like_legal_set_is_the_runway_list():
    m = veo_like()
    legal = legal_durations(m)
    assert legal == [4, 6, 8]
    assert clamp_to_legal(5, legal, 4, 8) == 6
    assert clamp_to_legal(7, legal, 4, 8) == 8
    assert clamp_to_legal(9, legal, 4, 8) == 8
    assert clamp_to_legal(1, legal, 4, 8) == 4


def test_clamp_to_legal_without_a_constraint_is_the_clamped_ceil():
    assert clamp_to_legal(5.2, [], 1, 15) == 6
    assert clamp_to_legal(40, [], 1, 15) == 15
    assert clamp_to_legal(0.1, [], 2, 15) == 2


def test_clamp_to_legal_falls_back_to_the_largest_legal_value():
    assert clamp_to_legal(9, [4, 6, 8], 1, 30) == 8


def test_a_manifest_without_a_constrained_route_has_no_legal_set():
    m = EngineManifest.model_validate({**manifest_doc([route_doc("atlas", "api.atlas.test")]), "params": []})
    assert legal_durations(m) == []


def test_a_retired_route_does_not_narrow_the_legal_set():
    m = veo_like()
    routes = [r if r.provider != "runway" else r.model_copy(update={"status": "retired"}) for r in m.routes]
    assert legal_durations(m.model_copy(update={"routes": routes})) == [4, 5, 6, 7, 8]


# ---------------------------------------------------------------------------- prefer_route


def test_runway_first_with_no_frame():
    m = kling_like()
    routes, reasons = prefer_route(m, list(m.routes), provider="runway")
    assert providers(routes) == ["runway", "atlas"]
    assert reasons == ["prefer: runway first for video"]


def test_runway_first_when_the_frame_has_a_legal_ratio():
    m = kling_like()
    routes, _ = prefer_route(
        m, list(m.routes), provider="runway", frame=(1920, 1080), tier="4K", ratio_for=stub_ratio_for
    )
    assert providers(routes) == ["runway", "atlas"]


def test_runway_is_skipped_when_the_frame_has_no_legal_ratio():
    m = kling_like()
    routes, reasons = prefer_route(
        m, list(m.routes), provider="runway", frame=(1920, 804), ratio_for=stub_ratio_for
    )
    assert providers(routes) == ["atlas", "runway"]
    assert reasons == ["prefer: runway skipped: no legal ratio for 1920x804 on klingO3_4k"]


def test_ratio_for_receives_the_slug_the_frame_and_the_tier():
    m = kling_like()
    seen = []

    def spy(model, w, h, tier):
        seen.append((model, w, h, tier))
        return "ok"

    prefer_route(m, list(m.routes), provider="runway", frame=(3840, 2160), tier="4K", ratio_for=spy)
    assert seen == [("klingO3_4k", 3840, 2160, "4K")]


def test_provider_none_keeps_manifest_order():
    m = kling_like()
    for provider in (None, "none"):
        routes, reasons = prefer_route(m, list(m.routes), provider=provider)
        assert providers(routes) == ["atlas", "runway"]
        assert reasons == ["prefer: manifest order"]


def test_an_image_manifest_is_unchanged():
    m = make(
        [route_doc("atlas", "api.atlas.test"), route_doc("runway", "api.dev.runwayml.com")],
        kind="image_edit",
        task="relight",
    )
    routes, reasons = prefer_route(m, list(m.routes), provider="runway")
    assert providers(routes) == ["atlas", "runway"]
    assert reasons == ["prefer: manifest order"]


def test_a_provider_with_no_candidate_leaves_the_order_and_says_so():
    m = kling_like()
    routes, reasons = prefer_route(m, [m.routes[0]], provider="runway")
    assert providers(routes) == ["atlas"]
    assert reasons == ["prefer: no runway route among the candidates, manifest order"]


def test_prefer_route_does_not_mutate_its_input():
    m = kling_like()
    candidates = list(m.routes)
    prefer_route(m, candidates, provider="runway")
    assert providers(candidates) == ["atlas", "runway"]


# ---------------------------------------------------------------------------- models: price, durations


def test_estimate_price_uses_the_route_price_and_its_floor():
    m = kling_like()
    runway = m.routes[1]
    assert estimate_price(m, seconds=4, route=runway) == pytest.approx(0.80)
    assert estimate_price(m, seconds=3, route=runway) == pytest.approx(0.80)  # 0.60 raised to the floor
    assert estimate_price(m, seconds=5, route=runway) == pytest.approx(1.0)
    assert estimate_price(m, seconds=5, images=2, route=runway) == pytest.approx(
        2.0
    )  # the floor is per output
    assert estimate_price(m, seconds=3, images=2, route=runway) == pytest.approx(1.6)


def test_estimate_price_without_a_route_price_uses_the_manifest():
    m = kling_like()
    assert estimate_price(m, seconds=5) == pytest.approx(0.5)
    assert estimate_price(m, seconds=5, route=m.routes[0]) == pytest.approx(0.5)


def test_price_floor_defaults_to_zero_and_rejects_negatives():
    assert PriceSpec(model="flat", usd=1.0).minimum_usd == 0.0
    with pytest.raises(ValidationError):
        PriceSpec(model="flat", usd=1.0, minimum_usd=-0.1)


def test_route_durations_must_increase():
    with pytest.raises(ValidationError, match="strictly increasing"):
        Route.model_validate(route_doc("runway", "api.dev.runwayml.com", durations=[5, 3]))
    with pytest.raises(ValidationError, match="strictly increasing"):
        Route.model_validate(route_doc("runway", "api.dev.runwayml.com", durations=[3, 3]))


def test_route_durations_must_be_positive_and_non_empty():
    with pytest.raises(ValidationError, match=">= 1"):
        Route.model_validate(route_doc("runway", "api.dev.runwayml.com", durations=[0, 2]))
    with pytest.raises(ValidationError, match="at least one"):
        Route.model_validate(route_doc("runway", "api.dev.runwayml.com", durations=[]))


def test_route_durations_outside_the_param_range_fail_at_manifest_level():
    with pytest.raises(ValidationError, match="route runway"):
        make([route_doc("runway", "api.dev.runwayml.com", durations=[4, 6, 20])])
    with pytest.raises(ValidationError, match="outside the duration param range"):
        make([route_doc("runway", "api.dev.runwayml.com", durations=[1, 4])], rng=(4, 8))


def test_durations_without_a_param_range_are_not_range_checked():
    doc = manifest_doc([route_doc("runway", "api.dev.runwayml.com", durations=[4, 40])])
    EngineManifest.model_validate({**doc, "params": []})


def test_shipped_manifests_still_validate():
    assert len(load_manifests()) == 15  # 14 + gen-4.5 (T119)


# ---------------------------------------------------------------------------- choose(): prefer_provider and pins

HEALTH = {
    "atlas": Health(True, 100.0),
    "runway": Health(True, 100.0),
    "vertex": Health(True, 100.0),
}


def pick(m: EngineManifest, **kw):
    return choose("crop_pair_motion", [m], {"D"}, kw.pop("pins", None), None, HEALTH, **kw)


def test_choose_prefers_runway_on_a_video_manifest():
    m = kling_like()
    without = pick(m, prefs=Prefs(seconds=5))
    assert without.provider == "atlas"  # 0.10 per second beats runway's 0.20 on price alone
    choice = pick(m, prefs=Prefs(seconds=5, prefer_provider="runway"))
    assert choice.provider == "runway"
    assert choice.route_id == "runway"
    assert any(r.startswith("prefer:") for r in choice.reasons)
    assert [a.provider for a in choice.alternatives] == ["atlas"]


def test_choose_reports_the_price_of_the_route_it_picked():
    m = kling_like()
    choice = pick(m, prefs=Prefs(seconds=5, prefer_provider="runway"))
    assert (choice.price_usd, choice.price_status) == (0.20, "confirmed")
    assert any(r.startswith("price: 1 USD") for r in choice.reasons)
    atlas = pick(m, prefs=Prefs(seconds=5))
    assert (atlas.price_usd, atlas.price_status) == (0.10, "unconfirmed")


def test_choose_ignores_the_preference_on_an_image_manifest():
    m = make(
        [route_doc("atlas", "api.atlas.test"), route_doc("runway", "api.dev.runwayml.com")],
        kind="image_edit",
        task="relight",
    )
    m = m.model_copy(update={"price": PriceSpec(model="per_image", usd=0.05)})
    choice = choose("relight", [m], {"D"}, None, None, HEALTH, prefs=Prefs(prefer_provider="runway"))
    assert choice.provider == "atlas"
    assert not any(r.startswith("prefer:") for r in choice.reasons)


def test_choose_falls_back_to_the_other_route_when_the_preferred_one_is_down():
    m = kling_like()
    health = {**HEALTH, "runway": Health(False)}
    choice = choose(
        "crop_pair_motion", [m], {"D"}, None, None, health, prefs=Prefs(seconds=5, prefer_provider="runway")
    )
    assert choice.provider == "atlas"
    assert not any(r.startswith("prefer:") for r in choice.reasons)


def test_prefer_provider_is_a_prefs_field_and_defaults_to_none():
    assert Prefs().prefer_provider is None
    assert Prefs(**{"prefer_provider": "runway"}).prefer_provider == "runway"


def test_pin_route_id_picks_that_route():
    m = kling_like()
    choice = pick(m, pins={"crop_pair_motion": Pin(engine_id="synthetic", route_id="atlas")})
    assert (choice.provider, choice.route_id) == ("atlas", "atlas")
    assert any("via route 'atlas'" in r for r in choice.reasons)
    # the pin beats the preference
    pinned = pick(
        m,
        pins={"crop_pair_motion": Pin(engine_id="synthetic", route_id="atlas")},
        prefs=Prefs(prefer_provider="runway"),
    )
    assert pinned.provider == "atlas"


def test_pin_route_id_through_a_mapping():
    m = kling_like()
    choice = pick(m, pins={"crop_pair_motion": {"engine_id": "synthetic", "route_id": "runway"}})
    assert choice.provider == "runway"


def test_pin_route_id_nowhere_is_a_retired_pin():
    m = kling_like()
    with pytest.raises(RetiredPin) as exc:
        pick(m, pins={"crop_pair_motion": Pin(engine_id="synthetic", route_id="nowhere")})
    assert "via route 'nowhere'" in exc.value.reason
    assert "which no manifest serves" in exc.value.reason
    assert exc.value.code == "retired_pin"


def test_a_pin_without_route_id_keeps_every_route():
    m = kling_like()
    choice = pick(
        m, pins={"crop_pair_motion": Pin(engine_id="synthetic")}, prefs=Prefs(prefer_provider="runway")
    )
    assert choice.provider == "runway"
    assert [a.provider for a in choice.alternatives] == ["atlas"]
