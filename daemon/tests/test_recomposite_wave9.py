"""Recomposite (router plan T99, decision 54), on the committed Blender packet `room_a`.

A stored raw take put through another mask, feather or grow/shrink is a new ledgered run at 0 USD on a
`local-recomposite` route: no engine is called, the parent's raw bytes come back out of the CAS, and the parent's
pinned size map makes a region crop land where it did (T92's seam). `Tint` is the echoing FakeEngine that darkens
the whole egress image whatever mask it was sent, so a take is the same for every mask and a re-mask of the stored
take can be compared, pixel for pixel, with a fresh generation under that mask.

Plan note on grow: the grown mask is stored as the plan says (a CAS PNG used through a `union`), and the staging
still clamps every mask to the packet's editable ids and away from its locked ids (decision 34). On `room_a` no two
editable ids touch, so growing a Mask node's ids enlarges the stored PNG but not the staged mask; a painted mask
(`mask_hash`) inside one id is what shows the staged mask growing.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import budgets, recomposite
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.custody import Grade, effective_grade
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.machine import get_run
from plate_daemon.pipeline import PipelineError, decode_png16

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
H, W = 360, 640
SKY, SHRUB, POOL = "o:929c9e95", "o:55cbe981", "o:c8a9bf52"  # editable on room_a
LOCKED_WALL = "o:57b3d007"
TINT = 0.9
# A local engine that takes up to four references (what the fake stands in for).
WITH_REFS = {
    "sizes": {"auto": True},
    "inputs": [
        {"name": "image", "type": "image", "required": True},
        {"name": "reference_images", "type": "images", "required": False, "max_items": 4},
    ],
}

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


class Tint(FakeEngine):
    """The echoing FakeEngine that keeps every payload it was sent and darkens the whole image it echoes."""

    def __init__(self, *args, factor: float = TINT, manifest=None, **kwargs):
        kwargs.setdefault("echo", True)
        super().__init__(*args, **kwargs)
        if manifest is not None:
            self.manifest = manifest
        self.factor = factor
        self.payloads: list[dict] = []

    def submit(self, payload, idempotency_key):
        self.payloads.append(copy.deepcopy(payload))
        payload = copy.deepcopy(payload)
        img = _decode(payload["inputs"]["image"]["b64"], cv2.IMREAD_UNCHANGED)
        out = np.rint(img.astype(np.float64) * self.factor).astype(img.dtype)
        payload["inputs"]["image"]["b64"] = base64.b64encode(bytes(cv2.imencode(".png", out)[1])).decode(
            "ascii"
        )
        return super().submit(payload, idempotency_key)


# ---------------------------------------------------------------------------- set-up


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    return Settings.load()


def _client(settings: Settings, engine=None) -> TestClient:
    c = TestClient(create_app(settings, engine or Tint()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _decode(b64: str, flags: int) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), flags)


def _setup(c: TestClient, auth: dict, *, nda: bool = False) -> str:
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    r = c.post(
        "/api/v0/packets",
        json={"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}},
        headers=hdr,
    )
    assert r.status_code == 202, r.text
    return r.json()["packet_id"]


def _put_graph(c: TestClient, auth: dict, nodes: list[dict], edges: list[dict]) -> None:
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text


def _plate(packet_id: str) -> dict:
    return {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}


def _edit(nid: str = "e", engine: str = "fake", **params) -> dict:
    return {"id": nid, "kind": "Edit", "label": "Edit", "params": {"engine": engine, **params}}


def _mask(nid: str, ids: list[str], **params) -> dict:
    return {"id": nid, "kind": "Mask", "params": {"ids": ids, "textLock": True, **params}}


def _reference(nid: str, key: str) -> dict:
    return {
        "id": nid,
        "kind": "Reference",
        "label": "Reference",
        "params": {"referenceHash": key, "role": "object"},
    }


def _edge(source: str, target: str, handle: str | None = None) -> dict:
    e = {"id": f"{source}->{target}", "source": source, "target": target}
    if handle is not None:
        e["targetHandle"] = handle
    return e


def _chain(packet_id: str, ids: list[str], **edit_params) -> tuple[list[dict], list[dict]]:
    nodes = [_plate(packet_id), _mask("m", ids), _edit(**edit_params)]
    return nodes, [_edge("p", "m"), _edge("m", "e")]


def _quote(c: TestClient, body: dict, node: str = "e"):
    with c.app.state.index.project(PROJECT) as conn:
        return c.app.state.pipeline.quote_node(conn, PROJECT, node, body)


def _approve(c: TestClient, auth: dict, run_id: str, by: str = "human") -> dict:
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": by}, headers=auth)
    assert r.status_code == 200, r.text
    return _run(c, auth, run_id)


def _run(c: TestClient, auth: dict, run_id: str) -> dict:
    r = c.get(f"/api/v0/runs/{run_id}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _make(c: TestClient, auth: dict, body: dict | None = None, node: str = "e", ok=("DONE",)) -> dict:
    """Quote the node as it stands and run it to its end: a take."""
    run, _q = _quote(c, body or {}, node)
    done = _approve(c, auth, run.id)
    assert done["state"] in ok, done.get("error") or done.get("reason_code")
    return done


def _re_mask(c: TestClient, auth: dict, run_id: str, **body) -> dict:
    r = c.post(f"/api/v0/runs/{run_id}/recomposite", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _cas(settings: Settings) -> CAS:
    return CAS(settings.projects_dir / PROJECT / "assets")


def _grey(cas: CAS, key: str) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(cas.read_bytes(key), np.uint8), cv2.IMREAD_GRAYSCALE) > 127


def _out(cas: CAS, run: dict) -> np.ndarray:
    return decode_png16(cas.read_bytes(run["outputs"][0]))


def _records(settings: Settings, run_id: str) -> list[dict]:
    return [r for r in Ledger(settings.projects_dir / PROJECT).records() if r.get("run_id") == run_id]


def _plate16() -> np.ndarray:
    from plate_pixels import imageio

    return np.rint(imageio.read(ROOM_A / "display.tif").rgb * 65535).astype(np.uint16)


def _packet(c: TestClient, packet_id: str):
    return c.app.state.pipeline.packet(PROJECT, packet_id)


def _get_run(c: TestClient, run_id: str):
    with c.app.state.index.project(PROJECT) as conn:
        return get_run(conn, run_id)


def _count(c: TestClient) -> int:
    with c.app.state.index.project(PROJECT) as conn:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def _refusal(c: TestClient, body: dict, node: str = "e") -> PipelineError:
    with pytest.raises(PipelineError) as e:
        _quote(c, body, node)
    return e.value


# ---------------------------------------------------------------------------- the re-mask itself


def test_a_new_mask_re_composites_the_stored_take_for_nothing(sync_settings, auth):
    engine = Tint()
    with _client(sync_settings, engine) as c:
        packet_id = _setup(c, auth)
        nodes, edges = _chain(packet_id, [SKY])
        _put_graph(c, auth, nodes, edges)
        parent = _make(c, auth)
        sent = len(engine.payloads)
        cas = _cas(sync_settings)

        nodes[1] = _mask("m", [SHRUB])  # the artist retargets the Mask node, then presses Re-mask
        _put_graph(c, auth, nodes, edges)
        view = _re_mask(c, auth, parent["id"])

        assert view["state"] == "DONE", view.get("error") or view.get("reason_code")
        assert len(engine.payloads) == sent  # no engine was called
        assert view["quote"]["price_usd"] == 0 and view["quote"]["price_status"] == "confirmed"
        assert view["quote"]["custody_grade"] == "C0"
        assert (view["route"]["provider"], view["route"]["route_id"]) == ("local-recomposite", "recomposite")
        assert view["route"]["custody_grade"] == "C0" and view["route"]["upload_visibility"] == "local"
        assert view["route"]["engine_id"] == parent["route"]["engine_id"]
        assert view["approved_by"] == "human" and view["inputs"]["recomposite_of"] == parent["id"]
        assert view["id"] != parent["id"] and view["attempt"] == 1
        # The raw take is the parent's own bytes, and stays the original take.
        assert view["result_key"] == parent["normalized"]["raw_key"] == view["normalized"]["raw_key"]
        assert view["normalized"]["sizemap"] == parent["normalized"]["sizemap"]
        assert view["inputs"]["staging"]["png_key"] == parent["normalized"]["egress_key"]

        plate, out = _plate16(), _out(cas, view)
        old, new = _grey(cas, parent["normalized"]["mask_key"]), _grey(cas, view["normalized"]["mask_key"])
        assert old.any() and new.any() and not (old & new).any()
        # Outside the new mask the output is the plate, bit for bit, including where the parent had edited.
        assert np.array_equal(out[~new], plate[~new])
        assert np.array_equal(_out(cas, parent)[~old], plate[~old])
        assert (out[new] != plate[new]).any() and (_out(cas, parent)[old] != plate[old]).any()
        assert not np.array_equal(out[old], _out(cas, parent)[old])
        # Inside it the output is the parent's restored take: a fresh generation under that mask agrees.
        fresh = _make(c, auth)
        assert len(engine.payloads) == sent + 1
        assert np.array_equal(out, _out(cas, fresh))

        rep = c.get(f"/api/v0/runs/{view['id']}/gate", headers=auth).json()
        assert rep["metrics"]["outside_mask"]["identical_outside"] is True
        assert view["gate_report"]["verdict"] == "pass"


def test_the_ledger_shows_a_free_local_run_that_sent_nothing(sync_settings, auth):
    with _client(sync_settings, Tint(manifest=WITH_REFS)) as c:
        packet_id = _setup(c, auth)
        img = np.zeros((32, 48, 3), np.uint8)
        img[:] = (40, 160, 220)
        r = c.post(
            f"/api/v0/assets?kind=reference&project_id={PROJECT}",
            content=bytes(cv2.imencode(".png", img)[1]),
            headers={**auth, "Content-Type": "image/png"},
        )
        assert r.status_code == 201, r.text
        ref = r.json()["hash"]
        nodes, edges = _chain(packet_id, [SKY])
        _put_graph(c, auth, [*nodes, _reference("r", ref)], [*edges, _edge("r", "e", "refs")])
        parent = _make(c, auth)
        assert parent["inputs"]["reference_images"][0]["hash"] == ref
        assert next(x for x in _records(sync_settings, parent["id"]) if x["kind"] == "submitted")["data"][
            "references"
        ] == [ref]

        view = _re_mask(c, auth, parent["id"], feather_px=6)
        assert view["state"] == "DONE"
        assert "reference_images" not in view["inputs"]  # nothing left the box, so no reference is claimed
        by_kind = {x["kind"]: x for x in _records(sync_settings, view["id"])}
        assert {"quoted", "approved", "submitted", "fetched", "done"} <= set(by_kind)
        assert by_kind["quoted"]["cost_usd_quoted"] == 0 and by_kind["quoted"]["custody_grade"] == "C0"
        assert by_kind["quoted"]["route_id"] == "recomposite"
        assert by_kind["quoted"]["data"]["route"]["provider"] == "local-recomposite"
        assert by_kind["quoted"]["data"]["inputs"]["recomposite_of"] == parent["id"]
        assert "references" not in by_kind["submitted"]["data"]
        assert by_kind["submitted"]["provider_job_id"] == f"recomp:{view['id']}"
        assert by_kind["done"]["data"]["verdict"] == "pass"
        assert view["inputs"]["staging"]["feather_px"] == 6.0
        assert view["normalized"]["composite"]["feather_px"] == 6.0


def test_a_free_replay_is_not_a_sample_of_the_engine(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        nodes, edges = _chain(packet_id, [SKY])
        _put_graph(c, auth, nodes, edges)
        parent = _make(c, auth)

        def sampled() -> int:
            r = c.get(f"/api/v0/engines/cards?project_id={PROJECT}", headers=auth)
            assert r.status_code == 200, r.text
            return sum(card["n"] for card in r.json())

        assert sampled() == 1
        for grow in (3, -3):
            _re_mask(c, auth, parent["id"], grow_px=grow)
        assert sampled() == 1  # the engine cards and their mean cost still count the one real take


def test_the_same_request_twice_is_attempts_one_and_two(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        nodes, edges = _chain(packet_id, [SKY])
        _put_graph(c, auth, nodes, edges)
        parent = _make(c, auth)
        first, second = (_re_mask(c, auth, parent["id"]) for _ in range(2))
        assert (first["attempt"], second["attempt"]) == (1, 2)
        assert first["state"] == second["state"] == "DONE"  # not a duplicate_request cancel
        other = _re_mask(c, auth, parent["id"], feather_px=5)
        assert other["attempt"] == 1  # another feather is another request
        # A recomposite is itself a take with a raw layer: it can be re-composited again.
        again = _re_mask(c, auth, first["id"], grow_px=2)
        assert again["state"] == "DONE" and again["normalized"]["raw_key"] == parent["normalized"]["raw_key"]
        assert again["inputs"]["recomposite_of"] == first["id"]


# ---------------------------------------------------------------------------- grow, shrink and a replacement mask


def test_grow_and_shrink_dilate_and_erode_the_editable_mask(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        nodes, edges = _chain(packet_id, [SHRUB])
        _put_graph(c, auth, nodes, edges)
        parent = _make(c, auth)
        cas = _cas(sync_settings)
        pv = _packet(c, packet_id)
        base = _grey(cas, parent["normalized"]["mask_key"])
        allowed = pv.editable_px((H, W)) & ~pv.locked_px((H, W))

        grown = _re_mask(c, auth, parent["id"], grow_px=8)
        spec = grown["inputs"]["mask"]
        assert spec["kind"] == "union" and spec["grow_px"] == 8 and len(spec["keys"]) == 1
        assert grown["params"]["grow_px"] == 8
        stored = _grey(cas, spec["keys"][0])
        assert stored.sum() > base.sum() and (stored & base).sum() == base.sum()
        assert np.array_equal(stored, recomposite.grow_mask(base, 8))
        # The staged mask is still inside the packet's editable ids and off its locked ids (decision 34).
        staged = _grey(cas, grown["normalized"]["mask_key"])
        assert staged.any() and not (staged & ~allowed).any()
        assert not staged[pv.locked_px((H, W))].any()

        shrunk = _re_mask(c, auth, parent["id"], grow_px=-4)
        small = _grey(cas, shrunk["normalized"]["mask_key"])
        assert 0 < small.sum() < base.sum() and not (small & ~base).any()
        assert np.array_equal(small, recomposite.grow_mask(base, -4) & allowed)
        assert shrunk["state"] == grown["state"] == "DONE"


def test_a_painted_mask_replaces_the_nodes_and_grows_inside_the_editable_ids(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        nodes, edges = _chain(packet_id, [SHRUB])
        _put_graph(c, auth, nodes, edges)
        parent = _make(c, auth)
        cas = _cas(sync_settings)
        pv = _packet(c, packet_id)
        allowed = pv.editable_px((H, W)) & ~pv.locked_px((H, W))

        sky = pv.id_mask(SKY) & allowed
        ys, xs = np.nonzero(sky)
        cy, cx = int(np.median(ys)), int(np.median(xs))
        box = np.zeros((H, W), bool)
        box[cy - 12 : cy + 12, cx - 20 : cx + 20] = True
        painted = box & allowed
        assert painted.sum() > 0
        key = cas.put_bytes(bytes(cv2.imencode(".png", np.where(box, 255, 0).astype(np.uint8))[1]), "png")

        view = _re_mask(c, auth, parent["id"], mask_hash=key)
        assert view["state"] == "DONE" and view["inputs"]["mask"]["keys"] == [key]
        assert view["inputs"]["mask"]["source"] != parent["inputs"]["mask"]["source"]  # not the Mask node's
        staged = _grey(cas, view["normalized"]["mask_key"])
        assert np.array_equal(staged, painted)
        out, plate = _out(cas, view), _plate16()
        assert np.array_equal(out[~staged], plate[~staged])

        wide = _re_mask(c, auth, parent["id"], mask_hash=key, grow_px=8)
        staged_wide = _grey(cas, wide["normalized"]["mask_key"])
        assert staged_wide.sum() > staged.sum() and (staged_wide & staged).sum() == staged.sum()
        assert not (staged_wide & ~allowed).any()  # growth never reaches a locked or unlisted pixel
        assert np.array_equal(_out(cas, wide)[~staged_wide], plate[~staged_wide])


def test_a_pinned_size_map_keeps_a_region_crop_where_it_was(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        nodes, edges = _chain(packet_id, [SHRUB])
        _put_graph(c, auth, nodes, edges)
        parent = _make(c, auth, {"region_crop": True}, ok=("DONE", "GATE_FAIL"))
        region = parent["normalized"]["sizemap"]["region"]
        assert region is not None

        nodes[1] = _mask("m", [SKY])  # another region
        _put_graph(c, auth, nodes, edges)
        view = _re_mask(c, auth, parent["id"])
        assert view["state"] in ("DONE", "GATE_FAIL"), view.get("error")
        assert view["normalized"]["sizemap"] == parent["normalized"]["sizemap"]
        assert view["inputs"]["staging"]["png_key"] == parent["normalized"]["egress_key"]
        fresh = _make(c, auth, {"region_crop": True}, ok=("DONE", "GATE_FAIL"))
        assert (
            fresh["normalized"]["sizemap"]["region"] != region
        )  # the crop the new mask would have asked for


def test_a_recomposite_of_a_take_made_on_a_base_keeps_the_union_gate(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        cas = _cas(sync_settings)
        nodes = [_plate(packet_id), _mask("m1", [SKY]), _edit("e1"), _mask("m2", [SHRUB]), _edit("e2")]
        edges = [_edge("p", "m1"), _edge("m1", "e1"), _edge("p", "m2"), _edge("m2", "e2"), _edge("e1", "e2")]
        _put_graph(c, auth, nodes, edges)
        first = _make(c, auth, node="e1")
        nodes[2] = {**nodes[2], "outputHash": first["outputs"][0]}  # the artist picked the take
        _put_graph(c, auth, nodes, edges)
        second = _make(c, auth, node="e2")
        assert second["inputs"]["base"] == first["outputs"][0]

        view = _re_mask(c, auth, second["id"], grow_px=-3)
        assert view["state"] == "DONE", view.get("error") or view.get("reason_code")
        assert (
            view["inputs"]["base"] == first["outputs"][0]
            and view["normalized"]["base"] == first["outputs"][0]
        )
        union = _grey(cas, view["normalized"]["mask_union_key"])
        mask1, mask2 = _grey(cas, first["normalized"]["mask_key"]), _grey(cas, view["normalized"]["mask_key"])
        assert np.array_equal(union, mask1 | mask2)
        assert np.array_equal(_out(cas, view)[~union], _plate16()[~union])
        rep = c.get(f"/api/v0/runs/{view['id']}/gate", headers=auth).json()
        assert rep["metrics"]["outside_mask"]["identical_outside"] is True


# ---------------------------------------------------------------------------- what is refused


def test_only_a_take_that_kept_its_raw_layer_can_be_re_composited(sync_settings, auth):
    with _client(sync_settings) as c:
        recomposite.install(c.app.state.pipeline)
        packet_id = _setup(c, auth)
        nodes, edges = _chain(packet_id, [SKY])
        _put_graph(c, auth, [*nodes, _edit("e2")], [*edges, _edge("m", "e2")])
        quoted, _q = _quote(c, {})  # never ran: no raw take
        r = c.post(f"/api/v0/runs/{quoted.id}/recomposite", json={}, headers=auth)
        assert r.status_code == 422 and r.json()["error"] == "not_recompositable"
        assert "raw take" in r.json()["detail"]

        done = _make(c, auth)
        other = _make(c, auth, node="e2")
        r = c.post("/api/v0/runs/run_00000000000000000000000000/recomposite", json={}, headers=auth)
        assert r.status_code == 404 and r.json()["error"] == "not_found"
        runs_before = _count(c)
        # A take of another node, a node that is not an Edit, and something that is not a run id.
        err = _refusal(c, {"params": {"recomposite_of": other["id"]}})
        assert err.code == "not_recompositable" and err.status == 422
        err = _refusal(c, {"params": {"recomposite_of": done["id"]}}, node="m")
        assert err.code == "not_recompositable"
        err = _refusal(c, {"params": {"recomposite_of": 7}})
        assert err.code == "not_recompositable"
        err = _refusal(c, {"params": {"recomposite_of": "run_00000000000000000000000000"}})
        assert (err.code, err.status) == ("not_found", 404)
        assert _count(c) == runs_before  # every refusal came before a run row

        # The raw take is gone from the CAS (both nodes echoed the same bytes, so they shared it).
        _cas(sync_settings).find(done["normalized"]["raw_key"]).unlink()
        r = c.post(f"/api/v0/runs/{done['id']}/recomposite", json={}, headers=auth)
        assert r.status_code == 422 and r.json()["error"] == "not_recompositable"
        assert "raw take is not stored" in r.json()["detail"]
        assert _count(c) == runs_before


def test_bad_numbers_and_masks_are_refused_before_any_run_row(sync_settings, auth):
    with _client(sync_settings) as c:
        recomposite.install(c.app.state.pipeline)
        packet_id = _setup(c, auth)
        nodes, edges = _chain(packet_id, [SKY])
        _put_graph(c, auth, nodes, edges)
        parent = _make(c, auth)
        url = f"/api/v0/runs/{parent['id']}/recomposite"
        for body in (
            {"feather_px": 3},
            {"feather_px": 13},
            {"grow_px": 33},
            {"grow_px": -33},
            {"grow_px": 1.5},
        ):
            assert c.post(url, json=body, headers=auth).status_code == 422, body
        assert c.post(url, json={"mask_hash": "xyz"}, headers=auth).status_code == 422
        assert c.post(url, json={"surprise": 1}, headers=auth).status_code == 422
        r = c.post(url, json={"mask_hash": "a" * 64}, headers=auth)
        assert r.status_code == 422 and r.json()["error"] == "mask_missing"
        # The same limits hold for the quote the wrappers see.
        params = {"recomposite_of": parent["id"]}
        assert _refusal(c, {"params": {**params, "grow_px": 40}}).code == "bad_grow"
        assert _refusal(c, {"params": {**params, "grow_px": 2.5}}).code == "bad_grow"
        assert _refusal(c, {"params": {**params, "grow_px": True}}).code == "bad_grow"
        assert _refusal(c, {"feather_px": 20, "params": params}).code == "bad_feather"
        assert _refusal(c, {"params": {**params, "mask_hash": "nope"}}).code == "bad_mask"
        assert _count(c) == 1
        # A mask shrunk to nothing, and a Mask node that now names a locked id.
        nodes[1] = _mask("m", [POOL])  # 119 px: nothing is left after an erosion by 32
        _put_graph(c, auth, nodes, edges)
        assert _refusal(c, {"params": {**params, "grow_px": -32}}).code == "mask_empty"
        nodes[1] = _mask("m", [LOCKED_WALL])
        _put_graph(c, auth, nodes, edges)
        assert _refusal(c, {"params": params}).code == "mask_locked"
        assert _count(c) == 1


def test_a_plate_that_names_another_packet_is_refused(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        nodes, edges = _chain(packet_id, [SKY])
        _put_graph(c, auth, nodes, edges)
        parent = _make(c, auth)
        nodes[0] = {**nodes[0], "packetId": "someone-elses-camera"}
        _put_graph(c, auth, nodes, edges)
        r = c.post(f"/api/v0/runs/{parent['id']}/recomposite", json={}, headers=auth)
        assert r.status_code == 422 and r.json()["error"] == "not_recompositable"
        assert "packet" in r.json()["detail"]


# ---------------------------------------------------------------------------- the install-wrapper


def test_install_is_idempotent_and_wraps_the_quote_approve_and_engine(sync_settings, auth):
    with _client(sync_settings) as c:
        pl = c.app.state.pipeline
        packet_id = _setup(c, auth)
        nodes, edges = _chain(packet_id, [SKY])
        _put_graph(c, auth, nodes, edges)
        runner = recomposite.install(pl)
        assert recomposite.install(pl) is runner and pl.recomposite_runner is runner
        wrapped = (pl.quote_node, pl.approve, pl.engine_for)
        recomposite.install(pl)
        assert (pl.quote_node, pl.approve, pl.engine_for) == wrapped

        # An ordinary quote falls through to the original, untouched.
        parent = _make(c, auth)
        assert parent["route"]["provider"] == "fake" and "recomposite_of" not in parent["inputs"]

        # The canvas quote route with the trigger makes a free QUOTED run; the generic approve route drives it.
        r = c.post(
            f"/api/v0/projects/{PROJECT}/nodes/e/quote",
            json={"params": {"recomposite_of": parent["id"], "grow_px": 2}},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        quoted = r.json()
        assert quoted["run"]["state"] == "QUOTED" and quoted["quote"]["price_usd"] == 0
        assert quoted["run"]["route"]["provider"] == "local-recomposite"
        pl.policy = budgets.Policy(
            per_run_auto_cap_usd=0.0, per_user_daily_cap_usd=0.0
        )  # no spend is allowed
        done = _approve(c, auth, quoted["run"]["id"], by="auto")
        assert done["state"] == "DONE" and done["approved_by"] == "auto" and done["reserved_usd"] == 0
        assert isinstance(
            pl.engine_for(_get_run(c, done["id"]), _cas(sync_settings)), recomposite.ReplayEngine
        )


def test_the_replay_engine_is_a_function_of_the_run(sync_settings, auth):
    with _client(sync_settings) as c:
        pl = c.app.state.pipeline
        packet_id = _setup(c, auth)
        nodes, edges = _chain(packet_id, [SKY])
        _put_graph(c, auth, nodes, edges)
        parent = _make(c, auth)
        view = _re_mask(c, auth, parent["id"])
        cas = _cas(sync_settings)
        run = _get_run(c, view["id"])
        engine = pl.engine_for(run, cas)
        assert isinstance(engine, recomposite.ReplayEngine) and recomposite.is_recomposite(run)
        assert not recomposite.is_recomposite(_get_run(c, parent["id"]))
        job = engine.submit({}, "key")
        assert job == f"recomp:{run.id}" == engine.lookup("another key")
        assert engine.submit({}, "key") == job  # a re-staged submit lands on the same job
        polled = engine.poll(job)
        assert polled.status == "succeeded"
        assert engine.fetch(polled.result_url) == cas.read_bytes(parent["normalized"]["raw_key"])
        with pytest.raises(ValueError):
            engine.fetch("plate-recomposite://run_other")
        assert engine.cancel(job) is None
        # No raw take named: the engine says so rather than guessing.
        bare = run.model_copy(
            update={"inputs": {k: v for k, v in run.inputs.items() if k != "recomposite_raw"}}
        )
        with pytest.raises(PipelineError) as e:
            recomposite.ReplayEngine(pl, bare, cas).submit({}, "key")
        assert e.value.code == "not_recompositable"


# ---------------------------------------------------------------------------- pure helpers


def test_the_route_is_a_confirmed_local_c0_whatever_the_vendors_was():
    pending = {
        "engine_id": "gemini-x",
        "provider": "google",
        "region": "us",
        "route_id": "gemini-direct",
        "custody_grade": "D",
        "custody_status": "pending_written_terms",
        "upload_visibility": "vendor",
        "model_version": "v7",
        "reasons": ["why"],
    }
    assert effective_grade(pending) is Grade.D
    route = recomposite.recomposite_route(pending)
    assert effective_grade(route) is Grade.C0
    assert (route["engine_id"], route["model_version"]) == ("gemini-x", "v7")
    assert (route["provider"], route["route_id"], route["region"]) == (
        "local-recomposite",
        "recomposite",
        "local",
    )
    assert route["upload_visibility"] == "local" and "reasons" not in route
    assert pending["provider"] == "google"  # the parent's own dict is not edited


def test_grow_mask_uses_an_elliptical_kernel_of_2n_plus_1():
    m = np.zeros((41, 41), bool)
    m[20, 20] = True
    grown = recomposite.grow_mask(m, 5)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)) > 0
    assert np.array_equal(grown[15:26, 15:26], kernel) and grown.sum() == kernel.sum()
    assert not grown[20, 25 + 1] and grown[20, 25]  # reaches 5 px along the axis, no further
    block = np.zeros((41, 41), bool)
    block[10:31, 10:31] = True
    eroded = recomposite.grow_mask(block, -3)
    assert eroded.sum() < block.sum() and not (eroded & ~block).any() and eroded[20, 20]
    assert (
        np.array_equal(recomposite.grow_mask(block, 0), block)
        and recomposite.grow_mask(block, 0) is not block
    )
    full = np.ones((20, 30), bool)
    assert (
        recomposite.grow_mask(full, -4).all() and recomposite.grow_mask(full, 4).all()
    )  # the frame is no edge


def test_the_module_keeps_its_public_names():
    for name in recomposite.__all__:
        assert hasattr(recomposite, name), name
    assert recomposite.GROW_RANGE == (-32, 32) and recomposite.FEATHER_RANGE == (4.0, 12.0)
