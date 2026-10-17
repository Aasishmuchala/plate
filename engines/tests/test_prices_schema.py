"""Price refresh from catalogue fixtures, the overlay, the 5% requote rule, schema diffs and retirement alerts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from plate_engines import prices, retire, schema_diff
from plate_engines.health import AlertLog
from plate_engines.loader import DEFAULT_MANIFEST_DIR, load_manifests
from plate_engines.models import PriceSpec
from plate_engines.router import Pin

KLING = "kwaivgi/kling-video-o3-4k/image-to-video"
SEEDANCE_ATLAS = "bytedance/seedance-2.5/reference-to-video"


def write_catalogue(
    directory: Path, vendor: str, models: list[dict], as_of: str = "2026-10-18", name: str | None = None
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name or f"{vendor}.json")
    path.write_text(
        json.dumps({"vendor": vendor, "as_of": as_of, "currency": "USD", "models": models}),
        encoding="utf-8",
        newline="\n",
    )
    return path


def entry(model_id, unit, usd, **extra):
    return {"id": model_id, "unit": unit, "usd": usd, **extra}


@pytest.fixture
def cat_dir(tmp_path):
    return tmp_path / "catalogues"


@pytest.fixture
def overlay(tmp_path):
    return tmp_path / "out" / "_prices.yaml"


# ---------------------------------------------------------------------------- the 6% case from the plan


def test_a_6_percent_price_move_triggers_a_requote(manifests, cat_dir, overlay, tmp_path):
    old = manifests["kling-o3-4k"].price.usd
    new = round(old * 1.06, 6)
    write_catalogue(cat_dir, "atlas", [entry(KLING, "per_second", new, tiers={"atlas_discounted": 0.0536})])
    log = AlertLog(tmp_path / "alerts.jsonl")
    changes = prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay, alerts=log)
    assert [c.engine_id for c in changes] == ["kling-o3-4k"]
    c = changes[0]
    assert (c.old, c.new, c.tier) == (old, new, None)
    assert c.pct == pytest.approx(6.0, abs=1e-3)
    assert c.requote is True
    assert changes.requotes == [c]
    # the alert says so
    alerts = log.read()
    assert [a.kind for a in alerts] == ["price_change"]
    assert alerts[0].severity == "warn" and "requote" in alerts[0].message
    assert alerts[0].data["requote"] is True
    # quoted runs on that engine are flagged; others are not
    quoted = [
        {"run_id": "r1", "engine_id": "kling-o3-4k"},
        {"run_id": "r2", "engine_id": "kling-v3-pro"},
        {"run_id": "r3", "engine_id": "kling-o3-4k", "tier": "atlas_discounted"},
    ]
    assert prices.requote_runs(changes, quoted, alerts=log) == ["r1"]
    assert [a.kind for a in log.read()] == ["price_change", "requote"]
    assert log.read()[1].data["run_id"] == "r1"


def test_the_requote_threshold_is_strictly_over_5_percent_either_way():
    assert prices.PriceChange("e", 100.0, 105.0, prices.pct_change(100.0, 105.0)).requote is False
    assert prices.PriceChange("e", 100.0, 105.5, prices.pct_change(100.0, 105.5)).requote is True
    assert prices.PriceChange("e", 100.0, 94.0, prices.pct_change(100.0, 94.0)).requote is True  # a fall too
    assert prices.PriceChange("e", 100.0, 96.0, prices.pct_change(100.0, 96.0)).requote is False
    assert prices.pct_change(0.0, 0.0) == 0.0
    jump = prices.PriceChange("e", 0.0, 1.0, prices.pct_change(0.0, 1.0))
    assert jump.requote is True and jump.to_dict()["pct"] is None


def test_a_4_percent_move_is_reported_but_not_flagged(manifests, cat_dir, overlay):
    old = manifests["kling-v3-pro"].price.usd
    write_catalogue(
        cat_dir, "atlas", [entry("kwaivgi/kling-v3.0-pro/image-to-video", "per_second", old * 1.04)]
    )
    changes = prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay)
    assert len(changes) == 1 and changes[0].requote is False and changes.requotes == []
    assert prices.requote_runs(changes, [{"run_id": "r1", "engine_id": "kling-v3-pro"}]) == []


# ---------------------------------------------------------------------------- the overlay


def test_refresh_writes_the_overlay_once_and_is_idempotent(manifests, cat_dir, overlay):
    write_catalogue(
        cat_dir,
        "atlas",
        [entry(KLING, "per_second", 0.40, tiers={"atlas_discounted": 0.0536, "preview": 0.01})],
    )
    first = prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay)
    assert len(first) == 1
    doc = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    kling = doc["engines"]["kling-o3-4k"]
    assert kling["usd"] == 0.40 and kling["status"] == "confirmed" and kling["source"] == "catalogue:atlas"
    assert kling["as_of"] == "2026-10-18"
    assert (
        kling["by"]["atlas_discounted"] == 0.0536 and kling["by"]["preview"] == 0.01
    )  # a new tier is added silently
    assert overlay.read_bytes().count(b"\r") == 0
    stamp = overlay.stat().st_mtime_ns
    again = prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay)
    assert list(again) == []  # the overlay is the price in force now
    assert overlay.stat().st_mtime_ns == stamp  # and was not rewritten
    assert not list(overlay.parent.glob("*.tmp"))


def test_overlay_is_laid_over_the_manifests_without_touching_them(manifests, cat_dir, overlay):
    write_catalogue(cat_dir, "atlas", [entry(KLING, "per_second", 0.40)])
    prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay)
    priced = prices.apply_overlay(manifests, prices.read_overlay(overlay))
    assert priced["kling-o3-4k"].price.usd == 0.40
    assert priced["kling-o3-4k"].price.status == "confirmed"
    assert (
        priced["kling-o3-4k"].price.by["atlas_discounted"]
        == manifests["kling-o3-4k"].price.by["atlas_discounted"]
    )
    assert str(priced["kling-o3-4k"].price.as_of) == "2026-10-18"
    assert manifests["kling-o3-4k"].price.usd == 0.357  # the input is unchanged
    assert priced["nano-banana-2.1"] is manifests["nano-banana-2.1"]
    assert prices.read_overlay(overlay.parent / "missing.yaml") == {}


def test_the_loader_skips_the_overlay_file_and_load_priced_manifests_uses_it(tmp_path, cat_dir):
    copy = tmp_path / "manifests"
    shutil.copytree(DEFAULT_MANIFEST_DIR, copy)
    write_catalogue(cat_dir, "atlas", [entry(KLING, "per_second", 0.50)])
    prices.refresh(cat_dir, overlay_path=copy / prices.OVERLAY_NAME, manifests=load_manifests(copy))
    assert (copy / "_prices.yaml").exists()
    assert len(load_manifests(copy)) == 14  # the overlay is not a manifest
    assert load_manifests(copy)["kling-o3-4k"].price.usd == 0.357
    assert prices.load_priced_manifests(copy)["kling-o3-4k"].price.usd == 0.50
    assert len(load_manifests()) == 14  # the real folder is untouched by all of this
    assert not (DEFAULT_MANIFEST_DIR / "_prices.yaml").exists()


def test_the_default_overlay_path_honours_the_environment(monkeypatch, tmp_path):
    assert prices.default_overlay_path() == DEFAULT_MANIFEST_DIR / "_prices.yaml"
    monkeypatch.setenv(prices.OVERLAY_ENV, str(tmp_path / "elsewhere.yaml"))
    assert prices.default_overlay_path() == tmp_path / "elsewhere.yaml"


def test_a_malformed_overlay_is_an_error_not_a_guess(tmp_path):
    bad = tmp_path / "_prices.yaml"
    bad.write_text("engines: [1, 2]\n", encoding="utf-8")
    with pytest.raises(prices.OverlayError):
        prices.read_overlay(bad)
    bad.write_text("engines:\n  x: {usd: 'a lot'}\n", encoding="utf-8")
    with pytest.raises(prices.OverlayError, match="numeric usd"):
        prices.read_overlay(bad)


# ---------------------------------------------------------------------------- tiers, skips, staleness


def test_a_tier_move_is_its_own_change(manifests, cat_dir, overlay):
    write_catalogue(cat_dir, "atlas", [entry(KLING, "per_second", 0.357, tiers={"atlas_discounted": 0.08})])
    changes = prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay)
    assert [(c.engine_id, c.tier) for c in changes] == [("kling-o3-4k", "atlas_discounted")]
    assert changes[0].pct == pytest.approx((0.08 - 0.0536) / 0.0536 * 100)
    assert changes[0].requote is True
    runs = [
        {"run_id": "r1", "engine_id": "kling-o3-4k", "tier": "atlas_discounted"},
        {"run_id": "r2", "engine_id": "kling-o3-4k"},
    ]
    assert prices.requote_runs(changes, runs) == ["r1"]


def test_a_unit_mismatch_is_skipped_and_alerted_never_guessed(manifests, cat_dir, overlay, tmp_path):
    write_catalogue(cat_dir, "atlas", [entry(KLING, "per_image", 9.0)])
    log = AlertLog(tmp_path / "alerts.jsonl")
    changes = prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay, alerts=log)
    assert list(changes) == [] and "per_image" in changes.skipped[0]
    assert [a.kind for a in log.read()] == ["unit_mismatch"]
    assert not overlay.exists()


def test_an_unavailable_model_and_a_stale_catalogue_leave_prices_alone(manifests, cat_dir, overlay):
    write_catalogue(cat_dir, "atlas", [entry(KLING, "per_second", 0.5, available=False)])
    changes = prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay)
    assert list(changes) == [] and "unavailable" in changes.skipped[0]
    shutil.rmtree(cat_dir)
    write_catalogue(cat_dir, "atlas", [entry(KLING, "per_second", 0.40)], as_of="2026-10-18")
    prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay)
    shutil.rmtree(cat_dir)
    write_catalogue(cat_dir, "atlas", [entry(KLING, "per_second", 0.30)], as_of="2026-10-01")
    stale = prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay)
    assert list(stale) == [] and "older" in stale.skipped[0]
    assert prices.read_overlay(overlay)["kling-o3-4k"]["usd"] == 0.40


def test_only_the_primary_route_sets_the_price_and_unknown_vendors_are_ignored(manifests, cat_dir, overlay):
    write_catalogue(
        cat_dir, "byteplus", [entry("dreamina-seedance-2-5-260628", "per_second", 5.0)]
    )  # a fallback route
    write_catalogue(cat_dir, "nobody", [entry("x", "flat", 1.0)])
    write_catalogue(cat_dir, "atlas", [entry(SEEDANCE_ATLAS, "per_second", 0.10)])
    changes = prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay)
    assert list(changes) == []  # unchanged atlas price; the byteplus catalogue did not move it
    assert prices.read_overlay(overlay)["seedance-2.5-r2v"]["usd"] == 0.10
    assert prices.primary_route(manifests["seedance-2.5-r2v"]).provider == "atlas"


def test_write_false_computes_without_touching_the_overlay(manifests, cat_dir, overlay):
    write_catalogue(cat_dir, "atlas", [entry(KLING, "per_second", 0.5)])
    assert len(prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay, write=False)) == 1
    assert not overlay.exists()


def test_a_cheaper_price_that_confirms_changes_the_routing_order(manifests, cat_dir, overlay):
    from plate_engines.health import HealthEntry
    from plate_engines.router import choose

    write_catalogue(cat_dir, "gemini", [entry("gemini-3-pro-image", "per_image", 0.001)])
    prices.refresh(cat_dir, manifests=manifests, overlay_path=overlay)
    priced = prices.apply_overlay(manifests, prices.read_overlay(overlay))
    cloud = {k: priced[k] for k in ("nano-banana-pro", "gpt-image-2.5")}
    health = {p: HealthEntry("ok", 100.0) for m in cloud.values() for p in (r.provider for r in m.routes)}
    assert (
        choose("relight", cloud, {"D"}, None, None, health).engine_id == "nano-banana-pro"
    )  # confirmed + cheap
    assert (
        choose("relight", {k: manifests[k] for k in cloud}, {"D"}, None, None, health).engine_id
        == "gpt-image-2.5"
    )


# ---------------------------------------------------------------------------- catalogue validation


@pytest.mark.parametrize(
    "raw, needle",
    [
        ({"as_of": "2026-10-18", "models": []}, "vendor"),
        ({"vendor": "atlas", "models": []}, "as_of"),
        ({"vendor": "atlas", "as_of": "18/10/2026", "models": []}, "as_of"),
        ({"vendor": "atlas", "as_of": "2026-10-18", "currency": "INR", "models": []}, "USD"),
        ({"vendor": "atlas", "as_of": "2026-10-18", "models": {}}, "list"),
        (
            {"vendor": "atlas", "as_of": "2026-10-18", "models": [{"id": "m", "unit": "per_year", "usd": 1}]},
            "unit",
        ),
        (
            {"vendor": "atlas", "as_of": "2026-10-18", "models": [{"id": "m", "unit": "flat", "usd": -1}]},
            "usd",
        ),
        (
            {
                "vendor": "atlas",
                "as_of": "2026-10-18",
                "models": [{"id": "m", "unit": "flat", "usd": 1, "tiers": {"a": "x"}}],
            },
            "tier",
        ),
        (
            {
                "vendor": "atlas",
                "as_of": "2026-10-18",
                "models": [{"id": "m", "unit": "flat", "usd": 1}, {"id": "m", "unit": "flat", "usd": 2}],
            },
            "duplicate",
        ),
    ],
)
def test_bad_catalogues_are_rejected(cat_dir, raw, needle):
    cat_dir.mkdir(parents=True)
    (cat_dir / "bad.json").write_text(json.dumps(raw), encoding="utf-8", newline="\n")
    with pytest.raises(prices.CatalogueError, match=needle):
        prices.load_catalogues(cat_dir)


def test_unreadable_json_names_the_file(cat_dir):
    cat_dir.mkdir(parents=True)
    (cat_dir / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(prices.CatalogueError, match="broken.json"):
        prices.load_catalogues(cat_dir)


def test_later_catalogue_for_the_same_vendor_wins_per_model(cat_dir):
    write_catalogue(cat_dir, "atlas", [entry("m", "flat", 1.0)], as_of="2026-10-01", name="a.json")
    write_catalogue(
        cat_dir,
        "atlas",
        [entry("m", "flat", 2.0), entry("n", "flat", 3.0)],
        as_of="2026-10-18",
        name="b.json",
    )
    cat = prices.load_catalogues(cat_dir)
    assert cat.entry("atlas", "m").usd == 2.0 and cat.entry("atlas", "n").usd == 3.0
    assert cat.vendors == {"atlas"} and cat.model_ids("atlas") == {"m", "n"}


def test_the_price_spec_overlay_does_not_break_manifest_validation(manifests):
    spec = manifests["kling-o3-4k"].price
    assert isinstance(spec, PriceSpec) and spec.status == "unconfirmed"


# ---------------------------------------------------------------------------- schema diff


def schema(props, required=()):
    return {"type": "object", "required": list(required), "properties": props}


OLD = schema(
    {
        "image": {"type": "string"},
        "duration": {"type": "integer", "minimum": 1, "maximum": 15, "description": "seconds"},
        "resolution": {"type": "string", "enum": ["1080P", "4K"]},
        "image_tail": {"type": "string"},
        "elements": {
            "type": "array",
            "items": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
        },
    },
    required=["image", "duration"],
)


def test_identical_schemas_have_no_diff():
    out = schema_diff.diff(OLD, json.loads(json.dumps(OLD)))
    assert out == {"added": [], "removed": [], "changed": [], "alert": None, "reasons": []}


def test_added_removed_changed_without_an_alert():
    new = json.loads(json.dumps(OLD))
    new["properties"]["cfg_scale"] = {"type": "number"}
    del new["properties"]["image_tail"]
    new["properties"]["duration"]["maximum"] = 10
    new["properties"]["duration"]["description"] = "length in seconds"  # prose is ignored
    new["properties"]["image"]["type"] = "object"
    out = schema_diff.diff(OLD, new)
    assert out["added"] == ["cfg_scale"]
    assert out["removed"] == ["image_tail"]
    assert {(c["path"], c["attr"]) for c in out["changed"]} == {("duration", "maximum"), ("image", "type")}
    assert out["alert"] is None


def test_a_removed_required_field_alerts(tmp_path):
    new = json.loads(json.dumps(OLD))
    del new["properties"]["image"]
    new["required"] = ["duration"]
    log = AlertLog(tmp_path / "alerts.jsonl")
    out = schema_diff.diff(OLD, new, alerts=log, engine_id="kling-o3-4k", route_id="atlas")
    assert out["alert"] == "schema_change"
    assert out["removed"] == ["image"] and "removed required field 'image'" in out["reasons"]
    (alert,) = log.read()
    assert (alert.kind, alert.engine_id, alert.route_id, alert.severity) == (
        "schema_change",
        "kling-o3-4k",
        "atlas",
        "critical",
    )
    assert alert.data["removed"] == ["image"]


def test_a_changed_enum_alerts_with_the_values():
    new = json.loads(json.dumps(OLD))
    new["properties"]["resolution"]["enum"] = ["1080P", "2160P"]
    out = schema_diff.diff(OLD, new)
    assert out["alert"] == "schema_change"
    (change,) = out["changed"]
    assert (change["path"], change["attr"]) == ("resolution", "enum")
    assert change["added_values"] == ["2160P"] and change["removed_values"] == ["4K"]


def test_an_enum_appearing_or_disappearing_alerts():
    new = json.loads(json.dumps(OLD))
    del new["properties"]["resolution"]["enum"]
    assert schema_diff.diff(OLD, new)["alert"] == "schema_change"
    assert schema_diff.diff(new, OLD)["alert"] == "schema_change"


def test_removing_an_optional_field_does_not_alert_and_becoming_required_is_a_change():
    new = json.loads(json.dumps(OLD))
    del new["properties"]["image_tail"]
    assert schema_diff.diff(OLD, new)["alert"] is None
    stricter = json.loads(json.dumps(OLD))
    stricter["required"] = ["image", "duration", "resolution"]
    out = schema_diff.diff(OLD, stricter)
    assert out["alert"] is None
    assert [(c["path"], c["attr"], c["old"], c["new"]) for c in out["changed"]] == [
        ("resolution", "required", False, True)
    ]


def test_nested_array_item_paths():
    flat = schema_diff.flatten(OLD)
    assert "elements[].name" in flat and flat["elements[].name"]["required"] is True
    new = json.loads(json.dumps(OLD))
    del new["properties"]["elements"]["items"]["properties"]["name"]
    new["properties"]["elements"]["items"]["required"] = []
    out = schema_diff.diff(OLD, new)
    assert out["removed"] == ["elements[].name"] and out["alert"] == "schema_change"


def test_no_alert_is_logged_when_nothing_breaks(tmp_path):
    log = AlertLog(tmp_path / "alerts.jsonl")
    new = json.loads(json.dumps(OLD))
    new["properties"]["extra"] = {"type": "string"}
    schema_diff.diff(OLD, new, alerts=log)
    assert not (tmp_path / "alerts.jsonl").exists()


# ---------------------------------------------------------------------------- retirement


def pinned_copy(manifests, engine_id, **update):
    return {**manifests, engine_id: manifests[engine_id].model_copy(update={"pinned": True, **update})}


def test_a_pinned_id_that_disappears_alerts(manifests, tmp_path):
    ms = pinned_copy(manifests, "kling-o3-4k")
    log = AlertLog(tmp_path / "alerts.jsonl")
    cat = {"atlas": ["kwaivgi/kling-v3.0-pro/image-to-video", SEEDANCE_ATLAS]}  # o3 4k is gone
    alerts = retire.check(ms, cat, alerts=log)
    assert [(a.kind, a.engine_id, a.route_id, a.severity) for a in alerts] == [
        ("retired_pin", "kling-o3-4k", "atlas", "critical")
    ]
    assert KLING in alerts[0].message and alerts[0].data["model_id"] == KLING
    assert alerts[0].at.endswith("Z")
    assert [a.kind for a in log.read()] == ["retired_pin"]


def test_a_pinned_id_that_is_still_listed_is_quiet(manifests):
    ms = pinned_copy(manifests, "kling-o3-4k")
    assert retire.check(ms, {"atlas": [KLING]}) == []


def test_unpinned_engines_are_not_alerted(manifests):
    assert retire.check(manifests, {"atlas": []}) == []  # nothing is pinned


def test_project_pins_count_as_pinned(manifests):
    alerts = retire.check(manifests, {"atlas": []}, pins={"crop_pair_motion": Pin("kling-o3-4k")})
    assert [a.engine_id for a in alerts] == ["kling-o3-4k"]
    assert "project pin for crop_pair_motion" in alerts[0].message


def test_a_pinned_version_the_manifest_no_longer_serves_alerts(manifests):
    alerts = retire.check(
        manifests, {"atlas": [KLING]}, pins={"crop_pair_motion": Pin("kling-o3-4k", "2026-06-01")}
    )
    assert (
        len(alerts) == 1
        and "2026-06-01" in alerts[0].message
        and alerts[0].data["manifest_version"] == "unpinned"
    )
    gone = retire.check(manifests, {}, pins={"relight": Pin("deleted-engine")})
    assert len(gone) == 1 and "no manifest" in gone[0].message


def test_providers_without_a_catalogue_and_routes_without_a_model_id_cannot_be_judged(manifests):
    ms = {**manifests}
    for engine_id in ("nano-banana-2.1", "qwen-image-edit-2509"):
        ms[engine_id] = ms[engine_id].model_copy(update={"pinned": True})
    assert (
        retire.check(ms, {"atlas": []}) == []
    )  # gemini has no catalogue, nano has no model id, qwen is local


def test_each_route_of_a_pinned_engine_is_checked_and_unavailable_counts_as_gone(manifests, cat_dir):
    ms = pinned_copy(manifests, "seedance-2.5-r2v")
    write_catalogue(cat_dir, "atlas", [entry(SEEDANCE_ATLAS, "per_second", 0.1, available=False)])
    write_catalogue(cat_dir, "byteplus", [entry("dreamina-seedance-2-5-260628", "per_second", 0.1)])
    write_catalogue(cat_dir, "wavespeed", [entry("something-else", "per_second", 0.1)])
    alerts = retire.check(ms, prices.load_catalogues(cat_dir))
    assert sorted((a.route_id) for a in alerts) == ["atlas", "wavespeed"]


def test_retired_routes_are_not_checked(manifests):
    m = manifests["kling-o3-4k"]
    retired = m.model_copy(
        update={"pinned": True, "routes": [r.model_copy(update={"status": "retired"}) for r in m.routes]}
    )
    assert retire.check({**manifests, m.id: retired}, {"atlas": []}) == []
