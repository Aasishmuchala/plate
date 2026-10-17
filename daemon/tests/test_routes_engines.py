"""`GET /api/v0/engines` and `GET /api/v0/recipes` (router plan T61)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plate_daemon import recipes
from plate_daemon.api import create_app
from plate_daemon.engine_fake import FakeEngine
from plate_engines.loader import load_manifests
from plate_engines.models import Route

BASE = "http://127.0.0.1:4610"
ENGINE_KEYS = {
    "id",
    "name",
    "kind",
    "allowed_tasks",
    "model_id",
    "version",
    "routes",
    "params",
    "presets",
    "price",
    "sizes",
    "flags",
}
ROUTE_KEYS = {
    "route_id",
    "provider",
    "region",
    "custody_grade",
    "custody_status",
    "effective_grade",
    "status",
    "upload_visibility",
}
LOCAL_ENGINES = {"qwen-image-edit-2509", "real-esrgan", "wan-2.2-vace", "fake"}


@pytest.fixture
def manifests():
    return load_manifests()


def engines(c: TestClient, auth: dict, **params) -> dict[str, dict]:
    r = c.get("/api/v0/engines", params=params, headers=auth)
    assert r.status_code == 200, r.text
    return {e["id"]: e for e in r.json()}


def make_project(c: TestClient, auth: dict, project_id: str, **custody) -> None:
    r = c.post("/api/v0/projects", json={"project_id": project_id, "custody": custody}, headers=auth)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------- GET /engines


def test_engines_lists_the_14_manifests_and_the_fake_engine(client, auth, manifests):
    assert len(manifests) == 14
    got = engines(client, auth)
    assert set(got) == set(manifests) | {"fake"} and len(got) == 15
    ids = list(got)
    assert ids[:-1] == sorted(manifests) and ids[-1] == "fake"  # sorted by id, the fake one last


def test_every_entry_has_the_fixed_shape(client, auth):
    for e in engines(client, auth).values():
        assert ENGINE_KEYS <= set(e), e["id"]
        # The fake engine's route is spelled out in the plan without a region (it never leaves the box).
        keys = ROUTE_KEYS - {"region"} if e["id"] == "fake" else ROUTE_KEYS
        assert e["routes"] and all(set(r) == keys for r in e["routes"]), e["id"]
        assert e["presets"][0] == "default"
        assert {"usd", "model", "status"} <= set(e["price"])
        for p in e["params"]:
            assert {"name", "type", "advanced"} <= set(p)
            assert set(p) <= {"name", "type", "enum", "range", "default", "advanced"}
        assert "allowed" not in e  # only with ?project_id=


def test_the_fake_engine_entry_is_the_daemons_own(client, auth):
    fake = engines(client, auth)["fake"]
    assert fake["name"] == "Fake (echo)" and fake["kind"] == "image_edit"
    assert fake["allowed_tasks"] == ["relight", "people", "vegetation", "sky", "rephotograph_close"]
    assert fake["routes"] == [
        {
            "route_id": "local",
            "provider": "local",
            "custody_grade": "C0",
            "custody_status": "confirmed",
            "effective_grade": "C0",
            "status": "active",
            "upload_visibility": "local",
        }
    ]
    assert fake["params"] == [] and fake["presets"] == ["default"]
    assert fake["price"] == {"usd": 0, "model": "flat", "status": "confirmed"}


def test_no_fake_engine_when_the_app_engine_is_not_a_stub(settings, auth, manifests):
    class Real(FakeEngine):
        stub = False

    with TestClient(create_app(settings, Real()), base_url=BASE) as c:
        got = engines(c, auth)
        assert set(got) == set(manifests) and "fake" not in got
        assert c.get("/api/v0/health").json()["stub_engine"] is False


def test_the_pending_route_is_graded_d(client, auth, manifests):
    nano = engines(client, auth)["nano-banana-2.1"]
    assert len(nano["routes"]) == 2
    gemini, atlas = nano["routes"]
    assert gemini["provider"] == "gemini" and gemini["custody_status"] == "pending_written_terms"
    assert gemini["custody_grade"] == "C1" and gemini["effective_grade"] == "D"  # pending counts as D
    assert atlas["provider"] == "atlas" and atlas["upload_visibility"] == "public"
    assert atlas["custody_grade"] == "D" and atlas["effective_grade"] == "D"
    assert nano["model_id"] == manifests["nano-banana-2.1"].model_id
    assert nano["version"] == manifests["nano-banana-2.1"].version


def test_effective_grade_is_what_the_router_acts_on(client, auth, manifests):
    from plate_daemon.custody import effective_grade

    for e in engines(client, auth).values():
        if e["id"] == "fake":
            continue
        for shown, route in zip(e["routes"], manifests[e["id"]].routes, strict=True):
            assert shown["effective_grade"] == effective_grade(route).value
            assert shown["route_id"] == route.route_id and shown["status"] == route.status


def test_params_sizes_price_and_flags_come_from_the_manifest(client, auth, manifests):
    got = engines(client, auth)
    flux = got["flux-3-image"]
    assert "unverified_p0" in flux["flags"]
    by_name = {p["name"]: p for p in flux["params"]}
    assert by_name["output_format"] == {
        "name": "output_format",
        "type": "enum",
        "enum": ["jpeg", "png"],
        "default": "png",
        "advanced": False,
    }
    assert by_name["safety_tolerance"]["range"] == [0, 6] and by_name["safety_tolerance"]["advanced"] is True
    assert by_name["prompt_upsampling"]["default"] is False  # a false default is kept, not dropped
    assert "default" not in by_name["seed"] and by_name["seed"]["range"] == [0, 4294967295]
    assert flux["price"] == {"usd": 0.12, "model": "per_image", "status": "unconfirmed", "by": {}}

    nano = got["nano-banana-2.1"]
    assert nano["sizes"]["auto"] is True and "16:9" in nano["sizes"]["aspects"]
    assert [s["label"] for s in nano["sizes"]["legal"]] == ["1K", "2K", "4K"]
    assert nano["price"]["by"] == manifests["nano-banana-2.1"].price.by

    assert got["qwen-image-edit-2509"]["sizes"] is None  # a local model with no declared sizes
    assert got["kling-o3-4k"]["kind"] == "video_startend"


def test_flux_3_declares_its_sizes(client, auth):
    sizes = engines(client, auth)["flux-3-image"]["sizes"]
    assert sizes == {
        "legal": [{"w": 1024, "h": 1024, "label": "1MP"}, {"w": 1440, "h": 1440, "label": ""}],
        "auto": False,
        "aspects": [],
    }


def test_presets_are_default_plus_the_recipes_naming_the_engine(client, auth):
    got = engines(client, auth)
    assert got["nano-banana-2.1"]["presets"] == ["default", "night_relight_changelist"]
    assert got["gpt-image-2.5"]["presets"] == ["default", "sunburst_rephotograph_close"]
    assert got["kling-o3-4k"]["presets"] == ["default", "croppair_pushin"]
    assert got["flux-3-image"]["presets"] == ["default"]
    named = {recipes.load_runnable(r).engine for r in recipes.list_runnable()}
    assert named <= set(got)  # every recipe points at an engine the catalogue lists


def test_project_id_adds_allowed_by_effective_grade(client, auth):
    make_project(client, auth, "nda-job", nda=True)
    got = engines(client, auth, project_id="nda-job")
    for e in got.values():
        assert isinstance(e["allowed"], bool)
    # NDA work stays on C0/C1: only the local engines (and the daemon's fake) qualify today, because every
    # cloud route is D, or pending written terms and so D.
    assert {i for i, e in got.items() if e["allowed"]} == LOCAL_ENGINES
    for gemini_only in ("nano-banana-pro", "nano-banana-2.1", "gpt-image-2.5"):
        assert got[gemini_only]["allowed"] is False

    make_project(client, auth, "open-job", nda=False)
    assert all(e["allowed"] for e in engines(client, auth, project_id="open-job").values())

    make_project(client, auth, "c1-job", nda=False, allowed_grades=["C0", "C1"])
    assert {i for i, e in engines(client, auth, project_id="c1-job").items() if e["allowed"]} == LOCAL_ENGINES


def test_a_retired_route_does_not_make_an_engine_allowed(settings, auth, manifests):
    local = Route(
        provider="local",
        endpoint="local",
        region="local",
        custody_grade="C0",
        custody_status="confirmed",
        terms_hash="unknown",
        upload_visibility="local",
        status="retired",
    )
    pinned = {"nano-banana-pro": manifests["nano-banana-pro"].model_copy(update={"routes": [local]})}
    with TestClient(create_app(settings, manifests=pinned), base_url=BASE) as c:
        make_project(c, auth, "nda-job", nda=True)
        got = engines(c, auth, project_id="nda-job")
        assert got["nano-banana-pro"]["routes"][0]["status"] == "retired"
        assert got["nano-banana-pro"]["allowed"] is False  # nothing active can run it
        assert got["fake"]["allowed"] is True


def test_an_unknown_or_malformed_project_id(client, auth):
    assert client.get("/api/v0/engines", params={"project_id": "nope"}, headers=auth).status_code == 404
    assert client.get("/api/v0/engines", params={"project_id": "../x"}, headers=auth).status_code == 422


def test_engines_and_recipes_need_the_token(client):
    for path in ("/api/v0/engines", "/api/v0/recipes"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


# ---------------------------------------------------------------------------- GET /recipes


def test_recipes_lists_the_runnable_ones(client, auth, manifests):
    r = client.get("/api/v0/recipes", headers=auth)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 3 and [x["id"] for x in rows] == recipes.list_runnable()
    by_id = {x["id"]: x for x in rows}
    assert "night_relight_changelist" in by_id
    for row in rows:
        assert set(row) == {"id", "title", "kind", "engine", "shot_class", "library", "mask", "params"}
        assert (
            row["engine"] in manifests and isinstance(row["mask"], dict) and isinstance(row["params"], dict)
        )
    night = by_id["night_relight_changelist"]
    assert night["kind"] == "still_edit" and night["engine"] == "nano-banana-2.1"
    assert night["shot_class"] == "wide" and night["library"] == "a01"
    assert night["mask"]["editable_classes"] == ["sky", "vegetation", "people"]
    assert night["mask"]["lock_rest"] is True and night["params"]["size"] == "native"
    assert by_id["croppair_pushin"]["kind"] == "motion_croppair"


def test_a_recipe_that_does_not_load_is_left_out(client, auth, monkeypatch):
    real = recipes.load_runnable

    def flaky(name):
        if name == "croppair_pushin":
            raise recipes.RecipeError("broken on purpose")
        return real(name)

    monkeypatch.setattr(recipes, "load_runnable", flaky)
    ids = [x["id"] for x in client.get("/api/v0/recipes", headers=auth).json()]
    assert "croppair_pushin" not in ids and "night_relight_changelist" in ids
    assert "croppair_pushin" not in engines(client, auth)["kling-o3-4k"]["presets"]
