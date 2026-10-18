"""Quote pricing by tier: build_quote picks the first manifest price tier whose params match the request (quality,
image size, resolution, audio), takes its price and status, and names it in Quote.price_tier. No match falls back
to price.by[<size label>], then price.usd with the manifest status, exactly as before."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

import pytest

from plate_daemon.db import tx
from plate_daemon.index import Index, apply_packet_entry
from plate_daemon.machine import create_run, get_run
from plate_daemon.models import Quote, Route, RunCreate
from plate_daemon.quotes import _norm, build_quote, get_quote, quote_run, requote_needed
from plate_engines.loader import load_manifests
from plate_engines.models import EngineManifest

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
MANIFESTS = load_manifests()
TIERED = sorted(k for k, m in MANIFESTS.items() if m.price.tiers)

GPT, NANO, SEEDANCE = (MANIFESTS[k] for k in ("gpt-image-2.5", "nano-banana-2.1", "seedance-2.5-i2v"))


def quote(manifest, params=None, route=None):
    return build_quote(manifest, route, {"image": "a" * 64}, params or {}, now=T0)


def fill(manifest, params):
    """The extras a price model needs on top of the tier params (a duration, a size)."""
    out = dict(params)
    if manifest.price.model == "per_second":
        out.setdefault("duration", 5)
    if manifest.price.model == "per_megapixel":
        out.setdefault("width", 2000)
        out.setdefault("height", 1000)
    return out


def synthetic(price: dict, params: list[dict] | None = None, sizes: dict | None = None) -> EngineManifest:
    return EngineManifest.model_validate(
        {
            **({"sizes": sizes} if sizes else {}),
            "id": "synthetic",
            "name": "Synthetic",
            "version": "unpinned",
            "pinned": False,
            "kind": "video_i2v",
            "inputs": [{"name": "image", "type": "image"}],
            "outputs": [{"name": "video", "type": "video"}],
            "params": params
            or [
                {"name": "resolution", "type": "enum", "default": "720P", "enum": ["480P", "720P", "1080P"]},
                {"name": "generate_audio", "type": "bool", "default": False},
                {"name": "duration", "type": "int", "default": 5, "range": [1, 30]},
            ],
            "price": price,
            "determinism": "nondeterministic",
            "licence": {"vendor_terms_url": "unknown"},
            "allowed_tasks": ["crop_pair_motion"],
            "routes": [
                {
                    "provider": "vendor",
                    "endpoint": "https://api.vendor.test",
                    "region": "us",
                    "custody_grade": "D",
                    "custody_status": "confirmed",
                    "terms_hash": "unknown",
                    "upload_visibility": "public",
                    "allowed_hosts": ["api.vendor.test"],
                }
            ],
        }
    )


# ---------------------------------------------------------------------------- every shipped tier moves the price


def _pairs():
    return [
        pytest.param(engine_id, a, b, id=f"{engine_id}:{a}-{b}")
        for engine_id in TIERED
        for a, b in combinations(range(len(MANIFESTS[engine_id].price.tiers)), 2)
    ]


@pytest.mark.parametrize(("engine_id", "a", "b"), _pairs())
def test_changing_the_matched_param_between_two_tier_values_changes_the_price(engine_id, a, b):
    m = MANIFESTS[engine_id]
    ta, tb = m.price.tiers[a], m.price.tiers[b]
    qa, qb = quote(m, fill(m, ta.match)), quote(m, fill(m, tb.match))
    assert qa.price_usd != qb.price_usd
    assert (qa.price_status, qb.price_status) == (ta.status, tb.status)
    assert qa.price_tier == ",".join(f"{k}={v}" for k, v in ta.match.items())  # these rows match on text
    assert qb.price_tier == ",".join(f"{k}={v}" for k, v in tb.match.items())


@pytest.mark.parametrize("engine_id", TIERED)
def test_each_tier_row_prices_exactly_its_own_usd(engine_id):
    m = MANIFESTS[engine_id]
    for tier in m.price.tiers:
        params = fill(m, tier.match)
        secs = params["duration"] if m.price.model == "per_second" else 1
        assert quote(m, params).price_usd == pytest.approx(tier.usd * secs)


# ---------------------------------------------------------------------------- the three shipped engines


def test_gpt_image_medium_is_cheaper_than_high_at_count_one():
    high = quote(GPT, {"quality": "high"})
    medium = quote(GPT, {"quality": "medium"})
    assert (high.price_usd, medium.price_usd) == (0.16, 0.07)
    assert (high.price_tier, medium.price_tier) == ("quality=high", "quality=medium")
    assert (high.price_status, medium.price_status) == ("unconfirmed", "unconfirmed")


def test_gpt_image_with_no_quality_uses_the_manifest_default_which_is_medium():
    q = quote(GPT)
    assert q.price_usd == 0.07 and q.price_tier == "quality=medium"


def test_gpt_image_low_is_an_estimate_and_n_multiplies_the_tier_price():
    low = quote(GPT, {"quality": "low"})
    assert (low.price_usd, low.price_status, low.price_tier) == (0.03, "estimated", "quality=low")
    assert quote(GPT, {"quality": "high", "n": 3}).price_usd == pytest.approx(0.48)


def test_gpt_image_quality_beats_the_size_label_and_the_old_by_keys():
    q = quote(GPT, {"quality": "medium", "size": "1024x1024"})
    assert q.price_usd == 0.07 and q.size == {"w": 0, "h": 0, "label": "1024x1024"}


def test_nano_banana_4k_is_a_flagged_estimate():
    q = quote(NANO, {"image_size": "4K"})
    assert q.price_usd == pytest.approx(0.136)
    assert q.price_status == "estimated" and q.price_tier == "image_size=4K"
    assert q.size == {"w": 4096, "h": 4096, "label": "4K"}


def test_nano_banana_1k_and_2k():
    one, two = quote(NANO, {"image_size": "1K"}), quote(NANO, {"image_size": "2K"})
    assert (one.price_usd, one.price_status) == (0.034, "unconfirmed")
    assert (two.price_usd, two.price_status) == (0.068, "estimated")
    assert two.price_usd == pytest.approx(2 * one.price_usd)
    assert quote(NANO, {"image_size": "4K"}).price_usd == pytest.approx(4 * one.price_usd)


def test_nano_banana_with_no_image_size_falls_back_to_the_plain_price():
    q = quote(NANO)
    assert (q.price_usd, q.price_status, q.price_tier) == (0.034, "unconfirmed", None)
    assert quote(NANO, {"image_size": "8K"}).price_tier is None  # a value no row names


def test_nano_banana_candidate_count_multiplies_the_tier_price():
    assert quote(NANO, {"image_size": "2K", "candidate_count": 2}).price_usd == pytest.approx(0.136)


def test_seedance_five_seconds_by_resolution():
    p480 = quote(SEEDANCE, {"resolution": "480P", "duration": 5})
    p720 = quote(SEEDANCE, {"resolution": "720P", "duration": 5})
    p1080 = quote(SEEDANCE, {"resolution": "1080P", "duration": 5})
    assert p480.price_usd == pytest.approx(0.50)
    assert p720.price_usd == pytest.approx(1.80)
    assert p1080.price_usd == pytest.approx(4.50)
    assert p480.price_usd < p720.price_usd < p1080.price_usd
    assert p1080.price_tier == "resolution=1080P" and p1080.price_status == "unconfirmed"


def test_seedance_default_resolution_is_480p_and_duration_is_still_required():
    assert quote(SEEDANCE, {"duration": 5}).price_usd == pytest.approx(0.50)
    with pytest.raises(ValueError, match="duration"):
        quote(SEEDANCE, {"resolution": "1080P"})


# ---------------------------------------------------------------------------- matching rules


def test_tier_values_compare_case_insensitively_and_by_text():
    assert quote(NANO, {"image_size": "4k"}).price_usd == pytest.approx(0.136)
    assert quote(GPT, {"quality": "HIGH"}).price_usd == 0.16
    assert quote(SEEDANCE, {"resolution": "1080p", "duration": 5}).price_usd == pytest.approx(4.5)
    assert (_norm(True), _norm(False), _norm(2.0), _norm(2.5), _norm(" Hi ")) == (
        "true",
        "false",
        "2",
        "2.5",
        "hi",
    )


def test_the_first_matching_row_wins_and_every_key_must_match():
    price = {
        "model": "per_second",
        "usd": 0.10,
        "tiers": [
            {"match": {"resolution": "1080P", "generate_audio": True}, "usd": 1.25, "status": "estimated"},
            {"match": {"resolution": "1080P"}, "usd": 0.90},
            {"match": {"resolution": "1080P"}, "usd": 7.00},  # shadowed
            {"match": {"generate_audio": True}, "usd": 0.20},
        ],
    }
    m = synthetic(price)
    both = quote(m, {"resolution": "1080P", "generate_audio": True, "duration": 2})
    assert both.price_usd == pytest.approx(2.5) and both.price_status == "estimated"
    assert both.price_tier == "resolution=1080P,generate_audio=true"
    silent = quote(m, {"resolution": "1080P", "duration": 2})
    assert silent.price_usd == pytest.approx(1.8) and silent.price_tier == "resolution=1080P"
    audio_only = quote(m, {"resolution": "720P", "generate_audio": True, "duration": 2})
    assert audio_only.price_usd == pytest.approx(0.4) and audio_only.price_tier == "generate_audio=true"


def test_manifest_param_defaults_feed_the_match_and_request_params_override_them():
    price = {"model": "per_second", "usd": 0.10, "tiers": [{"match": {"resolution": "720P"}, "usd": 0.36}]}
    m = synthetic(price)  # resolution defaults to 720P
    assert quote(m, {"duration": 1}).price_usd == pytest.approx(0.36)
    assert quote(m, {"duration": 1, "resolution": "480P"}).price_usd == pytest.approx(0.10)
    # a None param does not erase the default
    assert quote(m, {"duration": 1, "resolution": None}).price_usd == pytest.approx(0.36)


def test_a_boolean_default_matches_a_boolean_row():
    price = {"model": "flat", "usd": 1.0, "tiers": [{"match": {"generate_audio": False}, "usd": 0.5}]}
    m = synthetic(price)
    assert quote(m).price_usd == 0.5
    assert quote(m, {"generate_audio": True}).price_usd == 1.0


def test_the_size_label_is_a_match_key_too():
    m = synthetic(
        {
            "model": "per_image",
            "usd": 0.04,
            "by": {"2K": 0.08},
            "tiers": [{"match": {"size": "4K"}, "usd": 0.5}],
        },
        params=[{"name": "quality", "type": "str"}],
        sizes={"legal": [{"w": 2048, "h": 2048, "label": "2K"}, {"w": 4096, "h": 4096, "label": "4K"}]},
    )
    q4 = quote(m, {"size": {"w": 4096, "h": 4096, "label": "4K"}})
    assert q4.price_usd == 0.5 and q4.price_tier == "size=4K"
    q2 = quote(m, {"image_size": "2K"})  # no size row for 2K: the old by[label] price
    assert q2.price_usd == 0.08 and q2.price_tier is None


def test_no_match_keeps_the_old_by_then_usd_price_with_the_manifest_status():
    price = {
        "model": "per_image",
        "usd": 0.04,
        "by": {"1K": 0.03},
        "status": "confirmed",
        "tiers": [{"match": {"quality": "high"}, "usd": 0.5, "status": "estimated"}],
    }
    m = synthetic(price, params=[{"name": "quality", "type": "enum", "enum": ["low", "high"]}])
    plain = quote(m, {"quality": "low"})
    assert (plain.price_usd, plain.price_status, plain.price_tier) == (0.04, "confirmed", None)
    labelled = quote(m, {"quality": "low", "image_size": "1K"})
    assert (labelled.price_usd, labelled.price_status, labelled.price_tier) == (0.03, "confirmed", None)
    hit = quote(m, {"quality": "high", "image_size": "1K"})
    assert (hit.price_usd, hit.price_status, hit.price_tier) == (0.5, "estimated", "quality=high")


def test_a_per_megapixel_manifest_multiplies_the_tier_price_by_the_megapixels():
    m = synthetic(
        {"model": "per_megapixel", "usd": 0.01, "tiers": [{"match": {"quality": "high"}, "usd": 0.05}]},
        params=[{"name": "quality", "type": "enum", "enum": ["low", "high"], "default": "low"}],
    )
    wh = {"width": 2000, "height": 1000}
    assert quote(m, {**wh}).price_usd == pytest.approx(0.02)
    assert quote(m, {**wh, "quality": "high"}).price_usd == pytest.approx(0.10)


def test_manifests_without_tiers_price_exactly_as_before():
    for m in MANIFESTS.values():
        spec = m.routes[0].price or m.price  # Wave 11: a route that carries its own price (gen-4.5) wins
        if spec.tiers:
            continue
        params = fill(m, {})
        try:
            q = quote(m, params)
        except ValueError:
            continue  # a size or duration it needs is not in this bare request
        assert q.price_tier is None and q.price_status == spec.status


# ---------------------------------------------------------------------------- requote and storage


def test_requote_rule_is_unchanged_and_sees_a_tier_change():
    medium, high = quote(GPT, {"quality": "medium"}), quote(GPT, {"quality": "high"})
    assert requote_needed(medium, high) is True  # 0.07 -> 0.16
    assert requote_needed(high, high) is False
    assert requote_needed(medium, medium.model_copy(update={"price_usd": 0.0734})) is False  # +4.9 %
    assert requote_needed(medium, medium.model_copy(update={"price_usd": 0.0736})) is True  # +5.1 %


def test_price_tier_defaults_to_none_and_rides_the_quote_json():
    q = quote(GPT, {"quality": "high"})
    assert q.terms_json()["price_tier"] == "quality=high"
    plain = Quote(
        provider="p",
        model="m",
        region="r",
        custody_grade="D",
        price_usd=0.1,
        eta_s=1,
        created_utc=T0,
        expires_utc=T0,
    )
    assert plain.price_tier is None and plain.terms_json()["price_tier"] is None


@pytest.fixture
def idx(tmp_path: Path) -> Index:
    index = Index(tmp_path / "home")
    with index.project("tiers") as conn, tx(conn):
        apply_packet_entry(
            conn,
            "tiers",
            "pkt_tiers",
            {
                "shot_id": "s01",
                "kind": "still",
                "tier": "A",
                "packet_version": "0.1.1",
                "path": str(tmp_path / "packets" / "pkt_tiers"),
                "manifest_blake3": "a" * 64,
            },
        )
    return index


def test_a_stored_quote_keeps_its_tier(idx):
    route = Route(engine_id="nano-banana-2.1", provider="gemini", region="unknown")
    with idx.project("tiers") as conn:
        run = create_run(
            conn,
            RunCreate(
                project_id="tiers",
                packet_id="pkt_tiers",
                inputs={"plate": uuid.uuid4().hex},
                params={"image_size": "4K"},
                route=route,
            ),
            now=T0,
        )
        queued, q = quote_run(conn, run.id, now=T0, manifests=MANIFESTS)
        assert q.price_usd == pytest.approx(0.136) and q.price_status == "estimated"
        assert q.price_tier == "image_size=4K"
        assert queued.quote is not None and queued.quote.price_tier == "image_size=4K"
        assert get_quote(conn, q.id).price_tier == "image_size=4K"
        assert get_run(conn, run.id).quote.price_status == "estimated"
