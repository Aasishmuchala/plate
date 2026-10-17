"""Loader and model validation: duplicates, bad data and doctrine-in-data checks raise ManifestError."""

from __future__ import annotations

import copy

import pytest
import yaml

from plate_engines.loader import ManifestError, load_manifests

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
