"""Wave 9 pipeline (router plan T92), on the committed Blender packet `room_a`.

Reference images reach the engine as opaque, metadata-free PNGs (decision 46) and are refused at the quote when
the engine cannot take them; an Edit after an Edit starts from the first take and is gated against the plate
inside the union of the chain's masks (decision 47); the staged mask goes to an engine that declares one, in the
egress geometry; `params.tokens` fill a recipe's tokens and recipes resolve from the project's folder (decision
50); a pinned size map and PNG are reused as they are (decision 54); ingress hooks run on the engine's bytes
before restore (decision 55); a route the tracker reports degraded is named in the quote and never routed to
(decision 51).

`Recorder` is the echoing FakeEngine that keeps every payload it was sent (the "journal" of the plan). Given a
`manifest` it stands in for that engine (its inputs and sizes are what the pipeline reads); `darken` makes it an
engine that edits only inside the mask it was sent, so a take differs from the plate where it should.

Plan note: the plan names `flux-3-image` as the engine with no reference input, but its manifest declares
`reference_images` (T7/T61 data), so the refusal is proven on the daemon's fake engine, which declares none.
"""

from __future__ import annotations

import base64
import copy
import json
import re
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

from plate_daemon import pipeline as pipeline_mod
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine, shift_right
from plate_daemon.ledger import Ledger
from plate_daemon.machine import create_run, get_run
from plate_daemon.models import RunCreate
from plate_daemon.pipeline import PipelineError, decode_png16, encode_png16
from plate_daemon.quotes import quote_run
from plate_daemon.route_health import Tracker
from plate_engines.health import status_of

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
RECIPES = ROOT / "daemon" / "plate_daemon" / "recipes"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
H, W = 360, 640
SKY, SHRUB, POOL = "o:929c9e95", "o:55cbe981", "o:c8a9bf52"  # editable on room_a
LOCKED_WALL = "o:57b3d007"
OPAQUE = re.compile(r"^[0-9a-f]{26}\.png$")
DRIFT_PX = 12  # past the +-8 px registration window
# A local engine that takes a mask and up to four references, at any size (what the fake stands in for).
MASK_AND_REFS = {
    "sizes": {"auto": True},
    "inputs": [
        {"name": "image", "type": "image", "required": True},
        {"name": "mask", "type": "mask", "required": False},
        {"name": "reference_images", "type": "images", "required": False, "max_items": 4},
    ],
}

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


class Recorder(FakeEngine):
    """The echoing FakeEngine with a payload journal (see the module docstring)."""

    def __init__(self, *args, manifest=None, darken: float | None = None, **kwargs):
        kwargs.setdefault("echo", True)
        super().__init__(*args, **kwargs)
        if manifest is not None:
            self.manifest = manifest
        routes = getattr(manifest, "routes", None)
        if routes:  # in this process, so C0; the route id is the manifest's so the quote prices it
            self.route = {
                "engine_id": manifest.id,
                "provider": routes[0].provider,
                "region": "local",
                "route_id": routes[0].route_id,
                "custody_grade": "C0",
                "custody_status": "confirmed",
                "upload_visibility": "local",
                "model_version": "test-double",
            }
        self.darken = darken
        self.payloads: list[dict] = []

    def submit(self, payload, idempotency_key):
        self.payloads.append(copy.deepcopy(payload))
        if self.darken is not None and "mask" in payload["inputs"]:
            payload = copy.deepcopy(payload)
            img = _decode(payload["inputs"]["image"]["b64"], cv2.IMREAD_UNCHANGED)
            sel = _decode(payload["inputs"]["mask"]["b64"], cv2.IMREAD_GRAYSCALE) > 127
            img[sel] = np.rint(img[sel] * self.darken).astype(img.dtype)
            payload["inputs"]["image"]["b64"] = base64.b64encode(cv2.imencode(".png", img)[1]).decode("ascii")
        return super().submit(payload, idempotency_key)


# ---------------------------------------------------------------------------- set-up


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    return Settings.load()


def _client(settings: Settings, engine=None) -> TestClient:
    c = TestClient(create_app(settings, engine or Recorder()), base_url=BASE)
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
    return r.json()["packet_id"]


def _setup(c: TestClient, auth: dict, *, nda: bool = False) -> str:
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=auth)
    assert r.status_code == 201, r.text
    return _ingest(c, auth, ROOM_A)


def _put_graph(c: TestClient, auth: dict, nodes: list[dict], edges: list[dict]) -> None:
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text


def _plate(packet_id: str) -> dict:
    return {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}


def _edit(nid: str = "e", engine: str = "fake", **params) -> dict:
    return {"id": nid, "kind": "Edit", "label": "Edit", "params": {"engine": engine, **params}}


def _mask(nid: str, ids: list[str]) -> dict:
    return {"id": nid, "kind": "Mask", "params": {"ids": ids, "textLock": True}}


def _reference(nid: str, key: str | None, role: str | None = "object") -> dict:
    params: dict = {}
    if key is not None:
        params["referenceHash"] = key
    if role is not None:
        params["role"] = role
    return {"id": nid, "kind": "Reference", "label": "Reference", "params": params}


def _edge(source: str, target: str, handle: str | None = None) -> dict:
    e = {"id": f"{source}->{target}", "source": source, "target": target}
    if handle is not None:
        e["targetHandle"] = handle
    return e


def _quote(c: TestClient, body: dict, node: str = "e"):
    pl = c.app.state.pipeline
    with c.app.state.index.project(PROJECT) as conn:
        return pl.quote_node(conn, PROJECT, node, body)


def _refusal(c: TestClient, body: dict, node: str = "e") -> PipelineError:
    with pytest.raises(PipelineError) as e:
        _quote(c, body, node)
    return e.value


def _run_count(c: TestClient) -> int:
    with c.app.state.index.project(PROJECT) as conn:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def _approve(c: TestClient, auth: dict, run_id: str) -> dict:
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth)
    assert r.status_code == 200, r.text
    return c.get(f"/api/v0/runs/{run_id}", headers=auth).json()


def _gate(c: TestClient, auth: dict, run_id: str) -> dict:
    r = c.get(f"/api/v0/runs/{run_id}/gate", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _cas(settings: Settings) -> CAS:
    return CAS(settings.projects_dir / PROJECT / "assets")


def _grey(cas: CAS, key: str) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(cas.read_bytes(key), np.uint8), cv2.IMREAD_GRAYSCALE) > 127


def _records(settings: Settings, run_id: str) -> list[dict]:
    return [r for r in Ledger(settings.projects_dir / PROJECT).records() if r.get("run_id") == run_id]


def _upload_reference(c: TestClient, auth: dict, w: int, h: int, colour=(40, 160, 220)) -> str:
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = colour
    img[: h // 2, : w // 2] = (250, 250, 250)
    png = bytes(cv2.imencode(".png", img)[1])
    r = c.post(
        f"/api/v0/assets?kind=reference&project_id={PROJECT}",
        content=png,
        headers={**auth, "Content-Type": "image/png"},
    )
    assert r.status_code == 201, r.text
    assert (r.json()["width"], r.json()["height"]) == (w, h)
    return r.json()["hash"]


def _plate16() -> np.ndarray:
    from plate_pixels import imageio

    return np.rint(imageio.read(ROOM_A / "display.tif").rgb * 65535).astype(np.uint16)


# ---------------------------------------------------------------------------- references (decision 46)


def test_a_reference_node_reaches_the_engine_as_an_opaque_png_and_is_ledgered(sync_settings, auth):
    engine = Recorder(manifest=MASK_AND_REFS)
    with _client(sync_settings, engine) as c:
        packet_id = _setup(c, auth)
        ref = _upload_reference(c, auth, 48, 32)
        nodes = [_plate(packet_id), _mask("m", [SKY]), _reference("r", ref), _edit()]
        edges = [_edge("p", "m"), _edge("m", "e"), _edge("r", "e", "refs")]
        _put_graph(c, auth, nodes, edges)

        r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
        assert r.status_code == 200, r.text
        run = r.json()["run"]
        assert run["inputs"]["reference_images"] == [{"hash": ref, "role": "object", "node": "r"}]
        assert {n["id"] for n in run["inputs"]["subgraph"]["nodes"]} == {"p", "m", "r", "e"}
        done = _approve(c, auth, run["id"])
        assert done["state"] == "DONE", done.get("error")

        sent = engine.payloads[-1]["inputs"]["reference_images"]
        assert len(sent) == 1 and sent[0]["mime"] == "image/png" and sent[0]["role"] == "object"
        assert OPAQUE.match(sent[0]["name"]) and PROJECT not in sent[0]["name"]
        pixels = _decode(sent[0]["b64"], cv2.IMREAD_UNCHANGED)
        assert pixels.shape == (32, 48, 3)  # an auto-size engine: never resized
        assert (
            sent[0]["name"] == f"{pipeline_mod.blake3(base64.b64decode(sent[0]['b64'])).hexdigest()[:26]}.png"
        )

        submitted = next(x for x in _records(sync_settings, run["id"]) if x["kind"] == "submitted")
        assert submitted["data"]["references"] == [ref]
        # The reference is an engine input only: the composite is the plate outside the mask, bit for bit.
        assert _gate(c, auth, run["id"])["metrics"]["outside_mask"]["identical_outside"] is True
        out = decode_png16(_cas(sync_settings).read_bytes(done["outputs"][0]))
        staged = _grey(_cas(sync_settings), done["normalized"]["mask_key"])
        assert np.array_equal(out[~staged], _plate16()[~staged])


def test_two_quotes_with_different_references_each_start_at_attempt_one(sync_settings, auth):
    with _client(sync_settings, Recorder(manifest=MASK_AND_REFS)) as c:
        packet_id = _setup(c, auth)
        a = _upload_reference(c, auth, 40, 30, colour=(10, 200, 30))
        b = _upload_reference(c, auth, 40, 30, colour=(200, 10, 30))
        base_nodes = [_plate(packet_id), _mask("m", [SKY]), _edit()]
        base_edges = [_edge("p", "m"), _edge("m", "e")]
        _put_graph(c, auth, [*base_nodes, _reference("r", a)], [*base_edges, _edge("r", "e", "refs")])
        first, _q = _quote(c, {})
        # A null target handle resolves to the first input that takes a reference: refs.
        _put_graph(c, auth, [*base_nodes, _reference("r", b)], [*base_edges, _edge("r", "e")])
        other, _q = _quote(c, {})
        assert other.inputs["reference_images"][0]["hash"] == b
        _put_graph(c, auth, [*base_nodes, _reference("r", a)], [*base_edges, _edge("r", "e", "refs")])
        again, _q = _quote(c, {})
        assert (first.attempt, other.attempt, again.attempt) == (1, 1, 2)


def test_references_are_refused_before_any_run_row(sync_settings, auth):
    with _client(sync_settings, Recorder()) as c:  # the fake declares no reference_images input
        packet_id = _setup(c, auth)
        ref = _upload_reference(c, auth, 32, 32)
        plate, mask = _plate(packet_id), _mask("m", [SKY])
        edges = [_edge("p", "m"), _edge("m", "e")]

        _put_graph(c, auth, [plate, mask, _reference("r", ref), _edit()], [*edges, _edge("r", "e", "refs")])
        e = _refusal(c, {})
        assert (e.code, e.status) == ("references_unsupported", 422)

        refs = [_reference(f"r{i}", ref) for i in range(14)]
        _put_graph(
            c,
            auth,
            [plate, mask, *refs, _edit(engine="nano-banana-2.1")],
            [*edges, *(_edge(f"r{i}", "e", "refs") for i in range(14))],
        )
        e = _refusal(c, {"prompt": "late blue hour"})
        assert e.code == "too_many_references" and e.extra["max_items"] == 13
        # Thirteen is what nano-banana-2.1 takes.
        _put_graph(
            c,
            auth,
            [plate, mask, *refs[:13], _edit(engine="nano-banana-2.1")],
            [*edges, *(_edge(f"r{i}", "e", "refs") for i in range(13))],
        )
        run, _q = _quote(c, {"prompt": "late blue hour"})
        assert len(run.inputs["reference_images"]) == 13 and run.route.engine_id == "nano-banana-2.1"
        assert _run_count(c) == 1

        nano = _edit(engine="nano-banana-2.1")
        for bad in ("a" * 64, None):
            _put_graph(c, auth, [plate, mask, _reference("r", bad), nano], [*edges, _edge("r", "e", "refs")])
            assert _refusal(c, {"prompt": "x"}).code == "reference_missing"
        _put_graph(
            c, auth, [plate, mask, _reference("r", ref, "mood"), nano], [*edges, _edge("r", "e", "refs")]
        )
        assert _refusal(c, {"prompt": "x"}).code == "bad_reference"
        assert _run_count(c) == 1


def test_a_reference_is_never_longer_than_the_engines_largest_legal_side(sync_settings, auth):
    with _client(sync_settings) as c:
        pl = c.app.state.pipeline
        flux = Recorder(manifest=pl.manifests["flux-3-image"])  # legal sides 1024 and 1440
        pl.engines["flux-3-image"] = flux
        packet_id = _setup(c, auth)
        wide = _upload_reference(c, auth, 2000, 500)
        small = _upload_reference(c, auth, 100, 50)
        nodes = [_plate(packet_id), _mask("m", [SKY]), _reference("r1", wide), _reference("r2", small, None)]
        nodes.append(_edit(engine="flux-3-image"))
        edges = [_edge("p", "m"), _edge("m", "e"), _edge("r1", "e", "refs"), _edge("r2", "e", "refs")]
        _put_graph(c, auth, nodes, edges)
        run, _q = _quote(c, {"prompt": "late blue hour"})
        assert [r["role"] for r in run.inputs["reference_images"]] == ["object", "style"]  # style by default
        done = _approve(c, auth, run.id)
        assert done["state"] in ("DONE", "GATE_FAIL"), done.get("error")
        sent = flux.payloads[-1]["inputs"]["reference_images"]
        assert _decode(sent[0]["b64"], cv2.IMREAD_UNCHANGED).shape[:2] == (360, 1440)  # INTER_AREA down
        assert _decode(sent[1]["b64"], cv2.IMREAD_UNCHANGED).shape[:2] == (50, 100)  # never enlarged
        assert sent[0]["name"] != sent[1]["name"] and all(OPAQUE.match(s["name"]) for s in sent)


# ---------------------------------------------------------------------------- the mask to engines


def _expected_engine_mask(mask: np.ndarray, sm: dict) -> np.ndarray:
    """The plate mask in the size map's padded rectangle (0 beyond the frame), at the sent size."""
    from plate_pixels.egress import SizeMap

    m = SizeMap.from_dict(sm)
    x0, y0, x1, y1 = m.padded_rect
    out = np.zeros((y1 - y0, x1 - x0), np.uint8)
    ix0, iy0, ix1, iy1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    out[iy0 - y0 : iy1 - y0, ix0 - x0 : ix1 - x0] = np.where(mask, 255, 0)[iy0:iy1, ix0:ix1]
    if (out.shape[1], out.shape[0]) != tuple(m.dst):
        out = cv2.resize(out, tuple(m.dst), interpolation=cv2.INTER_NEAREST)
    return out


@pytest.mark.parametrize("region_crop", [False, True])
def test_the_mask_goes_to_an_engine_that_declares_one_in_the_egress_geometry(
    sync_settings, auth, region_crop
):
    with _client(sync_settings) as c:
        pl = c.app.state.pipeline
        nano = Recorder(manifest=pl.manifests["nano-banana-2.1"])  # declares mask
        pl.engines["nano-banana-2.1"] = nano
        packet_id = _setup(c, auth)
        _put_graph(
            c,
            auth,
            [_plate(packet_id), _mask("m", [SHRUB, POOL]), _edit(engine="nano-banana-2.1")],
            [_edge("p", "m"), _edge("m", "e")],
        )
        run, _q = _quote(c, {"prompt": "late blue hour", "region_crop": region_crop})
        done = _approve(c, auth, run.id)
        assert done["state"] in ("DONE", "GATE_FAIL"), done.get("error")
        assert done["normalized"]["mask_sent"] is True

        sent = nano.payloads[-1]["inputs"]
        image = _decode(sent["image"]["b64"], cv2.IMREAD_UNCHANGED)
        mask = _decode(sent["mask"]["b64"], cv2.IMREAD_UNCHANGED)
        assert mask.ndim == 2 and mask.shape == image.shape[:2]  # registers with the egress image
        assert set(np.unique(mask)) <= {0, 255} and (mask == 255).any()
        assert sent["mask"]["role"] == "mask" and OPAQUE.match(sent["mask"]["name"])
        staged = _grey(_cas(sync_settings), done["normalized"]["mask_key"])
        assert np.array_equal(mask, _expected_engine_mask(staged, done["normalized"]["sizemap"]))
        if region_crop:
            assert mask.shape != (H, W) and done["normalized"]["sizemap"]["region"] is not None


def test_no_mask_goes_where_it_is_not_declared_or_the_adapter_refuses_one(sync_settings, auth):
    engine = Recorder()  # declares nothing
    with _client(sync_settings, engine) as c:
        pl = c.app.state.pipeline
        packet_id = _setup(c, auth)
        _put_graph(
            c, auth, [_plate(packet_id), _mask("m", [SKY]), _edit()], [_edge("p", "m"), _edge("m", "e")]
        )
        run, _q = _quote(c, {})
        done = _approve(c, auth, run.id)
        assert done["state"] == "DONE" and done["normalized"]["mask_sent"] is False
        assert "mask" not in engine.payloads[-1]["inputs"]
        # Gemini's adapter has no mask channel (nano-banana-2.1 declares one): PLATE composites instead.
        nano, _q = _quote(c, {"engine_id": "nano-banana-2.1", "prompt": "x"})
        assert nano.route_dict["route_id"] == "gemini" and pl.sends_mask(nano) is False
        gpt, _q = _quote(c, {"engine_id": "gpt-image-2.5", "prompt": "x"})
        assert gpt.route_dict["route_id"] == "openai" and pl.sends_mask(gpt) is True


# ---------------------------------------------------------------------------- an Edit on a take (decision 47)


def test_an_edit_after_an_edit_starts_from_the_first_take_and_is_gated_on_the_union(sync_settings, auth):
    engine = Recorder(manifest=MASK_AND_REFS, darken=0.8)  # edits inside the mask it is sent
    with _client(sync_settings, engine) as c:
        packet_id = _setup(c, auth)
        cas = _cas(sync_settings)
        nodes = [_plate(packet_id), _mask("m1", [SKY]), _edit("e1"), _mask("m2", [SHRUB]), _edit("e2")]
        edges = [_edge("p", "m1"), _edge("m1", "e1"), _edge("p", "m2"), _edge("m2", "e2"), _edge("e1", "e2")]
        _put_graph(c, auth, nodes, edges)
        first, _q = _quote(c, {}, "e1")
        assert "base" not in first.inputs  # a Plate upstream: nothing changes
        one = _approve(c, auth, first.id)
        assert one["state"] == "DONE", one.get("error")
        assert "base" not in one["normalized"] and "mask_union_key" not in one["normalized"]
        out1_key = one["outputs"][0]

        nodes[2] = {**nodes[2], "outputHash": out1_key}  # the artist picked the take
        _put_graph(c, auth, nodes, edges)
        second, _q = _quote(c, {}, "e2")
        assert second.inputs["base"] == out1_key
        assert (second.inputs["base_node"], second.inputs["base_run"]) == ("e1", first.id)
        assert second.inputs["mask"]["source"] == "mask:m2"
        two = _approve(c, auth, second.id)
        assert two["state"] == "DONE", two.get("error")
        assert two["normalized"]["base"] == out1_key

        plate = _plate16()
        out1 = decode_png16(cas.read_bytes(out1_key))
        out2 = decode_png16(cas.read_bytes(two["outputs"][0]))
        mask1 = _grey(cas, one["normalized"]["mask_key"])
        mask2 = _grey(cas, two["normalized"]["mask_key"])
        union = _grey(cas, two["normalized"]["mask_union_key"])
        assert mask1.any() and mask2.any() and not (mask1 & mask2).any()
        assert np.array_equal(union, mask1 | mask2)
        # The second engine saw the first take: the sky it was sent is the darkened one.
        sent1 = _decode(engine.payloads[0]["inputs"]["image"]["b64"], cv2.IMREAD_COLOR)
        sent2 = _decode(engine.payloads[1]["inputs"]["image"]["b64"], cv2.IMREAD_COLOR)
        assert sent2[mask1].mean() < sent1[mask1].mean() - 10
        assert (out1[mask1] != plate[mask1]).any() and (out2[mask2] != out1[mask2]).any()
        # Outside its own mask the second take is the first, and outside the union it is the plate.
        assert np.array_equal(out2[~mask2], out1[~mask2])
        assert np.array_equal(out2[~union], plate[~union])
        rep = _gate(c, auth, second.id)
        assert rep["metrics"]["outside_mask"]["identical_outside"] is True
        assert rep["metrics"]["base_hash"] == out1_key


def test_an_edit_on_a_take_with_no_output_is_refused_before_any_run_row(sync_settings, auth):
    with _client(sync_settings, Recorder()) as c:
        packet_id = _setup(c, auth)
        cas = c.app.state.pipeline.cas_for(PROJECT)
        nodes = [_plate(packet_id), _mask("m1", [SKY]), _edit("e1"), _edit("e2")]
        edges = [_edge("p", "m1"), _edge("m1", "e1"), _edge("e1", "e2")]
        _put_graph(c, auth, nodes, edges)
        e = _refusal(c, {}, "e2")
        assert (e.code, e.status) == ("no_base", 422)
        assert str(e) == (
            "e1 has no output in this project's CAS; make and pick a take first (an Edit never silently falls "
            "back to the plate)"
        )
        nodes[2] = {**nodes[2], "outputHash": "b" * 64}  # not in the CAS
        _put_graph(c, auth, nodes, edges)
        assert _refusal(c, {}, "e2").code == "no_base"
        small = cas.put_bytes(encode_png16(np.zeros((10, 20, 3), np.uint16)), "png")
        nodes[2] = {**nodes[2], "outputHash": small}
        _put_graph(c, auth, nodes, edges)
        assert _refusal(c, {}, "e2").code == "base_size"
        # A mask wired to the image input by name is not a still.
        _put_graph(c, auth, nodes[:2] + [_edit("e2")], [_edge("p", "m1"), _edge("m1", "e2", "image")])
        assert _refusal(c, {}, "e2").code == "bad_base"
        assert _run_count(c) == 0


def test_a_take_made_outside_the_pipeline_is_gated_on_its_pixel_difference(sync_settings, auth):
    """A base with no DONE run behind it (here a hand-made PNG): the union is where it differs from the plate."""
    with _client(sync_settings, Recorder(manifest=MASK_AND_REFS)) as c:
        packet_id = _setup(c, auth)
        pl = c.app.state.pipeline
        plate = _plate16()
        painted = plate.copy()
        painted[20:60, 30:90] = (60000, 1000, 1000)
        key = pl.cas_for(PROJECT).put_bytes(encode_png16(painted), "png")
        nodes = [_plate(packet_id), {**_edit("e1"), "outputHash": key}, _mask("m2", [SHRUB]), _edit("e2")]
        _put_graph(c, auth, nodes, [_edge("p", "e1"), _edge("p", "m2"), _edge("m2", "e2"), _edge("e1", "e2")])
        run, _q = _quote(c, {}, "e2")
        assert (run.inputs["base"], run.inputs["base_run"]) == (key, None)
        done = _approve(c, auth, run.id)
        assert done["state"] in ("DONE", "GATE_FAIL"), done.get("error")
        cas = _cas(sync_settings)
        union = _grey(cas, done["normalized"]["mask_union_key"])
        mask2 = _grey(cas, done["normalized"]["mask_key"])
        assert np.array_equal(union, mask2 | np.any(painted != plate, axis=2))
        out = decode_png16(cas.read_bytes(done["outputs"][0]))
        assert np.array_equal(
            out[20:60, 30:90][~mask2[20:60, 30:90]], painted[20:60, 30:90][~mask2[20:60, 30:90]]
        )


# ---------------------------------------------------------------------------- recipe tokens (decision 50)


def test_tokens_fill_a_recipe_and_never_reach_the_engine(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        nodes = [_plate(packet_id), _mask("m", [SKY, SHRUB])]
        edges = [_edge("p", "m"), _edge("m", "e")]
        edit = _edit(engine="nano-banana-2.1", recipe="season_swap", tokens={"[SEASON]": "winter"})
        _put_graph(c, auth, [*nodes, edit], edges)
        run, _q = _quote(c, {})
        assert run.inputs["recipe_id"] == "season_swap" and run.inputs["prompt_source"] == "recipe"
        assert "Shift the season to winter at the same hour" in run.inputs["prompt"]
        assert run.inputs["recipe_tokens"] == {"[SEASON]": "winter"} and "tokens" not in run.params
        # The quote body's params win over the node's; without either the recipe's default fills the token.
        run, _q = _quote(c, {"params": {"tokens": {"[SEASON]": "spring"}}})
        assert "Shift the season to spring" in run.inputs["prompt"] and "tokens" not in run.params
        _put_graph(c, auth, [*nodes, _edit(engine="nano-banana-2.1", recipe="season_swap")], edges)
        run, _q = _quote(c, {})
        assert "Shift the season to autumn" in run.inputs["prompt"] and "recipe_tokens" not in run.inputs
        # An explicit prompt beats the recipe, so the tokens are not recorded.
        run, _q = _quote(c, {"prompt": "dusk", "params": {"tokens": {"[SEASON]": "winter"}}})
        assert run.inputs["prompt"] == "dusk" and "recipe_tokens" not in run.inputs
        assert _refusal(c, {"params": {"tokens": {"SEASON": "winter"}}}).code == "bad_tokens"
        assert _refusal(c, {"params": {"tokens": ["winter"]}}).code == "bad_tokens"


def test_a_recipe_in_the_project_folder_is_found_by_id(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        doc = yaml.safe_load((RECIPES / "season_swap.yaml").read_text(encoding="utf-8"))
        doc.update(id="our_season", title="Our season")
        folder = sync_settings.projects_dir / PROJECT / "recipes"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "our_season.yaml").write_text(
            yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n"
        )
        _put_graph(
            c,
            auth,
            [_plate(packet_id), _mask("m", [SKY]), _edit(engine="", recipe="our_season")],
            [_edge("p", "m"), _edge("m", "e")],
        )
        run, _q = _quote(c, {"params": {"tokens": {"[SEASON]": "monsoon"}}})
        assert run.inputs["recipe_id"] == "our_season" and run.route.engine_id == "nano-banana-2.1"
        assert "Shift the season to monsoon" in run.inputs["prompt"]


# ---------------------------------------------------------------------------- a pinned size map (decision 54)


def test_a_pinned_sizemap_and_png_are_staged_as_they_are(sync_settings, auth):
    engine = Recorder()
    with _client(sync_settings, engine) as c:
        pl = c.app.state.pipeline
        packet_id = _setup(c, auth)
        _put_graph(
            c, auth, [_plate(packet_id), _mask("m", [SHRUB]), _edit()], [_edge("p", "m"), _edge("m", "e")]
        )
        parent, _q = _quote(c, {"region_crop": True})
        done = _approve(c, auth, parent.id)
        assert done["state"] == "DONE", done.get("error")
        norm = done["normalized"]
        assert norm["sizemap"]["region"] is not None

        inputs = copy.deepcopy(parent.inputs)
        inputs["staging"] = {**inputs["staging"], "region_crop": False, "sizemap": norm["sizemap"]}
        inputs["staging"]["png_key"] = norm["egress_key"]
        with c.app.state.index.project(PROJECT) as conn:
            child = create_run(
                conn,
                RunCreate(
                    project_id=PROJECT,
                    packet_id=packet_id,
                    node_id="e",
                    inputs=inputs,
                    params=dict(parent.params),
                    route=parent.route_dict,
                ),
            )
            quote_run(conn, child.id, manifests=pl.manifests)
            child = get_run(conn, child.id)
        staged = pl.stage(child)
        cas = _cas(sync_settings)
        assert staged.png == cas.read_bytes(norm["egress_key"]) and staged.png_key == norm["egress_key"]
        assert staged.sizemap.to_dict() == norm["sizemap"]  # not a whole-frame prepare
        assert np.array_equal(staged.mask, _grey(cas, norm["mask_key"]))  # the mask is recomputed
        again = _approve(c, auth, child.id)
        assert again["state"] == "DONE", again.get("error")
        assert again["normalized"]["sizemap"] == norm["sizemap"]
        assert np.array_equal(
            decode_png16(cas.read_bytes(again["outputs"][0])),
            decode_png16(cas.read_bytes(done["outputs"][0])),
        )

        missing = copy.deepcopy(inputs)
        missing["staging"]["png_key"] = "c" * 64
        with c.app.state.index.project(PROJECT) as conn:
            orphan = create_run(
                conn,
                RunCreate(
                    project_id=PROJECT,
                    packet_id=packet_id,
                    node_id="e",
                    inputs=missing,
                    route=parent.route_dict,
                ),
            )
        with pytest.raises(PipelineError) as e:
            pl.stage(orphan)
        assert e.value.code == "pinned_png_missing"


# ---------------------------------------------------------------------------- ingress hooks (decision 55)


def test_an_ingress_hook_runs_on_the_engine_bytes_before_restore(sync_settings, auth):
    with _client(sync_settings, Recorder()) as c:
        pl = c.app.state.pipeline
        seen: list = []

        def shift(run, data, staged, pv):
            seen.append((run.id, staged.opaque_name, pv.packet_id))
            return shift_right(data, DRIFT_PX), {"shift_px": DRIFT_PX}

        pl.ingress_hooks.clear()  # create_app registers the AI-layer upscale (T107); only `shift` is under test
        pl.ingress_hooks.append(("shift", shift))
        packet_id = _setup(c, auth)
        _put_graph(
            c, auth, [_plate(packet_id), _mask("m", [SKY]), _edit()], [_edge("p", "m"), _edge("m", "e")]
        )
        run, _q = _quote(c, {})
        done = _approve(c, auth, run.id)
        assert done["state"] == "GATE_FAIL", done.get("error")
        assert done["normalized"]["ingress_hooks"] == {"shift": {"shift_px": DRIFT_PX}}
        assert seen and seen[0][0] == run.id and seen[0][2] == packet_id
        rep = _gate(c, auth, run.id)
        assert rep["metrics"]["registration"]["camera_drift"] is True
        assert rep["metrics"]["outside_mask"]["identical_outside"] is True  # the plate is safe even so


def test_a_raising_ingress_hook_fails_the_run_with_its_name(sync_settings, auth):
    with _client(sync_settings, Recorder()) as c:
        pl = c.app.state.pipeline

        def boom(run, data, staged, pv):
            raise RuntimeError("the upscaler is out of memory")

        pl.ingress_hooks.append(("upscale", boom))
        packet_id = _setup(c, auth)
        _put_graph(
            c, auth, [_plate(packet_id), _mask("m", [SKY]), _edit()], [_edge("p", "m"), _edge("m", "e")]
        )
        run, _q = _quote(c, {})
        done = _approve(c, auth, run.id)
        assert (done["state"], done["reason_code"]) == ("FAILED", "ingress_hook:upscale")
        assert "out of memory" in (done.get("error") or "")
        failed = next(x for x in _records(sync_settings, run.id) if x["kind"] == "failed")
        assert failed["data"]["reason_code"] == "ingress_hook:upscale"
        assert not pl._hook_failed  # nothing left behind for the next step


# ---------------------------------------------------------------------------- degraded routes (decision 51)


def test_a_degraded_route_is_named_in_the_quote_and_never_routed_to(sync_settings, auth):
    with _client(sync_settings) as c:
        pl = c.app.state.pipeline
        tracker = Tracker(sync_settings.home)
        pl.route_tracker = tracker
        packet_id = _setup(c, auth)
        _put_graph(
            c, auth, [_plate(packet_id), _mask("m", [SKY]), _edit()], [_edge("p", "m"), _edge("m", "e")]
        )
        body = {"engine_id": "nano-banana-2.1", "prompt": "late blue hour"}
        run, _q = _quote(c, body)
        assert not any("degraded" in x for x in run.inputs["router"]["reasons"])

        for _ in range(3):
            tracker.note_failure("nano-banana-2.1", "atlas", "provider_poll_error")
        board = pl.health_board()
        assert status_of(board["nano-banana-2.1/atlas"]) == "degraded"
        assert status_of(board["nano-banana-2.1/gemini"]) == "ok"
        r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json=body, headers=auth)
        assert r.status_code == 200, r.text
        named = [
            x for x in r.json()["router"]["reasons"] if x.startswith("health: nano-banana-2.1/atlas degraded")
        ]
        assert named and "3 consecutive provider failures" in named[0] and "provider_poll_error" in named[0]
        assert r.json()["run"]["route"]["route_id"] == "gemini"

        for _ in range(3):
            tracker.note_failure("nano-banana-2.1", "gemini", "submit_error")
        e = _refusal(c, body)
        assert e.code == "router_refused:health" and "degraded" in str(e)
        tracker.reset("nano-banana-2.1/gemini")
        assert _quote(c, body)[0].route_dict["route_id"] == "gemini"

        # An in-process engine is not routed by the router; its degraded route is still named.
        for _ in range(3):
            tracker.note_failure("fake", "fake-local", "provider_poll_error")
        run, _q = _quote(c, {})
        assert any(x.startswith("health: fake/fake-local degraded") for x in run.inputs["router"]["reasons"])


def test_no_tracker_or_an_unreadable_one_degrades_nothing(sync_settings, auth):
    with _client(sync_settings) as c:
        pl = c.app.state.pipeline
        pl.route_tracker = None  # create_app attaches a Tracker (T107); this test is about having none
        assert pl.route_tracker is None and pl.degraded_routes() == {}

        class Broken:
            def degraded(self):
                raise OSError("the disk went away")

        pl.route_tracker = Broken()
        assert pl.degraded_routes() == {}
        assert all(status_of(v) != "degraded" for v in pl.health_board().values())


def test_the_adapter_bridge_hands_every_image_input_over_as_bytes():
    class Adapter:
        engine_id = "stub"

        def __init__(self):
            self.staged = None

        def validate_params(self, params):
            return params

        def submit(self, staged):
            self.staged = staged
            return "job-1"

    png = encode_png16(np.zeros((4, 6, 3), np.uint16))
    b64 = base64.b64encode(png).decode("ascii")
    adapter = Adapter()
    bridge = pipeline_mod.AdapterBridge(adapter)
    payload = {
        "inputs": {
            "image": {"b64": b64, "name": "a.png", "role": "plate"},
            "mask": {"b64": b64, "name": "m.png", "role": "mask"},
            "reference_images": [{"b64": b64, "name": "r.png", "role": "object"}],
            "prompt": "x",
        },
        "params": {},
    }
    assert bridge.submit(payload, "key") == "job-1"
    got = adapter.staged.inputs
    assert got["image"]["bytes"] == png and "b64" not in got["image"] and got["image"]["role"] == "plate"
    assert got["mask"]["bytes"] == png and got["mask"]["role"] == "mask"
    assert got["reference_images"][0]["bytes"] == png and got["reference_images"][0]["role"] == "object"
    assert got["prompt"] == "x"


def test_the_module_keeps_its_public_names():
    for name in ("Staged", "IngressHook", "REFERENCE_INPUT", "repeat_key", "encode_png16"):
        assert name in pipeline_mod.__all__
