"""Router vectors from the plan (a)-(h) plus the custody, fallback and health edges around them."""

from __future__ import annotations

import pytest

from plate_engines.cards import Card
from plate_engines.health import HealthBoard, HealthEntry
from plate_engines.models import PriceSpec
from plate_engines.router import (
    CARD_MIN_N,
    GRADE_ORDER,
    NDA_MAX_GRADE,
    Health,
    Pin,
    Prefs,
    Refusal,
    RetiredPin,
    RouteChoice,
    Task,
    choose,
    custody_filter,
    doctrine_filter,
    effective_grade,
    estimate_price,
    next_same_model,
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
    # Wave 11: Kling also has a runway route, so "no Kling" needs Atlas AND Runway down.
    health = {**health_up, "atlas": Health(False), "runway": Health(False)}
    ranked = rank(doctrine_filter(custody_filter(manifests, "D"), "crop_pair_motion"), health)
    assert "kling-o3-4k" not in engine_order(ranked)
    assert all(r.provider not in {"atlas", "runway"} for _, r in ranked)
    with pytest.raises(Refusal) as exc:
        route("crop_pair_motion", "D", manifests, health, engine_pin="kling-o3-4k")
    assert exc.value.code == "health"
    # Atlas alone down: Kling is still reachable, on its runway route
    atlas_only = {**health_up, "atlas": Health(False)}
    pinned = route("crop_pair_motion", "D", manifests, atlas_only, engine_pin="kling-o3-4k")
    assert (pinned.engine_id, pinned.provider, pinned.custody_grade) == ("kling-o3-4k", "runway", "D")


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
    # byteplus / wavespeed held back while atlas is up; runway (Wave 11) is a route in its own right, not a fallback
    assert sorted(seedance_up) == ["atlas", "runway"]
    atlas_only = {**health_up, "atlas": Health(False)}
    assert {r.provider for m, r in rank(pairs, atlas_only) if m.id == "seedance-2.5-r2v"} == {
        "runway",
        "byteplus",
        "wavespeed",
    }

    health = {**health_up, "atlas": Health(False), "runway": Health(False)}
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


# ---------------------------------------------------------------------------- T27: choose()

ALL_GRADES = {"C0", "C1", "C2", "D"}
NDA_GRADES = {"C0", "C1"}


def subset(manifests, *ids):
    return {k: manifests[k] for k in ids}


def entries_for(manifests, status="ok", latency=100.0):
    return {r.provider: HealthEntry(status, latency) for m in manifests.values() for r in m.routes}


def test_choose_custody_filter_removes_d_on_nda(manifests, health_up):
    choice = choose("relight", manifests, NDA_GRADES, None, None, health_up)
    assert isinstance(choice, RouteChoice)
    assert (choice.engine_id, choice.route_id, choice.provider) == ("qwen-image-edit-2509", "local", "local")
    assert choice.custody_grade == "C0"
    assert choice.requote is False
    assert any(r.startswith("custody:") for r in choice.reasons)


def test_choose_pending_cloud_engine_is_removed_at_c1_whatever_its_price(manifests, health_up):
    only_cloud = subset(manifests, "nano-banana-2.1", "gpt-image-2.5")
    with pytest.raises(Refusal) as exc:
        choose("relight", only_cloud, NDA_GRADES, None, None, health_up)
    assert exc.value.code == "custody"
    assert "pending" in str(exc.value) or "public" in str(exc.value)


def test_choose_accepts_the_custody_module_grade_enum(manifests, health_up):
    from enum import StrEnum

    class Grade(StrEnum):  # the shape of plate_daemon.custody.Grade
        C0 = "C0"
        C1 = "C1"

    choice = choose("relight", manifests, {Grade.C0, Grade.C1}, None, None, health_up)
    assert choice.engine_id == "qwen-image-edit-2509"


def test_choose_unknown_grade_is_a_value_error(manifests, health_up):
    with pytest.raises(ValueError, match="unknown custody grade"):
        choose("relight", manifests, {"C9"}, None, None, health_up)


@pytest.mark.parametrize(
    "task", ["text_to_image", "plate_upscale", "hero_wide_rephotograph", "invent_camera_angle"]
)
def test_choose_forbidden_tasks_are_doctrine_refusals(manifests, health_up, task):
    for grades in (ALL_GRADES, NDA_GRADES, set()):
        with pytest.raises(Refusal) as exc:
            choose(task, manifests, grades, None, None, health_up)
        assert exc.value.code == "doctrine"


@pytest.mark.parametrize("shot_class", ["wide", "aerial", "hero", None])
def test_choose_rephotograph_refuses_wide_and_undeclared_shots(manifests, health_up, shot_class):
    with pytest.raises(Refusal) as exc:
        choose(
            Task("rephotograph_close", shot_class=shot_class), manifests, ALL_GRADES, None, None, health_up
        )
    assert exc.value.code == "doctrine"
    assert "close and medium" in str(exc.value)


@pytest.mark.parametrize("shot_class", ["close", "medium"])
def test_choose_rephotograph_allows_close_and_medium(manifests, health_up, shot_class):
    choice = choose(
        Task("rephotograph_close", shot_class=shot_class), manifests, ALL_GRADES, None, None, health_up
    )
    assert "rephotograph_close" in manifests[choice.engine_id].allowed_tasks


def test_choose_task_kind_must_match_the_manifest_kind(manifests, health_up):
    with pytest.raises(Refusal) as exc:
        choose(Task("relight", kind="video"), manifests, ALL_GRADES, None, None, health_up)
    assert exc.value.code == "doctrine"
    assert "kind" in str(exc.value)
    choice = choose(Task("relight", kind="image_edit"), manifests, ALL_GRADES, None, None, health_up)
    assert manifests[choice.engine_id].kind == "image_edit"
    assert choice.engine_id != "qwen-image-edit-2509"  # local_image is a different manifest kind
    with pytest.raises(ValueError, match="unknown task kind"):
        Task("relight", kind="hologram").kinds()


def test_choose_a_plate_never_reaches_an_upscaler(manifests, health_up):
    with pytest.raises(Refusal) as exc:
        choose(Task("upscale_ai_layers", target="plate"), manifests, ALL_GRADES, None, None, health_up)
    assert exc.value.code == "doctrine"
    assert "plate_upscale_forbidden" in str(exc.value)
    layers = choose(Task("upscale_ai_layers", target="layer"), manifests, ALL_GRADES, None, None, health_up)
    assert manifests[layers.engine_id].plate_upscale_forbidden is True


def test_choose_a_task_nobody_serves_is_doctrine_not_custody(manifests, health_up):
    with pytest.raises(Refusal) as exc:
        choose("recolour", manifests, ALL_GRADES, None, None, health_up)
    assert exc.value.code == "doctrine"  # numeric: no generator serves it


def test_choose_custody_removed_it_is_custody_not_doctrine(manifests, health_up):
    with pytest.raises(Refusal) as exc:
        choose("crop_pair_motion", manifests, NDA_GRADES, None, None, health_up)
    assert exc.value.code == "custody"
    assert "public" in str(exc.value)
    assert choose("previs", manifests, NDA_GRADES, None, None, health_up).engine_id == "wan-2.2-vace"


def test_choose_atlas_is_never_chosen_below_d(manifests, health_up):
    for grades in ({"C0"}, NDA_GRADES, {"C0", "C1", "C2"}):
        for task in ("relight", "crop_pair_motion", "previs", "upscale_ai_layers"):
            try:
                assert choose(task, manifests, grades, None, None, health_up).provider != "atlas"
            except Refusal as refusal:
                assert refusal.code in {"custody", "doctrine"}


def test_choose_project_pin_wins_over_a_cheaper_engine(manifests, health_up):
    cloud = subset(
        priced(manifests, **{"nano-banana-2.1": 0.30, "gpt-image-2.5": 0.02}),
        "nano-banana-2.1",
        "gpt-image-2.5",
    )
    assert choose("relight", cloud, ALL_GRADES, None, None, health_up).engine_id == "gpt-image-2.5"
    pinned = choose("relight", cloud, ALL_GRADES, {"relight": Pin("nano-banana-2.1")}, None, health_up)
    assert pinned.engine_id == "nano-banana-2.1"
    assert any(r.startswith("pin:") for r in pinned.reasons)
    # a pin may also be given as an engine id, a pair or a mapping
    for form in ("nano-banana-2.1", ("nano-banana-2.1", "unpinned"), {"engine_id": "nano-banana-2.1"}):
        assert (
            choose("relight", cloud, ALL_GRADES, {"relight": form}, None, health_up).engine_id
            == "nano-banana-2.1"
        )
    # a pin for another task does not apply
    assert (
        choose("relight", cloud, ALL_GRADES, {"sky": Pin("nano-banana-2.1")}, None, health_up).engine_id
        == "gpt-image-2.5"
    )


def test_choose_pin_to_a_retired_version_raises(manifests, health_up):
    with pytest.raises(RetiredPin) as exc:
        choose(
            "relight",
            manifests,
            ALL_GRADES,
            {"relight": Pin("nano-banana-2.1", "2026-01-01")},
            None,
            health_up,
        )
    assert isinstance(exc.value, Refusal)
    assert exc.value.code == "retired_pin"
    assert "nano-banana-2.1@2026-01-01" in str(exc.value)
    assert (
        choose(
            "relight", manifests, ALL_GRADES, {"relight": Pin("nano-banana-2.1", "unpinned")}, None, health_up
        ).engine_id
        == "nano-banana-2.1"
    )


def test_choose_pin_to_an_engine_that_is_gone_raises(manifests, health_up):
    with pytest.raises(RetiredPin):
        choose("relight", manifests, ALL_GRADES, {"relight": Pin("no-such-engine")}, None, health_up)


def test_choose_a_pin_never_overrides_custody_or_swaps_engines(manifests, health_up):
    with pytest.raises(Refusal) as exc:
        choose("relight", manifests, NDA_GRADES, {"relight": Pin("nano-banana-2.1")}, None, health_up)
    assert exc.value.code == "custody"  # not quietly routed to the local qwen engine


def test_choose_pin_to_an_engine_that_does_not_serve_the_task_is_doctrine(manifests, health_up):
    with pytest.raises(Refusal) as exc:
        choose("previs", manifests, ALL_GRADES, {"previs": Pin("nano-banana-2.1")}, None, health_up)
    assert exc.value.code == "doctrine"


def test_choose_only_status_ok_routes_are_used(manifests):
    cloud = subset(manifests, "nano-banana-2.1", "gpt-image-2.5")
    cloud = priced(cloud, **{"nano-banana-2.1": 0.01, "gpt-image-2.5": 0.50})
    health = entries_for(cloud)
    assert choose("relight", cloud, ALL_GRADES, None, None, health).engine_id == "nano-banana-2.1"
    for bad in ("degraded", "down", "suspended"):
        health = {**entries_for(cloud), "gemini": HealthEntry(bad), "atlas": HealthEntry(bad)}
        assert choose("relight", cloud, ALL_GRADES, None, None, health).engine_id == "gpt-image-2.5"
    everything = {p: HealthEntry("degraded") for p in entries_for(cloud)}
    with pytest.raises(Refusal) as exc:
        choose("relight", cloud, ALL_GRADES, None, None, everything)
    assert exc.value.code == "health"
    assert "degraded" in str(exc.value)


def test_choose_no_health_signal_is_no_spend(manifests):
    with pytest.raises(Refusal) as exc:
        choose("relight", manifests, ALL_GRADES, None, None, {})
    assert exc.value.code == "health"
    with pytest.raises(Refusal) as exc:
        choose("relight", manifests, ALL_GRADES, None, None, None)
    assert exc.value.code == "health"


def test_choose_a_suspended_engine_is_skipped_and_resumes(manifests, health_up, tmp_path):
    cloud = priced(subset(manifests, "nano-banana-2.1", "gpt-image-2.5"), **{"nano-banana-2.1": 0.01})
    board = HealthBoard(entries_for(cloud))
    assert choose("relight", cloud, ALL_GRADES, None, None, board.as_mapping()).engine_id == "nano-banana-2.1"
    board.suspend("nano-banana-2.1", "model string changed")
    assert choose("relight", cloud, ALL_GRADES, None, None, board.as_mapping()).engine_id == "gpt-image-2.5"
    with pytest.raises(Refusal) as exc:  # a suspended pin is refused, not swapped
        choose("relight", cloud, ALL_GRADES, {"relight": Pin("nano-banana-2.1")}, None, board.as_mapping())
    assert exc.value.code == "health"
    assert "model string changed" in str(exc.value)
    board.resume("nano-banana-2.1")
    assert choose("relight", cloud, ALL_GRADES, None, None, board.as_mapping()).engine_id == "nano-banana-2.1"


def test_choose_route_key_is_more_specific_than_the_provider_key(manifests):
    nano = subset(manifests, "nano-banana-2.1")
    health = {"gemini": HealthEntry("ok", 50.0), "atlas": HealthEntry("ok", 100.0)}
    assert choose("relight", nano, ALL_GRADES, None, None, health).provider == "gemini"
    health["nano-banana-2.1/gemini"] = HealthEntry("down", reason="quota exhausted")
    assert choose("relight", nano, ALL_GRADES, None, None, health).provider == "atlas"


def test_choose_cards_steer_only_from_n_20_and_rank_by_wilson_lower_bound(manifests, health_up):
    cloud = priced(
        subset(manifests, "nano-banana-2.1", "gpt-image-2.5"),
        **{"nano-banana-2.1": 0.30, "gpt-image-2.5": 0.02},
    )
    kw = dict(task="relight", manifests=cloud, allowed_grades=ALL_GRADES, project_pins=None, health=health_up)
    assert CARD_MIN_N == 20

    def card(engine, passes, n, shot="medium"):
        return Card.make(engine, "relight", shot, passes, n)

    # gpt-image is cheaper; nano's cell is too small to steer (19 runs)
    thin = [card("nano-banana-2.1", 19, 19, shot=None)]
    assert choose(cards=thin, **kw, prefs=Prefs()).engine_id == "gpt-image-2.5"
    # at n = 20 the card steers: nano's lower bound beats gpt-image's missing evidence
    steering = [card("nano-banana-2.1", 20, 20, shot=None)]
    choice = choose(cards=steering, **kw)
    assert choice.engine_id == "nano-banana-2.1"
    assert any(r.startswith("cards:") and "n=20" in r for r in choice.reasons)
    # both have steering cells: the higher lower bound wins even though it costs 15 times more
    both = [card("nano-banana-2.1", 25, 30, shot=None), card("gpt-image-2.5", 49, 50, shot=None)]
    assert choose(cards=both, **kw).engine_id == "gpt-image-2.5"
    both = [card("nano-banana-2.1", 29, 30, shot=None), card("gpt-image-2.5", 25, 50, shot=None)]
    assert choose(cards=both, **kw).engine_id == "nano-banana-2.1"
    # the cell is (engine, task, shot_class): a card for another shot class or task never steers
    other = [card("nano-banana-2.1", 30, 30, shot="close")]
    assert choose(cards=other, **kw).engine_id == "gpt-image-2.5"
    assert (
        choose(cards=other, **{**kw, "task": Task("relight", shot_class="close")}).engine_id
        == "nano-banana-2.1"
    )
    # cards may be a mapping, and min_card_n is a preference
    assert (
        choose(cards={c.key: c for c in thin}, **kw, prefs=Prefs(min_card_n=10)).engine_id
        == "nano-banana-2.1"
    )


def test_choose_cards_never_override_custody_or_doctrine(manifests, health_up):
    great = [Card.make("nano-banana-2.1", "relight", None, 100, 100)]
    choice = choose("relight", manifests, NDA_GRADES, None, great, health_up)
    assert choice.engine_id == "qwen-image-edit-2509"


def test_choose_price_then_latency_then_region(manifests):
    nano = subset(manifests, "nano-banana-2.1")  # gemini and atlas, same price
    health = {"gemini": HealthEntry("ok", 900.0), "atlas": HealthEntry("ok", 100.0)}
    assert (
        choose("relight", nano, ALL_GRADES, None, None, health).provider == "atlas"
    )  # latency breaks the tie
    health = {"gemini": HealthEntry("ok", 100.0), "atlas": HealthEntry("ok", 100.0)}
    assert choose("relight", nano, ALL_GRADES, None, None, health).provider == "atlas"  # route id order
    tweaked = dict(nano)
    m = nano["nano-banana-2.1"]
    tweaked["nano-banana-2.1"] = m.model_copy(
        update={
            "routes": [
                r.model_copy(update={"region": "asia-south1" if r.provider == "gemini" else "eu"})
                for r in m.routes
            ]
        }
    )
    chosen = choose("relight", tweaked, ALL_GRADES, None, None, health, Prefs(region="asia-south1"))
    assert chosen.provider == "gemini"
    assert any(r.startswith("region:") for r in chosen.reasons)
    # price outranks latency: the cheaper engine wins even when it is 8x slower
    two = priced(
        subset(manifests, "nano-banana-2.1", "gpt-image-2.5"),
        **{"nano-banana-2.1": 0.01, "gpt-image-2.5": 0.02},
    )
    slow = {
        "gemini": HealthEntry("ok", 800.0),
        "atlas": HealthEntry("ok", 800.0),
        "openai": HealthEntry("ok", 100.0),
    }
    assert choose("relight", two, ALL_GRADES, None, None, slow).engine_id == "nano-banana-2.1"


def test_choose_unconfirmed_prices_sort_after_confirmed_ones(manifests, health_up):
    two = priced(subset(manifests, "nano-banana-2.1", "gpt-image-2.5"), **{"gpt-image-2.5": 9.99})
    assert choose("relight", two, ALL_GRADES, None, None, health_up).engine_id == "gpt-image-2.5"


def test_choose_prefs_quantities_make_prices_comparable(manifests, health_up):
    per_mp = manifests["nano-banana-2.1"].model_copy(
        update={"price": PriceSpec(model="per_megapixel", usd=0.01, status="confirmed")}
    )
    per_image = manifests["gpt-image-2.5"].model_copy(
        update={"price": PriceSpec(model="per_image", usd=0.05, status="confirmed")}
    )
    two = {"nano-banana-2.1": per_mp, "gpt-image-2.5": per_image}
    # raw unit prices: 0.01 per megapixel looks cheaper; a 12 MP job costs 0.12, dearer than 0.05 per image
    assert choose("relight", two, ALL_GRADES, None, None, health_up).engine_id == "nano-banana-2.1"
    assert (
        choose("relight", two, ALL_GRADES, None, None, health_up, Prefs(megapixels=12)).engine_id
        == "gpt-image-2.5"
    )


def test_next_same_model_offers_only_the_same_model_with_requote(manifests, health_up):
    seedance = subset(manifests, "seedance-2.5-r2v", "seedance-2.5-i2v", "kling-o3-4k", "kling-v3-pro")
    pins = {"crop_pair_motion": Pin("seedance-2.5-r2v")}
    choice = choose("crop_pair_motion", seedance, ALL_GRADES, pins, None, health_up)
    assert (choice.engine_id, choice.provider, choice.requote) == ("seedance-2.5-r2v", "atlas", False)
    nxt = next_same_model(choice)
    assert [(c.engine_id, c.provider) for c in nxt] == [
        ("seedance-2.5-r2v", "byteplus"),
        ("seedance-2.5-r2v", "wavespeed"),
        ("seedance-2.5-r2v", "runway"),  # Wave 11: the same model on Runway, quoted again like the others
    ]
    assert all(c.requote is True for c in nxt)
    assert all(c.model_id == choice.model_id for c in nxt)
    # runway is a route of its own, not a fallback of atlas
    assert [c.fallback_of for c in nxt] == ["atlas", "atlas", None]
    assert all("requote" in " ".join(c.reasons) or "quoted again" in " ".join(c.reasons) for c in nxt)
    # walking down the chain: each step lists what is left, never the failed route, never another model
    after_byteplus = next_same_model(nxt[0])
    assert [c.provider for c in after_byteplus] == ["wavespeed", "runway"]
    after_wavespeed = next_same_model(after_byteplus[0])
    assert [c.provider for c in after_wavespeed] == ["runway"]
    assert next_same_model(after_wavespeed[0]) == []


def test_next_same_model_never_crosses_to_a_different_model(manifests, health_up):
    kling = subset(manifests, "kling-o3-4k", "kling-v3-pro", "veo-3.1")
    choice = choose("crop_pair_motion", kling, ALL_GRADES, None, None, health_up)
    # the cheapest of the three: Kling v3 Pro on its confirmed runway price (0.12 a second)
    assert (choice.engine_id, choice.provider) == ("kling-v3-pro", "runway")
    # the same model on its other route (atlas), quoted again; kling o3 and veo are other models: a person decides
    assert [(c.engine_id, c.provider, c.requote) for c in next_same_model(choice)] == [
        ("kling-v3-pro", "atlas", True)
    ]


def test_next_same_model_does_not_cross_between_reference_and_image_to_video(manifests, health_up):
    both = subset(manifests, "seedance-2.5-r2v", "seedance-2.5-i2v")
    assert manifests["seedance-2.5-r2v"].model_id == manifests["seedance-2.5-i2v"].model_id
    choice = choose("crop_pair_motion", both, ALL_GRADES, None, None, health_up)
    assert {c.engine_id for c in next_same_model(choice)} == {choice.engine_id}


def test_next_same_model_respects_custody_and_health(manifests):
    seedance = subset(manifests, "seedance-2.5-r2v")
    health = {
        "atlas": HealthEntry("ok", 100.0),
        "byteplus": HealthEntry("down"),
        "wavespeed": HealthEntry("ok", 300.0),
    }
    choice = choose("crop_pair_motion", seedance, ALL_GRADES, None, None, health)
    assert [c.provider for c in next_same_model(choice)] == ["wavespeed"]  # byteplus is down: not offered


def test_choose_picks_the_fallback_route_when_the_primary_is_down(manifests, health_up):
    seedance = subset(manifests, "seedance-2.5-r2v")
    health = {**health_up, "atlas": Health(False)}
    choice = choose("crop_pair_motion", seedance, ALL_GRADES, None, None, health)
    assert choice.provider in {"byteplus", "wavespeed"}
    assert choice.fallback_of == "atlas"
    assert any(r.startswith("fallback:") for r in choice.reasons)


def test_choose_skips_retired_routes(manifests, health_up):
    nano = manifests["nano-banana-2.1"]
    retired = nano.model_copy(
        update={
            "routes": [
                r.model_copy(update={"status": "retired" if r.provider == "gemini" else "active"})
                for r in nano.routes
            ]
        }
    )
    assert choose("relight", {nano.id: retired}, ALL_GRADES, None, None, health_up).provider == "atlas"


def test_choose_accepts_a_list_of_manifests_and_a_task_mapping(manifests, health_up):
    choice = choose(
        {"name": "relight", "shot_class": "medium"},
        list(manifests.values()),
        NDA_GRADES,
        None,
        None,
        health_up,
    )
    assert choice.engine_id == "qwen-image-edit-2509"


def test_choose_is_pure(manifests, health_up):
    before = {k: v.model_dump() for k, v in manifests.items()}
    pins = {"relight": Pin("nano-banana-2.1")}
    cards = [Card.make("nano-banana-2.1", "relight", None, 20, 20)]
    first = choose("relight", manifests, ALL_GRADES, pins, cards, health_up)
    second = choose("relight", manifests, ALL_GRADES, pins, cards, health_up)
    assert first == second
    assert {k: v.model_dump() for k, v in manifests.items()} == before
