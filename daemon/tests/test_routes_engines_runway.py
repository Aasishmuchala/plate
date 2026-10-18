"""`GET /engines` route price and preference (router plan T123): the Runway routes carry their own price and read preferred."""

from __future__ import annotations

from fastapi.testclient import TestClient

from plate_daemon.projects import Project

API = "/api/v0"


def engines(c: TestClient, auth: dict, **params) -> dict[str, dict]:
    r = c.get(f"{API}/engines", params=params, headers=auth)
    assert r.status_code == 200, r.text
    return {e["id"]: e for e in r.json()}


def make_project(c: TestClient, auth: dict, project_id: str, **custody) -> None:
    custody.setdefault("nda", False)
    r = c.post(f"{API}/projects", json={"project_id": project_id, "custody": custody}, headers=auth)
    assert r.status_code == 201, r.text


def set_setting(c: TestClient, project_id: str, key: str, value) -> None:
    """Write a project setting straight into project.json (the settings route learns this key in T120)."""
    root = c.app.state.ctx.pipeline.project_dir(project_id)
    project = Project.load(root)
    with project.lock(timeout_s=5.0):
        project = Project.load(root)
        settings = project.doc.get("settings")
        project.doc["settings"] = settings = settings if isinstance(settings, dict) else {}
        settings[key] = value
        project.save()


def by_provider(engine: dict) -> dict[str, dict]:
    return {r["provider"]: r for r in engine["routes"]}


def test_kling_o3_has_a_priced_preferred_runway_route_and_an_unpriced_atlas_one(client, auth):
    kling = engines(client, auth)["kling-o3-4k"]
    assert [r["provider"] for r in kling["routes"]] == [
        "atlas",
        "runway",
    ]  # manifest order: Atlas stays first
    routes = by_provider(kling)
    runway, atlas = routes["runway"], routes["atlas"]
    assert runway["price"]["usd"] == 0.43
    assert runway["price"] == {"usd": 0.43, "model": "per_second", "status": "confirmed", "minimum_usd": 0.0}
    assert runway["preferred"] is True and runway["effective_grade"] == "D"
    assert atlas["price"] is None and atlas["preferred"] is False


def test_veo_and_seedance_runway_routes_carry_the_catalogue_price(client, auth):
    got = engines(client, auth)
    assert by_provider(got["veo-3.1"])["runway"]["price"]["usd"] == 0.20
    for eid in ("seedance-2.5-i2v", "seedance-2.5-r2v"):
        runway = by_provider(got[eid])["runway"]
        assert runway["price"]["minimum_usd"] == 0.80 and runway["price"]["model"] == "per_second", eid
        assert runway["preferred"] is True, eid
        assert [r["preferred"] for r in got[eid]["routes"]].count(True) == 1, eid  # only the runway route
    assert by_provider(got["veo-3.1"])["vertex"]["preferred"] is False
    assert by_provider(got["veo-3.1"])["vertex"]["price"] is None


def test_an_image_engine_never_reads_preferred(client, auth):
    got = engines(client, auth)
    for e in got.values():
        if not e["kind"].startswith("video") and e["kind"] != "local_video":
            assert not any(r["preferred"] for r in e["routes"]), e["id"]
            assert all(r["price"] is None for r in e["routes"]), e["id"]
    assert got["nano-banana-2.1"]["routes"]  # the loop above covered real image engines


def test_the_project_setting_none_makes_every_route_unpreferred(client, auth):
    make_project(client, auth, "plain")
    make_project(client, auth, "manual-order")
    set_setting(client, "manual-order", "preferred_video_provider", "none")
    assert (
        by_provider(engines(client, auth, project_id="plain")["kling-o3-4k"])["runway"]["preferred"] is True
    )
    got = engines(client, auth, project_id="manual-order")
    assert not any(r["preferred"] for e in got.values() for r in e["routes"])
    assert by_provider(got["kling-o3-4k"])["runway"]["price"]["usd"] == 0.43  # the price is not a preference


def test_the_project_setting_can_name_another_provider(client, auth):
    make_project(client, auth, "atlas-first")
    set_setting(client, "atlas-first", "preferred_video_provider", "atlas")
    got = engines(client, auth, project_id="atlas-first")
    kling = by_provider(got["kling-o3-4k"])
    assert kling["atlas"]["preferred"] is True and kling["runway"]["preferred"] is False
    assert by_provider(got["veo-3.1"])["vertex"]["preferred"] is False  # veo has no atlas route
    assert not any(r["preferred"] for r in got["nano-banana-2.1"]["routes"])  # images stay unpreferred


def test_a_malformed_setting_falls_back_to_the_default(client, auth):
    make_project(client, auth, "odd")
    set_setting(client, "odd", "preferred_video_provider", 7)
    assert by_provider(engines(client, auth, project_id="odd")["kling-o3-4k"])["runway"]["preferred"] is True


def test_gen_4_5_is_start_frame_only_and_closed_to_nda_projects(client, auth):
    make_project(client, auth, "nda-job", nda=True)
    make_project(client, auth, "open-job", nda=False)
    gen = engines(client, auth)["gen-4.5"]
    assert gen["capabilities"]["image_tail"] is False
    assert gen["kind"] == "video_i2v" and gen["capabilities"]["runnable"] is True
    assert [r["provider"] for r in gen["routes"]] == ["runway"] and gen["routes"][0]["preferred"] is True
    assert engines(client, auth, project_id="nda-job")["gen-4.5"]["allowed"] is False
    assert engines(client, auth, project_id="open-job")["gen-4.5"]["allowed"] is True
