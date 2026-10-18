"""Wave 12 stress fixes at the quote and the approval (router plan T129; decisions 72 and 80), on `room_a`.

Each test encodes an attacker's repro from the stress run:

- base laundering (A): an upload (a softened plate) set as a Colour node's `outputHash` became an Edit's base;
  the gate's union took in every pixel the base changed, so the take passed with 95 % of the frame "editable".
  A base is now a take of its node: 422 `base_not_a_take`;
- GATE_FAIL laundering (B): a GATE_FAIL import picked as a base gave a clean DONE child. Now 422 `base_gate_fail`
  until the import is overridden, and the child records the base's state and override reason (inputs and gate
  report);
- depth bypass (C): a depth-2 take relayed through a Colour node (or a rewired graph) gave a third generation.
  The lineage of `inputs.base_run` is counted at the quote: 422 `DEPTH_EXCEEDED`;
- the money caps: attempts numbered under a per-node lock, an attempt ceiling, priority and param validation
  before a run row, a quote that fails half-way leaves no DRAFT, the daily cap counts every project in the home,
  an expired (or future) quote is ledgered `expired`, envelopes are bounded.

The engine is the echoing FakeEngine (`Echo`), or `Darken`, which edits only inside the mask it is sent, so a
take differs from its base where it should and nowhere else.
"""

from __future__ import annotations

import base64
import copy
import json
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest
from blake3 import blake3
from fastapi.testclient import TestClient

from plate_daemon import budgets, canvas_rules
from plate_daemon import pipeline as pipeline_mod
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.pipeline import ATTEMPT_MAX, Pipeline, PipelineError, decode_png16
from plate_daemon.quotes import build_quote, create_envelope
from plate_daemon.states import Priority
from plate_daemon.timeutil import utcnow

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
H, W = 360, 640
SKY, SHRUB = "o:929c9e95", "o:55cbe981"  # editable on room_a
OVERRIDE = "client approved the softened look on the 18 Oct call"
# A local engine that takes a mask, at any size (what `Darken` stands in for).
TAKES_A_MASK = {
    "sizes": {"auto": True},
    "inputs": [
        {"name": "image", "type": "image", "required": True},
        {"name": "mask", "type": "mask", "required": False},
    ],
}

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


class Echo(FakeEngine):
    """The FakeEngine handing back the PNG it was sent."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("echo", True)
        super().__init__(*args, **kwargs)


class Darken(Echo):
    """Echo, but darkens the sent image inside the mask it was sent (the manifest declares a mask input)."""

    manifest = TAKES_A_MASK

    def submit(self, payload, idempotency_key):
        if "mask" in payload["inputs"]:
            payload = copy.deepcopy(payload)
            img = _decode(payload["inputs"]["image"]["b64"], cv2.IMREAD_UNCHANGED)
            sel = _decode(payload["inputs"]["mask"]["b64"], cv2.IMREAD_GRAYSCALE) > 127
            img[sel] = np.rint(img[sel] * 0.8).astype(img.dtype)
            payload["inputs"]["image"]["b64"] = base64.b64encode(cv2.imencode(".png", img)[1]).decode("ascii")
        return super().submit(payload, idempotency_key)


# ---------------------------------------------------------------------------- set-up


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    return Settings.load()


def _client(settings: Settings, engine=None) -> TestClient:
    c = TestClient(create_app(settings, engine or Echo()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _decode(b64: str, flags: int) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), flags)


def _ingest(c: TestClient, auth: dict, packet: Path) -> str:
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    r = c.post(
        "/api/v0/packets",
        json={"packet_path": str(packet), "sender": {"name": "test", "version": "0"}},
        headers=hdr,
    )
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
    return packet_id


def _setup(c: TestClient, auth: dict, project: str = PROJECT, packet: Path = ROOM_A) -> str:
    r = c.post("/api/v0/projects", json={"project_id": project, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    return _ingest(c, auth, packet)


def _stage_copy(settings: Settings, project: str, packet_id: str) -> Path:
    """room_a under another project and packet id (the legend names the packet, so its hash is redone)."""
    dest = settings.packets_dirs[0] / f"room_a_{project}"
    shutil.copytree(ROOM_A, dest)
    m = json.loads((dest / "manifest.json").read_text(encoding="utf-8-sig"))
    old, m["project_id"], m["packet_id"] = m["packet_id"], project, packet_id
    legend = dest / "id_legend.json"
    data = legend.read_bytes().replace(old.encode(), packet_id.encode())
    legend.write_bytes(data)
    entry = m["files"]["id_legend"]
    entry["bytes"], entry["hash"]["hex"] = len(data), blake3(data).hexdigest()
    (dest / "manifest.json").write_text(json.dumps(m, indent=1) + "\n", encoding="utf-8", newline="\n")
    return dest


def _put_graph(c: TestClient, auth: dict, nodes: list[dict], edges: list[dict], project: str = PROJECT):
    r = c.put(f"/api/v0/projects/{project}/graph", json={"nodes": nodes, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text


def _plate(packet_id: str) -> dict:
    return {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}


def _edit(nid: str, engine: str = "fake", **extra) -> dict:
    return {"id": nid, "kind": "Edit", "label": nid, "params": {"engine": engine}, **extra}


def _mask(nid: str = "m", ids: list | None = None, **extra) -> dict:
    node = {"id": nid, "kind": "Mask", "label": "Mask", "params": {"ids": [SHRUB] if ids is None else ids}}
    node.update(extra)
    return node


def _edge(source: str, target: str, handle: str | None = None) -> dict:
    e = {"id": f"{source}->{target}", "source": source, "target": target}
    if handle is not None:
        e["targetHandle"] = handle
    return e


def _quote_http(c: TestClient, auth: dict, node: str, project: str = PROJECT, **body):
    return c.post(f"/api/v0/projects/{project}/nodes/{node}/quote", json=body, headers=auth)


def _quote(c: TestClient, body: dict, node: str, project: str = PROJECT):
    with c.app.state.index.project(project) as conn:
        return c.app.state.pipeline.quote_node(conn, project, node, body)


def _refusal(c: TestClient, body: dict, node: str) -> PipelineError:
    with pytest.raises(PipelineError) as e:
        _quote(c, body, node)
    return e.value


def _count(c: TestClient, where: str = "1 = 1", project: str = PROJECT) -> int:
    with c.app.state.index.project(project) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM runs WHERE {where}").fetchone()[0]


def _make(c: TestClient, auth: dict, node: str, **body) -> dict:
    """Quote, approve as a human and run a node; the finished run."""
    r = _quote_http(c, auth, node, **body)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth)
    assert r.status_code == 200, r.text
    return c.get(f"/api/v0/runs/{run_id}", headers=auth).json()


def _gate(c: TestClient, auth: dict, run_id: str) -> dict:
    r = c.get(f"/api/v0/runs/{run_id}/gate", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _cas(settings: Settings, project: str = PROJECT) -> CAS:
    return CAS(settings.projects_dir / project / "assets")


def _grey(cas: CAS, key: str) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(cas.read_bytes(key), np.uint8), cv2.IMREAD_GRAYSCALE) > 127


def _plate16() -> np.ndarray:
    from plate_pixels import imageio

    return np.rint(imageio.read(ROOM_A / "display.tif").rgb * 65535).astype(np.uint16)


def _softened16() -> np.ndarray:
    soft = cv2.GaussianBlur(_plate16().astype(np.float64) / 65535.0, (0, 0), 1.2)
    return np.clip(np.rint(soft * 65535.0), 0, 65535).astype(np.uint16)


def _records(settings: Settings, run_id: str, project: str = PROJECT) -> list[dict]:
    return [r for r in Ledger(settings.projects_dir / project).records() if r.get("run_id") == run_id]


# ---------------------------------------------------------------------------- (1) a base is a take of its node


def test_repro_a_an_uploaded_reference_relayed_as_a_base_is_not_a_take(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        soft8 = np.clip(np.rint(_softened16() / 257.0), 0, 255).astype(np.uint8)[..., ::-1]
        r = c.post(
            f"/api/v0/assets?kind=reference&project_id={PROJECT}",
            content=bytes(cv2.imencode(".png", np.ascontiguousarray(soft8))[1]),
            headers={**auth, "Content-Type": "image/png"},
        )
        assert r.status_code == 201, r.text
        ref = r.json()["hash"]
        colour = {"id": "col", "kind": "Colour", "label": "Colour", "outputHash": ref}
        nodes = [_plate(packet_id), _mask(), colour, _edit("a")]
        edges = [_edge("p", "m"), _edge("p", "col"), _edge("col", "a", "image"), _edge("m", "a", "mask")]
        _put_graph(c, auth, nodes, edges)

        r = _quote_http(c, auth, "a", engine_id="fake")
        assert r.status_code == 422, r.text
        assert r.json()["error"] == "base_not_a_take" and r.json()["node"] == "col"
        assert "import it first" in r.json()["detail"]
        assert _count(c) == 0


# ---------------------------------------------------------------------------- (2) a GATE_FAIL base needs its override


def test_repro_b_a_gate_fail_import_is_a_base_only_after_its_override(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        nodes = [_plate(packet_id), _mask(), _edit("picked"), _edit("b")]
        edges = [_edge("p", "m"), _edge("m", "picked", "mask"), _edge("picked", "b", "image")]
        edges.append(_edge("m", "b", "mask"))
        _put_graph(c, auth, nodes, edges)
        soft = tmp_path / "softened.png"
        assert cv2.imwrite(str(soft), np.ascontiguousarray(_softened16()[..., ::-1]))
        r = c.post(
            f"/api/v0/projects/{PROJECT}/import",
            json={"path": str(soft), "packet_id": packet_id, "node_id": "picked", "source": "manual"},
            headers=auth,
        )
        assert r.status_code == 201, r.text
        imported = r.json()
        assert imported["state"] == "GATE_FAIL" and imported["exportable"] is False

        nodes[2] = {**nodes[2], "outputHash": imported["outputs"][0]}  # the artist picked the failed take
        _put_graph(c, auth, nodes, edges)
        r = _quote_http(c, auth, "b", engine_id="fake")
        assert r.status_code == 422, r.text
        assert r.json()["error"] == "base_gate_fail" and r.json()["run_id"] == imported["id"]
        assert "override it with a reason before building on it" in r.json()["detail"]
        assert _count(c, "node_id = 'b'") == 0

        r = c.post(f"/api/v0/runs/{imported['id']}/override", json={"reason": OVERRIDE}, headers=auth)
        assert r.status_code == 200, r.text
        r = _quote_http(c, auth, "b", engine_id="fake")
        assert r.status_code == 200, r.text
        inputs = r.json()["run"]["inputs"]
        assert inputs["base"] == imported["outputs"][0] and inputs["base_run"] == imported["id"]
        assert (inputs["base_state"], inputs["base_override_reason"], inputs["base_depth"]) == (
            "GATE_FAIL",
            OVERRIDE,
            1,  # an import counts as one generation
        )
        run_id = r.json()["run"]["id"]
        r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth)
        assert r.status_code == 200, r.text
        child = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        # The override was the import's, not the child's: outside the union the child carries the softened pixels,
        # so its own gate fails honestly (it needs its own override to ship). Nothing is laundered.
        assert child["state"] == "GATE_FAIL", child.get("error")
        report = _gate(c, auth, run_id)
        assert report["base"] == {
            "run_id": imported["id"],
            "state": "GATE_FAIL",
            "override_reason": OVERRIDE,
            "depth": 1,
        }
        assert report["metrics"]["outside_mask"]["identical_outside"] is False
        union = _grey(_cas(sync_settings), child["normalized"]["mask_union_key"])
        assert np.array_equal(
            union,
            _grey(_cas(sync_settings), child["normalized"]["mask_key"])
            | _grey(_cas(sync_settings), imported["normalized"]["mask_key"]),
        )  # the import's mask and the child's, never the pixel difference
        assert union.mean() < 0.5


# ---------------------------------------------------------------------------- (3) the lineage is counted


def test_repro_c_a_depth_two_take_relayed_or_rewired_is_depth_exceeded(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        nodes = [_plate(packet_id), _mask(), _edit("d1")]
        edges = [_edge("p", "m"), _edge("p", "d1"), _edge("m", "d1", "mask")]
        _put_graph(c, auth, nodes, edges)
        r1 = _make(c, auth, "d1")
        assert r1["state"] == "DONE", r1.get("error")

        nodes[2] = {**nodes[2], "outputHash": r1["outputs"][0]}
        nodes.append(_edit("d2"))
        edges += [_edge("d1", "d2", "image"), _edge("m", "d2", "mask")]
        _put_graph(c, auth, nodes, edges)
        r2 = _make(c, auth, "d2")
        assert r2["state"] == "DONE", r2.get("error")
        assert (r2["inputs"]["base_run"], r2["inputs"]["base_depth"]) == (r1["id"], 1)

        # The relay: Plate -> Colour {outputHash: d2's take} -> d3 counts as depth 1 on the canvas.
        relay = {"id": "col", "kind": "Colour", "label": "Colour", "outputHash": r2["outputs"][0]}
        nodes2 = [*nodes, relay, _edit("d3")]
        edges2 = [*edges, _edge("p", "col"), _edge("col", "d3", "image"), _edge("m", "d3", "mask")]
        _put_graph(c, auth, nodes2, edges2)
        r = _quote_http(c, auth, "d3", engine_id="fake")
        assert r.status_code == 422, r.text
        assert r.json()["error"] == "DEPTH_EXCEEDED"
        assert r.json()["chain"] == [r2["id"], r1["id"]]
        assert r2["id"] in r.json()["detail"] and r1["id"] in r.json()["detail"]

        # The rewire: d2's image input moved to the Plate, its outputHash kept, d2 -> d3.
        rewired = [e for e in edges if e["id"] != "d1->d2"] + [_edge("p", "d2")]
        rewired += [_edge("d2", "d3", "image"), _edge("m", "d3", "mask")]
        _put_graph(c, auth, [*nodes[:3], {**nodes[3], "outputHash": r2["outputs"][0]}, _edit("d3")], rewired)
        r = _quote_http(c, auth, "d3", engine_id="fake")
        assert r.status_code == 422, r.text
        assert r.json()["error"] == "DEPTH_EXCEEDED" and r.json()["chain"] == [r2["id"], r1["id"]]
        assert _count(c, "node_id = 'd3'") == 0


# ---------------------------------------------------------------------------- (4) the union is the real masks


def test_an_honest_chain_is_the_plate_outside_the_union_of_its_two_masks(sync_settings, auth):
    with _client(sync_settings, Darken()) as c:
        packet_id = _setup(c, auth)
        cas = _cas(sync_settings)
        nodes = [_plate(packet_id), _mask("m1", [SKY]), _edit("e1"), _mask("m2", [SHRUB]), _edit("e2")]
        edges = [_edge("p", "m1"), _edge("m1", "e1"), _edge("p", "m2"), _edge("m2", "e2"), _edge("e1", "e2")]
        _put_graph(c, auth, nodes, edges)
        one = _make(c, auth, "e1")
        assert one["state"] == "DONE", one.get("error")
        nodes[2] = {**nodes[2], "outputHash": one["outputs"][0]}
        _put_graph(c, auth, nodes, edges)
        two = _make(c, auth, "e2")
        assert two["state"] == "DONE", two.get("error")

        plate = _plate16()
        out1 = decode_png16(cas.read_bytes(one["outputs"][0]))
        out2 = decode_png16(cas.read_bytes(two["outputs"][0]))
        mask1, mask2 = _grey(cas, one["normalized"]["mask_key"]), _grey(cas, two["normalized"]["mask_key"])
        union = _grey(cas, two["normalized"]["mask_union_key"])
        assert np.array_equal(union, mask1 | mask2)
        assert (out1[mask1] != plate[mask1]).any() and (out2[mask2] != out1[mask2]).any()
        assert np.array_equal(out2[~union], plate[~union])  # bit for bit
        rep = _gate(c, auth, two["id"])
        share = rep["metrics"]["outside_mask"]["mask_share"]
        assert share == pytest.approx(float(union.mean()), abs=1e-9) and share < 0.5  # not ~0.95
        assert rep["metrics"]["outside_mask"]["identical_outside"] is True
        assert rep["base"] == {"run_id": one["id"], "state": "DONE", "override_reason": None, "depth": 1}
        assert two["normalized"]["composite"]["mask_union_share"] == pytest.approx(float(union.mean()))


def test_a_base_run_without_a_readable_mask_fails_normalizing(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        nodes = [_plate(packet_id), _mask("m1", [SKY]), _edit("e1"), _mask("m2", [SHRUB]), _edit("e2")]
        edges = [_edge("p", "m1"), _edge("m1", "e1"), _edge("p", "m2"), _edge("m2", "e2"), _edge("e1", "e2")]
        _put_graph(c, auth, nodes, edges)
        one = _make(c, auth, "e1")
        nodes[2] = {**nodes[2], "outputHash": one["outputs"][0]}
        _put_graph(c, auth, nodes, edges)
        r = _quote_http(c, auth, "e2")
        assert r.status_code == 200, r.text
        with c.app.state.index.project(PROJECT) as conn:  # the base run's masks are gone from its record
            norm = {k: v for k, v in one["normalized"].items() if k not in ("mask_key", "mask_union_key")}
            conn.execute("UPDATE runs SET normalized = ? WHERE id = ?", (json.dumps(norm), one["id"]))
        run_id = r.json()["run"]["id"]
        assert c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth).status_code == 200
        done = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert (done["state"], done["reason_code"]) == ("FAILED", "base_mask_missing"), done.get("error")


def test_the_lineage_walk_stops_on_a_cycle_and_after_eight_hops():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, inputs TEXT)")
    for rid, base in (("r1", None), ("r2", "r1"), ("r3", "r2"), ("x", "y"), ("y", "x")):
        conn.execute("INSERT INTO runs VALUES (?, ?)", (rid, json.dumps({"base_run": base} if base else {})))
    chain = [f"c{i}" for i in range(12)]
    for i, rid in enumerate(chain):
        conn.execute("INSERT INTO runs VALUES (?, ?)", (rid, json.dumps({"base_run": f"c{i + 1}"})))
    pl = Pipeline.__new__(Pipeline)  # the walk reads nothing of the pipeline but the connection
    assert pl.lineage_depth(conn, None) == 0
    assert pl.lineage_depth(conn, "r1") == 1 and pl.lineage_depth(conn, "r3") == 3
    assert pl.lineage_chain(conn, "r3") == ["r3", "r2", "r1"]
    assert pl.lineage_depth(conn, "x") == pipeline_mod.LINEAGE_MAX_HOPS == 8
    assert pl.lineage_depth(conn, "c0") == 8 and len(pl.lineage_chain(conn, "c0")) == 8
    assert pl.lineage_depth(conn, "gone") == 1  # a run the index lost was still a generation
    assert canvas_rules.MAX_GENERATION_DEPTH == 2


# ---------------------------------------------------------------------------- (5) masks are checked at the quote


def test_mask_ids_and_a_mask_take_are_checked_before_any_run_row(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        cas = c.app.state.pipeline.cas_for(PROJECT)
        small = cas.put_bytes(bytes(cv2.imencode(".png", np.full((90, 160), 255, np.uint8))[1]), "png")
        edges = [_edge("p", "m"), _edge("m", "e")]
        cases = [
            (_mask(ids=["o:deadbeef"]), "bad_mask", "o:deadbeef"),
            (_mask(ids=[1]), "bad_mask", "1"),
            (
                {"id": "m", "kind": "Mask", "label": "Mask", "params": {}, "outputHash": small},
                "mask_size",
                None,
            ),
        ]
        for mask, code, named in cases:
            _put_graph(c, auth, [_plate(packet_id), mask, _edit("e")], edges)
            r = _quote_http(c, auth, "e")
            assert r.status_code == 422 and r.json()["error"] == code, r.text
            if named is not None:
                assert r.json()["id"] == named and named in r.json()["detail"]
        assert _count(c) == 0
        # The ids the packet knows still quote, and a mask that leaves nothing editable is refused at the quote.
        _put_graph(c, auth, [_plate(packet_id), _mask(ids=[SKY, SHRUB]), _edit("e")], edges)
        assert _quote_http(c, auth, "e").status_code == 200
        blank = cas.put_bytes(bytes(cv2.imencode(".png", np.zeros((H, W), np.uint8))[1]), "png")
        _put_graph(c, auth, [_plate(packet_id), _mask(ids=[], maskHash=blank), _edit("e")], edges)
        r = _quote_http(c, auth, "e")
        assert r.status_code == 422 and r.json()["error"] == "mask_empty", r.text
        assert _count(c) == 1


# ---------------------------------------------------------------------------- (6, 7) attempts


def test_ten_concurrent_quotes_of_one_node_number_one_to_ten_and_all_run(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        _put_graph(c, auth, [_plate(packet_id), _mask(), _edit("e")], [_edge("p", "m"), _edge("m", "e")])
        start = threading.Barrier(10)

        def one(_i: int) -> dict:
            mine = TestClient(c.app, base_url=BASE)  # one client per thread, one app
            start.wait()
            r = mine.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
            assert r.status_code == 200, r.text
            return r.json()["run"]

        with ThreadPoolExecutor(10) as pool:
            runs = list(pool.map(one, range(10)))
        assert sorted(r["attempt"] for r in runs) == list(range(1, 11))
        for run in runs:
            r = c.post(f"/api/v0/runs/{run['id']}/approve", json={"by": "human"}, headers=auth)
            assert r.status_code == 200, r.text
        assert _count(c, "reason_code = 'duplicate_request'") == 0
        assert _count(c, "state = 'DONE'") == 10


def test_attempts_are_bounded(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        _put_graph(c, auth, [_plate(packet_id), _mask(), _edit("e")], [_edge("p", "m"), _edge("m", "e")])
        r = _quote_http(c, auth, "e", attempt=2**63 - 1)
        assert r.status_code == 422, r.text
        assert _count(c) == 0
        first = _quote_http(c, auth, "e").json()["run"]
        with c.app.state.index.project(PROJECT) as conn:
            conn.execute("UPDATE runs SET attempt = ? WHERE id = ?", (ATTEMPT_MAX, first["id"]))
        r = _quote_http(c, auth, "e")
        assert r.status_code == 422 and r.json()["error"] == "attempt_exhausted", r.text
        assert r.json()["attempts"] == ATTEMPT_MAX and _count(c) == 1
        e = _refusal(c, {"attempt": True}, "e")
        assert e.code == "bad_attempt"


# ---------------------------------------------------------------------------- (8) priority and params


def test_priority_and_params_are_refused_before_a_run_row(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        _put_graph(c, auth, [_plate(packet_id), _mask(), _edit("e")], [_edge("p", "m"), _edge("m", "e")])
        assert _refusal(c, {"priority": "high"}, "e").code == "bad_priority"
        assert _refusal(c, {"priority": 7}, "e").code == "bad_priority"
        e = _refusal(c, {"params": {"n": float("inf")}}, "e")
        assert (e.code, e.extra["param"]) == ("bad_params", "params.n")
        e = _refusal(c, {"params": {"deep": {"list": [1.0, float("nan")]}}}, "e")
        assert (e.code, e.extra["param"]) == ("bad_params", "params.deep.list[1]")
        for count, words in ((0.5, "expected an integer"), (9, "outside 1 to 4")):
            e = _refusal(
                c,
                {
                    "engine_id": "nano-banana-pro",
                    "prompt": "late blue hour",
                    "params": {"candidate_count": count},
                },
                "e",
            )
            assert e.code == "bad_params" and "candidate_count" in str(e) and words in str(e), str(e)
        e = _refusal(c, {"engine_id": "nano-banana-pro", "prompt": "x", "params": {"strength": 0.4}}, "e")
        assert e.code == "bad_params" and "unknown parameter" in str(e)
        assert _count(c) == 0
        run, _q = _quote(c, {"priority": 0}, "e")
        assert run.priority is Priority.CLIENT_DEADLINE and run.priority.label == "client_deadline"
        run, _q = _quote(
            c, {"engine_id": "nano-banana-pro", "prompt": "x", "params": {"candidate_count": 2}}, "e"
        )
        assert run.params["candidate_count"] == 2


def test_a_recipe_path_that_is_not_a_recipe_names_the_path_only(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        _put_graph(c, auth, [_plate(packet_id), _mask(), _edit("e")], [_edge("p", "m"), _edge("m", "e")])
        bad = tmp_path / "notes.yaml"
        bad.write_text("token: sk-very-secret-line\n  : [unclosed\n", encoding="utf-8", newline="\n")
        listy = tmp_path / "list.yaml"
        listy.write_text("- sk-another-secret\n", encoding="utf-8", newline="\n")
        for path in (bad, listy):
            r = _quote_http(c, auth, "e", recipe=str(path))
            assert r.status_code == 422 and r.json()["error"] == "unknown_recipe", r.text
            assert r.json()["detail"] == f"recipe {path}: not a readable runnable recipe file"
            assert "secret" not in r.text
        assert _count(c) == 0


# ---------------------------------------------------------------------------- (9) a half-made quote leaves nothing


def test_a_quote_that_fails_after_its_row_leaves_no_draft(sync_settings, auth, monkeypatch):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        _put_graph(c, auth, [_plate(packet_id), _mask(), _edit("e")], [_edge("p", "m"), _edge("m", "e")])

        def full(*_a, **_k):
            raise sqlite3.OperationalError("database or disk is full")

        monkeypatch.setattr(pipeline_mod, "quote_run", full)
        with pytest.raises(sqlite3.OperationalError):
            _quote(c, {}, "e")
        assert _count(c, "state = 'DRAFT'") == 0 and _count(c, "state = 'CANCELLED'") == 1
        with c.app.state.index.project(PROJECT) as conn:
            reason = conn.execute("SELECT reason FROM run_events WHERE to_state = 'CANCELLED'").fetchone()[0]
        assert reason == "quote failed: OperationalError"
        monkeypatch.undo()
        run, _q = _quote(c, {}, "e")  # the node still quotes; the cancelled row took attempt 1
        assert run.state.value == "QUOTED" and run.attempt == 2


# ---------------------------------------------------------------------------- (10) the daily cap is the user's


def test_the_daily_cap_counts_every_project_in_the_home(sync_settings, auth):
    with _client(sync_settings) as c:
        c.app.state.pipeline.policy = budgets.Policy(per_user_daily_cap_usd=0.2)
        projects = {PROJECT: _setup(c, auth)}
        for k, project in enumerate(("money-b", "money-c")):
            packet = _stage_copy(sync_settings, project, "pkt_2ZHQ7DX53BCK1J9P8GJB9Z2V5" + "AB"[k])
            projects[project] = _setup(c, auth, project, packet)
        for project, packet_id in projects.items():
            _put_graph(c, auth, [_plate(packet_id), _edit("e")], [_edge("p", "e")], project)
        plan = [PROJECT, PROJECT, "money-b", "money-b", "money-b", "money-c"]
        answers = []
        for project in plan:
            r = _quote_http(c, auth, "e", project)
            assert r.status_code == 200 and r.json()["quote"]["price_usd"] == pytest.approx(0.04), r.text
            r = c.post(
                f"/api/v0/runs/{r.json()['run']['id']}/approve",
                json={"by": "auto", "start": False},
                headers=auth,
            )
            answers.append(r)
        assert [r.status_code for r in answers] == [200] * 5 + [409], [r.text for r in answers]
        refusal = answers[-1].json()
        assert refusal["error"] == "daily_cap" and "across 3 projects" in refusal["detail"], refusal
        assert "0.20 + 0.04 USD" in refusal["detail"]
        spent = budgets.spend_snapshot_all(c.app.state.index, utcnow(), None)
        assert spent.today_usd == pytest.approx(0.2) and spent.projects == 3


# ---------------------------------------------------------------------------- (11) an expired quote is ledgered


@pytest.mark.parametrize("shift", [timedelta(minutes=16), timedelta(minutes=-5)])
def test_approving_an_expired_or_future_quote_is_409_and_ledgered(sync_settings, auth, shift):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        _put_graph(c, auth, [_plate(packet_id), _mask(), _edit("e")], [_edge("p", "m"), _edge("m", "e")])
        run_id = _quote_http(c, auth, "e").json()["run"]["id"]
        later = utcnow() + shift
        c.app.state.clock = lambda: later
        r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
        assert r.status_code == 409 and r.json()["error"] == "quote_expired", r.text
        if shift < timedelta(0):
            assert "the clock moved" in r.json()["detail"]
        assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "EXPIRED"
        kinds = [x["kind"] for x in _records(sync_settings, run_id)]
        assert kinds == ["quoted", "expired"]


# ---------------------------------------------------------------------------- (12) envelopes and prices are bounded


def test_envelopes_and_price_counts_are_bounded(sync_settings):
    from plate_daemon.index import Index

    index = Index(sync_settings.home)
    with index.project(PROJECT, create=True) as conn:  # T144: opening never creates
        for bad in (
            {"max_total_usd": float("inf")},
            {"max_total_usd": 1e308},
            {"max_per_run_usd": float("nan")},
            {"max_per_run_usd": 1_000_001.0},
            {"ttl_s": float("inf")},
            {"ttl_s": 31_622_401},
        ):
            kwargs = {"owner": "agent", "max_total_usd": 1.0, "max_per_run_usd": 0.5, "ttl_s": 60, **bad}
            with pytest.raises(ValueError):
                create_envelope(conn, **kwargs)
        assert conn.execute("SELECT COUNT(*) FROM budget_envelopes").fetchone()[0] == 0
        env = create_envelope(conn, owner="agent", max_total_usd=1e6, max_per_run_usd=1e6, ttl_s=31_622_400)
        assert env.max_total_usd == 1e6
    from plate_engines.loader import load_manifests

    nano = load_manifests()["nano-banana-pro"]
    for count in (0, 0.5, 2.0, True, "2", -1):
        with pytest.raises(ValueError, match="whole number of at least 1"):
            build_quote(nano, None, {}, {"candidate_count": count})
    assert build_quote(nano, None, {}, {"candidate_count": 2}).price_usd == pytest.approx(
        2 * build_quote(nano, None, {}, {}).price_usd
    )
