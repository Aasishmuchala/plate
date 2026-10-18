"""Engine capabilities and route health, engine cards, recipe folders and the save/assemble routes (router plan T83)."""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest
import yaml
from fastapi.testclient import TestClient

from plate_daemon import recipes
from plate_daemon.ledger import Ledger
from plate_engines.health import AlertLog
from plate_engines.loader import load_manifests

API = "/api/v0"
HEALTH = {"ok", "degraded", "down", "suspended"}
CAPABILITY_KEYS = {
    "largest_size",
    "image_tail",
    "reference_images_max",
    "elements",
    "price_unit",
    "runnable",
    "unverified",
}


def make_project(c: TestClient, auth: dict, project_id: str = "p1", **custody) -> None:
    custody.setdefault("nda", False)
    r = c.post(f"{API}/projects", json={"project_id": project_id, "custody": custody}, headers=auth)
    assert r.status_code == 201, r.text


def engines(c: TestClient, auth: dict, **params) -> dict[str, dict]:
    r = c.get(f"{API}/engines", params=params, headers=auth)
    assert r.status_code == 200, r.text
    return {e["id"]: e for e in r.json()}


def project_dir(c: TestClient, project_id: str):
    return c.app.state.ctx.pipeline.project_dir(project_id)


def recipe_doc(rid: str = "my_recipe", **patch) -> dict:
    doc = {
        "schema": 1,
        "id": rid,
        "title": "My recipe",
        "kind": "still_edit",
        "engine": "nano-banana-2.1",
        "group": "mine",
        "mask": {"editable_classes": ["sky"], "region": "none", "lock_rest": True},
        "prompt": "Relight to [TARGET HOUR].",
        "negative_prompt": "new objects",
        "overrides": {"[TARGET HOUR]": "dawn"},
    }
    doc.update(patch)
    return doc


# ---------------------------------------------------------------------------- GET /engines: capabilities and health


def test_every_engine_has_capabilities_and_a_health_per_route(client, auth):
    got = engines(client, auth)
    assert len(got) == 15
    for e in got.values():
        assert set(e["capabilities"]) == CAPABILITY_KEYS, e["id"]
        assert all(r["health"] in HEALTH for r in e["routes"]), e["id"]
    for e in got.values():  # no alert logs and no canary: every active route is assumed ok
        assert {r["health"] for r in e["routes"] if r["status"] == "active"} == {"ok"}, e["id"]


def test_capabilities_come_from_the_manifest(client, auth):
    got = engines(client, auth)
    manifests = load_manifests()
    nano = got["nano-banana-2.1"]["capabilities"]
    assert nano["reference_images_max"] == 13 and nano["image_tail"] is False and nano["elements"] is False
    assert nano["largest_size"] == "4096x4096" and nano["price_unit"] == "per_image"
    assert (
        nano["unverified"] is True and nano["runnable"] is False
    )  # NB 2.1 has no vendor model id until the P0 smoke test
    kling = got["kling-o3-4k"]["capabilities"]
    assert kling["image_tail"] is True and kling["elements"] is True and kling["price_unit"] == "per_second"
    assert kling["runnable"] is True and kling["reference_images_max"] == 0
    assert got["veo-3.1"]["capabilities"]["image_tail"] is True  # end_image counts
    assert got["seedream-5-pro"]["capabilities"]["runnable"] is False
    assert got["nano-banana-pro"]["capabilities"]["runnable"] is True
    assert got["gpt-image-2.5"]["capabilities"]["reference_images_max"] == 15
    assert got["qwen-image-edit-2509"]["capabilities"]["largest_size"] is None  # no declared sizes
    assert got["seedance-2.5-r2v"]["capabilities"]["reference_images_max"] == 30
    for eid, m in manifests.items():
        cap = got[eid]["capabilities"]
        assert cap["runnable"] is bool(m.model_id or any(r.model_slug for r in m.routes)), eid
        assert cap["unverified"] is ("unverified_p0" in m.flags), eid
        assert cap["price_unit"] == m.price.model, eid
        if m.sizes and m.sizes.legal:
            biggest = max(m.sizes.legal, key=lambda s: s.w * s.h)
            assert cap["largest_size"] == f"{biggest.w}x{biggest.h}", eid


def test_the_arbitrary_envelope_gives_the_largest_box_when_nothing_is_legal(client, auth, settings):
    from plate_daemon.api import create_app

    base = load_manifests()["seedance-2.5-i2v"]
    assert base.sizes is not None and base.sizes.arbitrary is not None and not base.sizes.legal
    with TestClient(create_app(settings, manifests={base.id: base}), base_url="http://127.0.0.1:4610") as c:
        cap = engines(c, auth)[base.id]["capabilities"]
    a = base.sizes.arbitrary
    assert cap["largest_size"] == f"{a.max_side}x{a.max_side}"


def test_the_fake_engine_has_capabilities_and_a_healthy_route(client, auth):
    fake = engines(client, auth)["fake"]
    assert set(fake["capabilities"]) == CAPABILITY_KEYS and fake["capabilities"]["runnable"] is True
    assert fake["routes"][0]["health"] == "ok"


def test_a_suspended_engine_reads_suspended_on_every_route(client, auth, settings):
    assert {r["health"] for r in engines(client, auth)["nano-banana-2.1"]["routes"]} == {"ok"}
    AlertLog(settings.home / "alerts.jsonl").write(
        "engine_suspended",
        "canary drift: 3 of 4 cases moved",
        severity="critical",
        engine_id="nano-banana-2.1",
    )
    got = engines(client, auth)
    assert [r["health"] for r in got["nano-banana-2.1"]["routes"]] == ["suspended", "suspended"]
    assert {r["health"] for r in got["gpt-image-2.5"]["routes"]} == {"ok"}
    AlertLog(settings.home / "alerts.jsonl").resume("nano-banana-2.1")
    assert {r["health"] for r in engines(client, auth)["nano-banana-2.1"]["routes"]} == {"ok"}


# ---------------------------------------------------------------------------- GET /engines/cards


def scored_run(
    run_id: str, verdict: str, *, engine="nano-banana-2.1", task="relight", shot="wide", by_path=False
):
    ref = f"gates/{run_id}.1.json" if by_path else {"verdict": verdict}
    data = {"task": task, "shot_class": shot}
    return [
        {
            "kind": "quoted",
            "run_id": run_id,
            "engine_id": engine,
            "custody_grade": "D",
            "cost_usd_quoted": 0.04,
            "data": data,
        },
        {"kind": "approved", "run_id": run_id, "cost_usd_quoted": 0.04},
        {"kind": "gated", "run_id": run_id, "gate_report": ref, "data": {"verdict": verdict}},
        {"kind": "done", "run_id": run_id, "gate_report": ref, "data": {"verdict": verdict}},
    ]


def fill_ledger(c: TestClient, project_id: str, runs: list[list[dict]]) -> None:
    ledger = Ledger(project_dir(c, project_id))
    for records in runs:
        for rec in records:
            ledger.append(rec)


def test_cards_without_a_project_are_empty_and_an_unknown_project_is_404(client, auth):
    assert client.get(f"{API}/engines/cards", headers=auth).json() == []
    assert client.get(f"{API}/engines/cards", params={"task": "relight"}, headers=auth).json() == []
    assert client.get(f"{API}/engines/cards", params={"project_id": "nope"}, headers=auth).status_code == 404
    assert client.get(f"{API}/engines/cards", params={"project_id": "../x"}, headers=auth).status_code == 422


def test_a_project_without_runs_has_no_cards(client, auth):
    make_project(client, auth)
    assert client.get(f"{API}/engines/cards", params={"project_id": "p1"}, headers=auth).json() == []


def test_three_scored_runs_make_one_card_with_n_3(client, auth):
    make_project(client, auth)
    fill_ledger(client, "p1", [scored_run("r1", "pass"), scored_run("r2", "pass"), scored_run("r3", "fail")])
    r = client.get(f"{API}/engines/cards", params={"project_id": "p1"}, headers=auth)
    assert r.status_code == 200, r.text
    (card,) = r.json()
    assert (card["engine_id"], card["task"], card["shot_class"]) == ("nano-banana-2.1", "relight", "wide")
    assert (card["n"], card["passes"]) == (3, 2) and card["steering"] is False
    assert card["pass_rate"] == pytest.approx(2 / 3) and 0 < card["wilson_lower_95"] < card["pass_rate"]
    assert card["mean_cost"] == pytest.approx(0.04) and card["custody"] == "D"


def test_cards_count_the_verdict_a_ledger_record_carries_beside_a_report_path(client, auth):
    """The pipeline names a gate report by its path and puts the verdict in data.verdict."""
    make_project(client, auth)
    fill_ledger(client, "p1", [scored_run(f"r{i}", "pass", by_path=True) for i in range(3)])
    (card,) = client.get(f"{API}/engines/cards", params={"project_id": "p1"}, headers=auth).json()
    assert (card["n"], card["passes"]) == (3, 3)


def test_cards_filter_by_task_and_shot_class_and_report_steering(client, auth):
    make_project(client, auth)
    fill_ledger(
        client,
        "p1",
        [
            *[scored_run(f"w{i}", "pass") for i in range(20)],
            scored_run("c1", "pass", shot="close", task="rephotograph_close", engine="gpt-image-2.5"),
            scored_run("v1", "fail", task="vegetation"),
        ],
    )
    cards = client.get(f"{API}/engines/cards", params={"project_id": "p1"}, headers=auth).json()
    assert {(c["engine_id"], c["task"], c["shot_class"]): c["steering"] for c in cards} == {
        ("gpt-image-2.5", "rephotograph_close", "close"): False,
        ("nano-banana-2.1", "relight", "wide"): True,  # 20 scored runs: the router ranks on it
        ("nano-banana-2.1", "vegetation", "wide"): False,
    }
    by_task = client.get(
        f"{API}/engines/cards", params={"project_id": "p1", "task": "relight"}, headers=auth
    ).json()
    assert [c["task"] for c in by_task] == ["relight"]
    by_shot = client.get(
        f"{API}/engines/cards", params={"project_id": "p1", "shot_class": "close"}, headers=auth
    ).json()
    assert [c["engine_id"] for c in by_shot] == ["gpt-image-2.5"]
    both = {"project_id": "p1", "task": "relight", "shot_class": "close"}
    assert client.get(f"{API}/engines/cards", params=both, headers=auth).json() == []


# ---------------------------------------------------------------------------- GET /recipes


def test_recipes_list_carries_origin_group_task_and_tokens(client, auth):
    rows = client.get(f"{API}/recipes", headers=auth).json()
    assert len(rows) >= 14 and [r["id"] for r in rows] == recipes.list_runnable()
    by_id = {r["id"]: r for r in rows}
    swap = by_id["season_swap"]
    assert (swap["origin"], swap["group"], swap["task"], swap["library"]) == (
        "shipped",
        "season",
        "vegetation",
        "a05",
    )
    assert swap["shot_class"] == "wide" and swap["thumbnail"] is None
    assert swap["tokens"] == [] and swap["overrides"] == {
        "[SEASON]": "autumn"
    }  # a default for every token: none open
    assert by_id["imperfections_close"]["shot_class"] == "close"
    assert by_id["croppair_pushin"]["group"] is None and by_id["croppair_pushin"]["task"] is None
    assert all(r["origin"] == "shipped" for r in rows)


def test_recipes_kind_filter(client, auth):
    rows = client.get(f"{API}/recipes", params={"kind": "still_rephotograph"}, headers=auth).json()
    assert {r["id"] for r in rows} == {"sunburst_rephotograph_close", "imperfections_close"}
    motion = [
        r["id"] for r in client.get(f"{API}/recipes", params={"kind": "motion_croppair"}, headers=auth).json()
    ]
    assert "croppair_pushin" in motion and "motion_time_of_day" in motion  # T94 ships the motion recipes
    shipped = [r for r in recipes.list_runnable() if recipes.load_runnable(r).kind == "motion_croppair"]
    assert motion == shipped
    assert client.get(f"{API}/recipes", params={"kind": "banner"}, headers=auth).status_code == 422


def test_a_project_recipe_lists_with_its_project_only(client, auth):
    make_project(client, auth)
    r = client.post(
        f"{API}/recipes",
        json={"scope": "project", "project_id": "p1", "recipe": recipe_doc("only_p1")},
        headers=auth,
    )
    assert r.status_code == 201, r.text
    assert "only_p1" not in {x["id"] for x in client.get(f"{API}/recipes", headers=auth).json()}
    rows = client.get(f"{API}/recipes", params={"project_id": "p1"}, headers=auth).json()
    mine = next(x for x in rows if x["id"] == "only_p1")
    assert (mine["origin"], mine["group"]) == ("project", "mine")
    assert [x["id"] for x in rows if x["id"] != "only_p1"] == recipes.list_runnable()  # shadows nothing
    assert "only_p1" in engines(client, auth, project_id="p1")["nano-banana-2.1"]["presets"]
    assert "only_p1" not in engines(client, auth)["nano-banana-2.1"]["presets"]
    assert client.get(f"{API}/recipes", params={"project_id": "nope"}, headers=auth).status_code == 404


def test_a_recipe_with_an_open_token_lists_it(client, auth):
    doc = recipe_doc("open_token", prompt="Make it [MOOD] and [TARGET HOUR].")
    assert (
        client.post(f"{API}/recipes", json={"scope": "studio", "recipe": doc}, headers=auth).status_code
        == 201
    )
    row = next(r for r in client.get(f"{API}/recipes", headers=auth).json() if r["id"] == "open_token")
    assert row["tokens"] == ["[MOOD]"] and row["origin"] == "studio"


# ---------------------------------------------------------------------------- POST /recipes


def test_saving_a_studio_recipe_writes_the_file_and_it_reads_back(client, auth, plate_env):
    doc = recipe_doc("studio_one", task="relight", shot_class="medium")
    r = client.post(f"{API}/recipes", json={"scope": "studio", "recipe": doc}, headers=auth)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "studio_one" and body["origin"] == "studio"
    path = plate_env / "recipes" / "studio_one.yaml"
    assert body["path"] == str(path) and path.is_file()
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == doc
    got = recipes.load_runnable("studio_one")
    assert (got.origin, got.shot_class, got.task, got.group) == ("studio", "medium", "relight", "mine")
    assert any(x["id"] == "studio_one" for x in client.get(f"{API}/recipes", headers=auth).json())


def test_saving_the_same_id_twice_is_409_unless_overwrite(client, auth):
    body = {"scope": "studio", "recipe": recipe_doc("twice")}
    assert client.post(f"{API}/recipes", json=body, headers=auth).status_code == 201
    again = client.post(f"{API}/recipes", json=body, headers=auth)
    assert again.status_code == 409 and again.json()["error"] == "recipe_exists"
    changed = {**body, "overwrite": True, "recipe": recipe_doc("twice", title="Second")}
    assert client.post(f"{API}/recipes", json=changed, headers=auth).status_code == 201
    assert recipes.load_runnable("twice").title == "Second"


def test_a_shipped_id_is_409_without_overwrite_and_shadowed_with_it(client, auth, plate_env):
    body = {"scope": "studio", "recipe": recipe_doc("night_exterior", title="Mine")}
    r = client.post(f"{API}/recipes", json=body, headers=auth)
    assert (
        r.status_code == 409 and r.json()["error"] == "recipe_exists" and not (plate_env / "recipes").exists()
    )
    assert client.post(f"{API}/recipes", json={**body, "overwrite": True}, headers=auth).status_code == 201
    rows = {r["id"]: r for r in client.get(f"{API}/recipes", headers=auth).json()}
    assert (rows["night_exterior"]["origin"], rows["night_exterior"]["title"]) == ("studio", "Mine")


def test_a_project_save_needs_a_project(client, auth):
    body = {"scope": "project", "recipe": recipe_doc("p_only")}
    r = client.post(f"{API}/recipes", json=body, headers=auth)
    assert r.status_code == 422 and "project_id" in r.json()["errors"][0]
    assert client.post(f"{API}/recipes", json={**body, "project_id": "nope"}, headers=auth).status_code == 404
    assert client.post(f"{API}/recipes", json={**body, "project_id": "../x"}, headers=auth).status_code == 422


def test_a_project_save_lands_in_the_project_folder(client, auth):
    make_project(client, auth)
    r = client.post(
        f"{API}/recipes",
        json={"scope": "project", "project_id": "p1", "recipe": recipe_doc("here")},
        headers=auth,
    )
    assert r.status_code == 201 and r.json()["origin"] == "project"
    assert (project_dir(client, "p1") / "recipes" / "here.yaml").is_file()
    assert recipes.load_runnable("here", project_dir(client, "p1")).origin == "project"


def test_a_bad_doc_is_422_with_the_recipe_error_text_and_writes_nothing(client, auth, plate_env):
    for patch, text in (
        ({"prompt": ""}, "prompt"),
        ({"kind": "banner"}, "kind must be one of"),
        ({"id": "Bad-Id"}, "lower_snake_case"),
    ):
        r = client.post(
            f"{API}/recipes", json={"scope": "studio", "recipe": recipe_doc(**patch)}, headers=auth
        )
        assert r.status_code == 422, r.text
        assert text in r.json()["errors"][0]
    assert not (plate_env / "recipes").exists()


def test_an_engine_that_is_not_a_manifest_is_422(client, auth, plate_env):
    r = client.post(
        f"{API}/recipes",
        json={"scope": "studio", "recipe": recipe_doc(engine="made-up-engine")},
        headers=auth,
    )
    assert r.status_code == 422 and "made-up-engine" in r.json()["errors"][0]
    assert not (plate_env / "recipes").exists()


def test_the_save_body_is_strict(client, auth):
    for body in (
        {"scope": "shipped", "recipe": recipe_doc()},
        {"scope": "studio", "recipe": recipe_doc(), "surprise": 1},
        {"scope": "studio"},
        {"recipe": recipe_doc()},
    ):
        assert client.post(f"{API}/recipes", json=body, headers=auth).status_code == 422, body


def test_the_origin_key_in_a_posted_doc_is_dropped(client, auth, plate_env):
    r = client.post(
        f"{API}/recipes",
        json={"scope": "studio", "recipe": recipe_doc("claims", origin="shipped")},
        headers=auth,
    )
    assert r.status_code == 201 and r.json()["origin"] == "studio"
    assert "origin" not in yaml.safe_load((plate_env / "recipes" / "claims.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------- GET /recipes/{id}/assemble


def assemble(c: TestClient, auth: dict, rid: str, **params):
    return c.get(f"{API}/recipes/{rid}/assemble", params=params, headers=auth)


def test_assemble_fills_the_recipe_from_its_defaults_and_the_callers_tokens(client, auth):
    r = assemble(client, auth, "season_swap")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"prompt", "negative_prompt", "unfilled", "unfilled_tokens", "prompt_source"}
    assert body["prompt_source"] == "recipe" and body["unfilled"] == 0 and body["unfilled_tokens"] == []
    assert "Shift the season to autumn" in body["prompt"]
    assert body["negative_prompt"] == recipes.load_runnable("season_swap").negative_prompt
    winter = assemble(client, auth, "season_swap", tokens=json.dumps({"[SEASON]": "winter"})).json()
    assert "Shift the season to winter" in winter["prompt"] and "[SEASON]" not in winter["prompt"]
    quoted = f"{API}/recipes/season_swap/assemble?tokens={quote(json.dumps({'[SEASON]': 'monsoon'}))}"
    assert "Shift the season to monsoon" in client.get(quoted, headers=auth).json()["prompt"]


def test_assemble_uses_the_default_negative_prompt_when_the_recipe_has_none(client, auth):
    doc = recipe_doc("no_negative")
    del doc["negative_prompt"]
    assert (
        client.post(f"{API}/recipes", json={"scope": "studio", "recipe": doc}, headers=auth).status_code
        == 201
    )
    assert assemble(client, auth, "no_negative").json()["negative_prompt"] == recipes.DEFAULT_NEGATIVE_PROMPT


def test_assemble_reports_the_tokens_nobody_filled(client, auth):
    doc = recipe_doc("open", prompt="Edit [MOOD]; light [TARGET HOUR]; again [MOOD].")
    assert (
        client.post(f"{API}/recipes", json={"scope": "studio", "recipe": doc}, headers=auth).status_code
        == 201
    )
    body = assemble(client, auth, "open").json()
    assert body["unfilled"] == 2 and body["unfilled_tokens"] == ["[MOOD]"]
    filled = assemble(client, auth, "open", tokens=json.dumps({"[MOOD]": "calm"})).json()
    assert filled["unfilled"] == 0 and filled["prompt"] == "Edit calm; light dawn; again calm."


def test_assemble_reads_the_projects_sheet(client, auth):
    make_project(client, auth)
    doc = recipe_doc("by_place", prompt="A view in [LOCATION] at [TARGET HOUR].")
    assert (
        client.post(f"{API}/recipes", json={"scope": "studio", "recipe": doc}, headers=auth).status_code
        == 201
    )
    assert assemble(client, auth, "by_place").json()["unfilled_tokens"] == ["[LOCATION]"]
    assert assemble(client, auth, "by_place", project_id="p1").json()["unfilled_tokens"] == ["[LOCATION]"]
    ctx = client.app.state.ctx
    project = ctx.pipeline.load_project("p1")
    project.doc.setdefault("settings", {})["project_sheet"] = {"location": "Bengaluru"}
    project.save()
    body = assemble(client, auth, "by_place", project_id="p1").json()
    assert body["prompt"] == "A view in Bengaluru at dawn." and body["unfilled"] == 0
    assert (
        assemble(client, auth, "by_place").json()["unfilled"] == 1
    )  # without the project the sheet is empty
    project.doc["settings"]["project_sheet"] = {"no_such_field": "x"}
    project.save()
    bad = assemble(client, auth, "by_place", project_id="p1")
    assert bad.status_code == 422 and bad.json()["error"] == "bad_project_sheet"


def test_assemble_finds_a_project_recipe_only_with_its_project(client, auth):
    make_project(client, auth)
    client.post(
        f"{API}/recipes",
        json={"scope": "project", "project_id": "p1", "recipe": recipe_doc("p_recipe")},
        headers=auth,
    )
    assert assemble(client, auth, "p_recipe", project_id="p1").status_code == 200
    assert assemble(client, auth, "p_recipe").status_code == 404


def test_assemble_refusals(client, auth):
    assert assemble(client, auth, "no_such_recipe").status_code == 404
    assert assemble(client, auth, "no_such_recipe").json()["error"] == "unknown_recipe"
    assert assemble(client, auth, "season_swap", project_id="nope").status_code == 404
    for bad in ("not json", "[1, 2]", json.dumps({"SEASON": "x"}), json.dumps({"[SEASON]": 3})):
        r = assemble(client, auth, "season_swap", tokens=bad)
        assert r.status_code == 422, bad


def test_assemble_never_reads_a_file_path(client, auth, tmp_path, monkeypatch):
    """`load_runnable` treats `x.yaml` as a path; the route takes ids only."""
    loose = tmp_path / "loose.yaml"
    loose.write_text(yaml.safe_dump(recipe_doc("loose")), encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # a relative `loose.yaml` would resolve here if the route let it through
    assert recipes.load_runnable("loose.yaml").id == "loose"
    r = client.get(f"{API}/recipes/loose.yaml/assemble", headers=auth)
    assert r.status_code == 404 and r.json()["error"] == "unknown_recipe"
    for rid in (str(loose), "night_exterior.yaml", "..%2F..%2Fetc%2Fpasswd", "con"):
        assert client.get(f"{API}/recipes/{quote(rid, safe='%')}/assemble", headers=auth).status_code in (
            404,
            422,
        ), rid


# ---------------------------------------------------------------------------- auth


def test_the_new_routes_need_the_token(client):
    for method, path in (
        ("get", f"{API}/engines/cards"),
        ("get", f"{API}/recipes"),
        ("get", f"{API}/recipes/season_swap/assemble"),
        ("post", f"{API}/recipes"),
    ):
        r = getattr(client, method)(path)
        assert r.status_code == 401, path
        r = getattr(client, method)(path, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401, path
