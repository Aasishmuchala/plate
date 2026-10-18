"""Templates and batch chains, and the cross-engine quote_many (router plan T95).

Three layers. `graph_build` and the template loader are pure and run on hand-made packet summaries. The routes
run on the committed Blender packets `fixtures/packets/room_a` and `room_a_v2` (one project, the second
superseding the first) through the real pipeline with an engine that echoes the PNG it was sent, so an
auto-approved chain really reaches DONE. `quote_many` prices the second engine from its manifest only; nothing
here is sent to a vendor (the runs it makes are cancelled before they start).
"""

from __future__ import annotations

import base64
import copy
import json
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

from plate_daemon import canvas_rules, graph_build, templates
from plate_daemon.api import create_app
from plate_daemon.budgets import Policy
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.quotes import FAKE_PRICE_USD
from plate_daemon.sse import EventFilter

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
ROOM_A_V2 = ROOT / "fixtures" / "packets" / "room_a_v2"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # the fixture manifests' project_id
NB = "nano-banana-2.1"

needs_rooms = pytest.mark.skipif(
    not all((p / "manifest.json").is_file() and (p / "beauty.exr").is_file() for p in (ROOM_A, ROOM_A_V2)),
    reason="fixtures/packets/room_a and room_a_v2 not built yet (T2)",
)


def summary(
    packet_id: str = "pkt_one",
    camera: str | None = "CAM_a",
    entries: list[dict[str, Any]] | None = None,
    tier: str = "A",
) -> dict[str, Any]:
    """The parts of a `GET /packets/{id}/summary` answer that the builders read."""
    if entries is None:
        entries = [
            {"id": "o:sky", "class": "sky", "editable": True, "locked": False},
            {"id": "m:sky", "class": "sky", "editable": True, "locked": False},
            {"id": "o:tree", "class": "vegetation", "editable": True, "locked": False},
            {"id": "o:man", "class": "people", "editable": False, "locked": False},  # policy does not list it
            {"id": "o:wall", "class": "architecture", "editable": False, "locked": True},
            {"id": "o:pool", "class": "water", "editable": True, "locked": False},
        ]
    return {
        "packet_id": packet_id,
        "project_id": PROJECT,
        "shot_id": "shot-1",
        "tier": tier,
        "camera": {"name": camera} if camera is not None else None,
        "legend": {"entries": entries},
    }


def kinds(graph: dict[str, Any]) -> list[str]:
    return [n["kind"] for n in graph["nodes"]]


def node(graph: dict[str, Any], kind: str, n: int = 0) -> dict[str, Any]:
    return [x for x in graph["nodes"] if x["kind"] == kind][n]


# ---------------------------------------------------------------------------- the shipped templates


def test_the_shipped_templates_are_valid_and_listed_by_id():
    found = {t["id"]: t for t in templates.list()}
    assert sorted(found) == ["edit_chain", "night_chain", "set_chain"]
    assert [n["kind"] for n in found["edit_chain"]["nodes"]] == ["Plate", "Mask", "Edit", "Judge", "Export"]
    assert [n["kind"] for n in found["night_chain"]["nodes"]] == [
        "Plate",
        "LightMix",
        "Mask",
        "Edit",
        "Export",
    ]
    assert [n["kind"] for n in found["set_chain"]["nodes"]] == ["Plate", "Mask", "Edit"]
    assert [t["set"] for t in found.values()] == [False, False, True]
    assert {t["origin"] for t in found.values()} == {"shipped"}
    mask = found["edit_chain"]["nodes"][1]
    assert mask["params"] == {"classes": ["sky", "vegetation", "people"]}
    night = found["night_chain"]["nodes"]
    assert night[1]["params"] == {"preset": "dusk"}
    assert night[3]["params"] == {"recipe": "night_relight_changelist"}
    assert night[3]["inputs"] == {"image": "lightmix", "mask": "mask"}
    assert templates.load("edit_chain") == found["edit_chain"]
    with pytest.raises(templates.TemplateNotFound):
        templates.load("nope")


def test_project_templates_shadow_shipped_ones_and_a_broken_one_is_skipped(tmp_path):
    folder = templates.project_templates_dir(tmp_path)
    folder.mkdir()
    mine = {
        "schema": 1,
        "id": "edit_chain",
        "title": "My edit chain",
        "nodes": [{"suffix": "plate", "kind": "Plate"}, {"suffix": "edit", "kind": "Edit"}],
    }
    (folder / "edit_chain.yaml").write_text(yaml.safe_dump(mine), encoding="utf-8")
    (folder / "broken.yaml").write_text("schema: 1\nid: broken\ntitle: x\nnodes: []\n", encoding="utf-8")
    by_id = {t["id"]: t for t in templates.list(tmp_path)}
    assert sorted(by_id) == ["edit_chain", "night_chain", "set_chain"]
    assert by_id["edit_chain"]["origin"] == "project" and by_id["edit_chain"]["title"] == "My edit chain"
    assert templates.load("edit_chain", tmp_path)["nodes"][1]["inputs"] == {}
    assert templates.load("edit_chain")["origin"] == "shipped"  # without the project the shipped one
    with pytest.raises(templates.TemplateError, match="nodes"):
        templates.load("broken", tmp_path)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"schema": 2}, "schema: 1"),
        ({"id": "Bad Id"}, "id must be"),
        ({"title": ""}, "title"),
        ({"set": "yes"}, "set must be"),
        ({"nodes": [{"suffix": "a", "kind": "Wibble"}]}, "kind 'Wibble'"),
        ({"nodes": [{"suffix": "a", "kind": "Frame"}]}, "kind 'Frame'"),
        ({"nodes": [{"suffix": "a", "kind": "Plate"}, {"suffix": "a", "kind": "Edit"}]}, "appears twice"),
        ({"nodes": [{"suffix": "a", "kind": "Plate"}, {"suffix": "b", "kind": "Plate"}]}, "one Plate"),
        ({"nodes": [{"suffix": "a", "kind": "Edit", "inputs": {"wibble": "$plate"}}]}, "no input 'wibble'"),
        ({"nodes": [{"suffix": "a", "kind": "Edit", "inputs": {"mask": "b"}}]}, "not $plate"),
        ({"nodes": [{"suffix": "a", "kind": "Edit", "inputs": {"mask": "a"}}]}, "not $plate"),
        (
            {"nodes": [{"suffix": "a", "kind": "Mask", "params": {"classes": "sky"}}]},
            "params.classes",
        ),
        # a mask never feeds an image input; a report never reaches Export
        ({"nodes": [{"suffix": "a", "kind": "Edit", "inputs": {"image": "$packet"}}]}, "carries packet"),
        (
            {
                "nodes": [
                    {"suffix": "p", "kind": "Plate"},
                    {"suffix": "j", "kind": "Judge", "inputs": {"candidate": "$plate"}},
                    {"suffix": "x", "kind": "Export", "inputs": {"media": "j"}},
                ]
            },
            "carries report",
        ),
    ],
)
def test_a_template_that_cannot_work_is_refused_when_read(patch, message):
    doc = {
        "schema": 1,
        "id": "t",
        "title": "T",
        "set": False,
        "nodes": [{"suffix": "plate", "kind": "Plate"}],
        **patch,
    }
    with pytest.raises(templates.TemplateError, match=re.escape(message)):
        templates.parse(doc)


# ---------------------------------------------------------------------------- graph_build (pure)


def test_edit_chain_ids_positions_edges_and_mask_ids():
    tpl = templates.load("edit_chain")
    empty = {"schema": 1, "nodes": [], "edges": []}
    before = copy.deepcopy(empty)
    g = graph_build.chain_for_packet(empty, summary(), template=tpl, engine_id="fake", task="relight")
    assert empty == before  # pure
    assert g["schema"] == 1
    assert [n["id"] for n in g["nodes"]] == [
        "plate_cam_a_1",
        "mask_cam_a_1",
        "edit_cam_a_1",
        "judge_cam_a_1",
        "export_cam_a_1",
    ]
    assert [n["position"] for n in g["nodes"]] == [{"x": 0, "y": y * 180} for y in range(5)]
    plate, mask, edit = g["nodes"][:3]
    assert plate == {
        "id": "plate_cam_a_1",
        "kind": "Plate",
        "label": "Plate",
        "packetId": "pkt_one",
        "tier": "A",
        "position": {"x": 0, "y": 0},
    }
    # sky, vegetation and people ids that the packet lets an edit touch: the locked wall and the
    # architecture/water classes are out, and people is out because the policy lists editable ids and not that one
    assert mask["params"]["ids"] == ["o:sky", "m:sky", "o:tree"]
    assert mask["params"]["classes"] == ["sky", "vegetation", "people"]
    assert edit["params"] == {"engine": "fake", "task": "relight"}
    edges = {(e["source"], e["sourceHandle"], e["target"], e["targetHandle"]) for e in g["edges"]}
    assert edges == {
        ("plate_cam_a_1", "packet", "mask_cam_a_1", "source"),
        ("mask_cam_a_1", "mask", "edit_cam_a_1", "mask"),
        ("edit_cam_a_1", "image", "judge_cam_a_1", "candidate"),
        ("edit_cam_a_1", "image", "export_cam_a_1", "media"),
    }
    assert len({e["id"] for e in g["edges"]}) == 4
    assert canvas_rules.check_nodes(g) == [] and canvas_rules.validate_graph(g) == []


def test_a_second_chain_for_the_same_packet_reuses_the_plate_and_takes_the_next_number():
    tpl = templates.load("edit_chain")
    s = summary()
    g = graph_build.chain_for_packet({"nodes": [], "edges": []}, s, template=tpl)
    again = graph_build.chain_for_packet(g, s, template=tpl, column=1)
    assert kinds(again).count("Plate") == 1
    ids = [n["id"] for n in again["nodes"]]
    assert ids[5:] == ["mask_cam_a_2", "edit_cam_a_2", "judge_cam_a_2", "export_cam_a_2"]
    # the new chain hangs off the Plate that was already there, in its own column
    assert ("plate_cam_a_1", "packet", "mask_cam_a_2", "source") in {
        (e["source"], e["sourceHandle"], e["target"], e["targetHandle"]) for e in again["edges"]
    }
    assert again["nodes"][5]["position"] == {"x": 340, "y": 180}
    # another packet seen by the same camera gets its own Plate and the next number
    other = graph_build.chain_for_packet(again, summary("pkt_two"), template=tpl, column=2)
    assert kinds(other).count("Plate") == 2
    assert "plate_cam_a_2" in [n["id"] for n in other["nodes"]]


def test_a_summary_without_a_camera_name_falls_back_to_its_shot_then_its_packet():
    assert graph_build.camera_slug(summary(camera=None)) == "shot_1"
    bare = {"packet_id": "pkt_ABC"}
    assert graph_build.camera_slug(bare) == "pkt_abc"
    assert graph_build.slug("CAM a / 2") == "cam_a_2"


def test_the_mask_ids_follow_the_policy_rule_the_cli_uses():
    entries = [
        {"id": "s:1", "class": "sky", "editable": False, "locked": False},
        {"id": "s:2", "class": "sky", "editable": False, "locked": True},
    ]
    # a Tier C packet lists no editable id: every id of the class that is not locked counts
    assert graph_build.mask_ids(summary(entries=entries), ["sky"]) == ["s:1"]
    # a packet that lists editable ids restricts the mask to those
    assert graph_build.mask_ids(summary(), ["people"]) == []
    assert graph_build.mask_ids(summary(), ["sky", "water"]) == ["o:sky", "m:sky", "o:pool"]


def test_night_chain_takes_the_edit_image_from_the_lightmix_and_passes_the_rules():
    g = graph_build.chain_for_packet(
        {"nodes": [], "edges": []}, summary(), template=templates.load("night_chain")
    )
    assert [n["id"] for n in g["nodes"]] == [
        "plate_cam_a_1",
        "lightmix_cam_a_1",
        "mask_cam_a_1",
        "edit_cam_a_1",
        "export_cam_a_1",
    ]
    assert node(g, "LightMix")["params"] == {"preset": "dusk"}
    assert node(g, "Edit")["params"] == {"recipe": "night_relight_changelist"}
    edges = {(e["source"], e["sourceHandle"], e["target"], e["targetHandle"]) for e in g["edges"]}
    assert ("lightmix_cam_a_1", "image", "edit_cam_a_1", "image") in edges
    assert ("plate_cam_a_1", "packet", "lightmix_cam_a_1", "packet") in edges
    assert canvas_rules.check_nodes(g) == [] and canvas_rules.validate_graph(g) == []
    assert canvas_rules.generation_depth(g["nodes"], g["edges"], "edit_cam_a_1") == 1


def test_explicit_arguments_beat_what_the_template_edit_says():
    g = graph_build.chain_for_packet(
        {"nodes": [], "edges": []},
        summary(),
        template=templates.load("night_chain"),
        recipe="relight_same_family",
        prompt="dusk, lamps on",
    )
    assert node(g, "Edit")["params"] == {"recipe": "relight_same_family", "prompt": "dusk, lamps on"}


def test_apply_template_lays_packets_in_columns_and_ends_a_set_template_with_one_set_node():
    tpl = templates.load("set_chain")
    existing = {"nodes": [{"id": "keep", "kind": "Concept", "position": {"x": 680, "y": 0}}], "edges": []}
    applied = graph_build.apply_template(
        existing, [summary("pkt_one", "CAM_a"), summary("pkt_two", "CAM_b")], template=tpl
    )
    g = applied.graph
    assert applied.edits == ["edit_cam_a_1", "edit_cam_b_1"]
    assert applied.set_id == "set_1" and applied.plates == {
        "pkt_one": "plate_cam_a_1",
        "pkt_two": "plate_cam_b_1",
    }
    assert applied.added == [
        "plate_cam_a_1",
        "mask_cam_a_1",
        "edit_cam_a_1",
        "plate_cam_b_1",
        "mask_cam_b_1",
        "edit_cam_b_1",
        "set_1",
    ]
    x = {n["id"]: n["position"]["x"] for n in g["nodes"]}
    assert (x["plate_cam_a_1"], x["plate_cam_b_1"], x["set_1"]) == (
        3 * 340,
        4 * 340,
        5 * 340,
    )  # right of "keep"
    assert node(g, "Set")["position"]["y"] == 2 * 180
    views = [e for e in g["edges"] if e["target"] == "set_1"]
    assert {(e["source"], e["sourceHandle"], e["targetHandle"]) for e in views} == {
        ("edit_cam_a_1", "image", "views"),
        ("edit_cam_b_1", "image", "views"),
    }
    assert canvas_rules.validate_graph(g) == []
    assert graph_build.next_column({"nodes": []}) == 0
    assert graph_build.next_column(g) == 6


def test_a_template_with_no_plate_entry_still_gets_the_packet_plate_and_starts_one_row_down():
    tpl = templates.parse(
        {
            "schema": 1,
            "id": "bare",
            "title": "Bare",
            "nodes": [{"suffix": "mask", "kind": "Mask", "inputs": {"source": "$packet"}}],
        }
    )
    g = graph_build.chain_for_packet({"nodes": [], "edges": []}, summary(), template=tpl)
    assert kinds(g) == ["Plate", "Mask"]
    assert node(g, "Mask")["position"] == {"x": 0, "y": 180}
    assert node(g, "Mask").get("params") is None  # no classes, no params of its own


def test_a_ref_to_a_later_node_is_a_build_error():
    tpl = {
        "id": "x",
        "nodes": [{"suffix": "e", "kind": "Edit", "inputs": {"mask": "m"}}, {"suffix": "m", "kind": "Mask"}],
    }
    with pytest.raises(graph_build.GraphBuildError, match="earlier node"):
        graph_build.chain_for_packet({"nodes": [], "edges": []}, summary(), template=tpl)


# ---------------------------------------------------------------------------- the routes


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent, so the gates score real pixels."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        data = self.sent[result_url[len(RESULT_SCHEME) :]]
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        return bytes(cv2.imencode(".png", img)[1])


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


def _client(settings: Settings) -> TestClient:
    c = TestClient(create_app(settings, EchoEngine()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _setup(c: TestClient, auth: dict) -> tuple[str, str]:
    """Project plus room_a and room_a_v2 ingested; their packet ids."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    ids = []
    for folder in (ROOM_A, ROOM_A_V2):
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
        body = {"packet_path": str(folder), "sender": {"name": "test", "version": "0"}}
        r = c.post("/api/v0/packets", json=body, headers=hdr)
        assert r.status_code == 202, r.text
        ids.append(r.json()["packet_id"])
        assert c.get(f"/api/v0/packets/{ids[-1]}", headers=auth).json()["status"] == "ingested"
    return ids[0], ids[1]


def _apply(c: TestClient, auth: dict, **body) -> dict:
    r = c.post(f"/api/v0/projects/{PROJECT}/templates/apply", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _graph(c: TestClient, auth: dict) -> dict:
    return c.get(f"/api/v0/projects/{PROJECT}/graph", headers=auth).json()


def _view(c: TestClient, auth: dict, run_id: str) -> dict:
    return c.get(f"/api/v0/runs/{run_id}", headers=auth).json()


@needs_rooms
def test_list_names_every_template_and_a_project_adds_its_own(sync_settings, auth):
    with _client(sync_settings) as c:
        _setup(c, auth)
        r = c.get("/api/v0/templates", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json() == [
            {
                "id": "edit_chain",
                "title": "Edit chain (sky, planting, people)",
                "nodes": 5,
                "set": False,
                "origin": "shipped",
            },
            {
                "id": "night_chain",
                "title": "Night chain (dusk LightMix, change-list Edit)",
                "nodes": 5,
                "set": False,
                "origin": "shipped",
            },
            {
                "id": "set_chain",
                "title": "Set chain (every view edited, then checked together)",
                "nodes": 3,
                "set": True,
                "origin": "shipped",
            },
        ]
        folder = templates.project_templates_dir(sync_settings.projects_dir / PROJECT)
        folder.mkdir()
        doc = {"schema": 1, "id": "mine", "title": "Mine", "nodes": [{"suffix": "p", "kind": "Plate"}]}
        (folder / "mine.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
        mine = c.get(f"/api/v0/templates?project_id={PROJECT}", headers=auth).json()
        assert [t["id"] for t in mine] == ["edit_chain", "mine", "night_chain", "set_chain"]
        assert [t["id"] for t in c.get("/api/v0/templates", headers=auth).json()] == [
            "edit_chain",
            "night_chain",
            "set_chain",
        ]
        assert c.get("/api/v0/templates?project_id=absent", headers=auth).status_code == 404
        assert c.get("/api/v0/templates").status_code == 401


@needs_rooms
def test_edit_chain_on_two_packets_makes_two_columns_and_a_second_apply_reuses_the_plates(
    sync_settings, auth
):
    with _client(sync_settings) as c:
        a, v2 = _setup(c, auth)
        out = _apply(c, auth, template_id="edit_chain", packet_ids=[a, v2], quote=False)
        assert out["nodes_added"] == 10 and len(out["edits"]) == 2
        assert out["runs"] == [] and out["refused"] == [] and out["total_usd"] == 0
        g = _graph(c, auth)
        assert out["graph"] == g
        assert {k: kinds(g).count(k) for k in ("Plate", "Mask", "Edit", "Judge", "Export")} == dict.fromkeys(
            ("Plate", "Mask", "Edit", "Judge", "Export"), 2
        )
        plates = [n for n in g["nodes"] if n["kind"] == "Plate"]
        assert {n["packetId"] for n in plates} == {a, v2}
        assert len({n["position"]["x"] for n in plates}) == 2  # distinct columns
        assert [n["id"] for n in plates] == ["plate_cam_a_1", "plate_cam_a_2"]
        for n in g["nodes"]:  # every chain node sits in its packet's column
            assert n["position"]["x"] in {0, 340}
        masks = [n for n in g["nodes"] if n["kind"] == "Mask"]
        assert all(m["params"]["ids"] for m in masks)  # sky, planting, people ids of the fixture legend
        assert canvas_rules.validate_graph(g) == []

        again = _apply(c, auth, template_id="edit_chain", packet_ids=[a, v2], quote=False)
        assert again["nodes_added"] == 8  # no new Plate
        g2 = _graph(c, auth)
        assert kinds(g2).count("Plate") == 2 and kinds(g2).count("Edit") == 4
        xs = {n["position"]["x"] for n in g2["nodes"] if n["id"] in again["added"]}
        assert xs == {2 * 340, 3 * 340}  # the second apply goes to the right of the first


@needs_rooms
def test_quote_true_quotes_each_new_edit_with_the_fake_engine(sync_settings, auth):
    with _client(sync_settings) as c:
        a, v2 = _setup(c, auth)
        out = _apply(c, auth, template_id="edit_chain", packet_ids=[a, v2], engine_id="fake")
        # the fake engine quotes quotes.FAKE_PRICE_USD (0.04) a run, not nothing
        assert len(out["runs"]) == 2 and out["refused"] == []
        assert out["total_usd"] == pytest.approx(2 * FAKE_PRICE_USD)
        assert {r["state"] for r in out["runs"]} == {"QUOTED"}
        assert [r["node_id"] for r in out["runs"]] == out["edits"]
        assert {r["packet_id"] for r in out["runs"]} == {a, v2}
        edit = next(n for n in out["graph"]["nodes"] if n["id"] == out["edits"][0])
        assert edit["params"] == {"engine": "fake"}
        # the quoted run carries the mask the Mask node built
        mask = out["runs"][0]["inputs"]["mask"]
        assert mask["kind"] == "ids" and mask["ids"]


@needs_rooms
def test_auto_approve_runs_every_chain_to_done(sync_settings, auth):
    with _client(sync_settings) as c:
        a, v2 = _setup(c, auth)
        out = _apply(
            c, auth, template_id="edit_chain", packet_ids=[a, v2], engine_id="fake", auto_approve=True
        )
        assert out["awaiting_approval"] == [] and len(out["runs"]) == 2
        for r in out["runs"]:
            final = _view(c, auth, r["id"])
            assert final["state"] == "DONE", final.get("error")
            assert final["approved_by"] == "auto"


@needs_rooms
def test_a_refused_approval_leaves_the_quote_waiting_and_raises_one_alert(sync_settings, auth):
    with _client(sync_settings) as c:
        a, v2 = _setup(c, auth)
        c.app.state.pipeline.policy = Policy(per_run_auto_cap_usd=0.0001)  # no quote of a real engine fits
        out = _apply(
            c,
            auth,
            template_id="edit_chain",
            packet_ids=[a, v2],
            engine_id=NB,
            prompt="dusk",
            auto_approve=True,
        )
        assert out["refused"] == [] and len(out["runs"]) == 2
        assert {r["state"] for r in out["runs"]} == {"QUOTED"}
        assert out["total_usd"] > 0
        assert [w["error"] for w in out["awaiting_approval"]] == ["auto_cap", "auto_cap"]
        assert sum(w["usd"] for w in out["awaiting_approval"]) == pytest.approx(out["total_usd"])
        events = c.app.state.bus.read(EventFilter(PROJECT), 0)
        alerts = [
            e["data"] for e in events if e["kind"] == "alert" and e["data"].get("source") == "templates"
        ]
        assert len(alerts) == 1 and alerts[0]["level"] == "warning"
        assert alerts[0]["message"] == f"2 quotes waiting for approval: {out['total_usd']:.2f} USD"


@needs_rooms
def test_a_quote_the_pipeline_refuses_is_listed_and_does_not_fail_the_batch(sync_settings, auth):
    with _client(sync_settings) as c:
        a, v2 = _setup(c, auth)
        out = _apply(c, auth, template_id="edit_chain", packet_ids=[a, v2])  # no engine anywhere
        assert out["nodes_added"] == 10 and out["runs"] == [] and out["total_usd"] == 0
        assert [r["node"] for r in out["refused"]] == out["edits"]
        assert all(r["status"] == 422 and "engine" in r["detail"] for r in out["refused"])
        assert kinds(_graph(c, auth)).count("Edit") == 2  # the chains were stored all the same


@needs_rooms
def test_n_fans_each_quote_out_to_a_best_of_n_group(sync_settings, auth):
    with _client(sync_settings) as c:
        a, v2 = _setup(c, auth)
        out = _apply(c, auth, template_id="edit_chain", packet_ids=[a, v2], engine_id="fake", n=2)
        assert len(out["runs"]) == 4
        groups = {}
        for r in out["runs"]:
            groups.setdefault(r["sibling_group"], []).append(r["sibling_index"])
        assert len(groups) == 2 and all(sorted(v) == [0, 1] for v in groups.values()) and None not in groups


@needs_rooms
def test_night_chain_validates_through_the_graph_route(sync_settings, auth):
    with _client(sync_settings) as c:
        a, _v2 = _setup(c, auth)
        out = _apply(c, auth, template_id="night_chain", packet_ids=[a], quote=False)
        g = _graph(c, auth)
        assert out["nodes_added"] == 5 and kinds(g) == ["Plate", "LightMix", "Mask", "Edit", "Export"]
        edit = node(g, "Edit")
        assert edit["params"] == {"recipe": "night_relight_changelist"}
        into_edit = {(e["source"], e["targetHandle"]) for e in g["edges"] if e["target"] == edit["id"]}
        assert (node(g, "LightMix")["id"], "image") in into_edit
        assert canvas_rules.validate_graph(g) == [] and canvas_rules.check_nodes(g) == []
        # the stored graph is exactly what the PUT route accepts back
        r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=g, headers=auth)
        assert r.status_code == 200, r.text


@needs_rooms
def test_set_chain_adds_one_set_node_over_every_new_edit(sync_settings, auth):
    with _client(sync_settings) as c:
        a, v2 = _setup(c, auth)
        out = _apply(c, auth, template_id="set_chain", packet_ids=[a, v2], quote=False)
        g = _graph(c, auth)
        assert out["nodes_added"] == 7 and out["set_node"] == "set_1"
        assert kinds(g).count("Set") == 1 and kinds(g).count("Edit") == 2
        views = [e for e in g["edges"] if e["target"] == "set_1"]
        assert sorted(e["source"] for e in views) == sorted(out["edits"]) and {
            e["targetHandle"] for e in views
        } == {"views"}


@needs_rooms
def test_apply_refuses_what_it_cannot_lay_down(sync_settings, auth):
    with _client(sync_settings) as c:
        a, _v2 = _setup(c, auth)
        url = f"/api/v0/projects/{PROJECT}/templates/apply"
        ok = {"template_id": "edit_chain", "packet_ids": [a]}

        def post(body, project=PROJECT):
            return c.post(f"/api/v0/projects/{project}/templates/apply", json=body, headers=auth)

        r = post({**ok, "template_id": "nope"})
        assert r.status_code == 404 and r.json() == {"error": "not_found", "template_id": "nope"}
        assert post({**ok, "packet_ids": ["pkt_absent"]}).status_code == 404
        assert post({**ok, "packet_ids": []}).status_code == 422
        assert post({**ok, "auto_approve": True, "quote": False}).status_code == 422
        assert post({**ok, "n": 0}).status_code == 422
        assert post({**ok, "n": 17}).status_code == 422
        assert post({**ok, "extra": 1}).status_code == 422
        assert post(ok, project="absent").status_code == 404
        assert c.post(url, json=ok).status_code == 401
        other = c.post(
            "/api/v0/projects", json={"project_id": "other", "custody": {"nda": False}}, headers=auth
        )
        assert other.status_code == 201
        r = post(ok, project="other")  # a packet of plate-fixture is not this project's
        assert r.status_code == 422 and "belongs to project" in r.json()["errors"][0]
        assert kinds(_graph(c, auth)) == []  # nothing stored by a refused apply


@needs_rooms
def test_a_graph_the_canvas_rules_refuse_is_not_stored(sync_settings, auth):
    with _client(sync_settings) as c:
        a, _v2 = _setup(c, auth)
        folder = templates.project_templates_dir(sync_settings.projects_dir / PROJECT)
        folder.mkdir()
        # an Edit that hangs off nothing but a LightMix chain is fine; two Edits deep from the plate is three
        # generations (Edit -> Edit -> Edit), which the depth rule refuses
        doc = {
            "schema": 1,
            "id": "too_deep",
            "title": "Too deep",
            "nodes": [
                {"suffix": "plate", "kind": "Plate"},
                {"suffix": "m", "kind": "Mask", "inputs": {"source": "$packet"}},
                {"suffix": "e1", "kind": "Edit", "inputs": {"mask": "m"}},
                {"suffix": "e2", "kind": "Edit", "inputs": {"image": "e1", "mask": "m"}},
                {"suffix": "e3", "kind": "Edit", "inputs": {"image": "e2", "mask": "m"}},
            ],
        }
        (folder / "too_deep.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
        r = c.post(
            f"/api/v0/projects/{PROJECT}/templates/apply",
            json={"template_id": "too_deep", "packet_ids": [a], "quote": False},
            headers=auth,
        )
        assert r.status_code == 422, r.text
        assert {v["code"] for v in r.json()["violations"]} == {"DEPTH_EXCEEDED"}
        assert kinds(_graph(c, auth)) == []


# ---------------------------------------------------------------------------- quote_many


def _quote_many(c: TestClient, auth: dict, node: str, **body):
    return c.post(f"/api/v0/projects/{PROJECT}/nodes/{node}/quote_many", json=body, headers=auth)


@needs_rooms
def test_quote_many_prices_one_node_on_two_engines_as_one_group(sync_settings, auth):
    with _client(sync_settings) as c:
        a, _v2 = _setup(c, auth)
        edit = _apply(c, auth, template_id="edit_chain", packet_ids=[a], quote=False)["edits"][0]
        r = _quote_many(c, auth, edit, engine_ids=["fake", NB], prompt="dusk, lamps on")
        assert r.status_code == 200, r.text
        out = r.json()
        fake, nb = out["runs"]
        assert out["sibling_group"] and fake["sibling_group"] == nb["sibling_group"] == out["sibling_group"]
        assert (fake["sibling_index"], nb["sibling_index"]) == (0, 1)
        assert (fake["route"]["engine_id"], nb["route"]["engine_id"]) == ("fake", NB)
        assert fake["cost_usd_quoted"] == FAKE_PRICE_USD and nb["cost_usd_quoted"] > 0
        assert out["total_usd"] == pytest.approx(FAKE_PRICE_USD + nb["cost_usd_quoted"])
        assert {fake["state"], nb["state"]} == {"QUOTED"}
        events = c.app.state.bus.read(EventFilter(PROJECT), 0)
        made = [e["data"] for e in events if e["kind"] == "siblings.created"]
        assert made == [
            {"kind": "siblings.created", "group": out["sibling_group"], "runs": [fake["id"], nb["id"]]}
        ]

        # approving the primary approves the group, against the sum of the prices; start=false keeps it from running
        r = c.post(f"/api/v0/runs/{fake['id']}/approve", json={"by": "human", "start": False}, headers=auth)
        assert r.status_code == 200, r.text
        assert sorted(v["id"] for v in r.json()["runs"]) == sorted([fake["id"], nb["id"]])
        assert {_view(c, auth, rid)["state"] for rid in (fake["id"], nb["id"])} == {"APPROVED"}
        for rid in (fake["id"], nb["id"]):
            assert c.post(f"/api/v0/runs/{rid}/cancel", json={}, headers=auth).json()["state"] == "CANCELLED"


@needs_rooms
def test_quote_many_is_refused_for_a_bad_engine_list_and_leaves_no_half_group(sync_settings, auth):
    with _client(sync_settings) as c:
        a, _v2 = _setup(c, auth)
        edit = _apply(c, auth, template_id="edit_chain", packet_ids=[a], quote=False)["edits"][0]
        for engine_ids in (["fake"], ["fake", "fake"], ["a", "b", "c", "d", "e", "f"], []):
            assert _quote_many(c, auth, edit, engine_ids=engine_ids).status_code == 422, engine_ids
        r = _quote_many(c, auth, edit, engine_ids=["fake", NB], engine_id="fake")
        assert r.status_code == 422 and "engine_ids" in r.text
        assert _quote_many(c, auth, edit, prompt="x").status_code == 422
        assert _quote_many(c, auth, "absent_node", engine_ids=["fake", NB]).status_code == 404
        assert (
            c.post(
                f"/api/v0/projects/absent/nodes/{edit}/quote_many",
                json={"engine_ids": ["fake", NB]},
                headers=auth,
            ).status_code
            == 404
        )
        assert c.post(f"/api/v0/projects/{PROJECT}/nodes/{edit}/quote_many", json={}).status_code == 401
        assert c.get(f"/api/v0/projects/{PROJECT}/runs", headers=auth).json() == []  # nothing was left behind

        # the second engine is refused (NB needs a prompt): the first engine's quote is cancelled, not left QUOTED
        r = _quote_many(c, auth, edit, engine_ids=["fake", NB])
        assert r.status_code == 422, r.text
        assert r.json()["engine_id"] == NB and r.json()["error"] == "prompt_required"
        runs = c.get(f"/api/v0/projects/{PROJECT}/runs", headers=auth).json()
        assert [v["state"] for v in runs] == ["CANCELLED"] and runs[0]["sibling_group"] is None
