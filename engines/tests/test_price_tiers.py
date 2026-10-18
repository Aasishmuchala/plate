"""Price tiers (PriceSpec.tiers) and manifest size limits (EngineManifest.limits): the model, the shipped rows and
the overlay round trip. Quote arithmetic over these rows is tested in daemon/tests/test_quotes_tiers.py."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from plate_engines import prices
from plate_engines.loader import DEFAULT_MANIFEST_DIR, ManifestError, load_manifests
from plate_engines.models import EngineManifest, Limits, PriceSpec, PriceTier

TIERED = ("gpt-image-2.5", "nano-banana-2.1", "seedance-2.5-i2v")


def raw_manifest(name: str = "gpt-image-2.5") -> dict:
    return yaml.safe_load((DEFAULT_MANIFEST_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------- the model


def test_tiers_default_to_empty_and_old_price_specs_load():
    spec = PriceSpec(model="flat", usd=1.0)
    assert spec.tiers == []
    assert PriceSpec.model_validate({"model": "per_image", "usd": 0.1, "by": {"1K": 0.1}}).tiers == []


def test_a_price_tier_has_a_match_a_price_a_status_and_a_note():
    tier = PriceTier(match={"quality": "high", "n": 2, "ratio": 1.5, "audio": True}, usd=0.5)
    assert tier.status == "unconfirmed" and tier.note == ""
    assert tier.match == {"quality": "high", "n": 2, "ratio": 1.5, "audio": True}
    for status in ("unconfirmed", "confirmed", "estimated"):
        assert PriceTier(match={"k": "v"}, usd=0, status=status).status == status


@pytest.mark.parametrize(
    "body",
    [
        {"match": {"k": "v"}, "usd": -0.01},  # a price is not negative
        {"match": {"k": "v"}, "usd": 1, "status": "guessed"},
        {"match": {"k": "v"}, "usd": 1, "colour": "red"},  # extra keys are typos
        {"match": {}, "usd": 1},  # would win for every request
        {"match": {"": "v"}, "usd": 1},
        {"match": {"k": None}, "usd": 1},
        {"match": {"k": ["a"]}, "usd": 1},
        {"usd": 1},
    ],
)
def test_a_bad_price_tier_fails_loudly(body):
    with pytest.raises(ValidationError):
        PriceTier.model_validate(body)


def test_a_price_spec_rejects_an_unknown_tier_key():
    with pytest.raises(ValidationError):
        PriceSpec.model_validate(
            {"model": "flat", "usd": 1, "tiers": [{"match": {"k": "v"}, "usd": 1, "x": 1}]}
        )


def test_price_spec_status_stays_two_valued():
    # "estimated" belongs to a tier row, not to the engine's whole price
    with pytest.raises(ValidationError):
        PriceSpec(model="flat", usd=1.0, status="estimated")


def test_limits_default_none_and_accept_megapixels():
    raw = raw_manifest()
    assert EngineManifest.model_validate(raw).limits is None
    raw["limits"] = {"max_input_mp": 25.0, "max_output_mp": 4.2}
    m = EngineManifest.model_validate(raw)
    assert m.limits == Limits(max_input_mp=25.0, max_output_mp=4.2)
    raw["limits"] = {"max_output_mp": 8}
    assert EngineManifest.model_validate(raw).limits == Limits(max_input_mp=None, max_output_mp=8.0)
    raw["limits"] = {}
    assert EngineManifest.model_validate(raw).limits == Limits(max_input_mp=None, max_output_mp=None)


def test_an_unknown_key_under_limits_fails_validation():
    raw = raw_manifest()
    raw["limits"] = {"max_input_mp": 25.0, "max_output_pixels": 4_000_000}
    with pytest.raises(ValidationError, match="max_output_pixels"):
        EngineManifest.model_validate(raw)


@pytest.mark.parametrize("mp", [0, -1.0])
def test_limits_must_be_positive(mp):
    with pytest.raises(ValidationError):
        Limits(max_input_mp=mp)
    with pytest.raises(ValidationError):
        Limits(max_output_mp=mp)


def test_the_loader_names_the_file_for_a_bad_limits_key(tmp_path):
    raw = raw_manifest()
    raw["limits"] = {"max_input_mp": 25.0, "bogus": 1}
    (tmp_path / "gpt-image-2.5.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")
    with pytest.raises(ManifestError, match=r"gpt-image-2\.5\.yaml.*limits"):
        load_manifests(tmp_path)


def test_a_manifest_with_limits_loads_from_disk(tmp_path):
    raw = raw_manifest()
    raw["limits"] = {"max_input_mp": 20, "max_output_mp": 4.19}
    (tmp_path / "gpt-image-2.5.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")
    assert load_manifests(tmp_path)["gpt-image-2.5"].limits == Limits(max_input_mp=20.0, max_output_mp=4.19)


# ---------------------------------------------------------------------------- the shipped rows


def test_the_three_manifests_ship_their_rows(manifests):
    def rows(engine_id):
        return [(t.match, t.usd, t.status) for t in manifests[engine_id].price.tiers]

    assert rows("gpt-image-2.5") == [
        ({"quality": "high"}, 0.16, "unconfirmed"),
        ({"quality": "medium"}, 0.07, "unconfirmed"),
        ({"quality": "low"}, 0.03, "estimated"),
    ]
    assert rows("nano-banana-2.1") == [
        ({"image_size": "1K"}, 0.034, "unconfirmed"),
        ({"image_size": "2K"}, 0.068, "estimated"),
        ({"image_size": "4K"}, 0.136, "estimated"),
    ]
    assert rows("seedance-2.5-i2v") == [
        ({"resolution": "480P"}, 0.10, "unconfirmed"),
        ({"resolution": "720P"}, 0.36, "unconfirmed"),
        ({"resolution": "1080P"}, 0.90, "unconfirmed"),
    ]
    assert manifests["gpt-image-2.5"].price.tiers[2].note == "no reading; half of medium"


def test_usd_and_by_stay_as_they_were(manifests):
    gpt, nano, seed = (manifests[k].price for k in TIERED)
    assert (gpt.usd, gpt.by) == (0.16, {"1K_high": 0.16, "1K_medium": 0.07})
    assert (nano.usd, nano.by) == (0.034, {"1K": 0.034})
    assert (seed.usd, seed.by) == (0.10, {"base": 0.134, "atlas_discounted": 0.0268})
    assert all(price.status == "unconfirmed" for price in (gpt, nano, seed))


def test_only_the_three_priced_manifests_carry_tiers_and_only_the_upscalers_have_limits(manifests):
    assert sorted(k for k, m in manifests.items() if m.price.tiers) == sorted(TIERED)
    # T100 shipped the first limits: Magnific (6 MP in, 50 MP out) and Real-ESRGAN (50 MP out).
    assert sorted(k for k, m in manifests.items() if m.limits is not None) == [
        "magnific-precision-v2",
        "real-esrgan",
    ]


@pytest.mark.parametrize("engine_id", TIERED)
def test_every_tier_names_a_real_param_and_one_of_its_values(manifests, engine_id):
    """A typo in a tier row never matches and silently falls back to the plain price: catch it here."""
    m = manifests[engine_id]
    params = {p.name: p for p in m.params}
    for tier in m.price.tiers:
        for key, value in tier.match.items():
            assert key in params, f"{engine_id}: tier key {key!r} is not a param"
            assert value in (params[key].enum or []), f"{engine_id}: {value!r} is not a {key} value"
    seen = [tuple(sorted(t.match.items())) for t in m.price.tiers]
    assert len(set(seen)) == len(seen), "two rows with the same match: the second can never win"
    assert len({t.usd for t in m.price.tiers}) == len(m.price.tiers), "tier prices must differ"


@pytest.mark.parametrize("engine_id", TIERED)
def test_an_estimated_tier_says_why(manifests, engine_id):
    for tier in manifests[engine_id].price.tiers:
        if tier.status == "estimated":
            assert tier.note, f"{engine_id}: an estimated row needs a note"


# ---------------------------------------------------------------------------- the overlay round trip


def write_catalogue(directory: Path, vendor: str, models: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{vendor}.json").write_text(
        json.dumps({"vendor": vendor, "as_of": "2026-10-18", "currency": "USD", "models": models}),
        encoding="utf-8",
        newline="\n",
    )


def test_a_price_refresh_overlay_round_trips_the_tiers(manifests, tmp_path):
    cat_dir, overlay = tmp_path / "catalogues", tmp_path / "out" / "_prices.yaml"
    write_catalogue(cat_dir, "openai", [{"id": "gpt-image-2.5", "unit": "per_image", "usd": 0.20}])
    changes = prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay)
    assert [c.engine_id for c in changes] == ["gpt-image-2.5"]
    assert overlay.exists()

    priced = prices.apply_overlay(manifests, prices.read_overlay(overlay))
    gpt = priced["gpt-image-2.5"].price
    assert gpt.usd == 0.20 and gpt.status == "confirmed"  # the overlay did land
    assert gpt.tiers == manifests["gpt-image-2.5"].price.tiers  # and the tiers rode through it untouched
    # the overlay file itself never carries or drops tier rows
    assert "tiers" not in yaml.safe_load(overlay.read_text(encoding="utf-8"))["engines"]["gpt-image-2.5"]
    # and an engine without an overlay entry is the very same object
    assert priced["nano-banana-2.1"] is manifests["nano-banana-2.1"]


def test_load_priced_manifests_keeps_tiers(tmp_path):
    copy_dir = tmp_path / "manifests"
    copy_dir.mkdir()
    for name in TIERED:
        (copy_dir / f"{name}.yaml").write_bytes((DEFAULT_MANIFEST_DIR / f"{name}.yaml").read_bytes())
    base = load_manifests(copy_dir)
    prices.write_overlay(
        copy_dir / prices.OVERLAY_NAME,
        {
            "nano-banana-2.1": {
                "usd": 0.05,
                "by": {"1K": 0.05},
                "status": "confirmed",
                "source": "t",
                "as_of": "",
            }
        },
    )
    priced = prices.load_priced_manifests(copy_dir)
    assert priced["nano-banana-2.1"].price.usd == 0.05
    for name in TIERED:
        assert priced[name].price.tiers == base[name].price.tiers
    # the model dump (what a UI or an exporter reads) carries the rows with their statuses
    dumped = priced["nano-banana-2.1"].price.model_dump(mode="json")
    assert [(t["match"], t["usd"], t["status"]) for t in dumped["tiers"]] == [
        ({"image_size": "1K"}, 0.034, "unconfirmed"),
        ({"image_size": "2K"}, 0.068, "estimated"),
        ({"image_size": "4K"}, 0.136, "estimated"),
    ]
    assert PriceSpec.model_validate(copy.deepcopy(dumped)) == priced["nano-banana-2.1"].price
