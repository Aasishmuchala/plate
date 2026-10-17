"""Project settings, spend and docs routes (router plan T88).

Settings live in `project.json` under `settings`; a PUT replaces the keys it carries, validates the sheet
through `ProjectSheet.from_dict` and the default engines against the manifests, and writes under the project
lock. Spend is a read of `runs.jsonl`: the ledger's own `summarize` plus a series folded from the records.
Docs are the repo's `docs/**.md`, confined to that folder. The quote that must pick up the sheet runs the real
pipeline on the committed Blender packet `room_a`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.ledger import Ledger
from plate_daemon.projects import Project
from plate_daemon.recipes import ProjectSheet
from plate_daemon.routes import docs as docs_routes

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
API = "/api/v0"
PROJECT = "plate-fixture"  # room_a's manifest project_id
UI = {"X-Plate-Client": "ui"}

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)

SEASON_SWAP = """\
schema: 1
id: season_swap
title: Season swap
kind: still_edit
engine: nano-banana-2.1
shot_class: wide
mask:
  editable_classes: [vegetation, sky, water]
  region: none
  lock_rest: true
params:
  n: 1
  size: native
overrides:
  "[SEASON]": "autumn"
prompt: |-
  Change the season to [SEASON]. [NAME THE SPECIES] keep their positions and their counts.
negative_prompt: new objects, removed objects, changed architecture
"""


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


@pytest.fixture
def c(settings: Settings):
    with TestClient(create_app(settings), base_url=BASE) as client:
        yield client


def _project(c: TestClient, auth: dict, project_id: str = "alpha", **custody) -> dict:
    r = c.post(
        f"{API}/projects", json={"project_id": project_id, "custody": {"nda": False, **custody}}, headers=auth
    )
    assert r.status_code == 201, r.text
    return r.json()


def _doc(settings: Settings, project_id: str = "alpha") -> dict:
    return json.loads((settings.projects_dir / project_id / "project.json").read_text(encoding="utf-8"))


def _get(c: TestClient, auth: dict, project_id: str = "alpha") -> dict:
    r = c.get(f"{API}/projects/{project_id}/settings", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _put(c: TestClient, auth: dict, body: dict, project_id: str = "alpha", **headers):
    return c.put(f"{API}/projects/{project_id}/settings", json=body, headers={**auth, **headers})


# ---------------------------------------------------------------------------- settings


def test_a_new_project_answers_the_defaults(c, auth):
    _project(c, auth)
    got = _get(c, auth)
    assert set(got) == {"project_sheet", "sheet_fields", "default_engine", "auto_carry_over", "policy"}
    assert got["project_sheet"] == ProjectSheet().as_dict() and len(got["project_sheet"]) == 14
    assert got["project_sheet"]["format"] == "4K architectural film, 16:9"
    assert [f["key"] for f in got["sheet_fields"]] == list(got["project_sheet"])  # form order
    assert set(got["sheet_fields"][3]) == {"key", "lensbook_key", "default"}
    assert got["sheet_fields"][3] == {"key": "light_logic", "lensbook_key": "lightLogic", "default": ""}
    assert all(got["project_sheet"][f["key"]] == f["default"] for f in got["sheet_fields"])
    assert got["default_engine"] == {} and got["auto_carry_over"] is True and got["policy"] == {}


def test_put_a_sheet_then_get_returns_it_and_it_is_stored_under_settings(c, auth, settings):
    _project(c, auth)
    sheet = {"name": "Tula Heights", "species": "Rain tree, Tabebuia", "lightLogic": "low sun from the west"}
    r = _put(c, auth, {"project_sheet": sheet})
    assert r.status_code == 200, r.text
    got = _get(c, auth)
    assert r.json() == got
    assert got["project_sheet"]["name"] == "Tula Heights"
    assert got["project_sheet"]["species"] == "Rain tree, Tabebuia"
    assert got["project_sheet"]["light_logic"] == "low sun from the west"  # Lensbook's camelCase accepted
    assert got["project_sheet"]["format"] == "4K architectural film, 16:9"
    stored = _doc(settings)["settings"]["project_sheet"]
    assert stored == got["project_sheet"] and "lightLogic" not in stored  # stored under the snake_case names

    # A PUT carries the whole form: a field left out is back to its default, the other keys are untouched.
    assert _put(c, auth, {"auto_carry_over": False}).status_code == 200
    assert _put(c, auth, {"project_sheet": {"location": "Bengaluru"}}).status_code == 200
    got = _get(c, auth)
    assert got["project_sheet"]["location"] == "Bengaluru" and got["project_sheet"]["species"] == ""
    assert got["auto_carry_over"] is False


def test_an_unknown_sheet_key_or_a_non_text_value_is_422_and_writes_nothing(c, auth, settings):
    _project(c, auth)
    assert _put(c, auth, {"project_sheet": {"species": "Neem"}}).status_code == 200
    before = (settings.projects_dir / "alpha" / "project.json").read_bytes()

    r = _put(c, auth, {"project_sheet": {"species": "Teak", "colour_of_the_moon": "blue"}})
    assert r.status_code == 422 and "colour_of_the_moon" in json.dumps(r.json())
    r = _put(c, auth, {"project_sheet": {"species": 12}})
    assert r.status_code == 422
    r = _put(c, auth, {"project_sheet": ["species"]})
    assert r.status_code == 422
    r = _put(c, auth, {"project_sheet": {"species": "Teak"}, "unknown_setting": 1})  # no stray top-level keys
    assert r.status_code == 422
    r = _put(c, auth, {"auto_carry_over": "yes"})
    assert r.status_code == 422
    assert (settings.projects_dir / "alpha" / "project.json").read_bytes() == before
    assert _get(c, auth)["project_sheet"]["species"] == "Neem"


def test_default_engines_must_be_manifest_ids(c, auth, settings):
    _project(c, auth)
    r = _put(c, auth, {"default_engine": {"relight": "nano-banana-2.1", "sky": "gpt-image-2.5"}})
    assert r.status_code == 200, r.text
    assert r.json()["default_engine"] == {"relight": "nano-banana-2.1", "sky": "gpt-image-2.5"}
    assert _doc(settings)["settings"]["default_engine"] == r.json()["default_engine"]

    r = _put(c, auth, {"default_engine": {"relight": "no-such-engine"}})
    assert r.status_code == 422 and "no-such-engine" in json.dumps(r.json())
    r = _put(c, auth, {"default_engine": {"Not A Task": "nano-banana-2.1"}})
    assert r.status_code == 422
    r = _put(c, auth, {"default_engine": {"relight": 3}})
    assert r.status_code == 422
    assert _get(c, auth)["default_engine"] == {"relight": "nano-banana-2.1", "sky": "gpt-image-2.5"}

    # The app's own stub engine is offered by the Make bar as `fake`, so it may be a default too.
    assert _put(c, auth, {"default_engine": {"relight": "fake"}}).json()["default_engine"] == {
        "relight": "fake"
    }
    assert _put(c, auth, {"default_engine": {}}).json()["default_engine"] == {}  # an empty map clears them


def test_policy_is_shown_read_only(c, auth, settings):
    _project(c, auth)
    project = Project.load(settings.projects_dir / "alpha")
    project.doc["settings"]["policy"] = {"locked_classes": ["vegetation"], "lint_threshold": 0.2}
    project.save()
    assert _get(c, auth)["policy"] == {"locked_classes": ["vegetation"], "lint_threshold": 0.2}
    assert _put(c, auth, {"policy": {"locked_classes": []}}).status_code == 422  # not settable here
    assert _put(c, auth, {"auto_carry_over": True}).json()["policy"]["locked_classes"] == ["vegetation"]


def test_settings_and_the_graph_do_not_overwrite_each_other(c, auth, settings):
    _project(c, auth)
    graph = {
        "nodes": [{"id": "p", "kind": "Plate", "packetId": "pkt_x", "tier": "A", "label": "Plate"}],
        "edges": [],
    }
    assert c.put(f"{API}/projects/alpha/graph", json=graph, headers=auth).status_code == 200
    assert _put(c, auth, {"project_sheet": {"name": "A"}, "auto_carry_over": False}).status_code == 200
    assert _doc(settings)["graph"] == graph
    assert c.put(f"{API}/projects/alpha/graph", json=graph, headers=auth).status_code == 200
    assert _get(c, auth)["project_sheet"]["name"] == "A" and _get(c, auth)["auto_carry_over"] is False


def test_an_unknown_project_is_404_and_an_empty_put_changes_nothing(c, auth, settings):
    assert c.get(f"{API}/projects/ghost/settings", headers=auth).status_code == 404
    assert _put(c, auth, {"auto_carry_over": False}, project_id="ghost").status_code == 404
    _project(c, auth)
    before = (settings.projects_dir / "alpha" / "project.json").read_bytes()
    r = _put(c, auth, {})
    assert r.status_code == 200 and r.json() == _get(c, auth)
    assert (settings.projects_dir / "alpha" / "project.json").read_bytes() == before
    assert c.get(f"{API}/projects/alpha/settings").status_code == 401  # no token, no settings


def test_only_a_ui_request_resets_the_unattended_clock(c, auth):
    _project(c, auth)
    pipeline = c.app.state.pipeline
    pipeline.last_human_utc = None
    assert _put(c, auth, {"auto_carry_over": False}).status_code == 200
    assert _get(c, auth) and pipeline.last_human_utc is None
    assert _put(c, auth, {"auto_carry_over": False}, **{"X-Plate-Client": "mcp"}).status_code == 200
    assert pipeline.last_human_utc is None
    assert _put(c, auth, {"auto_carry_over": True}, **UI).status_code == 200
    assert pipeline.last_human_utc is not None


def _ingest_room_a(c: TestClient, auth: dict) -> str:
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    r = c.post(
        f"{API}/packets",
        json={"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}},
        headers=hdr,
    )
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert c.get(f"{API}/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
    return packet_id


@needs_room_a
def test_a_quote_with_the_season_swap_recipe_uses_the_sheets_species(sync_settings, auth, tmp_path):
    recipe = tmp_path / "season_swap.yaml"  # a Wave 9 recipe in shape; written here so the test owns its text
    recipe.write_text(SEASON_SWAP, encoding="utf-8", newline="\n")
    with TestClient(create_app(sync_settings), base_url=BASE) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        _project(c, auth, PROJECT)
        packet_id = _ingest_room_a(c, auth)
        graph = {
            "nodes": [
                {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
                {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "nano-banana-2.1"}},
            ],
            "edges": [{"id": "p->e", "source": "p", "target": "e"}],
        }
        assert c.put(f"{API}/projects/{PROJECT}/graph", json=graph, headers=auth).status_code == 200

        def prompt() -> str:
            body = {"engine_id": "nano-banana-2.1", "recipe": str(recipe)}
            r = c.post(f"{API}/projects/{PROJECT}/nodes/e/quote", json=body, headers=auth)
            assert r.status_code == 200, r.text
            return r.json()["run"]["inputs"]["prompt"]

        assert "[NAME THE SPECIES]" in prompt()  # the sheet is empty: the token stays visible
        sheet = {"species": "Rain tree and Tabebuia rosea"}
        assert _put(c, auth, {"project_sheet": sheet}, project_id=PROJECT).status_code == 200
        text = prompt()
        assert "Rain tree and Tabebuia rosea keep their positions" in text and "autumn" in text
        assert "[NAME THE SPECIES]" not in text


# ---------------------------------------------------------------------------- spend


def _rec(ledger: Ledger, kind: str, run: str, at: str, engine: str | None = "nano-banana-2.1", **kw) -> None:
    ledger.append({"kind": kind, "run_id": run, "at": at, **({"engine_id": engine} if engine else {}), **kw})


def _fill(root: Path) -> None:
    """Three runs on two engines over two ISO weeks, plus one bill that names no run."""
    led = Ledger(root)
    _rec(led, "quoted", "r1", "2026-10-12T09:00:00Z", cost_usd_quoted=0.04)
    _rec(led, "approved", "r1", "2026-10-12T09:01:00Z", cost_usd_quoted=0.04)
    _rec(led, "submitted", "r1", "2026-10-12T09:02:00Z")
    _rec(led, "billed", "r1", "2026-10-12T09:05:00Z", cost_usd_billed=0.05)
    _rec(led, "done", "r1", "2026-10-12T09:06:00Z", data={"decision": "accepted"})
    _rec(led, "approved", "r2", "2026-10-13T10:00:00Z", cost_usd_quoted=0.04)  # same week, next day
    _rec(led, "submitted", "r2", "2026-10-13T10:01:00Z")
    _rec(led, "done", "r2", "2026-10-13T10:02:00Z", data={"decision": "rerolled"})
    _rec(led, "approved", "r3", "2026-10-20T11:00:00Z", engine="kling-o3-4k", cost_usd_quoted=0.5)
    _rec(led, "submitted", "r3", "2026-10-20T11:01:00Z", engine="kling-o3-4k")
    _rec(led, "billed", "r3", "2026-10-20T11:30:00Z", engine="kling-o3-4k", cost_usd_billed=0.6)
    _rec(led, "done", "r3", "2026-10-20T11:31:00Z", engine="kling-o3-4k", data={"decision": "accepted"})
    _rec(led, "quoted", "r4", "2026-10-21T08:00:00Z", cost_usd_quoted=0.04)  # quoted only: spent nothing
    led.append({"kind": "billed", "at": "2026-10-13T12:00:00Z", "cost_usd_billed": 0.1})  # a bill with no run


def test_spend_series_by_engine_day_and_week(c, auth, settings):
    _project(c, auth)
    _fill(settings.projects_dir / "alpha")

    r = c.get(f"{API}/projects/alpha/spend", headers=auth)  # by=engine by default
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == "alpha" and body["by"] == "engine" and body["since"] is None
    assert body["generations"] == 3 and body["accepted"] == 2 and body["rerolled"] == 1
    assert body["spent_usd_quoted"] == pytest.approx(0.58) and body["spent_usd_billed"] == pytest.approx(0.75)
    assert set(body["by_engine"]) == {"nano-banana-2.1", "kling-o3-4k", "unattributed"}
    assert body["series"] == [
        {
            "bucket": "kling-o3-4k",
            "engine_id": "kling-o3-4k",
            "runs": 1,
            "quoted_usd": 0.5,
            "billed_usd": 0.6,
            "accepted": 1,
            "cost_per_accepted": 0.6,
        },
        {
            "bucket": "nano-banana-2.1",
            "engine_id": "nano-banana-2.1",
            "runs": 2,
            "quoted_usd": 0.08,
            "billed_usd": 0.05,
            "accepted": 1,
            "cost_per_accepted": 0.05,
        },
        {
            "bucket": "unattributed",
            "engine_id": "unattributed",
            "runs": 0,
            "quoted_usd": 0.0,
            "billed_usd": 0.1,
            "accepted": 0,
            "cost_per_accepted": None,
        },
    ]

    week = c.get(f"{API}/projects/alpha/spend?by=week", headers=auth).json()["series"]
    assert [(r["bucket"], r["engine_id"], r["runs"]) for r in week] == [
        ("2026-W42", "nano-banana-2.1", 2),
        ("2026-W42", "unattributed", 0),
        ("2026-W43", "kling-o3-4k", 1),
    ]
    assert sum(r["billed_usd"] for r in week) == pytest.approx(0.75)  # the rows add up to the totals
    day = c.get(f"{API}/projects/alpha/spend?by=day", headers=auth).json()["series"]
    assert [(r["bucket"], r["engine_id"], r["runs"]) for r in day] == [
        ("2026-10-12", "nano-banana-2.1", 1),
        ("2026-10-13", "nano-banana-2.1", 1),
        ("2026-10-13", "unattributed", 0),
        ("2026-10-20", "kling-o3-4k", 1),
    ]
    day13 = next(r for r in day if r["bucket"] == "2026-10-13" and r["runs"] == 1)
    assert day13["accepted"] == 0 and day13["cost_per_accepted"] is None and day13["billed_usd"] == 0.0

    since = c.get(f"{API}/projects/alpha/spend?by=week&since=2026-10-19", headers=auth).json()
    assert since["since"].startswith("2026-10-19") and since["generations"] == 1
    assert [(r["bucket"], r["engine_id"]) for r in since["series"]] == [("2026-W43", "kling-o3-4k")]


def test_spend_refuses_a_bad_window_or_bucket_and_an_unknown_project(c, auth):
    _project(c, auth)
    assert c.get(f"{API}/projects/alpha/spend?by=month", headers=auth).status_code == 422
    assert c.get(f"{API}/projects/alpha/spend?since=last-tuesday", headers=auth).status_code == 422
    assert c.get(f"{API}/projects/ghost/spend", headers=auth).status_code == 404
    body = c.get(f"{API}/projects/alpha/spend", headers=auth).json()  # no ledger yet: zeros, an empty series
    assert body["series"] == [] and body["generations"] == 0 and body["spent_usd_billed"] == 0


def test_spend_over_every_project_folds_runs_that_share_an_id(c, auth, settings):
    for pid in ("alpha", "beta"):
        _project(c, auth, pid)
    _fill(settings.projects_dir / "alpha")
    led = Ledger(settings.projects_dir / "beta")  # the same run id as alpha's r1, another project
    _rec(led, "approved", "r1", "2026-10-14T09:00:00Z", cost_usd_quoted=0.04)
    _rec(led, "submitted", "r1", "2026-10-14T09:01:00Z")
    _rec(led, "done", "r1", "2026-10-14T09:02:00Z", data={"decision": "accepted"})

    assert c.get(f"{API}/spend", headers=auth).status_code == 422  # the flag is required
    assert c.get(f"{API}/spend?all_projects=0", headers=auth).status_code == 422
    r = c.get(f"{API}/spend?all_projects=1&by=week", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "all_projects" and body["projects"] == ["alpha", "beta"] and body["by"] == "week"
    assert body["generations"] == 4 and body["accepted"] == 3  # beta's r1 is not alpha's r1
    nano = next(
        r for r in body["series"] if r["bucket"] == "2026-W42" and r["engine_id"] == "nano-banana-2.1"
    )
    assert nano["runs"] == 3 and nano["accepted"] == 2 and nano["quoted_usd"] == pytest.approx(0.12)
    assert nano["billed_usd"] == pytest.approx(0.05)
    assert nano["cost_per_accepted"] == pytest.approx(0.025)
    since = c.get(f"{API}/spend?all_projects=true&since=2026-10-14", headers=auth).json()
    assert since["generations"] == 2  # beta's r1 and alpha's r3
    assert c.get(f"{API}/spend?all_projects=1").status_code == 401


@needs_room_a
def test_spend_series_for_a_fake_run_has_one_bucket_with_one_run(sync_settings, auth):
    with TestClient(create_app(sync_settings), base_url=BASE) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        _project(c, auth, PROJECT)
        packet_id = _ingest_room_a(c, auth)
        graph = {
            "nodes": [
                {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
                {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
            ],
            "edges": [{"id": "p->e", "source": "p", "target": "e"}],
        }
        assert c.put(f"{API}/projects/{PROJECT}/graph", json=graph, headers=auth).status_code == 200
        q = c.post(f"{API}/projects/{PROJECT}/nodes/e/quote", json={"engine_id": "fake"}, headers=auth)
        assert q.status_code == 200, q.text
        run_id = q.json()["run"]["id"]
        assert c.post(f"{API}/runs/{run_id}/approve", json={"by": "auto"}, headers=auth).status_code == 200

        body = c.get(f"{API}/projects/{PROJECT}/spend?by=day", headers=auth).json()
        assert body["generations"] == 1
        assert len(body["series"]) == 1
        row = body["series"][0]
        assert row["runs"] == 1 and row["engine_id"] == "fake" and row["accepted"] == 0
        assert row["cost_per_accepted"] is None
        assert c.get(f"{API}/spend?all_projects=1", headers=auth).json()["series"][0]["runs"] == 1


# ---------------------------------------------------------------------------- docs


def test_the_docs_index_lists_the_repo_docs(c, auth):
    r = c.get(f"{API}/docs/", headers=auth)
    assert r.status_code == 200, r.text
    docs = {d["name"]: d for d in r.json()["docs"]}
    assert "artist-manual.md" in docs and "runbooks/nightly.md" in docs
    manual = docs["artist-manual.md"]
    assert manual["href"] == f"{API}/docs/artist-manual.md" and manual["bytes"] > 1000 and manual["title"]
    assert all(name.endswith(".md") and ".." not in name and "\\" not in name for name in docs)
    assert c.get(f"{API}/docs/").status_code == 401  # behind the token like every API route


def test_a_doc_is_served_as_markdown(c, auth):
    on_disk = (ROOT / "docs" / "artist-manual.md").read_bytes()
    r = c.get(f"{API}/docs/artist-manual.md", headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert r.content == on_disk
    r = c.get(f"{API}/docs/runbooks/nightly.md", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert c.get(f"{API}/docs/artist-manual.md").status_code == 401
    assert c.get("/docs/artist-manual.md").status_code == 404  # never at a bare /docs (test_auth pins that)


@pytest.mark.parametrize(
    "path",
    [
        "/docs/../pyproject.toml",
        "/docs/%2e%2e/pyproject.toml",
        "/docs/%2E%2E%2Fpyproject.toml",
        "/docs/..%2Fpyproject.toml",
        "/docs/runbooks/../../pyproject.toml",
        "/docs/pyproject.toml",
        "/docs/artist-manual",
        "/docs/artist-manual.md.bak",
        "/docs/artist-manual.MD",
        "/docs/runbooks/",
        "/docs/runbooks.md",
        "/docs/%5Cwindows%5Cwin.ini",
        "/docs/C%3A/x.md",
        "/docs//etc/passwd.md",
        "/docs/.hidden.md",
        "/docs/nothing-here.md",
        "/docs/artist-manual.md%00.png",
    ],
)
def test_nothing_outside_the_docs_folder_is_served(c, auth, path):
    r = c.get(API + path, headers=auth)
    assert r.status_code == 404, (API + path, r.status_code)
    assert b"[project]" not in r.content and b"root:" not in r.content


def test_resolve_doc_confines_names_to_the_folder(tmp_path):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "a.md").write_text("# A title\n", encoding="utf-8")
    (root / "sub" / "b.md").write_text("no heading\n", encoding="utf-8")
    (root / ".secret.md").write_text("x", encoding="utf-8")
    (root / "dir.md").mkdir()
    (tmp_path / "outside.md").write_text("# outside\n", encoding="utf-8")

    assert docs_routes.resolve_doc("a.md", root) == (root / "a.md").resolve()
    assert docs_routes.resolve_doc("sub/b.md", root) == (root / "sub" / "b.md").resolve()
    for bad in (
        "",
        "a",
        "a.txt",
        "../outside.md",
        "sub/../../outside.md",
        "./a.md",
        "sub//b.md",
        "/a.md",
        "\\a.md",
        "sub\\b.md",
        "C:/a.md",
        "a.md\x00",
        ".secret.md",
        "dir.md",
        "missing.md",
    ):
        assert docs_routes.resolve_doc(bad, root) is None, bad

    # The listing leaves out hidden files and directories named like a doc.
    assert [p.name for p in docs_routes.list_docs(root)] == ["a.md", "b.md"]
    assert docs_routes.list_docs(tmp_path / "absent") == []


def test_the_index_reads_titles_from_the_first_heading(c, auth, tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.md").write_text("intro\n\n# The first heading\n\n# second\n", encoding="utf-8", newline="\n")
    (root / "two.md").write_text("Ünïcode only, no heading\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(docs_routes, "DOCS_DIR", root)
    docs = {d["name"]: d for d in c.get(f"{API}/docs/", headers=auth).json()["docs"]}
    assert list(docs) == ["one.md", "two.md"]
    assert docs["one.md"]["title"] == "The first heading" and docs["two.md"]["title"] == "two"
    r = c.get(f"{API}/docs/two.md", headers=auth)
    assert r.status_code == 200 and r.content.decode("utf-8") == "Ünïcode only, no heading\n"
