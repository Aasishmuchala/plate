"""Wave 8 pipeline doctrine at the quote (router plan T58), on the committed Blender packet `room_a`.

The quote now runs the router (doctrine, custody, pins, health, cards) and the render-it numbers before a run
row exists, builds the prompt from a recipe when none is given, takes a Mask node's ids, painted mask and
segment mask as one union that never leaves the packet's editable ids, numbers repeats itself, snapshots the
subgraph, and refuses to quote or approve what the ledger cannot record. Outputs carry an sRGB tag and a
perceptual hash, so a re-encoded copy dropped later still resolves. `EchoEngine` is the FakeEngine that
returns the PNG egress sent it, so the gates and the composite work on real pixels.
"""

from __future__ import annotations

import base64
import io
import json
import struct
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import plate_spec
from plate_daemon import pipeline as pipeline_mod
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.custody import CustodyViolation
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.llm.client import AnthropicClient, LlmResponse, Usage
from plate_daemon.machine import create_run
from plate_daemon.models import RunCreate
from plate_daemon.phash import PhashIndex, resolve_drop
from plate_daemon.pipeline import ASSUMED_OK, PipelineError, decode_png16, encode_png16
from plate_daemon.projects import Project
from plate_daemon.quotes import quote_run
from plate_engines.health import AlertLog
from plate_gates import router_math

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
H, W = 360, 640
VERDICTS = {"vray_only", "ai_by_region", "whole_frame_ok"}
NIGHT_RECIPE_OPENING = "This is an approved architectural render"

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        return self.sent[result_url[len(RESULT_SCHEME) :]]


class CannedTransport:
    """An LLM transport that answers every request with one JSON reply (records what it was asked)."""

    name = "canned"

    def __init__(self, reply: dict):
        self.text = json.dumps(reply)
        self.requests: list = []

    def send(self, request):
        self.requests.append(request)
        return LlmResponse(
            text=self.text, usage=Usage(1200, 300), model=request.model, key=request.key, transport=self.name
        )


# ---------------------------------------------------------------------------- set-up


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    return Settings.load()


def _client(settings: Settings, engine=None) -> TestClient:
    c = TestClient(create_app(settings, engine or EchoEngine()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


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


def _graph(packet_id: str, engine: str = "fake", mask: dict | None = None) -> dict:
    nodes = [
        {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
        {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": engine}},
    ]
    edges = [{"id": "p->e", "source": "p", "target": "e"}]
    if mask is not None:
        nodes.insert(1, {"id": "m", "kind": "Mask", "params": mask})
        edges = [{"id": "p->m", "source": "p", "target": "m"}, {"id": "m->e", "source": "m", "target": "e"}]
    return {"nodes": nodes, "edges": edges}


def _setup(
    c: TestClient, auth: dict, *, nda: bool = False, engine: str = "fake", mask: dict | None = None
) -> str:
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=auth)
    assert r.status_code == 201, r.text
    packet_id = _ingest(c, auth, ROOM_A)
    _put_graph(c, auth, _graph(packet_id, engine, mask))
    return packet_id


def _put_graph(c: TestClient, auth: dict, graph: dict) -> None:
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth)
    assert r.status_code == 200, r.text


def _quote_http(c: TestClient, auth: dict, **body):
    return c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json=body, headers=auth)


def _quote(c: TestClient, body: dict, node: str = "e"):
    """`Pipeline.quote_node` directly: the body fields T59 adds to the route model (task, shot_class, recipe,
    no attempt) reach the pipeline as they will once the route passes them."""
    pl = c.app.state.pipeline
    with c.app.state.index.project(PROJECT) as conn:
        return pl.quote_node(conn, PROJECT, node, body)


def _refusal(c: TestClient, body: dict) -> PipelineError:
    with pytest.raises(PipelineError) as e:
        _quote(c, body)
    return e.value


def _run_count(c: TestClient) -> int:
    with c.app.state.index.project(PROJECT) as conn:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def _approve(c: TestClient, auth: dict, run_id: str, by: str = "auto") -> dict:
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": by}, headers=auth)
    assert r.status_code == 200, r.text
    return c.get(f"/api/v0/runs/{run_id}", headers=auth).json()


def _pins(settings: Settings, pins: dict) -> None:
    project = Project.load(settings.projects_dir / PROJECT)
    project.doc["engine_pins"] = pins
    project.save()


def _ids_mask(pv, ids) -> np.ndarray:
    out = np.zeros((H, W), dtype=bool)
    for cid in ids:
        try:
            out |= pv.id_mask(cid)
        except PipelineError:
            continue
    return out


def _png_mask(mask: np.ndarray) -> bytes:
    return bytes(cv2.imencode(".png", np.where(mask, 255, 0).astype(np.uint8))[1])


def _plate16() -> np.ndarray:
    from plate_pixels import imageio

    return np.rint(imageio.read(ROOM_A / "display.tif").rgb * 65535).astype(np.uint16)


def _records(settings: Settings, run_id: str) -> list[dict]:
    return [r for r in Ledger(settings.projects_dir / PROJECT).records() if r.get("run_id") == run_id]


def _chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    out, pos = [], 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        ctype = png[pos + 4 : pos + 8]
        out.append((ctype, png[pos + 8 : pos + 8 + length]))
        pos += 12 + length
    return out


# ---------------------------------------------------------------------------- routing doctrine at the quote


def test_a_quote_carries_router_reasons_and_the_render_it_verdict(sync_settings, auth):
    with _client(sync_settings) as c:
        _setup(c, auth)
        r = _quote_http(c, auth)
        assert r.status_code == 200, r.text
        run = r.json()["run"]
        assert run["route"]["reasons"] and any("relight" in x for x in run["route"]["reasons"])
        router = run["inputs"]["router"]
        assert router["verdict"] in VERDICTS and router["mode"] == "advisory"
        assert router["D_px_median"] > 0 and set(router["per_class"]) <= {"vegetation", "ground", "furniture"}
        assert router["reasons"][0].startswith("render-it:")
        staging = run["inputs"]["staging"]
        assert (staging["task"], staging["shot_class"]) == ("relight", "wide")

        # The subgraph the run was quoted from, as project.json holds it.
        sub = run["inputs"]["subgraph"]
        assert {n["id"] for n in sub["nodes"]} == {"p", "e"}
        assert [(e["source"], e["target"]) for e in sub["edges"]] == [("p", "e")]


def test_a_manifest_engine_is_routed_by_the_router_with_the_health_assumption_named(sync_settings, auth):
    with _client(sync_settings) as c:
        _setup(c, auth, nda=True)
        r = _quote_http(c, auth, engine_id="qwen-image-edit-2509", prompt="late blue hour")
        assert r.status_code == 200, r.text
        run = r.json()["run"]
        reasons = run["route"]["reasons"]
        assert run["route"]["route_id"] == "local" and any(x.startswith("custody:") for x in reasons)
        assert f"health: qwen-image-edit-2509/local {ASSUMED_OK}" in reasons
        assert any(ASSUMED_OK in x for x in run["inputs"]["router"]["reasons"])


def test_render_it_blocks_a_vray_only_frame_in_blocking_mode_before_any_run_row(
    sync_settings, auth, monkeypatch
):
    numbers = {
        "D_px_median": 3.25,
        "per_class": {"vegetation": 3.25},
        "per_id": {},
        "verdict": "vray_only",
        "n_pixels": 900,
        "fx": 622.2,
        "meters_per_unit": 1.0,
    }
    monkeypatch.setattr(router_math, "detail_resolvability", lambda *a, **k: dict(numbers))
    monkeypatch.setenv("PLATE_ROUTER_MODE", "blocking")
    with _client(sync_settings) as c:
        _setup(c, auth)
        r = _quote_http(c, auth)
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["error"] == "render_it" and body["router"]["verdict"] == "vray_only"
        assert body["router"]["mode"] == "blocking" and _run_count(c) == 0
        # People stay allowed: AI for people by region is the router's own exception.
        run, _q = _quote(c, {"task": "people"})
        assert run.inputs["router"]["verdict"] == "vray_only" and run.state.value == "QUOTED"
        # Advisory mode only reports.
        monkeypatch.setenv("PLATE_ROUTER_MODE", "advisory")
        r = _quote_http(c, auth)
        assert r.status_code == 200 and r.json()["run"]["inputs"]["router"]["verdict"] == "vray_only"
        monkeypatch.setenv("PLATE_ROUTER_MODE", "sometimes")
        assert _refusal(c, {}).code == "router_mode"


def test_a_retired_pin_and_a_pin_to_another_engine_are_refused(sync_settings, auth):
    with _client(sync_settings) as c:
        _setup(c, auth)
        body = {"engine_id": "nano-banana-2.1", "prompt": "late blue hour"}
        _pins(sync_settings, {"relight": {"engine_id": "nano-banana-2.1", "version": "0.0"}})
        r = _quote_http(c, auth, **body)
        assert r.status_code == 409 and r.json()["error"] == "retired_pin", r.text
        _pins(sync_settings, {"nano-banana-2.1": "0.0"})  # the {engine_id: version} form pins this engine too
        assert _quote_http(c, auth, **body).json()["error"] == "retired_pin"
        _pins(sync_settings, {"relight": "gpt-image-2.5"})
        r = _quote_http(c, auth, **body)
        assert r.status_code == 409 and r.json()["error"] == "pin_conflict"
        _pins(sync_settings, {"relight": {"engine_id": "nano-banana-2.1"}})
        r = _quote_http(c, auth, **body)
        assert r.status_code == 200, r.text
        reasons = r.json()["run"]["route"]["reasons"]
        assert any(x.startswith("pin:") for x in reasons) and r.json()["run"]["route"]["route_id"] == "gemini"
        assert _run_count(c) == 1


def test_doctrine_refusals_come_before_custody_and_engines(sync_settings, auth):
    with _client(sync_settings) as c:
        _setup(c, auth)
        e = _refusal(c, {"engine_id": "nano-banana-2.1", "task": "rephotograph_close", "shot_class": "wide"})
        assert e.code == "router_refused:doctrine" and e.status == 422
        # The local engine is held to the same doctrine.
        assert _refusal(c, {"task": "rephotograph_close"}).code == "router_refused:doctrine"
        assert _refusal(c, {"task": "text_to_image"}).code == "router_refused:doctrine"
        assert _refusal(c, {"task": "teleport"}).code == "bad_task"
        assert _refusal(c, {"shot_class": "aerial"}).code == "bad_shot_class"
        run, _q = _quote(
            c,
            {
                "engine_id": "nano-banana-2.1",
                "task": "rephotograph_close",
                "shot_class": "close",
                "prompt": "x",
            },
        )
        assert run.inputs["staging"]["task"] == "rephotograph_close" and run.state.value == "QUOTED"
        assert _run_count(c) == 1


def test_a_suspended_engine_is_refused_on_health_once_the_alert_lands(sync_settings, auth):
    with _client(sync_settings) as c:
        _setup(c, auth)
        body = {"engine_id": "nano-banana-2.1", "prompt": "late blue hour"}
        run, _q = _quote(c, body)
        assert run.state.value == "QUOTED"
        AlertLog(sync_settings.home / "logs" / "alerts.jsonl").write(
            "engine_suspended", "canary pass rate fell 30 points", engine_id="nano-banana-2.1"
        )
        e = _refusal(c, body)
        assert e.code == "router_refused:health" and "canary pass rate" in str(e)


def test_no_prompt_on_an_engine_that_needs_one_is_refused_before_a_run_row(sync_settings, auth):
    with _client(sync_settings) as c:
        _setup(c, auth)
        r = _quote_http(c, auth, engine_id="nano-banana-2.1")
        assert r.status_code == 422 and r.json()["error"] == "prompt_required", r.text
        assert _run_count(c) == 0
        # The local fake engine declares no prompt input: no prompt is fine there.
        assert _quote_http(c, auth).status_code == 200


# ---------------------------------------------------------------------------- the prompt from a recipe


def test_a_recipe_fills_the_prompt_from_the_template_when_the_composer_cannot_answer(sync_settings, auth):
    with _client(sync_settings) as c:
        _setup(c, auth)
        run, _q = _quote(c, {"engine_id": "nano-banana-2.1", "recipe": "night_relight_changelist"})
        assert run.inputs["prompt"].startswith(NIGHT_RECIPE_OPENING)
        assert "late blue hour" in run.inputs["prompt"]
        assert (
            run.inputs["prompt_source"] == "recipe" and run.inputs["recipe_id"] == "night_relight_changelist"
        )
        assert "negative_prompt" not in run.params  # nano-banana-2.1 declares no negative_prompt param
        # Explicit beats a recipe; the recipe is still named.
        run, _q = _quote(
            c, {"engine_id": "nano-banana-2.1", "recipe": "night_relight_changelist", "prompt": "dusk"}
        )
        assert (run.inputs["prompt"], run.inputs["prompt_source"]) == ("dusk", "explicit")
        assert run.inputs["recipe_id"] == "night_relight_changelist"
        # The recipe names its engine when the node does not.
        _put_graph(c, auth, _graph(run.packet_id, engine=""))
        run, _q = _quote(c, {"recipe": "night_relight_changelist"})
        assert run.route.engine_id == "nano-banana-2.1"
        assert _refusal(c, {"recipe": "no_such_recipe"}).code == "unknown_recipe"
        assert _refusal(c, {"recipe": "../../etc/passwd"}).code == "unknown_recipe"
        assert _refusal(c, {"recipe": "croppair_pushin"}).code == "recipe_kind"


def test_an_nda_recipe_stays_template_only_and_the_negative_goes_where_the_engine_takes_it(
    sync_settings, auth
):
    with _client(sync_settings) as c:
        _setup(c, auth, nda=True)
        run, _q = _quote(c, {"engine_id": "qwen-image-edit-2509", "recipe": "night_relight_changelist"})
        assert run.inputs["prompt_source"] == "recipe" and run.inputs["prompt"].startswith(
            NIGHT_RECIPE_OPENING
        )
        assert run.params["negative_prompt"].startswith("new objects, removed objects")


def test_the_composer_tailors_the_recipe_when_custody_allows_the_llm(sync_settings, auth, monkeypatch):
    transport = CannedTransport(
        {"prompt": "Relight to late blue hour; change nothing else.", "negative_prompt": "daylight"}
    )
    with _client(sync_settings) as c:
        _setup(c, auth)
        pl = c.app.state.pipeline
        made: list = []

        def client(project_id, allowed):
            cl = AnthropicClient(transport=transport, allowed_grades=sorted(g.value for g in allowed))
            made.append(cl)
            return cl

        monkeypatch.setattr(pl, "llm_client", client)
        run, _q = _quote(c, {"engine_id": "qwen-image-edit-2509", "recipe": "night_relight_changelist"})
        assert made and made[0].available
        assert run.inputs["prompt_source"] == "composed"
        assert run.inputs["prompt"] == "Relight to late blue hour; change nothing else."
        assert run.params["negative_prompt"] == "daylight"
        sent = json.dumps([dict(m) for m in transport.requests[0].messages])
        assert PROJECT not in sent and "CAM_a" not in sent  # the composer never names the project or camera


# ---------------------------------------------------------------------------- the mask union


def test_a_painted_mask_alone_quotes_and_stages_inside_the_editable_ids(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        painted = np.zeros((H, W), dtype=bool)
        painted[:, : W // 2] = True
        key = c.app.state.pipeline.cas_for(PROJECT).put_bytes(_png_mask(painted), "png")
        _put_graph(c, auth, _graph(packet_id, mask={"maskHash": key, "textLock": False}))
        r = _quote_http(c, auth)
        assert r.status_code == 200, r.text
        run_id = r.json()["run"]["id"]
        assert r.json()["run"]["inputs"]["mask"] == {
            "kind": "union",
            "ids": [],
            "keys": [key],
            "source": "mask:m",
        }
        run = _approve(c, auth, run_id)
        assert run["state"] in ("DONE", "GATE_FAIL"), run.get("error")

        pv = c.app.state.pipeline.packet(PROJECT, packet_id)
        expected = painted & _ids_mask(pv, pv.editable_ids()) & ~_ids_mask(pv, pv.locked_ids())
        cas = CAS(sync_settings.projects_dir / PROJECT / "assets")
        staged = cv2.imdecode(np.frombuffer(cas.read_bytes(run["normalized"]["mask_key"]), np.uint8), 0) > 127
        assert expected.any() and np.array_equal(staged, expected)

        # Refusals at the quote: a key that is not a CAS key, a mask that is not there, the wrong size.
        _put_graph(c, auth, _graph(packet_id, mask={"maskHash": "not-a-hash"}))
        assert _quote_http(c, auth).json()["error"] == "bad_mask"
        _put_graph(c, auth, _graph(packet_id, mask={"samHash": "a" * 64}))
        assert _quote_http(c, auth).json()["error"] == "mask_missing"
        small = c.app.state.pipeline.cas_for(PROJECT).put_bytes(_png_mask(np.ones((10, 10), bool)), "png")
        _put_graph(c, auth, _graph(packet_id, mask={"maskHash": small}))
        assert _quote_http(c, auth).json()["error"] == "mask_size"
        _put_graph(c, auth, _graph(packet_id, mask={"textLock": True}))
        assert _quote_http(c, auth).json()["error"] == "mask_empty"


def test_a_painted_mask_over_a_locked_id_composites_nothing_onto_it(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        key = c.app.state.pipeline.cas_for(PROJECT).put_bytes(_png_mask(np.ones((H, W), bool)), "png")
        sky = "o:929c9e95"
        _put_graph(c, auth, _graph(packet_id, mask={"ids": [sky], "maskHash": key}))
        r = _quote_http(c, auth)
        assert r.status_code == 200, r.text
        assert r.json()["run"]["inputs"]["mask"]["kind"] == "union"
        run = _approve(c, auth, r.json()["run"]["id"])
        assert run["state"] in ("DONE", "GATE_FAIL"), run.get("error")

        pv = c.app.state.pipeline.packet(PROJECT, packet_id)
        locked = _ids_mask(pv, pv.locked_ids())
        cas = CAS(sync_settings.projects_dir / PROJECT / "assets")
        out = decode_png16(cas.read_bytes(run["outputs"][0]))
        plate = _plate16()
        staged = cv2.imdecode(np.frombuffer(cas.read_bytes(run["normalized"]["mask_key"]), np.uint8), 0) > 127
        assert locked.any() and not (staged & locked).any()
        assert np.array_equal(
            out[locked], plate[locked]
        )  # the wall, the glass, the signage: the plate, bit for bit
        assert (out[staged] != plate[staged]).any()  # while the editable pixels did change
        # Asking for a locked id by name is still refused at the quote.
        _put_graph(c, auth, _graph(packet_id, mask={"ids": ["o:57b3d007"], "maskHash": key}))
        assert _quote_http(c, auth).json()["error"] == "mask_locked"


# ---------------------------------------------------------------------------- attempts, ledger fields, phash


def test_two_identical_quotes_number_themselves_and_both_run(sync_settings, auth):
    with _client(sync_settings) as c:
        _setup(c, auth)
        first, _q = _quote(c, {})
        second, _q = _quote(c, {})
        assert (first.attempt, second.attempt) == (1, 2)
        other, _q = _quote(c, {"region_crop": True})  # a different request starts again at 1
        assert other.attempt == 1
        assert _approve(c, auth, first.id)["state"] == "DONE"
        done = _approve(c, auth, second.id)
        assert done["state"] == "DONE", done.get("error")
        assert (
            done["idempotency_key"]
            != c.get(f"/api/v0/runs/{first.id}", headers=auth).json()["idempotency_key"]
        )

        quoted = {
            rid: next(r for r in _records(sync_settings, rid) if r["kind"] == "quoted")
            for rid in (first.id, second.id)
        }
        data = quoted[first.id]["data"]
        assert (data["model"], data["task"], data["shot_class"]) == ("fake", "relight", "wide")
        assert len(data["repeat_key"]) == 16 and data["repeat_key"] == quoted[second.id]["data"]["repeat_key"]
        submitted = next(r for r in _records(sync_settings, second.id) if r["kind"] == "submitted")
        assert (
            submitted["data"]["repeat_key"] == data["repeat_key"] and submitted["data"]["task"] == "relight"
        )
        other_q = next(r for r in _records(sync_settings, other.id) if r["kind"] == "quoted")
        assert other_q["data"]["repeat_key"] != data["repeat_key"]


def test_a_reencoded_copy_of_an_output_resolves_by_phash(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        _setup(c, auth)
        run = _approve(c, auth, _quote_http(c, auth).json()["run"]["id"])
        assert run["state"] == "DONE", run.get("error")
        out_key = run["outputs"][0]
        cas = CAS(sync_settings.projects_dir / PROJECT / "assets")
        rgb16 = decode_png16(cas.read_bytes(out_key))
        bgr8 = np.ascontiguousarray((rgb16 >> 8).astype(np.uint8)[..., ::-1])
        dropped = tmp_path / "from_the_client.jpg"
        dropped.write_bytes(bytes(cv2.imencode(".jpg", bgr8, [cv2.IMWRITE_JPEG_QUALITY, 80])[1]))
        hit = resolve_drop(dropped, PhashIndex(c.app.state.index, project_id=PROJECT))
        assert hit is not None and hit["by"] == "phash" and hit["content_hash"] == out_key


def test_encode_png16_tags_srgb_and_round_trips():
    rng = np.random.default_rng(7)
    rgb = rng.integers(0, 65535, (24, 40, 3)).astype(np.uint16)
    png = encode_png16(rgb)
    chunks = _chunks(png)
    names = [t for t, _ in chunks]
    assert names[:3] == [b"IHDR", b"sRGB", b"gAMA"] and names[-1] == b"IEND"
    assert dict(chunks)[b"sRGB"] == b"\x00" and struct.unpack(">I", dict(chunks)[b"gAMA"])[0] == 45455
    assert b"iCCP" not in names and b"tEXt" not in names
    assert np.array_equal(decode_png16(png), rgb)
    with Image.open(io.BytesIO(png)) as im:
        assert im.size == (40, 24) and im.info.get("srgb") == 0


# ---------------------------------------------------------------------------- the ledger fails closed


def _flaky_ledger(monkeypatch, failing_kind: str) -> None:
    orig = Ledger.append

    def append(self, record=None, /, **fields):
        kind = (record or fields).get("kind")
        if kind == failing_kind:
            raise OSError("the NAS went away")
        return orig(self, record, **fields)

    monkeypatch.setattr(Ledger, "append", append)


def test_a_quote_the_ledger_cannot_record_is_cancelled_with_503(sync_settings, auth, monkeypatch):
    with _client(sync_settings) as c:
        _setup(c, auth)
        _flaky_ledger(monkeypatch, "quoted")
        r = _quote_http(c, auth)
        assert r.status_code == 503 and r.json()["error"] == "ledger_unavailable", r.text
        with c.app.state.index.project(PROJECT) as conn:
            rows = conn.execute("SELECT id, state, reason_code FROM runs").fetchall()
        assert [(row["state"], row["reason_code"]) for row in rows] == [("CANCELLED", "ledger_unavailable")]
        kinds = [x["kind"] for x in _records(sync_settings, rows[0]["id"])]
        assert kinds == ["cancelled"]  # the cancellation is in the folder; the quote never was


def test_an_approval_the_ledger_cannot_record_is_cancelled_and_its_reservation_released(
    sync_settings, auth, monkeypatch
):
    with _client(sync_settings) as c:
        _setup(c, auth)
        run_id = _quote_http(c, auth).json()["run"]["id"]
        _flaky_ledger(monkeypatch, "approved")
        r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth)
        assert r.status_code == 503 and r.json()["error"] == "ledger_unavailable", r.text
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert run["state"] == "CANCELLED" and run["reason_code"] == "ledger_unavailable"
        assert run["reserved_usd"] == 0 and run["provider_job_id"] is None


# ---------------------------------------------------------------------------- custody on every approval


def test_approve_checks_custody_for_seed_runs_too(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth, nda=True)
        pl = c.app.state.pipeline
        d_route = {
            "engine_id": "fake",
            "provider": "fake",
            "region": "local",
            "custody_grade": "D",
            "custody_status": "confirmed",
        }
        bare = {"engine_id": "fake", "provider": "fake", "region": "local"}
        with c.app.state.index.project(PROJECT) as conn:
            loud = create_run(conn, RunCreate(project_id=PROJECT, packet_id=packet_id, route=d_route))
            quote_run(conn, loud.id)
            with pytest.raises(CustodyViolation):
                pl.approve(conn, PROJECT, loud.id, "human")
            quiet = create_run(conn, RunCreate(project_id=PROJECT, packet_id=packet_id, route=bare))
            quote_run(conn, quiet.id)
            # A bare seed route is served by the daemon's own engine (C0), which an NDA project allows.
            assert [r.state.value for r in pl.approve(conn, PROJECT, quiet.id, "human")] == ["APPROVED"]


# ---------------------------------------------------------------------------- Tier C


def test_a_tier_c_quote_reports_an_unknown_render_it_verdict(sync_settings, auth):
    with _client(sync_settings) as c:
        packet = plate_spec.materialize_example("tierC_client_plate", sync_settings.packets_dirs[0] / "tc")
        manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
        project = manifest["project_id"]
        r = c.post("/api/v0/projects", json={"project_id": project, "custody": {"nda": True}}, headers=auth)
        assert r.status_code == 201, r.text
        packet_id = _ingest(c, auth, packet)
        graph = _graph(packet_id)
        graph["nodes"][0]["tier"] = "C"
        assert c.put(f"/api/v0/projects/{project}/graph", json=graph, headers=auth).status_code == 200
        r = c.post(f"/api/v0/projects/{project}/nodes/e/quote", json={}, headers=auth)
        assert r.status_code == 200, r.text
        router = r.json()["run"]["inputs"]["router"]
        assert router["verdict"] == "unknown" and router["D_px_median"] is None
        assert r.json()["run"]["inputs"]["mask"]["source"] == "policy"


def test_the_module_keeps_its_public_names():
    for name in ("repeat_key", "repeat_canonical", "subgraph_snapshot", "ASSUMED_OK", "encode_png16"):
        assert name in pipeline_mod.__all__
