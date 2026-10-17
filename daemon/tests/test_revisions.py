"""revisions.py on the committed Blender packets `room_a` and `room_a_v2` (router plan T41).

`room_a_v2` is the same camera and scene as `room_a` with one wall recoloured (`ARCH_wall_E`), and it `supersedes`
`room_a`. A people layer (mask on `PEOPLE_dummy`) carries across; a layer on the changed wall drops with the reason
`material changed`. Around that: which cameras a change reaches (`view_hash`), the superseded note and the Plate
re-bind, the folder-first run record of a carried layer (ledger `done` with `data.carried_from`, rebuildable into the
index), and every way a layer is dropped.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from plate_daemon import lineage
from plate_daemon.cas import CAS
from plate_daemon.db import open_index
from plate_daemon.index import Index, apply_run, packet_entry
from plate_daemon.ingest import begin_ingest, finish_ingest
from plate_daemon.ledger import Ledger
from plate_daemon.machine import create_run, get_run, run_events, transition
from plate_daemon.models import RunCreate
from plate_daemon.pipeline import PacketView, decode_png16, encode_png16
from plate_daemon.projects import Project
from plate_daemon.revisions import (
    CARRY_ENGINE_ID,
    RevisionError,
    affected_cameras,
    camera_changes,
    carry_over,
    is_superseded,
    on_new_packet,
    superseded_info,
    superseded_runs,
)
from plate_daemon.states import RunState
from plate_gates import composite

ROOT = Path(__file__).resolve().parents[2]
PACKETS = ROOT / "fixtures" / "packets"
PROJECT = "plate-fixture"  # the fixture manifests' project_id
PEOPLE = "o:ebb52806"  # PEOPLE_dummy: editable, same pixels in both renders
WALL = "o:5263bdbf"  # ARCH_wall_E: the recoloured wall
WALL_MAT = "m:5263bdbf"
SHRUB = "o:55cbe981"

pytestmark = pytest.mark.skipif(
    not all((PACKETS / n / "beauty.exr").is_file() for n in ("room_a", "room_b", "room_a_v2")),
    reason="fixtures/packets/room_a, room_b, room_a_v2 not built yet (T2)",
)


def _manifest(name: str) -> dict[str, Any]:
    return json.loads((PACKETS / name / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def views() -> dict[str, PacketView]:
    """PacketViews decode the EXR once for the whole module."""
    out = {}
    for name in ("room_a", "room_b", "room_a_v2"):
        out[name] = PacketView.load(_manifest(name)["packet_id"], PACKETS / name)
    return out


class Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str | None, dict[str, Any]]] = []

    def publish(self, run_id: str | None, event: dict[str, Any], *, project_id: str | None = None) -> int:
        assert project_id == PROJECT
        self.events.append((run_id, event))
        return len(self.events)

    def kinds(self) -> list[str]:
        return [e["kind"] for _, e in self.events]


class View:
    """A PacketView with some behaviour overridden: shifted id masks, a different legend or lock list."""

    def __init__(
        self, base: PacketView, *, shift: int = 0, legend: dict | None = None, locked=None, blank=()
    ):
        self._base = base
        self._shift = shift
        self._legend = legend
        self._locked = locked
        self._blank = set(blank)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    @property
    def legend(self) -> dict | None:
        return self._legend if self._legend is not None else self._base.legend

    def locked_ids(self) -> list[str]:
        return list(self._locked) if self._locked is not None else self._base.locked_ids()

    def id_mask(self, cid: str) -> np.ndarray:
        m = self._base.id_mask(cid)
        if cid in self._blank:
            return np.zeros_like(m)
        return np.roll(m, self._shift, axis=1) if self._shift else m


@dataclass
class World:
    root: Path
    project: Project
    conn: Any
    cas: CAS
    ids: dict[str, str]
    views: dict[str, PacketView]

    def view(self, name: str) -> PacketView:
        return self.views[name]


@pytest.fixture
def world(tmp_path, views) -> World:
    root = tmp_path / "projects" / PROJECT
    root.mkdir(parents=True)
    project = Project.create(root, PROJECT, {"nda": False})
    conn = open_index(tmp_path / "index" / f"{PROJECT}.sqlite")
    ids = {}
    for name in ("room_a", "room_b", "room_a_v2"):
        status, rec = begin_ingest(conn, str(PACKETS / name), sender={"name": "test", "version": "0"})
        assert status == "ingesting"
        done = finish_ingest(conn, project.cas(), rec["packet_id"])
        assert done["status"] == "ingested", done.get("error")
        ids[name] = rec["packet_id"]
    return World(root, project, conn, project.cas(), ids, views)


def _route() -> dict[str, str]:
    return {"engine_id": "fake", "provider": "fake", "region": "local"}


def _draft(w: World, packet: str = "room_a", node_id: str | None = "e"):
    from plate_daemon.models import Route

    return create_run(
        w.conn,
        RunCreate(project_id=PROJECT, packet_id=w.ids[packet], node_id=node_id, route=Route(**_route())),
    )


def _done_run(w: World, packet: str = "room_a", node_id: str = "e", job: str = "job-1") -> str:
    """A DONE run on `packet`, put in the index the way `plate reindex` would."""
    rid = f"run_test_{job}"
    apply_run(
        w.conn,
        PROJECT,
        {
            "run_id": rid,
            "created_utc": "2026-10-17T08:00:00.000000Z",
            "updated_utc": "2026-10-17T08:05:00.000000Z",
            "state": RunState.DONE,
            "packet_id": w.ids[packet],
            "node_id": node_id,
            "priority": "interactive",
            "inputs": {},
            "params": {},
            "route": _route(),
            "attempt": 1,
            "provider_job_id": job,
        },
    )
    return rid


def _mask_png(mask: np.ndarray) -> bytes:
    return bytes(cv2.imencode(".png", np.where(mask, 255, 0).astype(np.uint8))[1])


def _layer(w: World, run_id: str, ids: list[str], *, ai_edit=None) -> tuple[dict, np.ndarray, np.ndarray]:
    """An approved layer on room_a: the editable mask of `ids`, and the pipeline's NORMALIZING output (the AI
    pixels composited into the plate inside the mask)."""
    a = w.view("room_a")
    plate = a.plate16()
    mask = np.zeros(plate.shape[:2], dtype=bool)
    for cid in ids:
        mask |= a.id_mask(cid)
    ai = plate.copy()
    ai[mask] = ai_edit(ai[mask]) if ai_edit else 65535 - ai[mask]  # an unmistakable AI edit
    out = composite.composite_masked(plate, ai, mask, feather_px=8)
    layer = {
        "run_id": run_id,
        "mask_hash": w.cas.put_bytes(_mask_png(mask), "png"),
        "ids": list(ids),
        "rgb16_hash": w.cas.put_bytes(encode_png16(out), "png"),
    }
    return layer, out, mask


def _by_run(results: list[dict]) -> dict[str, dict]:
    return {r["run_id"]: r for r in results}


# ---------------------------------------------------------------------------- which cameras a change reaches


def test_affected_cameras_lists_only_cameras_whose_view_hash_changed(world):
    a, b, v2 = (_manifest(n) for n in ("room_a", "room_b", "room_a_v2"))
    b_resent = copy.deepcopy(b)
    b_resent["packet_id"] = "pkt_RESENT_ROOM_B"
    b_resent["supersedes"] = b["packet_id"]  # same view_hash: the camera did not change
    scene = a["scene_hash"]["hex"]

    assert b["scene_hash"] == a["scene_hash"] and b["view_hash"] != a["view_hash"]
    assert affected_cameras(scene, [v2, b_resent], conn=world.conn) == ["CAM_a"]
    assert affected_cameras(scene, [v2, b_resent], old_packets=[a, b]) == ["CAM_a"]
    changes = {c.camera: c for c in camera_changes(scene, [v2, b_resent], conn=world.conn)}
    assert changes["CAM_a"].changed and changes["CAM_a"].old_packet_id == a["packet_id"]
    assert changes["CAM_a"].reason == "view_hash changed"
    assert not changes["CAM_b"].changed and "keeps its packet" in changes["CAM_b"].reason
    assert changes["CAM_b"].to_dict()["old_packet_id"] == b["packet_id"]


def test_affected_cameras_accepts_views_paths_and_index_rows(world):
    scene = _manifest("room_a")["scene_hash"]["hex"]
    row = world.conn.execute(
        "SELECT * FROM packets WHERE packet_id = ?", (world.ids["room_a_v2"],)
    ).fetchone()
    assert affected_cameras(scene, [world.view("room_a_v2")], conn=world.conn) == ["CAM_a"]
    assert affected_cameras(scene, [PACKETS / "room_a_v2"], conn=world.conn) == ["CAM_a"]
    assert affected_cameras(scene, [row], conn=world.conn) == ["CAM_a"]
    # the same packet again is not a change
    assert affected_cameras(scene, [world.view("room_b")], conn=world.conn) == []


def test_new_cameras_and_unknown_view_hashes_count_as_affected():
    a, b = _manifest("room_a"), _manifest("room_b")
    scene = a["scene_hash"]["hex"]
    cam_z = copy.deepcopy(b)
    cam_z["packet_id"], cam_z["supersedes"] = "pkt_CAM_Z", None
    cam_z["camera"]["name"] = "CAM_z"
    blind = copy.deepcopy(a)
    blind["packet_id"], blind["supersedes"] = "pkt_BLIND", a["packet_id"]
    blind["view_hash"] = None
    got = {c.camera: c for c in camera_changes(scene, [cam_z, blind], old_packets=[a, b])}
    assert got["CAM_z"].changed and got["CAM_z"].old_packet_id is None and "new camera" in got["CAM_z"].reason
    assert got["CAM_a"].changed and got["CAM_a"].reason == "view_hash unknown"
    assert affected_cameras(scene, [cam_z, blind], old_packets=[a, b]) == ["CAM_z", "CAM_a"]
    with pytest.raises(ValueError, match="old_packets= or conn="):
        affected_cameras(scene, [cam_z])


# ---------------------------------------------------------------------------- a new packet that supersedes


def test_on_new_packet_marks_the_old_packets_runs_and_rebinds_plate_nodes(world):
    old, new, other = world.ids["room_a"], world.ids["room_a_v2"], world.ids["room_b"]
    draft = _draft(world)
    quoted = _draft(world)
    transition(world.conn, quoted.id, RunState.QUOTED, actor="api")
    done = _done_run(world)
    elsewhere = _done_run(world, packet="room_b", job="job-b")
    old_plate = _manifest("room_a")["files"]["display_tiff16"]["hash"]["hex"]
    new_plate = _manifest("room_a_v2")["files"]["display_tiff16"]["hash"]["hex"]
    world.project.save_graph(
        {
            "nodes": [
                {"id": "p", "kind": "Plate", "packetId": old, "tier": "A", "outputHash": old_plate},
                {"id": "q", "data": {"kind": "Plate", "packet_id": old, "tier": "A"}},
                {"id": "pb", "kind": "Plate", "packetId": other, "tier": "A"},
                {"id": "e", "kind": "Edit", "params": {"engine": "fake"}},
            ],
            "edges": [{"id": "p->e", "source": "p", "target": "e"}],
        }
    )
    bus = Bus()

    rev = on_new_packet(world.project, world.view("room_a_v2"), conn=world.conn, bus=bus)

    assert rev.invalidated and rev.supersedes == old and rev.packet_id == new
    assert rev.reason == "view_hash changed"
    assert sorted(rev.superseded_runs) == sorted([draft.id, quoted.id, done])
    assert elsewhere not in rev.superseded_runs
    assert {n["node_id"] for n in rev.rebound_nodes} == {"p", "q"}
    # marked, not moved: the state machine has no SUPERSEDED and a DONE run stays DONE
    assert get_run(world.conn, draft.id).state is RunState.DRAFT
    assert get_run(world.conn, quoted.id).state is RunState.QUOTED
    assert get_run(world.conn, done).state is RunState.DONE
    assert sorted(superseded_runs(world.conn, old)) == sorted(rev.superseded_runs)
    assert superseded_runs(world.conn, other) == [] and not is_superseded(world.conn, elsewhere)
    info = superseded_info(world.conn, done)
    assert info["by_packet"] == new and info["by"] == "revisions"
    assert run_events(world.conn, done)[-1].kind == "superseded"
    # project.json: the Plate nodes follow the new packet (and its beauty), the other camera's does not
    nodes = {n["id"]: n for n in Project.load(world.root).graph["nodes"]}
    assert nodes["p"]["packetId"] == new and nodes["p"]["outputHash"] == new_plate
    assert nodes["q"]["data"]["packet_id"] == new
    assert nodes["pb"]["packetId"] == other and nodes["e"].get("packetId") is None
    # the bus told the UI
    kinds = bus.kinds()
    assert kinds.count("run.superseded") == 3 and kinds[-1] == "packet.superseded"
    assert bus.events[-1][1]["rebound_nodes"][0]["to"] == new

    # idempotent: a second call finds everything done already and says nothing new
    bus2 = Bus()
    again = on_new_packet(world.project, world.view("room_a_v2"), conn=world.conn, bus=bus2)
    assert again.superseded_runs == [] and sorted(again.already_superseded) == sorted(rev.superseded_runs)
    assert again.rebound_nodes == []
    assert bus2.kinds() == ["packet.superseded"]
    assert len([e for e in run_events(world.conn, done) if e.kind == "superseded"]) == 1


def test_on_new_packet_without_supersedes_or_with_the_same_view_does_nothing(world):
    run = _done_run(world)
    assert on_new_packet(world.project, world.view("room_a"), conn=world.conn).invalidated is False
    assert on_new_packet(world.project, world.view("room_a"), conn=world.conn).supersedes is None

    same_view = copy.deepcopy(_manifest("room_a_v2"))
    same_view["view_hash"] = _manifest("room_a")[
        "view_hash"
    ]  # the client change moved nothing this camera sees
    rev = on_new_packet(world.project, same_view, conn=world.conn)
    assert not rev.invalidated and "keeps its packet" in rev.reason and rev.superseded_runs == []
    assert not is_superseded(world.conn, run)

    forced = on_new_packet(world.project, same_view, conn=world.conn, force=True)
    assert forced.invalidated and forced.superseded_runs == [run]


def test_on_new_packet_when_the_old_packet_is_not_indexed(world):
    stray = copy.deepcopy(_manifest("room_a_v2"))
    stray["supersedes"] = "pkt_NEVER_SEEN"
    rev = on_new_packet(world.project, stray, conn=world.conn)
    assert rev.invalidated and rev.superseded_runs == [] and "not in the index" in rev.reason


# ---------------------------------------------------------------------------- carry-over


def test_people_layer_carries_and_the_changed_wall_drops(world):
    old_run = _done_run(world)
    wall_run = _done_run(world, job="job-wall")
    people, _, _ = _layer(world, old_run, [PEOPLE])
    wall, _, _ = _layer(world, wall_run, [WALL])

    res = carry_over(
        world.view("room_a"), world.view("room_a_v2"), [people, wall], cas=world.cas
    )  # dry: decide only

    got = _by_run(res)
    assert got[old_run]["status"] == "carried" and got[old_run]["iou"] == pytest.approx(1.0, abs=0.02)
    assert got[old_run]["gates"]["verdict"] in ("pass", "advisory")
    assert got[old_run]["new_run_id"] is None  # nothing written without project= and conn=
    assert got[wall_run]["status"] == "dropped" and got[wall_run]["reason"] == "material changed"
    assert got[wall_run]["code"] == "material_changed"
    assert (
        world.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE packet_id = ?", (world.ids["room_a_v2"],)
        ).fetchone()[0]
        == 0
    )
    assert not (world.root / "runs.jsonl").exists()


def test_a_material_id_of_the_changed_wall_drops_too(world):
    layer, _, _ = _layer(world, _done_run(world), [WALL_MAT])
    [res] = carry_over(world.view("room_a"), world.view("room_a_v2"), [layer], cas=world.cas)
    assert res["status"] == "dropped" and res["reason"] == "material changed"


def test_a_carried_layer_becomes_a_folder_first_run_on_the_new_packet(world, tmp_path):
    old_run = _done_run(world, node_id="e")
    wall_run = _done_run(world, node_id="e", job="job-wall")
    people, old_out, old_mask = _layer(world, old_run, [PEOPLE])
    wall, _, _ = _layer(world, wall_run, [WALL])
    bus = Bus()
    new_pid = world.ids["room_a_v2"]

    res = carry_over(
        world.view("room_a"),
        world.view("room_a_v2"),
        [people, wall],
        project=world.project,
        conn=world.conn,
        bus=bus,
    )

    got = _by_run(res)
    assert got[wall_run]["status"] == "dropped" and got[wall_run]["new_run_id"] is None
    carried = got[old_run]
    assert carried["status"] == "carried"
    rid = carried["new_run_id"]
    assert rid and rid != old_run

    # the index row: DONE, on the new packet, the old run's node, no vendor, no spend, no quote
    run = get_run(world.conn, rid)
    assert run.state is RunState.DONE and run.packet_id == new_pid and run.node_id == "e"
    assert run.route.engine_id == CARRY_ENGINE_ID and run.route_dict["custody_grade"] == "C0"
    assert run.provider_job_id == f"carried-{old_run}" and run.quote is None and run.cost_usd_billed is None
    assert run.inputs["carried_from"] == old_run and run.inputs["mask"]["ids"] == [PEOPLE]
    assert run.gate_report["verdict"] in ("pass", "advisory")
    assert run.gate_report_path and (world.root / run.gate_report_path).is_file()
    assert [e.kind for e in run_events(world.conn, rid)] == ["carried"]
    assert carried["output"] == run.outputs[0]

    # the pixels: the new plate bit for bit outside the mask, the approved AI pixels inside it
    new_plate = world.view("room_a_v2").plate16()
    out = decode_png16(world.cas.read_bytes(run.outputs[0]))
    mask = cv2.imdecode(np.frombuffer(world.cas.read_bytes(run.normalized["mask_key"]), np.uint8), 0) > 127
    assert out.shape == new_plate.shape and out.dtype == np.uint16
    assert np.array_equal(out[~mask], new_plate[~mask])
    assert not np.array_equal(
        new_plate[~mask], world.view("room_a").plate16()[~mask]
    )  # the plates did differ
    core = cv2.erode(mask.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=12).astype(bool)
    assert core.sum() > 1000 and np.array_equal(out[core], old_out[core])
    # the people did not move: the new mask is the approved one less the pixels a locked id also covers
    assert not (mask & ~old_mask).any() and (mask & old_mask).sum() / old_mask.sum() > 0.98

    # the quick gates, versioned in gates/
    report = json.loads((world.root / run.gate_report_path).read_text(encoding="utf-8"))
    status = {c["metric_id"]: c["status"] for c in report["checks"]}
    assert status == {
        "registration.residual_px": "pass",
        "registration.shift_px": "pass",
        "outside_mask.de00": "pass",
    }
    assert report["engine"]["id"] == CARRY_ENGINE_ID and "quick gates only" in report["backend_notes"][0]

    # lineage is inside the PNG, so a dropped file resolves to the carried run
    lin = lineage.read(world.cas.find(run.outputs[0]))
    assert lin is not None and lin.run_id == rid and lin.packet_id == new_pid and lin.node_id == "e"
    assert lin.engine_id == CARRY_ENGINE_ID and lin.gate_report == run.gate_report_path

    # the folder is the truth: a `done` record with data.carried_from, chain intact, no generation counted
    led = Ledger(world.root)
    assert led.verify() == (True, None)
    recs = [r for r in led.records() if r["run_id"] == rid]
    assert [r["kind"] for r in recs] == ["done"]
    assert (
        recs[0]["data"]["carried_from"] == old_run
        and recs[0]["data"]["carried_from_packet"] == world.ids["room_a"]
    )
    assert recs[0]["engine_id"] == CARRY_ENGINE_ID and recs[0]["provider_job_id"] == f"carried-{old_run}"
    assert led.summarize()["generations"] == 0 and led.summarize()["spent_usd_billed"] == 0

    # the bus: one run.carried and one layer.dropped
    assert sorted(bus.kinds()) == ["layer.dropped", "run.carried"]
    assert next(e for _, e in bus.events if e["kind"] == "layer.dropped")["reason"] == "material changed"

    # carrying it again is the same run, not a second one
    n_before = len(led.records())
    [again] = carry_over(
        world.view("room_a"), world.view("room_a_v2"), [people], project=world.project, conn=world.conn
    )
    assert again["status"] == "carried" and again["new_run_id"] == rid and again["code"] == "already_carried"
    assert len(Ledger(world.root).records()) == n_before

    # and a rebuilt index (another machine, from the folder alone) holds the same run
    world.project.register_packet(new_pid, packet_entry(world.conn, new_pid))
    other = Index(tmp_path / "other-machine")
    out_counts = other.rebuild_from_project(world.root)
    assert out_counts["problems"] == []
    with other.project(PROJECT) as c2:
        again_run = get_run(c2, rid)
    assert again_run.state is RunState.DONE and again_run.packet_id == new_pid
    assert again_run.route.engine_id == CARRY_ENGINE_ID and again_run.node_id == "e"
    assert again_run.outputs == run.outputs and again_run.provider_job_id == run.provider_job_id


def test_a_dropped_layer_writes_nothing(world):
    wall, _, _ = _layer(world, _done_run(world), [WALL])
    [res] = carry_over(
        world.view("room_a"), world.view("room_a_v2"), [wall], project=world.project, conn=world.conn
    )
    assert res["status"] == "dropped"
    assert world.conn.execute("SELECT COUNT(*) FROM runs WHERE id != ?", (wall["run_id"],)).fetchone()[0] == 0
    assert not (world.root / "runs.jsonl").exists() and not (world.root / "gates").exists()


def test_a_layer_may_touch_several_ids_and_one_changed_id_drops_it(world):
    layer, _, _ = _layer(world, _done_run(world), [PEOPLE, SHRUB])
    [ok] = carry_over(world.view("room_a"), world.view("room_a_v2"), [layer], cas=world.cas)
    assert ok["status"] == "carried"

    layer2 = {**layer, "ids": [PEOPLE, WALL]}
    [no] = carry_over(world.view("room_a"), world.view("room_a_v2"), [layer2], cas=world.cas)
    assert no["status"] == "dropped" and no["reason"] == "material changed"
    assert [c["id"] for c in no["changed_ids"]] == [WALL]


# ---------------------------------------------------------------------------- every way a layer is dropped


def _legend(view: PacketView, edit) -> dict:
    legend = copy.deepcopy(view.legend)
    edit({e["id"]: e for e in legend["entries"]}, legend)
    return legend


def _drop_reason(world, layer, new_view) -> dict:
    [res] = carry_over(world.view("room_a"), new_view, [layer], cas=world.cas)
    assert res["status"] == "dropped", res
    return res


def test_each_legend_difference_names_its_reason(world):
    layer, _, _ = _layer(world, _done_run(world), [PEOPLE])
    base = world.view("room_a")  # compare room_a with edited copies of itself
    cases = {
        "legend_changed": lambda e, _l: e[PEOPLE].update(name="PEOPLE_renamed"),
        "legend_changed ": lambda e, _l: e[PEOPLE].update(**{"class": "vegetation"}),
        "legend_changed  ": lambda e, _l: e[PEOPLE].update(material="PEOPLE_other"),
        "object_changed": lambda e, _l: e[PEOPLE].update(digest="0" * 64),
        "material_changed": lambda e, _l: e["m:ebb52806"].update(digest="1" * 64),
        "id_removed": lambda _e, doc: doc["entries"].remove(
            next(x for x in doc["entries"] if x["id"] == PEOPLE)
        ),
    }
    for want, edit in cases.items():
        res = _drop_reason(world, layer, View(base, legend=_legend(base, edit)))
        assert res["code"] == want.strip(), (want, res)
    # a changed digest on the object whose own material changed is a material change, not an object change
    both = _legend(
        base, lambda e, _l: (e[PEOPLE].update(digest="2" * 64), e["m:ebb52806"].update(digest="3" * 64))
    )
    assert _drop_reason(world, layer, View(base, legend=both))["reason"] == "material changed"
    # the layer's id absent from the OLD legend is a malformed approval
    assert _drop_reason(world, {**layer, "ids": ["o:deadbeef"]}, base)["code"] == "id_missing"
    # an unchanged legend carries
    [ok] = carry_over(base, View(base), [layer], cas=world.cas)
    assert ok["status"] == "carried"


def test_an_id_the_new_packet_locks_is_dropped(world):
    layer, _, _ = _layer(world, _done_run(world), [PEOPLE])
    new = View(world.view("room_a_v2"), locked=[*world.view("room_a_v2").locked_ids(), PEOPLE])
    res = _drop_reason(world, layer, new)
    assert res["code"] == "id_locked" and res["reason"] == "id locked in the new packet"


def test_a_moved_id_mask_is_dropped_below_the_iou_bound(world):
    layer, _, _ = _layer(world, _done_run(world), [PEOPLE])
    res = _drop_reason(world, layer, View(world.view("room_a_v2"), shift=8))
    assert res["code"] == "mask_moved" and res["reason"] == "id mask moved" and res["iou"] < 0.98
    # the bound is a knob: the same move carries under a looser one, onto the overlap of the two masks
    [loose] = carry_over(
        world.view("room_a"), View(world.view("room_a_v2"), shift=8), [layer], cas=world.cas, iou_min=0.5
    )
    assert loose["status"] == "carried" and loose["iou"] < 0.98


def test_pixels_that_cannot_be_read_or_do_not_fit_are_dropped(world):
    run = _done_run(world)
    layer, _, _ = _layer(world, run, [PEOPLE])
    new = world.view("room_a_v2")
    old = world.view("room_a")

    gone = {**layer, "rgb16_hash": "f" * 64}
    assert carry_over(old, new, [gone], cas=world.cas)[0]["code"] == "pixels_missing"
    gone_mask = {**layer, "mask_hash": "e" * 64}
    assert carry_over(old, new, [gone_mask], cas=world.cas)[0]["code"] == "pixels_missing"

    small = world.cas.put_bytes(encode_png16(np.zeros((90, 160, 3), np.uint16)), "png")
    assert carry_over(old, new, [{**layer, "rgb16_hash": small}], cas=world.cas)[0]["code"] == "size_mismatch"
    small_mask = world.cas.put_bytes(_mask_png(np.ones((90, 160), bool)), "png")
    assert (
        carry_over(old, new, [{**layer, "mask_hash": small_mask}], cas=world.cas)[0]["code"]
        == "size_mismatch"
    )

    blank = world.cas.put_bytes(_mask_png(np.zeros((360, 640), bool)), "png")
    assert carry_over(old, new, [{**layer, "mask_hash": blank}], cas=world.cas)[0]["code"] == "mask_empty"


def test_malformed_layers_are_dropped_not_raised(world):
    layer, _, _ = _layer(world, _done_run(world), [PEOPLE])
    bad = [
        {**layer, "ids": []},
        {**layer, "ids": "o:1"},
        {**layer, "run_id": ""},
        {**layer, "mask_hash": "nothex"},
        {**layer, "rgb16_hash": None},
        {**layer, "feather_px": -1},
        "not a layer",
    ]
    res = carry_over(world.view("room_a"), world.view("room_a_v2"), bad, cas=world.cas)
    assert [r["code"] for r in res] == ["layer_invalid"] * len(bad)
    assert all(r["status"] == "dropped" and r["reason"] == "layer record is malformed" for r in res)
    assert carry_over(world.view("room_a"), world.view("room_a_v2"), [], cas=world.cas) == []


def test_a_missing_id_mask_is_dropped(world):
    layer, _, _ = _layer(world, _done_run(world), [PEOPLE])

    # a locked-id lookup that raises is skipped, but an id of the layer itself that cannot be decoded drops it
    class NoCrypto(View):
        def id_mask(self, cid):
            from plate_daemon.pipeline import PipelineError

            raise PipelineError(f"{cid}: packet has no Cryptomatte")

    res = _drop_reason(world, layer, NoCrypto(world.view("room_a_v2")))
    assert res["code"] == "mask_unavailable" and "new" in res["detail"]


def test_quick_gates_drop_a_layer_whose_pixels_are_not_on_the_new_plate(world):
    people, out, _ = _layer(world, _done_run(world), [PEOPLE])
    shifted = np.roll(out, 14, axis=1)  # the stored pixels sit 14 px off the new plate
    layer = {**people, "rgb16_hash": world.cas.put_bytes(encode_png16(shifted), "png")}
    res = _drop_reason(world, layer, world.view("room_a_v2"))
    assert res["code"] == "gates_failed" and "registration.shift_px" in res["detail"]
    assert res["gates"]["checks"]["registration.shift_px"] == "fail"


def test_quick_gates_drop_a_composite_that_leaks_outside_the_mask(world, monkeypatch):
    people, _, _ = _layer(world, _done_run(world), [PEOPLE])

    def leaky(plate, ai, mask, **kw):
        out = plate.copy()
        out[0, 0] = np.where(out[0, 0] > 30000, 0, 65535)  # one pixel outside the mask changed
        out[mask] = ai[mask]
        return out

    monkeypatch.setattr(composite, "composite_masked", leaky)
    res = _drop_reason(world, people, world.view("room_a_v2"))
    assert res["code"] == "gates_failed" and "outside_mask.de00" in res["detail"]


def test_a_layer_must_belong_to_the_old_packet_when_persisting(world):
    people, _, _ = _layer(world, _done_run(world, packet="room_b", job="job-b"), [PEOPLE])
    [res] = carry_over(
        world.view("room_a"), world.view("room_a_v2"), [people], project=world.project, conn=world.conn
    )
    assert res["code"] == "run_elsewhere" and res["status"] == "dropped"
    [unknown] = carry_over(
        world.view("room_a"),
        world.view("room_a_v2"),
        [{**people, "run_id": "run_nope"}],
        project=world.project,
        conn=world.conn,
    )
    assert unknown["code"] == "run_unknown"


def test_misuse_is_an_error_not_a_verdict(world):
    old, new = world.view("room_a"), world.view("room_a_v2")
    with pytest.raises(RevisionError, match="cas="):
        carry_over(old, new, [])
    with pytest.raises(RevisionError, match="go together"):
        carry_over(old, new, [], project=world.project)
    stray = View(new)
    stray.packet_id = "pkt_NOT_INDEXED"
    with pytest.raises(RevisionError, match="not in the project index"):
        carry_over(old, stray, [], project=world.project, conn=world.conn)
    with pytest.raises(RevisionError, match="packet_id"):
        on_new_packet(world.project, {"supersedes": "x"}, conn=world.conn)
    with pytest.raises(RevisionError, match="manifest.json"):
        on_new_packet(world.project, world.root / "nowhere", conn=world.conn)
