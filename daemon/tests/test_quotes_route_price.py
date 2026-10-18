"""build_quote prices a route that carries its own price (Runway) from that spec: its tiers, its status, its
per-generation floor. A route without a price keeps pricing from the manifest, exactly as before."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from plate_daemon.quotes import build_quote
from plate_engines.models import EngineManifest

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)

RUNWAY_PRICE = {
    "model": "per_second",
    "usd": 0.20,
    "status": "confirmed",
    "minimum_usd": 0.80,
    "tiers": [
        {"match": {"resolution": "480p"}, "usd": 0.20, "status": "confirmed"},
        {"match": {"resolution": "720p"}, "usd": 0.30, "status": "confirmed"},
        {"match": {"resolution": "1080p"}, "usd": 0.68, "status": "confirmed"},
    ],
}


def synthetic() -> EngineManifest:
    return EngineManifest.model_validate(
        {
            "id": "synthetic",
            "name": "Synthetic",
            "version": "unpinned",
            "pinned": False,
            "kind": "video_i2v",
            "inputs": [{"name": "image", "type": "image"}],
            "outputs": [{"name": "video", "type": "video"}],
            "params": [
                {"name": "resolution", "type": "enum", "default": "720P", "enum": ["480P", "720P", "1080P"]},
                {"name": "duration", "type": "int", "default": 5, "range": [1, 30]},
            ],
            "price": {"model": "per_second", "usd": 0.10, "status": "unconfirmed"},
            "determinism": "nondeterministic",
            "licence": {"vendor_terms_url": "unknown"},
            "allowed_tasks": ["crop_pair_motion"],
            "model_id": "synthetic-model",
            "routes": [
                {
                    "provider": "atlas",
                    "endpoint": "https://api.atlas.test",
                    "region": "us",
                    "custody_grade": "D",
                    "custody_status": "confirmed",
                    "terms_hash": "unknown",
                    "upload_visibility": "public",
                    "allowed_hosts": ["api.atlas.test"],
                },
                {
                    "provider": "runway",
                    "endpoint": "https://api.dev.runwayml.com",
                    "region": "us",
                    "custody_grade": "D",
                    "custody_status": "confirmed",
                    "terms_hash": "unknown",
                    "upload_visibility": "private",
                    "allowed_hosts": ["api.dev.runwayml.com"],
                    "model_slug": "synthetic-runway",
                    "price": RUNWAY_PRICE,
                },
            ],
        }
    )


def quote(route: str, **params):
    return build_quote(synthetic(), route, {"image": "a" * 64}, params, now=T0)


def test_runway_route_prices_from_its_own_tier():
    q = quote("runway", duration=5, resolution="1080P")
    assert q.price_usd == pytest.approx(3.4)
    assert q.price_status == "confirmed"
    assert q.price_tier == "resolution=1080p"
    assert (q.provider, q.route_id, q.model_version) == ("runway", "runway", "synthetic-runway")


def test_runway_minimum_is_the_floor():
    at_floor = quote("runway", duration=4, resolution="480P")
    assert at_floor.price_usd == pytest.approx(0.80)
    assert at_floor.price_tier == "resolution=480p"
    below = quote("runway", duration=3, resolution="480P")  # 3 x 0.20 = 0.60
    assert below.price_usd == pytest.approx(0.80)


def test_the_floor_applies_per_output():
    q = quote("runway", duration=3, resolution="480P", candidate_count=2)
    assert q.price_usd == pytest.approx(1.6)


def test_no_tier_match_falls_back_to_the_route_spec_not_the_manifest():
    q = quote("runway", duration=10, resolution="4K")  # no 4K row: the route's flat 0.20 per second
    assert q.price_usd == pytest.approx(2.0)
    assert q.price_status == "confirmed"
    assert q.price_tier is None


def test_the_atlas_route_still_prices_from_the_manifest_spec():
    q = quote("atlas", duration=5, resolution="1080P")
    assert q.price_usd == pytest.approx(0.5)
    assert q.price_status == "unconfirmed"
    assert q.price_tier is None
    assert q.provider == "atlas"


def test_no_route_argument_picks_the_first_route_and_its_manifest_price():
    q = quote(None, duration=5)
    assert (q.provider, q.price_usd, q.price_status) == ("atlas", pytest.approx(0.5), "unconfirmed")


def test_a_manifest_floor_applies_on_the_manifest_path_too():
    m = synthetic()
    floored = m.model_copy(update={"price": m.price.model_copy(update={"minimum_usd": 2.0})})
    q = build_quote(floored, "atlas", {}, {"duration": 5}, now=T0)
    assert q.price_usd == pytest.approx(2.0)
