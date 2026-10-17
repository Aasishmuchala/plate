"""Loader and model validation: duplicates, bad data and doctrine-in-data checks raise ManifestError."""

from __future__ import annotations

import copy
from datetime import date

import pytest
import yaml

from plate_engines.loader import ManifestError, load_manifests
from plate_engines.models import EngineManifest, EngineSize, Route, host_allowed

GOOD = {
    "id": "demo",
    "name": "Demo",
    "version": "unpinned",
    "pinned": False,
    "kind": "image_edit",
    "inputs": [{"name": "image", "type": "image", "required": True}],
    "outputs": [{"name": "image", "type": "image", "required": True}],
    "params": [{"name": "seed", "type": "int"}],
    "price": {"model": "flat", "usd": 0.0},
    "determinism": "best_effort",
    "licence": {"vendor_terms_url": "unknown", "training_opt_out": None},
    "allowed_tasks": ["relight"],
    "routes": [
        {
            "provider": "local",
            "endpoint": "local",
            "region": "local",
            "custody_grade": "C0",
            "custody_status": "confirmed",
            "terms_hash": "unknown",
            "upload_visibility": "local",
        }
    ],
}


def write(dir_, name, doc):
    (dir_ / name).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_good_manifest_loads(tmp_path):
    write(tmp_path, "demo.yaml", GOOD)
    assert set(load_manifests(tmp_path)) == {"demo"}


def test_duplicate_ids_raise(tmp_path):
    write(tmp_path, "demo.yaml", GOOD)
    write(tmp_path, "demo2.yaml", GOOD)
    with pytest.raises(ManifestError, match=r"demo2\.yaml: duplicate id 'demo' \(also in demo\.yaml\)"):
        load_manifests(tmp_path)


def test_file_name_must_match_id(tmp_path):
    write(tmp_path, "other.yaml", GOOD)
    with pytest.raises(ManifestError, match=r"other\.yaml: file name must be <id>\.yaml"):
        load_manifests(tmp_path)


def test_validation_error_names_the_file(tmp_path):
    bad = copy.deepcopy(GOOD)
    bad["kind"] = "text_to_image"
    write(tmp_path, "demo.yaml", bad)
    with pytest.raises(ManifestError, match=r"^demo\.yaml: kind"):
        load_manifests(tmp_path)


def test_unparsable_yaml_and_non_mapping(tmp_path):
    (tmp_path / "demo.yaml").write_text("a: [unclosed", encoding="utf-8", newline="\n")
    with pytest.raises(ManifestError, match=r"demo\.yaml: unreadable YAML"):
        load_manifests(tmp_path)
    (tmp_path / "demo.yaml").write_text("- just\n- a list\n", encoding="utf-8", newline="\n")
    with pytest.raises(ManifestError, match="top level must be a mapping"):
        load_manifests(tmp_path)


def mutate(**changes):
    doc = copy.deepcopy(GOOD)
    for dotted, value in changes.items():
        node = doc
        *path, last = dotted.split("__")
        for p in path:
            node = node[int(p)] if p.isdigit() else node[p]
        node[int(last) if last.isdigit() else last] = value
    return doc


@pytest.mark.parametrize(
    ("doc", "needle"),
    [
        (mutate(id="Bad_ID"), "id"),
        (mutate(allowed_tasks=["text_to_image"]), "forbidden task"),
        (mutate(allowed_tasks=["plate_upscale"]), "forbidden task"),
        (mutate(allowed_tasks=["make_coffee"]), "task vocabulary"),
        (mutate(routes=[]), "routes"),
        (mutate(unknown_key=1), "unknown_key"),
        (mutate(routes__0__terms_hash="not-a-hash"), "terms_hash"),
        (mutate(routes__0__endpoint="http://insecure.example"), "endpoint"),
        (mutate(licence__vendor_terms_url="ftp://x"), "vendor_terms_url"),
        (mutate(price__usd=-1), "usd"),
        (mutate(params=[{"name": "m", "type": "enum"}]), "enum"),
        (mutate(params=[{"name": "n", "type": "int", "range": [5, 1]}]), "range"),
        (
            mutate(params=[{"name": "n", "type": "int", "range": [0, 5], "default": 9}]),
            "default outside range",
        ),
        (mutate(params=[{"name": "s", "type": "int"}, {"name": "s", "type": "int"}]), "duplicate names"),
    ],
)
def test_invalid_documents_are_rejected(tmp_path, doc, needle):
    write(tmp_path, "demo.yaml", doc)
    with pytest.raises(ManifestError, match=needle):
        load_manifests(tmp_path)


ROUTE = GOOD["routes"][0]


@pytest.mark.parametrize(
    ("route", "needle"),
    [
        (
            {**ROUTE, "provider": "x", "custody_grade": "C1", "upload_visibility": "public"},
            "public URLs must be custody grade D",
        ),
        (
            {**ROUTE, "provider": "x", "custody_grade": "D", "upload_visibility": "local"},
            "C0 and upload_visibility 'local'",
        ),
        (
            {**ROUTE, "provider": "x", "custody_grade": "C1", "upload_visibility": "unknown"},
            "require upload_visibility 'private'",
        ),
        (
            {
                **ROUTE,
                "provider": "x",
                "custody_grade": "C1",
                "upload_visibility": "private",
                "custody_status": "confirmed",
            },
            "without a terms_hash",
        ),
        ({**ROUTE, "same_model_fallback_of": "local"}, "own provider"),
        ({**ROUTE, "provider": "x", "same_model_fallback_of": "nobody"}, "names no route"),
    ],
)
def test_route_doctrine_checks(tmp_path, route, needle):
    doc = copy.deepcopy(GOOD)
    doc["routes"] = [route] if route["provider"] != "x" else [route, ROUTE]
    write(tmp_path, "demo.yaml", doc)
    with pytest.raises(ManifestError, match=needle):
        load_manifests(tmp_path)


def test_c1_confirmed_needs_a_terms_hash_and_accepts_one(tmp_path):
    doc = copy.deepcopy(GOOD)
    doc["routes"] = [
        {
            **ROUTE,
            "provider": "vendor",
            "endpoint": "https://api.vendor.example",
            "custody_grade": "C1",
            "custody_status": "confirmed",
            "terms_hash": "a" * 64,
            "upload_visibility": "private",
        }
    ]
    write(tmp_path, "demo.yaml", doc)
    assert load_manifests(tmp_path)["demo"].routes[0].custody_grade == "C1"


# ---- T7: new fields, all optional ------------------------------------------------------------------------


def cloud_route(**extra):
    return {
        "provider": "vendor",
        "endpoint": "https://api.vendor.example/v1",
        "region": "unknown",
        "custody_grade": "D",
        "custody_status": "pending_written_terms",
        "terms_hash": "unknown",
        "upload_visibility": "unknown",
        **extra,
    }


def doc_with(**changes):
    doc = copy.deepcopy(GOOD)
    doc.update(changes)
    return doc


def loads(tmp_path, doc, name="demo.yaml"):
    write(tmp_path, name, doc)
    return load_manifests(tmp_path)["demo"]


def test_an_old_style_manifest_gets_every_default(tmp_path):
    m = loads(tmp_path, GOOD)
    r = m.routes[0]
    assert (r.route_id, r.model_slug, r.allowed_hosts, r.max_inflight, r.status) == (
        "local",
        None,
        [],
        2,
        "active",
    )
    assert (m.model_id, m.sizes, m.plate_upscale_forbidden, m.flags) == (None, None, False, [])
    assert (m.price.by, m.price.source, m.price.as_of) == ({}, "", None)
    assert m.params[0].advanced is False and m.inputs[0].max_items is None


def test_every_new_field_round_trips(tmp_path):
    doc = doc_with(
        model_id="vendor/model-1",
        flags=["unverified_p0"],
        kind="video_startend",
        price={
            "model": "per_second",
            "usd": 0.357,
            "by": {"base": 0.357, "atlas_discounted": 0.0536},
            "source": "dossier",
            "as_of": "2026-10-17",
            "status": "unconfirmed",
        },
        inputs=[{"name": "frames", "type": "images", "max_items": 30}],
        params=[{"name": "seed", "type": "int", "advanced": True}],
        sizes={"legal": [{"w": 1024, "h": 1024, "label": "1K"}], "auto": True, "aspects": ["16:9"]},
        routes=[
            cloud_route(
                route_id="vendor-eu",
                model_slug="vendor/slug",
                allowed_hosts=["api.vendor.example", ".cdn.example"],
                max_inflight=4,
                status="retired",
            )
        ],
    )
    m = loads(tmp_path, doc)
    assert (m.model_id, m.flags, m.kind) == ("vendor/model-1", ["unverified_p0"], "video_startend")
    assert m.price.by["atlas_discounted"] == 0.0536 and m.price.as_of == date(2026, 10, 17)
    assert m.inputs[0].max_items == 30 and m.params[0].advanced is True
    assert m.sizes.legal[0].label == "1K" and m.sizes.auto and m.sizes.aspects == ["16:9"]
    r = m.routes[0]
    assert (r.route_id, r.model_slug, r.max_inflight, r.status) == ("vendor-eu", "vendor/slug", 4, "retired")
    assert r.allowed_hosts == ["api.vendor.example", ".cdn.example"]


@pytest.mark.parametrize("kind", ["video_startend", "local_image", "local_video", "video_i2v", "image_gen"])
def test_new_and_old_kinds_load(tmp_path, kind):
    assert loads(tmp_path, doc_with(kind=kind)).kind == kind


@pytest.mark.parametrize(
    ("doc", "needle"),
    [
        # routes
        (doc_with(routes=[cloud_route(route_id="Bad Id")]), "route_id"),
        (doc_with(routes=[cloud_route(allowed_hosts=["https://api.vendor.example"])]), "allowed_hosts entry"),
        (doc_with(routes=[cloud_route(allowed_hosts=["API.vendor.example"])]), "allowed_hosts entry"),
        (doc_with(routes=[cloud_route(allowed_hosts=["api.vendor.example:443"])]), "allowed_hosts entry"),
        (
            doc_with(routes=[cloud_route(allowed_hosts=["api.vendor.example", "api.vendor.example"])]),
            "duplicates",
        ),
        (doc_with(routes=[cloud_route(allowed_hosts=["other.example"])]), "not covered by allowed_hosts"),
        (
            doc_with(routes=[{**GOOD["routes"][0], "allowed_hosts": ["x.example"]}]),
            "local route lists no allowed_hosts",
        ),
        (doc_with(routes=[cloud_route(max_inflight=0)]), "max_inflight"),
        (doc_with(routes=[cloud_route(status="paused")]), "status"),
        (
            doc_with(routes=[cloud_route(route_id="same"), cloud_route(provider="other", route_id="same")]),
            "duplicate names in route ids",
        ),
        # price
        (doc_with(price={"model": "flat", "usd": 1, "by": {"1K": -0.1}}), "usd >= 0"),
        (doc_with(price={"model": "flat", "usd": 1, "by": {"": 0.1}}), "key must be non-empty"),
        (doc_with(price={"model": "flat", "usd": 1, "as_of": "yesterday"}), "as_of"),
        # inputs and params
        (doc_with(inputs=[{"name": "p", "type": "text", "max_items": 3}]), "max_items only applies"),
        (doc_with(inputs=[{"name": "p", "type": "images", "max_items": 0}]), "max_items"),
        (doc_with(params=[{"name": "s", "type": "int", "advanced": "maybe"}]), "advanced"),
        # manifest level
        (doc_with(flags=["Unverified P0"]), "lower_snake_case"),
        (doc_with(flags=["a_flag", "a_flag"]), "duplicate flags"),
        (doc_with(kind="upscale", allowed_tasks=["upscale_ai_layers"]), "plate_upscale_forbidden"),
        (doc_with(model_id=5), "model_id"),
        (doc_with(plate_upscale_forbidden="sure"), "plate_upscale_forbidden"),
    ],
)
def test_new_field_validation(tmp_path, doc, needle):
    write(tmp_path, "demo.yaml", doc)
    with pytest.raises(ManifestError, match=needle):
        load_manifests(tmp_path)


def test_an_upscaler_that_forbids_plate_upscaling_loads(tmp_path):
    m = loads(
        tmp_path, doc_with(kind="upscale", allowed_tasks=["upscale_ai_layers"], plate_upscale_forbidden=True)
    )
    assert m.plate_upscale_forbidden is True


@pytest.mark.parametrize(
    ("sizes", "needle"),
    [
        ({}, "at least one of legal, arbitrary or auto"),
        ({"aspects": ["16:9"]}, "at least one of legal, arbitrary or auto"),
        ({"legal": [{"w": 0, "h": 10}]}, "w"),
        ({"legal": [{"w": 10, "h": 10}, {"w": 10, "h": 10}]}, "sizes.legal has duplicates"),
        ({"auto": True, "aspects": ["wide"]}, "must look like"),
        ({"auto": True, "aspects": ["0:9"]}, "must look like"),
        (
            {"arbitrary": {"min_side": 600, "max_side": 500, "aspect_min": 0.5, "aspect_max": 2}},
            "min_side <= max_side",
        ),
        (
            {"arbitrary": {"min_side": 5, "max_side": 500, "aspect_min": 3, "aspect_max": 2}},
            "aspect_min <= aspect_max",
        ),
        ({"arbitrary": {"min_side": 5, "max_side": 500, "aspect_min": 1}}, "aspect_max"),
        ({"auto": True, "mystery": 1}, "mystery"),
    ],
)
def test_sizes_validation(tmp_path, sizes, needle):
    write(tmp_path, "demo.yaml", doc_with(sizes=sizes))
    with pytest.raises(ManifestError, match=needle):
        load_manifests(tmp_path)


def test_sizes_accept_legal_arbitrary_and_auto_together(tmp_path):
    sizes = {
        "legal": [{"w": 1024, "h": 1024}, {"w": 1536, "h": 1024}],
        "arbitrary": {"min_side": 1024, "max_side": 1536, "aspect_min": 0.6667, "aspect_max": 1.5},
        "auto": True,
    }
    m = loads(tmp_path, doc_with(sizes=sizes))
    assert len(m.sizes.legal) == 2 and m.sizes.arbitrary.max_side == 1536 and m.sizes.auto


def test_route_id_defaults_to_the_provider_when_built_directly():
    r = Route(**cloud_route())
    assert r.route_id == "vendor"
    assert Route(**cloud_route(route_id="mine")).route_id == "mine"
    assert Route(**cloud_route(route_id="")).route_id == "vendor"


def test_models_stay_frozen_and_extra_forbidding(tmp_path):
    m = loads(tmp_path, GOOD)
    with pytest.raises((TypeError, ValueError)):
        m.flags = ["x"]
    with pytest.raises(ValueError):
        EngineManifest.model_validate({**GOOD, "typo_field": 1})
    with pytest.raises(ValueError):
        Route(**cloud_route(typo=1))


def test_engine_size_is_a_strict_record():
    assert EngineSize(w=0, h=0, label="auto").label == "auto"
    assert EngineSize(w=1536, h=1024).label == ""
    with pytest.raises(ValueError):
        EngineSize(w=-1, h=10)
    with pytest.raises(ValueError):
        EngineSize(w=1, h=1, extra=2)


@pytest.mark.parametrize(
    ("host", "allowed", "expected"),
    [
        ("api.openai.com", ["api.openai.com"], True),
        ("API.OpenAI.com", ["api.openai.com"], True),
        ("api.openai.com.", ["api.openai.com"], True),  # trailing dot of an absolute name
        ("x.api.openai.com", ["api.openai.com"], False),  # exact entries are exact
        ("a.b.aliyuncs.com", [".aliyuncs.com"], True),
        ("aliyuncs.com", [".aliyuncs.com"], False),
        ("evilaliyuncs.com", [".aliyuncs.com"], False),
        ("aliyuncs.com.evil.example", [".aliyuncs.com"], False),
        ("anything", [], False),
        ("", ["x"], False),
    ],
)
def test_host_allowed(host, allowed, expected):
    assert host_allowed(host, allowed) is expected
