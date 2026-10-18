"""Motion, Wave 9 (router plan T94, decision 49): the end still, elements, locked and pull-back moves, Veo start/end,
drawer parameters and the motion recipes from the shot library.

Daemon-level tests drive the real routes after `motion_runs.install(pipeline)`, as `test_motion_runs.py` does: the
stills are PNG16s put in the project CAS and marked gate-passed and approved in `project.json` (the shape the server
derives for an accepted take), the video engine is a FakeEngine whose clip cross-fades from the start frame it was
sent to the end frame (or holds the start for a single-image take). No socket is opened and nothing is paid for.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from blake3 import blake3
from fastapi.testclient import TestClient

from plate_daemon import motion_runs, nodes, recipes
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.machine import get_run
from plate_daemon.motion import croppair, kling_job, move_policy
from plate_daemon.pipeline import PipelineError, decode_png16, encode_png16
from plate_daemon.projects import Project
from plate_engines.adapters.base import JobStatus
from plate_engines.loader import load_manifests
from plate_gates import io as pio

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PACKETS = ROOT / "fixtures" / "packets"
ROOM_A, ROOM_B, ROOM_A_V2 = PACKETS / "room_a", PACKETS / "room_b", PACKETS / "room_a_v2"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"
MOTION_RECIPES = (
    "motion_locked_living",
    "motion_slider_drift",
    "motion_crane_rise",
    "motion_light_sweep",
    "motion_weather_pass",
    "motion_night_glow",
    "motion_vegetation_breeze",
    "motion_material_macro",
    "motion_water_study",
    "motion_entourage_life",
    "motion_pullback_reveal",
    "motion_time_of_day",
)

have_packets = pytest.mark.skipif(
    not all((p / "manifest.json").is_file() for p in (ROOM_A, ROOM_B, ROOM_A_V2)),
    reason="fixtures/packets not built yet (T2)",
)
needs_ffmpeg = pytest.mark.skipif(
    pio.ffmpeg() is None or pio.ffprobe() is None, reason="ffmpeg and ffprobe are needed for the clip tests"
)


# ---------------------------------------------------------------------------- helpers


def _rgb(png: bytes) -> np.ndarray:
    return np.ascontiguousarray(cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)[..., ::-1])


def _plate16(path: Path) -> np.ndarray:
    from plate_pixels import imageio

    return np.rint(imageio.read(path / "display.tif").rgb * 65535).astype(np.uint16)


def _png_bytes(rgb8: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(rgb8[..., ::-1]))
    assert ok
    return buf.tobytes()


def textured(w: int, h: int) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w]
    img = np.stack([x / max(w - 1, 1), y / max(h - 1, 1), ((x // 16 + y // 16) % 2) * 0.4 + 0.3], axis=-1)
    return (img * 65535).astype(np.uint16)


class FakeVideo(FakeEngine):
    """A FakeEngine whose clip cross-fades from the start frame it was sent to the end frame; with no `image_tail`
    (a single-image take) it holds the start frame."""

    def __init__(self, tmp: Path, *, seconds: int = 2, fps: int = 24, **kwargs: Any):
        super().__init__(**kwargs)
        self.tmp, self.seconds, self.fps = Path(tmp), seconds, fps
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.payloads: list[dict[str, Any]] = []
        self.frames: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.payloads.append(payload)
        ins = payload["inputs"]
        a = _rgb(base64.b64decode(ins["image"]["b64"]))
        b = _rgb(base64.b64decode(ins["image_tail"]["b64"])) if "image_tail" in ins else a
        self.frames[job] = (a, b)
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        a, b = (f.astype(np.float64) for f in self.frames[result_url[len(RESULT_SCHEME) :]])
        n = self.fps * self.seconds
        frames = [np.round(a * (1 - t) + b * t).astype(np.uint8) for t in np.linspace(0.0, 1.0, n)]
        path = self.tmp / "clip.mp4"
        pio.write_clip(path, frames, fps=self.fps)
        return path.read_bytes()


def _edge(a: str, b: str, handle: str | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {"id": f"{a}->{b}" + (f":{handle}" if handle else ""), "source": a, "target": b}
    if handle:
        e["targetHandle"] = handle
    return e


def _plate(packet_id: str, nid: str = "p") -> dict[str, Any]:
    return {"id": nid, "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}


def _still(nid: str, key: str, **fields: Any) -> dict[str, Any]:
    """An Edit node the way the server leaves an accepted take: gate passed, approved by a human, its output."""
    base = {"gateStatus": "pass", "approved": True, "approvedBy": "human", "outputTier": "measured"}
    return {"id": nid, "kind": "Edit", "label": "Edit", "outputHash": key, **base, **fields}


def _motion(**params: Any) -> dict[str, Any]:
    return {"id": "m", "kind": "Motion", "label": "Motion", "params": {"duration": 2, **params}}


def _reference(nid: str, key: str, **params: Any) -> dict[str, Any]:
    return {"id": nid, "kind": "Reference", "label": "Reference", "params": {"referenceHash": key, **params}}


@dataclass
class Env:
    client: TestClient
    auth: dict[str, str]
    settings: Settings
    pipeline: Any
    runner: motion_runs.MotionRunner
    tmp: Path

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
        return r.json()["packet_id"]

    def save_graph(self, nodes_: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
        project = Project.load(self.pipeline.project_dir(PROJECT))
        with project.lock(timeout_s=5.0):
            project.save_graph({"nodes": nodes_, "edges": edges})

    @property
    def cas(self) -> CAS:
        return self.pipeline.cas_for(PROJECT)

    def still_key(self, path: Path, *, gain: float = 1.0) -> str:
        """The packet's display master (times `gain`) as a PNG16 in the CAS: a still a node could have made."""
        rgb = np.clip(_plate16(path).astype(np.float64) * gain, 0, 65535).astype(np.uint16)
        return self.cas.put_bytes(encode_png16(rgb), "png")

    def quote(self, node_id: str = "m", body: dict[str, Any] | None = None):
        return self.client.post(
            f"/api/v0/projects/{PROJECT}/nodes/{node_id}/quote", json=body or {}, headers=self.auth
        )

    def approve(self, run_id: str) -> dict[str, Any]:
        r = self.client.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=self.auth)
        assert r.status_code == 200, r.text
        r = self.client.get(f"/api/v0/runs/{run_id}", headers=self.auth)
        assert r.status_code == 200, r.text
        return r.json()

    def node_runs(self, node_id: str = "m") -> int:
        with self.pipeline.index.project(PROJECT) as conn:
            return conn.execute("SELECT COUNT(*) FROM runs WHERE node_id = ?", (node_id,)).fetchone()[0]

    def run(self, run_id: str):
        with self.pipeline.index.project(PROJECT) as conn:
            return get_run(conn, run_id)

    def ledger(self, run_id: str) -> list[dict[str, Any]]:
        return [r for r in Ledger(self.pipeline.project_dir(PROJECT)).records() if r.get("run_id") == run_id]

    def video(self, engine_id: str = "kling-o3-4k") -> FakeVideo:
        fake = FakeVideo(self.tmp / engine_id)
        self.runner.engines[engine_id] = fake
        return fake


@pytest.fixture
def env(plate_env, tmp_path, monkeypatch) -> Env:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    app = create_app(settings, FakeEngine(echo=True))
    with TestClient(app, base_url=BASE) as client:
        pipeline = client.app.state.pipeline
        pipeline.sleep = lambda _s: None
        nodes.install(pipeline)
        runner = motion_runs.install(pipeline)
        yield Env(client, {"Authorization": f"Bearer {settings.token}"}, settings, pipeline, runner, tmp_path)


def _two_stills(
    env: Env, packet_id: str, *, end: dict[str, Any] | None = None, **motion: Any
) -> tuple[str, str]:
    """Plate -> e1 (start) and e2 (end, a brighter take of the same camera) -> Motion; returns the two CAS keys."""
    k1 = env.still_key(ROOM_A)
    k2 = env.still_key(ROOM_A, gain=1.12)
    env.save_graph(
        [_plate(packet_id), _still("e1", k1), end or _still("e2", k2), _motion(**motion)],
        [_edge("p", "e1"), _edge("p", "e2"), _edge("e1", "m"), _edge("e2", "m", "end")],
    )
    return k1, k2


# ---------------------------------------------------------------------------- the end still


@have_packets
@needs_ffmpeg
def test_two_approved_stills_of_one_packet_quote_and_run_to_a_clip_landing_on_both(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    k1, k2 = _two_stills(env, packet_id)
    fake = env.video()

    r = env.quote()
    assert r.status_code == 200, r.text
    q = r.json()
    inputs = q["run"]["inputs"]
    assert inputs["image_tail"] == inputs["end_hash"] and inputs["image"] == inputs["start_hash"]
    assert inputs["still"] == k1 and inputs["end_still"] == k2
    pair = inputs["motion"]["pair"]
    assert pair["mode"] == "two_stills" and pair["direction"] == "locked"
    # an end still defaults to the two-stills recipe
    assert inputs["motion_run"]["recipe"] == "motion_time_of_day"
    assert inputs["motion_run"]["end_node"] == "e2" and inputs["motion"]["move_kind"] == "locked_tripod"
    # both frames are the whole stills at z = 1, at their native size (the 4K envelope is larger than 640 x 360)
    start = _rgb(env.cas.read_bytes(inputs["start_hash"]))
    end = _rgb(env.cas.read_bytes(inputs["end_hash"]))
    assert start.shape == end.shape == (360, 640, 3)
    want = np.rint(decode_png16(env.cas.read_bytes(k2)).astype(np.float64) / 257.0)
    assert np.abs(end.astype(np.float64) - want).max() <= 1.0

    run = env.approve(q["run"]["id"])
    assert run["state"] == "DONE", run.get("error")
    clip, s_key, e_key = run["outputs"]
    assert (s_key, e_key) == (inputs["start_hash"], inputs["end_hash"])
    assert env.cas.find(clip).suffix == ".mp4"
    status = {c["metric_id"]: c["status"] for c in run["gate_report"]["checks"]}
    assert status["video.first_frame"] == "pass" and status["video.last_frame"] == "pass"
    ins = fake.payloads[0]["inputs"]
    assert base64.b64decode(ins["image_tail"]["b64"]) == env.cas.read_bytes(e_key)
    assert re.fullmatch(r"[0-9a-f]{26}\.png", ins["image_tail"]["name"])
    assert run["normalized"]["motion"]["pair"]["mode"] == "two_stills"


@have_packets
def test_an_unapproved_end_still_is_409_video_locked_naming_it(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    k2 = env.still_key(ROOM_A, gain=1.1)
    for end in (_still("e2", k2, approved=False), _still("e2", k2, gateStatus="fail")):
        _two_stills(env, packet_id, end=end)
        r = env.quote()
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["error"] == "VIDEO_LOCKED" and body["still"] == "e2" and "end still" in body["detail"]
    # approved in the graph but with no output in the CAS: still locked, still named
    _two_stills(env, packet_id, end=_still("e2", "f" * 64))
    body = env.quote().json()
    assert body["error"] == "VIDEO_LOCKED" and body["still"] == "e2"
    assert env.node_runs() == 0


@have_packets
def test_an_end_still_from_another_camera_or_scene_is_422_end_frame_camera(env):
    env.project()
    a = env.packet(ROOM_A)
    for other in (ROOM_B, ROOM_A_V2):  # another camera of the scene; the same camera name, another scene hash
        b = env.packet(other)
        k1, k2 = env.still_key(ROOM_A), env.still_key(other)
        env.save_graph(
            [_plate(a), _plate(b, "p2"), _still("e1", k1), _still("e2", k2), _motion()],
            [_edge("p", "e1"), _edge("p2", "e2"), _edge("e1", "m"), _edge("e2", "m", "end")],
        )
        r = env.quote()
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["error"] == "end_frame_camera" and body["still"] == "e2"
        assert (body["start_packet"], body["end_packet"]) == (a, b)
    assert env.node_runs() == 0


@have_packets
def test_single_image_conflicts_with_an_end_still(env):
    env.project()
    _two_stills(env, env.packet(ROOM_A), single_image=True)
    r = env.quote()
    assert r.status_code == 422 and r.json()["error"] == "params"


# ---------------------------------------------------------------------------- Veo 3.1 start/end


@have_packets
@needs_ffmpeg
def test_veo_quotes_on_an_open_project_and_runs_start_end(env):
    env.project()
    _two_stills(env, env.packet(ROOM_A))
    fake = env.video("veo-3.1")
    r = env.quote(body={"engine_id": "veo-3.1"})
    assert r.status_code == 200, r.text
    q = r.json()
    route = q["run"]["route"]
    assert (route["engine_id"], route["provider"]) == ("veo-3.1", "vertex")
    assert q["quote"]["custody_grade"] == "D"  # C1 pending written terms counts as D
    params = q["run"]["params"]
    assert params["duration"] == 4  # a 2 s shot clamps to Veo's 4 to 8
    assert params["resolution"] == "1080p" and params["aspect_ratio"] == "16:9"  # Veo's own spelling
    assert q["quote"]["price_usd"] == pytest.approx(4 * 0.21)
    run = env.approve(q["run"]["id"])
    assert run["state"] == "DONE", run.get("error")
    assert "image_tail" in fake.payloads[0]["inputs"] and fake.payloads[0]["engine_id"] == "veo-3.1"


@have_packets
def test_veo_is_refused_on_an_nda_project(env):
    env.project(nda=True)
    _two_stills(env, env.packet(ROOM_A))
    r = env.quote(body={"engine_id": "veo-3.1"})
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "custody" and env.node_runs() == 0


# ---------------------------------------------------------------------------- locked, single image, pull-back


def _one_still(env: Env, **motion: Any) -> str:
    packet_id = env.packet(ROOM_A)
    key = env.still_key(ROOM_A)
    env.save_graph(
        [_plate(packet_id), _still("e", key), _motion(**motion)], [_edge("p", "e"), _edge("e", "m")]
    )
    return key


@have_packets
def test_a_locked_tripod_has_identical_start_and_end_crops(env):
    env.project()
    _one_still(env, move="locked_tripod", z0=1.0, z1=1.18)  # an unequal pair is not a hold: one zoom is used
    q = env.quote().json()
    inputs = q["run"]["inputs"]
    assert inputs["start_hash"] == inputs["end_hash"] == inputs["image_tail"]
    motion = inputs["motion"]
    assert motion["pair"]["direction"] == "locked" and motion["push_ratio"] == 1.0
    assert motion["boxes"][0] == motion["boxes"][1] == [0, 0, 640, 360]
    # the shipped recipe gives the same hold
    _one_still(env, recipe="motion_locked_living")
    inputs = env.quote().json()["run"]["inputs"]
    assert inputs["start_hash"] == inputs["end_hash"] and inputs["motion"]["move_kind"] == "locked_tripod"


@have_packets
@needs_ffmpeg
def test_single_image_sends_no_image_tail(env):
    env.project()
    _one_still(env, move="locked_tripod", single_image=True)
    fake = env.video()
    q = env.quote().json()
    inputs = q["run"]["inputs"]
    assert "image_tail" not in inputs and inputs["end_hash"] is None
    assert inputs["motion"]["single_image"] is True
    run = env.approve(q["run"]["id"])
    assert run["state"] == "DONE", run.get("error")
    assert "image_tail" not in fake.payloads[0]["inputs"]
    assert len(run["outputs"]) == 2  # the clip and the start still
    metrics = {c["metric_id"] for c in run["gate_report"]["checks"]}
    assert "video.first_frame" in metrics and "video.last_frame" not in metrics
    assert "video.camera_lock_median_flow_px" in run["gate_report"]["metrics"]
    # a moving crop cannot drop its end: the engine would choose where the move lands
    _one_still(env, move="push_in", single_image=True)
    r = env.quote()
    assert r.status_code == 422 and r.json()["error"] == "single_image_move"


@have_packets
def test_a_pull_back_has_a_push_ratio_under_one(env):
    env.project()
    _one_still(env, z0=1.5, z1=1.0)
    q = env.quote()
    assert q.status_code == 200, q.text
    motion = q.json()["run"]["inputs"]["motion"]
    assert motion["push_ratio"] == pytest.approx(1.0 / 1.5) and motion["push_ratio"] < 1
    assert motion["pair"]["direction"] == "out" and motion["move_kind"] == "push_in"
    (sx, sy, sw, sh), end_box = motion["boxes"]
    assert end_box == [0, 0, 640, 360] and sw < 640  # it opens from the tight crop to the whole still
    # the frame is the tight start crop's native size: nothing upscaled
    start = _rgb(env.cas.read_bytes(q.json()["run"]["inputs"]["start_hash"]))
    assert start.shape[:2] == (sh, sw)


# ---------------------------------------------------------------------------- drawer params and elements


@have_packets
def test_cfg_scale_reaches_the_plan_params(env):
    env.project()
    _one_still(env, cfg_scale=0.6)
    q = env.quote().json()
    assert q["run"]["params"]["cfg_scale"] == 0.6
    assert q["run"]["inputs"]["motion_run"]["extra_params"] == {"cfg_scale": 0.6}
    _one_still(env, cfg_scale=1.5)  # outside the manifest's 0 to 1
    r = env.quote()
    assert r.status_code == 422 and r.json()["error"] == "params" and "cfg_scale" in r.json()["detail"]


@have_packets
@needs_ffmpeg
def test_reference_elements_ride_to_kling_and_are_refused_elsewhere(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    key = env.still_key(ROOM_A)
    rng = np.random.default_rng(1)
    ref = env.cas.put_bytes(_png_bytes(rng.integers(0, 256, (90, 120, 3), dtype=np.uint8)), "png")
    graph = [_plate(packet_id), _still("e", key), _reference("r", ref, name="car"), _motion()]
    edges = [_edge("p", "e"), _edge("e", "m"), _edge("r", "m")]  # no handle: a Reference lands on elements
    env.save_graph(graph, edges)
    fake = env.video()
    q = env.quote()
    assert q.status_code == 200, q.text
    inputs = q.json()["run"]["inputs"]
    assert inputs["elements"][0]["name"] == "car" and inputs["motion_run"]["elements"][0]["hash"] == ref
    run = env.approve(q.json()["run"]["id"])
    assert run["state"] == "DONE", run.get("error")
    (el,) = fake.payloads[0]["inputs"]["elements"]
    assert el["name"] == "car" and re.fullmatch(r"[0-9a-f]{26}\.png", el["image"]["name"])
    assert _rgb(base64.b64decode(el["image"]["b64"])).shape == (90, 120, 3)
    submitted = next(r for r in env.ledger(run["id"]) if r["kind"] == "submitted")
    assert submitted["data"]["elements"] == [inputs["elements"][0]["hash"]]

    # Veo takes no elements: 422 before anything is read
    r = env.quote(body={"engine_id": "veo-3.1"})
    assert r.status_code == 422 and r.json()["error"] == "elements_unsupported"
    # a reference whose picture is not in the CAS
    env.save_graph([*graph[:2], _reference("r", "e" * 64), graph[3]], edges)
    r = env.quote()
    assert r.status_code == 422 and r.json()["error"] == "reference_missing"


# ---------------------------------------------------------------------------- the shipped motion recipes


def test_the_shipped_motion_recipes_load_and_stay_on_the_ladder():
    ids = recipes.list_runnable()
    assert set(MOTION_RECIPES) <= set(ids)
    for high in ("as05", "as06", "as13"):  # the high-risk shots are not shipped
        assert not any(recipes.load_runnable(r).library == high for r in ids), high
    for rid in MOTION_RECIPES:
        r = recipes.load_runnable(rid)
        assert (r.kind, r.engine) == ("motion_croppair", "kling-o3-4k"), rid
        assert recipes.get_recipe(r.library).id == r.library
        move = move_policy.evaluate(r.motion["move"])
        assert move_policy.risk_rank(move["risk"]) <= move_policy.risk_rank("med"), rid
        assert len((r.negative_prompt or "").split()) <= kling_job.NEGATIVE_MAX_WORDS, rid
        text = recipes.assemble_runnable(r, recipes.ProjectSheet())
        assert recipes.unfilled_count(text) == 0, rid
        if r.motion.get("start_end", True):  # an end pin: no pace words to strip
            assert kling_job.apply_prompt_law(text, end_pinned=True)[1]["pace_words_stripped"] is False, rid
    assert recipes.load_runnable("motion_pullback_reveal").croppair["z0"] == 1.25
    assert recipes.load_runnable("motion_slider_drift").croppair["center_end"] == [0.53, 0.5]


@have_packets
@pytest.mark.parametrize("rid", MOTION_RECIPES)
def test_each_motion_recipe_plans_on_room_a(rid):
    plate = _plate16(ROOM_A)
    r = recipes.load_runnable(rid)
    if r.motion.get("two_stills"):
        with pytest.raises(PipelineError) as e:
            kling_job.plan_from_still(plate, recipe=rid, shot_s=2)
        assert e.value.code == "end_required"
        end = np.clip(plate.astype(np.float64) * 1.1, 0, 65535).astype(np.uint16)
        plan = kling_job.plan_two_stills(plate, end, recipe=rid, shot_s=2)
        assert plan.pair["mode"] == "two_stills" and plan.end_png is not None
    else:
        plan = kling_job.plan_from_still(plate, recipe=rid, shot_s=2)
        assert plan.pair["mode"] == "croppair"
    assert plan.recipe_id == rid and plan.size[0] <= 640 and plan.size[1] <= 360
    direction = plan.pair["direction"]
    want = r.croppair.get("direction", "in")
    assert direction == want, (rid, direction)
    if r.motion["move"] == "locked_tripod" and not r.motion.get("two_stills"):
        assert plan.end_png is None or plan.start_hash == plan.end_hash
    assert (plan.end_png is None) is (r.motion.get("start_end") is False)
    assert plan.law["end_pinned"] is (plan.end_png is not None)


@have_packets
def test_the_slider_drift_slides_the_end_crop_right():
    plan = kling_job.plan_from_still(_plate16(ROOM_A), recipe="motion_slider_drift", shot_s=2)
    (x0, y0, w0, h0), (x1, y1, w1, h1) = plan.pair["boxes"]
    assert (w0, h0) == (w1, h1) and y0 == y1 and x1 - x0 == pytest.approx(0.03 * 640, abs=1.0)
    assert plan.pair["push_ratio"] == 1.0 and plan.start_hash != plan.end_hash


# ---------------------------------------------------------------------------- the library: kling_job and croppair


@pytest.fixture(scope="module")
def manifests():
    return load_manifests()


def test_start_end_engines_and_the_old_name(manifests):
    assert kling_job.START_END_ENGINES == ("kling-o3-4k", "kling-v3-pro", "veo-3.1")
    assert kling_job.KLING_ENGINES == kling_job.START_END_ENGINES
    for eid in kling_job.START_END_ENGINES:
        assert eid in manifests and "crop_pair_motion" in manifests[eid].allowed_tasks
    still = textured(480, 270)
    veo = kling_job.plan_from_still(still, engine_id="veo-3.1", shot_s=6, manifests=manifests)
    assert veo.params()["resolution"] == "1080p" and veo.duration_s == 6
    # matched without regard to case, sent in the manifest's spelling
    up = kling_job.plan_from_still(still, engine_id="veo-3.1", shot_s=6, resolution="720P")
    assert up.params()["resolution"] == "720p"
    with pytest.raises(PipelineError, match="not a Kling"):
        kling_job.plan_from_still(still, engine_id="seedance-2.5-i2v", shot_s=2)
    portrait = kling_job.plan_from_still(textured(270, 480), engine_id="veo-3.1", shot_s=4)
    assert portrait.params()["aspect_ratio"] == "9:16"


def test_a_single_image_plan_has_no_end_and_no_pace_law():
    plan = kling_job.plan_from_still(
        textured(480, 270),
        recipe="motion_light_sweep",
        shot_s=2,
        prompt="the light sweeps slowly across the facade",
    )
    assert plan.end_png is None and plan.end_hash is None and plan.single_image
    assert "image_tail" not in plan.payload()["inputs"] and "image_tail" not in plan.input_hashes()
    assert plan.law["end_pinned"] is False and "slowly" in plan.prompt  # nothing pinned, nothing stripped
    assert plan.record()["end"] is None and plan.record()["single_image"] is True
    route = {"engine_id": plan.engine_id, "provider": "atlas", "region": "unknown"}
    spec = kling_job.to_run_create(plan, "p", "pkt", route)
    assert "image_tail" not in spec.inputs and spec.inputs["motion"]["single_image"] is True


def test_extra_params_are_checked_against_the_manifest(manifests):
    still = textured(480, 270)
    plan = kling_job.plan_from_still(still, shot_s=2, extra_params={"cfg_scale": 0.6}, manifests=manifests)
    assert plan.params()["cfg_scale"] == 0.6 and plan.params()["duration"] == 2
    other = kling_job.plan_from_still(still, shot_s=2, extra_params={"cfg_scale": 0.4})
    assert plan.idempotency_key() != other.idempotency_key()
    for bad in ({"cfg_scale": 2}, {"cfg_scael": 0.5}, {"duration": 5}, {"cfg_scale": "high"}):
        with pytest.raises(PipelineError) as e:
            kling_job.plan_from_still(still, shot_s=2, extra_params=bad, manifests=manifests)
        assert e.value.code == "params", bad


def test_elements_only_for_a_manifest_that_declares_them(manifests):
    still = textured(480, 270)
    png = _png_bytes(np.full((40, 60, 3), 128, np.uint8))
    plan = kling_job.plan_from_still(still, shot_s=2, elements=[{"name": "lamp", "png": png}])
    (el,) = plan.payload()["inputs"]["elements"]
    assert el["name"] == "lamp" and base64.b64decode(el["image"]["b64"]) == png
    assert plan.input_hashes()["elements"] != kling_job.plan_from_still(still, shot_s=2).input_hashes().get(
        "elements"
    )
    with pytest.raises(PipelineError) as e:
        kling_job.plan_from_still(still, engine_id="veo-3.1", shot_s=4, elements=[{"name": "l", "png": png}])
    assert e.value.code == "elements_unsupported"
    with pytest.raises(PipelineError):
        kling_job.plan_from_still(still, shot_s=2, elements=[{"name": "", "png": png}])


def test_element_png_strips_metadata_and_never_enlarges():
    big = np.random.default_rng(2).integers(0, 256, (2400, 4800, 3), dtype=np.uint8)
    out = kling_job.element_png(_png_bytes(big), "kling-v3-pro")  # 1080P: the frame's long side is 1920
    assert _rgb(out).shape[:2] == (960, 1920)
    small = np.full((30, 40, 3), 200, np.uint8)
    assert _rgb(kling_job.element_png(_png_bytes(small))).shape[:2] == (30, 40)
    with pytest.raises(PipelineError):
        kling_job.element_png(b"not a picture")


def test_two_stills_of_different_sizes_are_422_end_size():
    with pytest.raises(PipelineError) as e:
        kling_job.plan_two_stills(textured(480, 270), textured(470, 270), shot_s=2)
    assert e.value.code == "end_size"


def test_two_stills_shrink_both_to_the_envelope_together():
    a, b = textured(1280, 720), textured(1280, 720)[::-1].copy()
    pair = croppair.make_two_stills(a, b, (640, 360), quantise="round")
    assert pair["mode"] == "two_stills" and pair["size"] == (640, 360) and pair["push_ratio"] == 1.0
    assert pair["boxes"] == [(0, 0, 1280, 720), (0, 0, 1280, 720)]
    assert _rgb(pair["start_png"]).shape == _rgb(pair["end_png"]).shape == (360, 640, 3)
    native = croppair.make_two_stills(a, b, (3840, 2160))
    assert native["size"] == (1280, 720) and native["note"] == "native_below_engine_size"


def test_make_pair_records_the_direction_and_needs_asking_for_out_and_locked():
    src = textured(640, 360)
    assert croppair.make_pair(src, 320, 180, 1.0, 1.5)["direction"] == "in"
    with pytest.raises(ValueError, match="zoom pair"):
        croppair.make_pair(src, 320, 180, 1.5, 1.0)
    with pytest.raises(ValueError, match="zoom pair"):
        croppair.make_pair(src, 320, 180, 1.2, 1.2)
    out = croppair.make_pair(src, 320, 180, 1.5, 1.0, None, allow_out=True)
    assert out["direction"] == "out" and out["push_ratio"] < 1
    assert out["size"] == (out["boxes"][0][2], out["boxes"][0][3])  # the tighter start crop sets the frame
    held = croppair.make_pair(src, 320, 180, 1.2, 1.2, None, allow_equal=True)
    assert held["direction"] == "locked" and held["start_png"] == held["end_png"]
    drift = croppair.make_pair(src, 320, 180, 1.1, 1.1, None, allow_equal=True, cx1=339.2, cy1=180)
    assert drift["zoom"]["cx1"] == 339.2 and drift["boxes"][1][0] > drift["boxes"][0][0]
    # the T43 push-in record is unchanged but for the two new fields
    push = croppair.make_pair(src, 320, 180, 1.0, 1.5, None)
    assert push["mode"] == "croppair" and "cx1" not in push["zoom"]


def test_the_reference_push_in_is_byte_identical():
    """The amphi-plaza case through make_pair: the same boxes and the same frames as before T94."""
    big = textured(5504, 3072)
    pair = croppair.make_pair(big, 3633, 2174, 1.5, 2.3, (3840, 2160), quantise="round")
    assert pair["boxes"] == [(1799, 1024, 3668, 2048), (2437, 1507, 2392, 1334)]
    x0, y0, w, h = pair["boxes"][1]
    end = (big[y0 : y0 + h, x0 : x0 + w].astype(np.float64) / 257.0).round().astype(np.uint8)
    assert np.abs(_rgb(pair["end_png"]).astype(int) - end.astype(int)).max() <= 1
    assert pair["size"] == (2392, 1334) and pair["direction"] == "in"


class _StubAdapter:
    """The slice of a plate_engines video adapter AdapterEngine touches, with the manifest it renames to."""

    def __init__(self, manifest):
        self.manifest, self.engine_id, self.staged = manifest, manifest.id, None

    def validate_params(self, params):
        return dict(params)

    def submit(self, staged):
        self.staged = staged
        return "op_1"

    def poll(self, job):
        return JobStatus(state="done", outputs=["https://example.googleapis.com/clip.mp4"])

    def fetch(self, url):
        return b""


def test_adapter_engine_renames_inputs_to_the_manifest_names(manifests, tmp_path):
    still = textured(480, 270)
    veo = _StubAdapter(manifests["veo-3.1"])
    plan = kling_job.plan_from_still(still, engine_id="veo-3.1", shot_s=4, zoom=(1.0, 1.5))
    kling_job.AdapterEngine(veo, tmp_path / "veo").submit(plan.payload(), "k")
    assert set(veo.staged.inputs) == {"prompt", "start_image", "end_image"}
    assert Path(veo.staged.inputs["end_image"]["path"]).read_bytes() == plan.end_png
    kling = _StubAdapter(manifests["kling-o3-4k"])
    png = _png_bytes(np.full((20, 30, 3), 9, np.uint8))
    plan = kling_job.plan_from_still(still, shot_s=2, elements=[{"name": "car", "png": png}])
    kling_job.AdapterEngine(kling, tmp_path / "kling").submit(plan.payload(), "k")
    assert set(kling.staged.inputs) == {"prompt", "image", "image_tail", "elements"}
    (el,) = kling.staged.inputs["elements"]
    assert el["name"] == "car" and Path(el["path"]).read_bytes() == png
    assert blake3(png).hexdigest() == plan.element_hashes[0]
