"""The numeric and plugin nodes in the daemon (router plan T48): Set, Recolour, LightMix, Light, Scout.

Every node here is driven the way the daemon drives an Edit: quote (price 0, local C0), approve (`auto`), queue,
then the worker's transitions to DONE or GATE_FAIL, with a versioned gate report and a PNG16 in the CAS. The
fixtures are the committed Blender packets: `room_a` and `room_b` (one scene, two cameras: Set), `room_a`
(a panel at RAL 7016, with raw lighting, GI and reflection passes: Recolour; `room_a_ral3020` is the same camera
re-rendered with the panel at RAL 3020: the check), `room_a_v2` (the same camera with one material changed,
`supersedes` room_a: the Light node's receipt). LightMix runs on synthetic light selects written next to a copy
of room_a as `x_light_*` extension files. The Max dock is played by plain HTTP calls to `/plugin/jobs`.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import canvas_rules
from plate_daemon.api import create_app
from plate_daemon.cas import hash_file
from plate_daemon.config import Settings
from plate_daemon.idempotency import ulid
from plate_daemon.machine import get_run
from plate_daemon.nodes import (
    ACTIONS,
    KINDS,
    NUMERIC_ENGINE_ID,
    PLUGIN_ENGINE_ID,
    NodeRunner,
    PluginQueue,
    install,
    is_node_run,
)
from plate_daemon.nodes import routes as node_routes
from plate_daemon.nodes.plugin_queue import JobStateError, UnknownAction
from plate_daemon.pipeline import PipelineError, decode_png16, encode_png16
from plate_daemon.sse import EventFilter

ROOT = Path(__file__).resolve().parents[2]
PACKETS = ROOT / "fixtures" / "packets"
ROOM_A, ROOM_B, ROOM_A_V2, ROOM_A_RAL = (
    PACKETS / n for n in ("room_a", "room_b", "room_a_v2", "room_a_ral3020")
)
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"
PANEL = "m:735a8655"  # ARCH_panel_7016 (locked by the packet policy)
WALL = "m:57b3d007"  # ARCH_wall_N (locked)
PEOPLE = "m:ebb52806"  # PEOPLE_dummy (editable)

pytestmark = pytest.mark.skipif(
    not all(
        (p / "manifest.json").is_file() and (p / "beauty.exr").is_file() for p in (ROOM_A, ROOM_B, ROOM_A_V2)
    ),
    reason="fixtures/packets not built yet (T2)",
)

STATES = [
    "QUOTED",
    "APPROVED",
    "QUEUED",
    "STAGING",
    "SUBMITTED",
    "RUNNING",
    "FETCHING",
    "NORMALIZING",
    "GATING",
]


# ---------------------------------------------------------------------------- the plugin queue on its own


def test_plugin_queue_lifecycle(tmp_path):
    q = PluginQueue(tmp_path)
    a = q.enqueue(
        action="light.match",
        project_id="p",
        packet_id="pkt_a",
        payload={"camera": "CAM", "deep": False},
        key="k1",
    )
    again = q.enqueue(action="light.match", project_id="p", payload={}, key="k1")
    assert again.id == a.id and again.seq == a.seq  # the same key is the same job
    b = q.enqueue(action="scout", project_id="p", payload={"count": 3})
    assert (a.state, b.state) == ("queued", "queued") and b.seq > a.seq

    wire = a.to_dict()
    assert wire["camera"] == "CAM" and wire["action"] == "light.match" and wire["packet_id"] == "pkt_a"

    assert [j.id for j in q.list_jobs(0)] == [a.id, b.id]
    assert [j.id for j in q.list_jobs(a.seq)] == [b.id]  # the cursor skips what the dock has seen
    claimed = q.list_jobs(0, claim=True, limit=1)
    assert [j.state for j in claimed] == ["claimed"] and claimed[0].claimed_utc
    assert [j.id for j in q.list_jobs(0)] == [b.id]  # a claimed job is not offered twice
    assert {j.id for j in q.list_jobs(0, states=("queued", "claimed"))} == {a.id, b.id}

    done = q.finish(a.id, ok=True, result={"metrics": {"de00": 1.5}})
    assert done.state == "done" and done.result == {"metrics": {"de00": 1.5}} and done.finished_utc
    with pytest.raises(JobStateError):
        q.finish(a.id, ok=True)  # one answer per job
    cancelled = q.cancel(b.id, reason="run cancelled")
    assert cancelled.state == "cancelled"
    with pytest.raises(JobStateError):
        q.cancel(b.id)
    with pytest.raises(JobStateError):
        q.finish("pj_nope")
    assert q.get("pj_nope") is None and q.by_key("k1").id == a.id
    with pytest.raises(UnknownAction):
        q.enqueue(action="format.disk", project_id="p")
    with pytest.raises(ValueError):
        q.enqueue(action="scout", project_id="p", payload={"x": float("nan")})
    with pytest.raises(ValueError):
        q.enqueue(action="scout", project_id="p", payload={"blob": "x" * 70_000})
    live = q.enqueue(action="scout", project_id="p")
    with pytest.raises(ValueError):
        q.finish(live.id, result={"poses": "x" * 1_100_000})
    assert q.get(live.id).state == "queued"  # a refused answer leaves the job open
    assert set(ACTIONS) == {"light.match", "light.vantage_preview", "scout"}


# ---------------------------------------------------------------------------- the daemon around the nodes


@dataclass
class Env:
    client: TestClient
    auth: dict[str, str]
    settings: Settings
    pipeline: Any
    runner: NodeRunner

    # ---- setup
    def project(self, *, nda: bool = False) -> None:
        r = self.client.post(
            "/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=self.auth
        )
        assert r.status_code == 201, r.text

    def packet(self, path: Path) -> str:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        hdr = {**self.auth, "X-Plate-Packet-Version": manifest["packet_version"]}
        body = {"packet_path": str(path), "sender": {"name": "test", "version": "0"}}
        r = self.client.post("/api/v0/packets", json=body, headers=hdr)
        assert r.status_code in (200, 202), r.text
        packet_id = r.json()["packet_id"]
        status = self.client.get(f"/api/v0/packets/{packet_id}", headers=self.auth).json()["status"]
        assert status == "ingested", status
        return packet_id

    def graph(self, nodes: list[dict[str, Any]], edges: list[tuple[str, str]]) -> None:
        body = {"nodes": nodes, "edges": [{"id": f"{a}->{b}", "source": a, "target": b} for a, b in edges]}
        r = self.client.put(f"/api/v0/projects/{PROJECT}/graph", json=body, headers=self.auth)
        assert r.status_code == 200, r.text

    def one_plate(self, kind: str, params: dict[str, Any], path: Path = ROOM_A) -> str:
        """project + packet + graph `Plate -> <kind>` (node id `n`); returns the packet id."""
        self.project()
        packet_id = self.packet(path)
        self.graph(
            [
                {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
                {"id": "n", "kind": kind, "label": kind, "params": params},
            ],
            [("p", "n")],
        )
        return packet_id

    # ---- running
    @property
    def cas(self):
        return self.pipeline.cas_for(PROJECT)

    def make(self, node_id: str = "n", body: dict[str, Any] | None = None):
        with self.pipeline.index.project(PROJECT) as conn:
            return self.runner.make(conn, PROJECT, node_id, body)

    def advance(self, run_id: str):
        with self.pipeline.index.project(PROJECT) as conn:
            return self.runner.advance(conn, PROJECT, run_id)

    def run(self, run_id: str):
        with self.pipeline.index.project(PROJECT) as conn:
            return get_run(conn, run_id)

    def report(self, run_id: str) -> dict[str, Any]:
        """The stored gate report as `GET /runs/{id}/gate` serves it (the run's own copy has its verdict folded
        to pass or fail by the worker; the file keeps `advisory`)."""
        r = self.client.get(f"/api/v0/runs/{run_id}/gate", headers=self.auth)
        assert r.status_code == 200, r.text
        return r.json()

    def moves(self, run_id: str) -> list[str]:
        recs = self.client.app.state.bus.read(EventFilter(PROJECT, run_id), 0)
        return [r["data"]["to"] for r in recs if r["kind"] == "run.state"]

    def png(self, key: str) -> np.ndarray:
        return decode_png16(self.cas.read_bytes(key))

    def put_png(self, rgb16: np.ndarray) -> str:
        return self.cas.put_bytes(encode_png16(rgb16), "png")

    def dock(self, claim: bool = True) -> list[dict[str, Any]]:
        r = self.client.get(f"/api/v0/plugin/jobs?since=0&claim={int(claim)}", headers=self.auth)
        assert r.status_code == 200, r.text
        return r.json()["jobs"]

    def answer(self, job_id: str, **body: Any):
        return self.client.post(f"/api/v0/plugin/jobs/{job_id}/result", json=body, headers=self.auth)


@pytest.fixture
def env(plate_env, monkeypatch) -> Env:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    app = create_app(settings)
    with TestClient(app, base_url=BASE) as client:
        pipeline = client.app.state.pipeline
        pipeline.sleep = lambda _s: None
        runner = install(pipeline)
        client.app.include_router(node_routes.router)
        yield Env(client, {"Authorization": f"Bearer {settings.token}"}, settings, pipeline, runner)


def _display(path: Path) -> np.ndarray:
    from plate_pixels import imageio

    return np.rint(imageio.read(path / "display.tif").rgb * 65535).astype(np.uint16)


def _grade(view_dir: Path, rgb16: np.ndarray, offsets: dict[str, list[float]]) -> np.ndarray:
    """The picture with each material's Lab mean moved by `offsets` ([dL, da, db], within the +-6 a/b clip)."""
    from plate_gates import crossview, setgate

    return setgate.numeric_correction(rgb16, crossview.PacketView.load(view_dir), {}, offsets=offsets)


def _editable_cover(view_dir: Path) -> np.ndarray:
    """Coverage (0..1) of the editable materials of a packet: 0 where a Set may not have changed a pixel."""
    from plate_gates import crossview

    view = crossview.PacketView.load(view_dir)
    cover = np.zeros(view.shape, np.float32)
    for mid in view.material_entries():
        if mid in view.editable_ids:
            cover = np.maximum(cover, view.material_share(mid))
    return cover


def _check(report: dict[str, Any], metric_id: str) -> dict[str, Any]:
    return next(c for c in report["checks"] if c["metric_id"] == metric_id)


def test_install_is_idempotent_and_kinds_are_the_five(env):
    assert install(env.pipeline) is env.runner and env.pipeline.node_runner is env.runner
    assert KINDS == {"Set", "Recolour", "LightMix", "Light", "Scout"}
    assert canvas_rules.NODE_KINDS and KINDS <= set(canvas_rules.NODE_KINDS)


# ---------------------------------------------------------------------------- Set


def _set_graph(env: Env, params: dict[str, Any] | None = None) -> tuple[str, str]:
    env.project()
    a, b = env.packet(ROOM_A), env.packet(ROOM_B)
    env.graph(
        [
            {"id": "pa", "kind": "Plate", "packetId": a, "tier": "A", "label": "A"},
            {"id": "pb", "kind": "Plate", "packetId": b, "tier": "A", "label": "B"},
            {"id": "n", "kind": "Set", "label": "Set", "params": params or {}},
        ],
        [("pa", "n"), ("pb", "n")],
    )
    return a, b


def test_set_on_room_a_and_room_b_is_consistent(env):
    a, b = _set_graph(env)
    run = env.make()
    assert run.state.value == "DONE", run.error
    assert is_node_run(run) and run.route.engine_id == NUMERIC_ENGINE_ID
    assert run.packet_id == a  # anchored to the hero
    assert run.quote.price_usd == 0.0 and run.quote.custody_grade == "C0" and run.approved_by == "auto"
    assert env.moves(run.id) == [*STATES, "DONE"]

    report = run.gate_report
    assert report["verdict"] in ("pass", "advisory") and report["tier"] == "A"
    cross = _check(report, "set.cross_view_de00_p90")
    assert cross["status"] == "pass" and cross["value"] <= 3.0
    assert report["metrics"]["cross_view_p90"] <= 3.0 and report["metrics"]["n_corr"] >= 500
    assert _check(report, "set.outside_corrected_px") == {
        "metric_id": "set.outside_corrected_px",
        "value": 0.0,
        "status": "pass",
    }
    assert (
        report["report_path"].startswith("gates/")
        and (env.pipeline.project_dir(PROJECT) / report["report_path"]).is_file()
    )
    assert report["engine"]["id"] == NUMERIC_ENGINE_ID

    # One output per other camera, 16 bit, the camera's own size. Only editable materials were corrected, so
    # every pixel with no coverage of an editable id is the plate; the locked interiors did not move.
    assert len(run.outputs) == 1
    out, plate = env.png(run.outputs[0]), _display(ROOM_B)
    assert out.shape == plate.shape == (360, 640, 3) and out.dtype == np.uint16
    from plate_gates import crossview

    view = crossview.PacketView.load(ROOM_B)
    editable = _editable_cover(ROOM_B)
    assert np.array_equal(out[editable <= 0], plate[editable <= 0])
    assert set(run.normalized["views"][0]["corrected_ids"]) <= {
        m for m in view.material_entries() if m in view.editable_ids
    }
    assert report["metrics"]["views"][0]["stray_px"] == 0
    assert run.normalized["views"][0]["packet_id"] == b


def test_set_corrects_a_graded_editable_material(env):
    """A view whose people are +6 b* off the hero: the Set node measures that at the matched points and takes it out."""
    _a, b = _set_graph(env)
    graded = _grade(ROOM_B, _display(ROOM_B), {PEOPLE: [0.0, 0.0, 6.0]})
    key = env.put_png(graded)
    run = env.make(body={"params": {"images": {b: key}}})
    assert run.state.value == "DONE", run.error
    before = run.normalized["before"]["per_material"][PEOPLE]
    assert before["scored"] and before["de00_p90"] > 3.5  # the shift is plainly there
    assert PEOPLE in run.normalized["views"][0]["corrected_ids"]
    after = run.gate_report["metrics"]["per_material"][PEOPLE]
    assert after["de00_p90"] < 2.0 and after["de00_p90"] < before["de00_p90"] / 2
    out = env.png(run.outputs[0])
    assert not np.array_equal(out, graded)  # something moved
    assert run.normalized["views"][0]["changed_px"] > 0


def test_set_leaves_locked_materials_alone_unless_told(env):
    """The wall (locked) is 6 b* off in camera B. By default a Set will not regrade a locked material: the gate
    fails honestly and says why. With include_locked the artist asks for it, and the Set corrects it."""
    _a, b = _set_graph(env)
    graded = _grade(ROOM_B, _display(ROOM_B), {WALL: [0.0, 0.0, 6.0]})
    key = env.put_png(graded)

    refused = env.make(body={"params": {"images": {b: key}}})
    assert refused.state.value == "GATE_FAIL", refused.error
    rep = refused.gate_report
    assert (
        _check(rep, "set.cross_view_de00_p90")["status"] == "fail" and rep["metrics"]["cross_view_p90"] > 3.0
    )
    assert _check(rep, "set.outside_corrected_px")["status"] == "pass"
    assert WALL not in refused.normalized["views"][0]["corrected_ids"]  # the wall is locked: left alone
    assert WALL in refused.normalized["views"][0]["skipped_locked"]
    untouched = (
        _editable_cover(ROOM_B) <= 0
    )  # everything but the editable materials is the input, wall included
    assert np.array_equal(env.png(refused.outputs[0])[untouched], graded[untouched])
    assert any("include_locked" in n for n in rep["backend_notes"])

    told = env.make(body={"params": {"images": {b: key}, "include_locked": True}})
    assert told.state.value == "DONE", told.error
    assert WALL in told.normalized["views"][0]["corrected_ids"]
    assert told.gate_report["metrics"]["cross_view_p90"] <= 3.0
    assert any("include_locked" in n for n in told.gate_report["backend_notes"])
    assert _check(told.gate_report, "set.outside_corrected_px")["status"] == "pass"


def test_set_needs_two_cameras_and_tier_a(env):
    env.one_plate("Set", {})
    with pytest.raises(PipelineError) as e:
        env.make()
    assert e.value.code == "set_needs_views"
    with pytest.raises(PipelineError) as e:
        env.make(body={"params": {"views": ["pkt_unknown"]}})
    assert e.value.code == "unknown_packet"


def test_set_checks_the_pictures_it_is_given(env):
    _a, b = _set_graph(env)
    small = env.put_png(np.full((90, 160, 3), 20000, np.uint16))
    with pytest.raises(PipelineError) as e:
        env.make(body={"params": {"images": {b: small}}})
    assert e.value.code == "size_mismatch"
    with pytest.raises(PipelineError) as e:
        env.make(body={"params": {"images": {b: "0" * 64}}})
    assert e.value.code == "asset_missing"
    with pytest.raises(PipelineError) as e:
        env.make(body={"params": {"images": {"pkt_elsewhere": small}}})
    assert e.value.code == "bad_params"
    with pytest.raises(PipelineError) as e:
        env.make(body={"params": {"samples": 5}})
    assert e.value.code == "bad_params"


def test_set_through_the_daemon_routes(env):
    """The generic routes (quote, approve, gate) drive a Set run once the runner is installed."""
    _set_graph(env)
    r = env.client.post(f"/api/v0/projects/{PROJECT}/nodes/n/quote", json={}, headers=env.auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    assert r.json()["run"]["state"] == "QUOTED" and r.json()["quote"]["price_usd"] == 0.0
    r = env.client.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=env.auth)
    assert r.status_code == 200, r.text
    run = env.client.get(f"/api/v0/runs/{run_id}", headers=env.auth).json()
    assert run["state"] == "DONE", run.get("error")
    gate = env.client.get(f"/api/v0/runs/{run_id}/gate", headers=env.auth)
    assert gate.status_code == 200 and gate.json()["verdict"] in ("pass", "advisory")
    assert gate.json()["metrics"]["cross_view_p90"] <= 3.0
    # An Edit node still goes the Edit way (the wrapper falls through).
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": run["packet_id"], "tier": "A", "label": "P"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
        ],
        [("p", "e")],
    )
    r = env.client.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=env.auth)
    assert r.status_code == 200 and not is_node_run(env.run(r.json()["run"]["id"]))


def test_a_free_local_run_takes_any_valid_approver_and_no_budget_decision(env):
    env.one_plate("Recolour", {"material": PANEL, "target": "RAL 3020"})
    for by in ("auto", "human", "agent:env_1"):
        with env.pipeline.index.project(PROJECT) as conn:
            run, quote = env.runner.quote(conn, PROJECT, "n", {})
            assert quote.price_usd == 0.0 and run.state.value == "QUOTED"
            approved = env.pipeline.approve(
                conn, PROJECT, run.id, by
            )  # the installed wrapper: no envelope needed
            assert [r.state.value for r in approved] == ["APPROVED"] and approved[0].approved_by == by
            env.pipeline.queue(conn, PROJECT, run.id)
            assert env.pipeline.execute(run.id, PROJECT).value == "DONE"
    with env.pipeline.index.project(PROJECT) as conn:
        run, _ = env.runner.quote(conn, PROJECT, "n", {})
        with pytest.raises(ValueError):
            env.pipeline.approve(conn, PROJECT, run.id, "robot")
        assert get_run(conn, run.id).state.value == "QUOTED"  # a refused approval changes nothing


def test_a_node_run_twice_is_two_runs_not_a_duplicate(env):
    env.one_plate("Recolour", {"material": PANEL, "target": "RAL 3020"})
    first, second = env.make(), env.make()
    assert (first.state.value, second.state.value) == ("DONE", "DONE")
    assert (first.attempt, second.attempt) == (1, 2) and first.id != second.id
    assert first.outputs == second.outputs  # deterministic work: the same bytes, stored once


# ---------------------------------------------------------------------------- Recolour


def test_recolour_a_ral_panel_matches_the_rerender(env):
    from plate_gates import recolour

    env.one_plate("Recolour", {"material": PANEL, "target": "RAL 3020", "materialName": "ARCH_panel_7016"})
    run = env.make()
    assert run.state.value == "DONE", run.error
    assert env.moves(run.id) == [*STATES, "DONE"]
    info = run.normalized["recolour"]
    assert info["method"] == "recomposition" and info["expected_de00"] <= 1.0 and not info["degraded"]
    assert info["n_px"] > 1000 and info["target_lab"][0] > 30  # RAL 3020 is a traffic red
    rep = env.report(run.id)
    assert rep["verdict"] == "pass"
    assert _check(rep, "recolour.expected_de00")["status"] == "pass"
    assert rep["metrics"]["outside_mask"] == {"identical_outside": True, "changed_px": 0}
    assert any("locked" in n for n in rep["backend_notes"])  # the panel is a locked id: said, not hidden

    # Against the same camera re-rendered with the panel at RAL 3020: dE00 median <= the method's 1.0.
    out = env.png(run.outputs[0]).astype(np.float32) / 257.0
    ref = recolour.load_packet(ROOM_A_RAL, elements=["beauty"])
    src = recolour.load_packet(ROOM_A, elements=["beauty"])
    from plate_pixels import exr

    ranks, cov = exr.read_cryptomatte(src["exr_path"], src["manifest"], "material")
    mask = np.where(ranks == np.uint32(int(PANEL[2:], 16)), cov, 0).sum(axis=2) >= 0.5
    cmp = recolour.compare_display(out, ref["display8"], mask)
    assert cmp["de00_median"] <= 1.0, cmp

    # Outside the panel the output is the plate bit for bit.
    plate = _display(ROOM_A)
    soft = np.where(ranks == np.uint32(int(PANEL[2:], 16)), cov, 0).sum(axis=2) > 0
    assert np.array_equal(env.png(run.outputs[0])[~soft], plate[~soft])


def test_recolour_without_raw_passes_is_degraded_and_advisory(env):
    """room_b carries no raw lighting, GI or reflection: only the degraded path exists, and the report says so."""
    env.one_plate("Recolour", {"material": WALL, "target": "#CBD0CC"}, path=ROOM_B)
    run = env.make()
    assert run.state.value == "DONE", run.error  # advisory is not a failure
    info = run.normalized["recolour"]
    assert info["method"] == "degraded" and info["degraded"] and info["expected_de00"] == 4.0
    rep = env.report(run.id)
    assert rep["verdict"] == "advisory" and _check(rep, "recolour.expected_de00")["status"] == "advisory"
    assert _check(rep, "outside_mask.changed_px")["status"] == "pass"


def test_recolour_refuses_what_it_cannot_do(env):
    env.one_plate("Recolour", {})  # nothing saved on the node: every case below is the request's own
    cases = [
        ({"material": "m:deadbeef", "target": "RAL 3020"}, "unknown_id"),
        ({"material": "ARCH_panel", "target": "RAL 3020"}, "bad_params"),
        ({"material": PANEL}, "bad_params"),
        ({"material": PANEL, "target": "puce"}, "bad_target"),
        ({"material": PANEL, "target": "RAL 9999"}, "unknown_ral"),
        ({"material": PANEL, "target": "RAL 3020", "method": "magic"}, "bad_params"),
    ]
    for params, code in cases:
        with pytest.raises(PipelineError) as e:
            env.make(body={"params": params})
        assert e.value.code == code, (params, e.value)
    # The forms the UI writes: hex, Lab, a bare RAL number.
    for target in ("#383E42", [38.0, 1.0, -1.0], {"lab": [38.0, 1.0, -1.0]}, "7016"):
        run = env.make(body={"params": {"material": PANEL, "target": target}})
        assert run.state.value == "DONE", (target, run.error)


def test_nodes_run_on_an_nda_project(env):
    """Local C0 work is allowed under any custody, NDA included: nothing leaves the box."""
    env.project(nda=True)
    packet_id = env.packet(ROOM_A)
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "P"},
            {
                "id": "n",
                "kind": "Recolour",
                "label": "R",
                "params": {"material": PANEL, "target": "RAL 3020"},
            },
        ],
        [("p", "n")],
    )
    run = env.make()
    assert run.state.value == "DONE", run.error
    assert run.quote.custody_grade == "C0"


# ---------------------------------------------------------------------------- LightMix on x_light_* roles


def _variant_with_lights(tmp_path: Path, names_and_images: dict[str, np.ndarray]) -> tuple[Path, str]:
    """A copy of room_a under a new packet id with each image written as an `x_light_<name>` EXR file."""
    import plate_spec
    from plate_pixels import exr

    dest = tmp_path / "room_a_lights"
    shutil.copytree(ROOM_A, dest)
    new_id = f"pkt_{ulid()}"
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    manifest["packet_id"] = new_id
    legend = json.loads((dest / "id_legend.json").read_text(encoding="utf-8"))
    legend["packet_id"] = new_id
    (dest / "id_legend.json").write_text(json.dumps(legend, indent=1) + "\n", encoding="utf-8", newline="\n")
    digest, size = hash_file(dest / "id_legend.json")
    manifest["files"]["id_legend"]["hash"]["hex"], manifest["files"]["id_legend"]["bytes"] = digest, size
    h, w = next(iter(names_and_images.values())).shape[:2]
    for name, img in names_and_images.items():
        path = dest / f"light_{name}.exr"
        exr.write_multilayer(path, {"RGBA": {"R": img[..., 0], "G": img[..., 1], "B": img[..., 2]}})
        digest, size = hash_file(path)
        manifest["files"][f"x_light_{name}"] = {
            "kind": "file",
            "path": path.name,
            "bytes": size,
            "hash": {"algo": "blake3", "hex": digest},
            "format": "exr",
            "width": w,
            "height": h,
            "bit_depth": 32,
        }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    assert plate_spec.validate_packet_dir(dest, check_files=True) == []
    return dest, new_id


def _lights(h: int = 360, w: int = 640) -> dict[str, np.ndarray]:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    u, v = xx / (w - 1), yy / (h - 1)
    sun = np.stack([0.30 * u, 0.20 * u * v + 0.02, 0.10 * (1 - v)], -1)
    sky = np.stack([0.08 * (1 - u), 0.12 * v + 0.02, 0.22 * (1 - u) * v + 0.03], -1)
    lamp = np.stack(
        [0.30 * np.exp(-((u - 0.2) ** 2 + (v - 0.7) ** 2) * 8), 0.2 * (1 - u) * (1 - v), 0.05 * v], -1
    )
    return {
        "sun": sun.astype(np.float32),
        "sky": sky.astype(np.float32),
        "interior_lamp": lamp.astype(np.float32),
    }


def _lightmix_env(env: Env, tmp_path: Path, params: dict[str, Any]) -> tuple[Path, dict[str, np.ndarray]]:
    lights = _lights()
    packet_dir, _ = _variant_with_lights(tmp_path, lights)
    env.project()
    packet_id = env.packet(packet_dir)
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "P"},
            {"id": "n", "kind": "LightMix", "label": "LightMix", "params": params},
        ],
        [("p", "n")],
    )
    return packet_dir, lights


def test_lightmix_solves_known_gains_from_a_target_picture(env, tmp_path):
    from plate_pixels.ocio import Pipeline

    packet_dir, lights = _lightmix_env(env, tmp_path, {})
    truth = {"sun": 0.5, "sky": 2.0, "interior_lamp": 0.0}
    manifest = json.loads((packet_dir / "manifest.json").read_text(encoding="utf-8"))
    mix = sum(g * lights[n].astype(np.float64) for n, g in truth.items())
    target_display = Pipeline.from_manifest(manifest).rederive_display(mix.astype(np.float32))
    target_png = np.rint(np.clip(target_display, 0, 255) * 257.0).astype(np.uint16)
    key = env.put_png(target_png)

    run = env.make(body={"params": {"target_hash": key}})
    assert run.state.value == "DONE", run.error
    info = run.normalized["lightmix"]
    assert info["mode"] == "solve" and info["lights"] == ["interior_lamp", "sky", "sun"]
    for name, g in truth.items():
        assert info["gains"][name] == pytest.approx(g, abs=0.02), name
    assert info["residual_rms"] < 0.01 and info["clipped"] == []
    assert min(info["gains"].values()) >= 0.0  # a negative light does not exist

    out = env.png(run.outputs[0]).astype(np.float64)
    assert out.shape == target_png.shape
    assert (
        np.abs(out - target_png.astype(np.float64)).mean() / 257.0 < 0.25
    )  # within a quarter of an 8-bit code
    rep = env.report(run.id)
    assert rep["verdict"] == "pass"
    assert (
        _check(rep, "lightmix.gains_nonneg")["status"] == "pass"
        and _check(rep, "lightmix.residual_rms")["status"] == "pass"
    )


def test_lightmix_explicit_gains_and_presets(env, tmp_path):
    _lightmix_env(env, tmp_path, {})
    unity = env.make()
    assert unity.normalized["lightmix"]["mode"] == "unity"
    assert set(unity.normalized["lightmix"]["gains"].values()) == {1.0}

    explicit = env.make(body={"params": {"gains": {"sun": 0.25, "sky": 3.0}}})
    g = explicit.normalized["lightmix"]["gains"]
    assert explicit.normalized["lightmix"]["mode"] == "explicit"
    assert g == {"interior_lamp": 1.0, "sky": 3.0, "sun": 0.25}  # a light with no entry keeps gain 1.0
    assert explicit.outputs != unity.outputs

    dusk = env.make(body={"params": {"preset": "dusk"}})
    g = dusk.normalized["lightmix"]["gains"]
    assert dusk.normalized["lightmix"]["mode"] == "preset"
    assert g == {"interior_lamp": 3.0, "sky": 0.6, "sun": 0.15}

    capped = env.make(body={"params": {"gains": {"sun": 50.0}, "max_gain": 4.0}})
    assert capped.normalized["lightmix"]["gains"]["sun"] == 4.0 and capped.normalized["lightmix"][
        "clipped"
    ] == ["sun"]
    assert (
        capped.state.value == "DONE" and env.report(capped.id)["verdict"] == "advisory"
    )  # clipped is a number to look at

    for params, code in (
        ({"gains": {"moon": 1.0}}, "bad_params"),
        ({"gains": {"sun": -1.0}}, "bad_params"),
        ({"preset": "noon"}, "bad_params"),
        ({"max_gain": 0}, "bad_params"),
        ({"target_hash": "0" * 64}, "asset_missing"),
    ):
        with pytest.raises(PipelineError) as e:
            env.make(body={"params": params})
        assert e.value.code == code, params


def test_lightmix_keep_base_is_the_identity_at_unity_gains(env, tmp_path):
    """sum_i L_i + (beauty - sum_i L_i) is the beauty: with `keep_base` and every gain 1.0 the node returns the
    packet's own picture, and a target is matched after the base is taken off."""
    _lightmix_env(env, tmp_path, {"keep_base": True})
    run = env.make()
    assert run.state.value == "DONE", run.error
    info = run.normalized["lightmix"]
    assert info["keep_base"] is True and info["mode"] == "unity" and info["sum_vs_beauty_rms"] > 0.01
    out = env.png(run.outputs[0]).astype(np.float64) / 257.0
    plate = _display(ROOM_A).astype(np.float64) / 257.0
    assert np.abs(out - plate).mean() < 0.5  # the packet's master is itself rederived to within a code or so

    # A target is matched after the base is taken off. (The master clips the beauty's highlights at 1.0, so the
    # residual is a number to read, not zero.)
    key = env.put_png(_display(ROOM_A))
    solved = env.make(body={"params": {"target_hash": key}})
    assert solved.state.value == "DONE", solved.error
    assert solved.normalized["lightmix"]["mode"] == "solve"
    assert 0.0 <= solved.normalized["lightmix"]["residual_rms"] < 0.5


def test_lightmix_needs_light_selects(env):
    env.one_plate("LightMix", {})  # room_a carries no light_select elements and no x_light files
    with pytest.raises(PipelineError) as e:
        env.make()
    assert e.value.code == "no_light_selects"


# ---------------------------------------------------------------------------- the node preview route


def test_preview_numbers_for_the_editors(env, tmp_path):
    _set_graph(env)
    url = f"/api/v0/projects/{PROJECT}/nodes/n/preview"
    r = env.client.post(url, json={"what": "set", "params": {}}, headers=env.auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cross_view_p90"] <= 3.0 and body["verdict"] in ("pass", "advisory")
    assert PANEL in body["targets"] and len(body["per_view"]) == 1 and body["per_material"]
    assert env.client.post(url, json={"what": "router", "params": {}}, headers=env.auth).status_code == 404
    assert env.client.post(url, json={"what": "set"}).status_code == 401

    # Recolour: the method and the error to expect, without a picture (the node's first upstream plate is room_a).
    r = env.client.post(
        url, json={"what": "recolour", "params": {"material": PANEL, "ral": "3020"}}, headers=env.auth
    )
    assert r.status_code == 200, r.text
    assert (
        r.json()["method"] == "recomposition"
        and r.json()["expected_de00"] == 1.0
        and r.json()["metal"] is False
    )
    r = env.client.post(
        url, json={"what": "recolour", "params": {"material": PANEL, "hex": "#383E42"}}, headers=env.auth
    )
    assert r.status_code == 200 and r.json()["target"]["kind"] == "hex"
    r = env.client.post(
        url, json={"what": "recolour", "params": {"material": "m:00000000", "ral": "3020"}}, headers=env.auth
    )
    assert r.status_code == 422 and r.json()["error"] == "unknown_id"


def test_preview_lightmix_learns_the_lights(env, tmp_path):
    _lightmix_env(env, tmp_path, {})
    url = f"/api/v0/projects/{PROJECT}/nodes/n/preview"
    r = env.client.post(url, json={"what": "lightmix", "params": {}}, headers=env.auth)
    assert r.status_code == 200, r.text
    assert r.json()["lights"] == ["interior_lamp", "sky", "sun"] and r.json()["gains"] == {
        "interior_lamp": 1.0,
        "sky": 1.0,
        "sun": 1.0,
    }
    assert r.json()["residual_rms"] is None and r.json()["clipped"] == []
    key = env.put_png(np.full((360, 640, 3), 20000, np.uint16))
    r = env.client.post(url, json={"what": "lightmix", "params": {"target_hash": key}}, headers=env.auth)
    assert r.status_code == 200 and r.json()["residual_rms"] is not None


# ---------------------------------------------------------------------------- Light and Scout: the Max dock


def _light_graph(env: Env, **params: Any) -> tuple[str, str]:
    env.project()
    packet_id = env.packet(ROOM_A)
    reference = env.put_png(np.full((90, 160, 3), 30000, np.uint16))
    env.graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "P"},
            {
                "id": "n",
                "kind": "Light",
                "label": "Light",
                "params": {"referenceHash": reference, "locks": ["wb", "ev"], "deep": False, **params},
            },
        ],
        [("p", "n")],
    )
    return packet_id, reference


def test_light_enqueues_a_job_and_completes_when_the_superseding_packet_arrives(env):
    packet_id, reference = _light_graph(env)
    run = env.make()
    assert run.state.value == "SUBMITTED", run.error  # waiting for the dock
    assert is_node_run(run) and run.route.engine_id == PLUGIN_ENGINE_ID
    assert run.provider_job_id.startswith("pj_")

    jobs = env.dock(claim=False)
    assert len(jobs) == 1 and jobs[0]["id"] == run.provider_job_id and jobs[0]["state"] == "queued"
    job = jobs[0]
    assert job["action"] == "light.match" and job["run_id"] == run.id and job["node_id"] == "n"
    assert job["packet_id"] == packet_id and job["reference_hash"] == reference
    assert job["locks"] == ["ev", "wb"] and job["deep"] is False and job["camera"] == "CAM_a"
    assert env.advance(run.id).state.value == "SUBMITTED"  # nobody has picked it up

    claimed = env.dock(claim=True)
    assert [j["state"] for j in claimed] == ["claimed"] and env.dock() == []  # offered once
    assert env.advance(run.id).state.value == "RUNNING"

    # The dock finishes, but the re-rendered packet has not arrived: the run keeps waiting.
    r = env.answer(job["id"], ok=True, metrics={"de00": 1.4})
    assert r.status_code == 200 and r.json()["state"] == "done"
    assert env.advance(run.id).state.value == "RUNNING"
    assert env.answer(job["id"], ok=True).status_code == 409  # one answer per job

    # The superseding packet arrives (the dock's Send): the run completes.
    new_id = env.packet(ROOM_A_V2)
    done = env.advance(run.id)
    assert done.state.value == "DONE", done.error
    assert env.moves(run.id) == [*STATES, "DONE"]
    assert done.normalized["packet_id"] == new_id and done.normalized["supersedes"] == packet_id
    assert done.normalized["plugin_metrics"] == {"de00": 1.4}
    rep = env.report(done.id)
    assert rep["verdict"] == "pass"
    for metric in ("light.supersedes", "light.same_camera", "light.same_size"):
        assert _check(rep, metric)["status"] == "pass", metric
    assert rep["metrics"]["light"]["new_packet"] == new_id
    assert np.array_equal(env.png(done.outputs[0]), _display(ROOM_A_V2))


def test_light_ignores_a_superseding_packet_that_was_already_there(env):
    """A packet that superseded the source before the job was queued is not the dock's answer."""
    _light_graph(env)
    env.packet(ROOM_A_V2)
    run = env.make()
    job = env.dock()[0]
    env.answer(job["id"], ok=True)
    waiting = env.advance(run.id)
    assert waiting.state.value == "RUNNING" and waiting.state.value != "DONE"
    assert env.advance(run.id).state.value == "RUNNING"  # it keeps waiting for a packet from after the job


def test_light_packet_may_arrive_before_the_docks_answer(env):
    """The dock Sends the re-render, then answers the job: the run waits for the answer, then completes."""
    _light_graph(env)
    run = env.make()
    job = env.dock()[0]
    new_id = env.packet(ROOM_A_V2)
    assert env.advance(run.id).state.value == "RUNNING"  # claimed, packet here, no answer yet
    env.answer(job["id"], ok=True, packet_id=new_id)
    done = env.advance(run.id)
    assert done.state.value == "DONE", done.error
    assert done.normalized["packet_id"] == new_id


def test_light_failures_are_the_docks_to_report(env):
    _light_graph(env)
    run = env.make()
    job = env.dock()[0]
    assert env.answer(job["id"], ok=False, detail="MAXGAFFER_MISSING").status_code == 200
    failed = env.advance(run.id)
    assert failed.state.value == "FAILED" and failed.reason_code == "provider_failed"
    assert "MAXGAFFER_MISSING" in failed.error


def test_light_named_packet_that_moved_the_camera_fails_the_gate(env):
    """The dock names a packet that does not supersede the source and looks from another camera: GATE_FAIL."""
    _light_graph(env)
    run = env.make()
    job = env.dock()[0]
    other = env.packet(ROOM_B)
    env.answer(job["id"], ok=True, packet_id=other)
    done = env.advance(run.id)
    assert done.state.value == "GATE_FAIL", done.error
    rep = done.gate_report
    assert _check(rep, "light.supersedes")["status"] == "fail"
    assert _check(rep, "light.same_camera")["status"] == "fail"
    assert any("camera" in n for n in rep["backend_notes"])


def test_light_cancel_stops_the_job(env):
    _light_graph(env)
    run = env.make()
    job_id = run.provider_job_id
    with env.pipeline.index.project(PROJECT) as conn:
        cancelled = env.pipeline.cancel(conn, PROJECT, run.id, reason="not needed")
    assert cancelled.state.value == "CANCELLED"
    assert env.runner.queue.get(job_id).state == "cancelled"
    assert env.dock() == []  # the dock is never asked for it


def test_light_quote_checks_its_inputs(env):
    _light_graph(env)
    for params, code in (
        ({"referenceHash": "f" * 64}, "asset_missing"),
        ({"reference_hash": "not a hash"}, "asset_missing"),
        ({"locks": "wb"}, "bad_params"),
        ({"locks": [1, 2]}, "bad_params"),
    ):
        with pytest.raises(PipelineError) as e:
            env.make(body={"params": params})
        assert e.value.code == code, params


def test_a_plugin_run_resumes_after_a_restart(env):
    """The run's state is in the index and the job in the queue file: a new daemon finishes what the old one left."""
    packet_id, _ = _light_graph(env)
    run = env.make()
    job = env.dock()[0]
    env.answer(job["id"], ok=True)
    new_id = env.packet(ROOM_A_V2)
    assert env.run(run.id).state.value == "SUBMITTED"

    # A second daemon over the same app-data folder. Its pipeline is built and wired before anything runs (the
    # lifespan's boot-time `resume` would otherwise race the wiring), then it resumes the run the first left.
    pipeline2 = create_app(env.settings).state.pipeline
    pipeline2.sleep = lambda _s: None
    install(pipeline2)
    assert pipeline2.resume() == [run.id]
    with pipeline2.index.project(PROJECT) as conn:
        final = get_run(conn, run.id)
    assert final.state.value == "DONE", final.error
    assert final.normalized["packet_id"] == new_id and final.packet_id == packet_id


def test_scout_queues_a_job_and_keeps_the_poses(env):
    env.one_plate("Scout", {"count": 3, "keep": True})
    run = env.make()
    assert run.state.value == "SUBMITTED"
    job = env.dock()[0]
    assert job["action"] == "scout" and job["count"] == 3 and job["keep"] is True and job["scene_hash"]
    poses = [
        {"name": f"__PLATE_scout_{i}", "position": [i, 0.0, 1.5], "target": [0, 3, 1.2]} for i in range(1, 4)
    ]
    assert env.answer(job["id"], ok=True, poses=poses, previews=[], packets=[]).status_code == 200
    done = env.advance(run.id)
    assert done.state.value == "DONE", done.error
    info = done.normalized["scout"]
    assert (
        info["requested"] == 3 and info["returned"] == 3 and info["poses"] == poses and info["kept"] is True
    )
    assert env.report(done.id)["verdict"] == "pass"

    # Fewer poses than asked is a number to look at; none is a failure.
    short = env.make()
    env.answer(env.dock()[0]["id"], ok=True, poses=poses[:1])
    short = env.advance(short.id)
    assert short.state.value == "DONE" and env.report(short.id)["verdict"] == "advisory"
    none = env.make()
    env.answer(env.dock()[0]["id"], ok=True, poses=[])
    none = env.advance(none.id)
    assert (
        none.state.value == "GATE_FAIL"
        and _check(none.gate_report, "scout.poses_returned")["status"] == "fail"
    )

    for count in (0, 25, "many"):
        with pytest.raises(PipelineError) as e:
            env.make(body={"params": {"count": count}})
        assert e.value.code == "bad_params", count


def test_scout_stores_only_previews_that_exist(env):
    env.one_plate("Scout", {"count": 2})
    run = env.make()
    job = env.dock()[0]
    preview = env.put_png(np.full((72, 128, 3), 12000, np.uint16))
    env.answer(
        job["id"], ok=True, poses=[{"name": "a"}, {"name": "b"}], previews=[preview, "e" * 64, "not a key"]
    )
    done = env.advance(run.id)
    assert done.state.value == "DONE" and done.outputs == [preview]


# ---------------------------------------------------------------------------- /plugin/jobs over HTTP


def test_plugin_job_routes(env):
    packet_id = env.one_plate("Recolour", {"material": PANEL, "target": "RAL 3020"})
    url = "/api/v0/plugin/jobs"
    assert env.client.get(url).status_code == 401
    assert env.client.post(url, json={"action": "scout", "packet_id": packet_id}).status_code == 401

    r = env.client.post(
        url,
        json={
            "action": "light.match",
            "packet_id": packet_id,
            "camera": "CAM_a",
            "reference_hash": "a" * 64,
            "locks": ["wb"],
            "deep": True,
        },
        headers=env.auth,
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["state"] == "queued" and created["id"].startswith("pj_")
    vantage = env.client.post(
        url, json={"action": "light.vantage_preview", "packet_id": packet_id}, headers=env.auth
    )
    assert vantage.status_code == 201

    bad = env.client.post(url, json={"action": "format.disk", "packet_id": packet_id}, headers=env.auth)
    assert bad.status_code == 422 and bad.json()["error"] == "unknown_action"
    assert (
        env.client.post(url, json={"action": "scout", "packet_id": "pkt_nope"}, headers=env.auth).status_code
        == 404
    )
    assert env.client.post(url, json={"action": "scout"}, headers=env.auth).status_code == 422

    listed = env.client.get(f"{url}?since=0", headers=env.auth).json()
    assert [j["action"] for j in listed["jobs"]] == ["light.match", "light.vantage_preview"]
    first = listed["jobs"][0]
    assert (
        first["deep"] is True
        and first["locks"] == ["wb"]
        and first["project_id"] == PROJECT
        and first["run_id"] is None
    )
    assert listed["next"] == listed["jobs"][-1]["seq"]
    after = env.client.get(f"{url}?since={listed['next']}", headers=env.auth).json()
    assert after == {"jobs": [], "next": listed["next"]}  # the cursor holds when nothing is new
    assert env.client.get(f"{url}?state=sleeping", headers=env.auth).status_code == 422

    one = env.client.get(f"{url}/{created['id']}", headers=env.auth)
    assert one.status_code == 200 and one.json()["id"] == created["id"]
    assert env.client.get(f"{url}/pj_nope", headers=env.auth).status_code == 404

    r = env.answer(created["id"], ok=True, packet_id="pkt_x", metrics={"de00": 2.0})
    assert r.status_code == 200 and r.json()["state"] == "done"
    assert r.json()["result"] == {"ok": True, "packet_id": "pkt_x", "metrics": {"de00": 2.0}}
    assert env.answer(created["id"], ok=True).status_code == 409
    assert env.answer("pj_nope", ok=True).status_code == 404
    assert [
        j["action"] for j in env.client.get(f"{url}?since=0&claim=1", headers=env.auth).json()["jobs"]
    ] == ["light.vantage_preview"]
