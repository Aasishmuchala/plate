"""Motion, CropPair, People and previs nodes through the daemon's own routes (router plan T66, decision 38).

Every run here is quoted by `POST /projects/{id}/nodes/{node}/quote`, approved by `POST /runs/{id}/approve` and
driven by the pipeline the way an Edit is, after `motion_runs.install(pipeline)` (what `api.create_app` will call,
T73). Engines are fakes: the still upstream is an Edit run on the daemon's echo FakeEngine, the Kling engine is a
FakeEngine whose clip is an ffmpeg cross-fade from the start still to the end still it was sent (T43's fake), the
Seedance r2v engine holds the first reference for the clip's length (T44's fake), and STAFFAGE is the in-process
FakeStaffage (T45). No socket is opened and nothing is paid for.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from blake3 import blake3
from fastapi.testclient import TestClient

from plate_daemon import motion_runs, nodes
from plate_daemon.adapter import PollResult
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.machine import get_run
from plate_daemon.motion import previs
from plate_daemon.motion.staffage_client import StaffageClient
from plate_daemon.nodes import runner as node_runner
from plate_daemon.pipeline import FAKE_ROUTE, decode_png16, encode_png16, is_pipeline_run
from plate_daemon.projects import Project
from plate_daemon.sse import EventFilter
from plate_gates import io as pio

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PACKETS = ROOT / "fixtures" / "packets"
ROOM_A, DOLLY, DOLLY_HERO = PACKETS / "room_a", PACKETS / "dolly_12f", PACKETS / "dolly_hero"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"
WORKER_STATES = [
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

pytestmark = pytest.mark.skipif(
    not all((p / "manifest.json").is_file() for p in (ROOM_A, DOLLY, DOLLY_HERO)),
    reason="fixtures/packets not built yet (T2)",
)
needs_ffmpeg = pytest.mark.skipif(
    pio.ffmpeg() is None or pio.ffprobe() is None, reason="ffmpeg and ffprobe are needed for the clip tests"
)


def _load_fake_staffage():
    spec = importlib.util.spec_from_file_location("fake_staffage", HERE / "fake_staffage.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("fake_staffage", mod)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------- fake engines


def _rgb(png: bytes) -> np.ndarray:
    return np.ascontiguousarray(cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)[..., ::-1])


class FakeKling(FakeEngine):
    """A FakeEngine whose result is a clip cross-faded from the start still to the end still it was sent."""

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
        self.frames[job] = tuple(_rgb(base64.b64decode(ins[k]["b64"])) for k in ("image", "image_tail"))
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        a, b = (f.astype(np.float64) for f in self.frames[result_url[len(RESULT_SCHEME) :]])
        n = self.fps * self.seconds
        frames = [np.round(a * (1 - t) + b * t).astype(np.uint8) for t in np.linspace(0.0, 1.0, n)]
        path = self.tmp / "clip.mp4"
        pio.write_clip(path, frames, fps=self.fps)
        return path.read_bytes()


class FakeR2V:
    """Worker-protocol Seedance r2v: an mp4 of the first reference held for the asked duration at 24 fps."""

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.route = {**FAKE_ROUTE, "engine_id": previs.ENGINE_ID}
        self.payloads: list[dict[str, Any]] = []
        self.raw: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        self.payloads.append(payload)
        inputs, params = payload["inputs"], payload["params"]
        job = f"job-{idempotency_key[:12]}"
        out = self.workdir / f"{job}.mp4"
        cmd = [str(pio.ffmpeg()), "-v", "error", "-nostdin", "-y", "-loop", "1", "-framerate", "24"]
        cmd += ["-i", inputs["reference_images"][0]["path"], "-t", str(params["duration"])]
        cmd += ["-vf", "scale=320:180", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10"]
        cmd += ["-pix_fmt", "yuv420p", "-an", str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
        self.raw[job] = out.read_bytes()
        return job

    def poll(self, job):
        return PollResult("succeeded", result_url=f"fake://{job}")

    def fetch(self, url):
        return self.raw[url.removeprefix("fake://")]

    def cancel(self, job):
        return None


# ---------------------------------------------------------------------------- the daemon around the nodes


@dataclass
class Env:
    client: TestClient
    auth: dict[str, str]
    settings: Settings
    pipeline: Any
    runner: motion_runs.MotionRunner
    tmp: Path

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
        assert (
            self.client.get(f"/api/v0/packets/{packet_id}", headers=self.auth).json()["status"] == "ingested"
        )
        return packet_id

    @staticmethod
    def edges(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
        return [{"id": f"{a}->{b}", "source": a, "target": b} for a, b in pairs]

    def put_graph(self, nodes_: list[dict[str, Any]], pairs: list[tuple[str, str]]) -> dict[str, Any]:
        body = {"nodes": nodes_, "edges": self.edges(pairs)}
        r = self.client.put(f"/api/v0/projects/{PROJECT}/graph", json=body, headers=self.auth)
        assert r.status_code == 200, r.text
        return r.json()["graph"]

    def save_graph(self, nodes_: list[dict[str, Any]], pairs: list[tuple[str, str]]) -> None:
        """Write project.json directly: a stored graph the PUT route would refuse (a stale or edited file)."""
        project = Project.load(self.pipeline.project_dir(PROJECT))
        with project.lock(timeout_s=5.0):
            project.save_graph({"nodes": nodes_, "edges": self.edges(pairs)})

    @property
    def cas(self):
        return self.pipeline.cas_for(PROJECT)

    def plate_key(self, path: Path) -> str:
        """The packet's display master as a PNG16 in the CAS (a still a node could have made)."""
        from plate_pixels import imageio

        rgb = np.rint(imageio.read(path / "display.tif").rgb * 65535).astype(np.uint16)
        return self.cas.put_bytes(encode_png16(rgb), "png")

    # ---- runs
    def quote(self, node_id: str, body: dict[str, Any] | None = None):
        return self.client.post(
            f"/api/v0/projects/{PROJECT}/nodes/{node_id}/quote", json=body or {}, headers=self.auth
        )

    def approve(self, run_id: str, **body: Any) -> dict[str, Any]:
        r = self.client.post(
            f"/api/v0/runs/{run_id}/approve", json={"by": "human", **body}, headers=self.auth
        )
        assert r.status_code == 200, r.text
        return self.get(run_id)

    def get(self, run_id: str) -> dict[str, Any]:
        r = self.client.get(f"/api/v0/runs/{run_id}", headers=self.auth)
        assert r.status_code == 200, r.text
        return r.json()

    def run(self, run_id: str):
        with self.pipeline.index.project(PROJECT) as conn:
            return get_run(conn, run_id)

    def node_runs(self, node_id: str) -> int:
        with self.pipeline.index.project(PROJECT) as conn:
            return conn.execute("SELECT COUNT(*) FROM runs WHERE node_id = ?", (node_id,)).fetchone()[0]

    def moves(self, run_id: str) -> list[str]:
        recs = self.client.app.state.bus.read(EventFilter(PROJECT, run_id), 0)
        return [r["data"]["to"] for r in recs if r["kind"] == "run.state"]

    def ledger(self, run_id: str) -> list[dict[str, Any]]:
        return [r for r in Ledger(self.pipeline.project_dir(PROJECT)).records() if r.get("run_id") == run_id]

    def step(self, run_id: str):
        with self.pipeline.index.project(PROJECT) as conn:
            return self.pipeline.step(conn, PROJECT, run_id)

    # ---- an approved still, the way the server derives one (decision 31)
    def approved_edit(self, packet_id: str) -> dict[str, Any]:
        """Plate -> Edit on the echo engine, run to DONE, fully scrubbed and accepted. Returns the Edit node as the
        graph must carry it (gate pass, approved by a human against the logged scrub, its output)."""
        self.put_graph(
            [
                {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
                {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
            ],
            [("p", "e")],
        )
        r = self.quote("e")
        assert r.status_code == 200, r.text
        run = self.client.post(
            f"/api/v0/runs/{r.json()['run']['id']}/approve", json={"by": "auto"}, headers=self.auth
        ).json()
        run = self.get(run["id"])
        assert run["state"] == "DONE", run.get("error")
        scrub = self.client.post(
            f"/api/v0/runs/{run['id']}/scrub",
            json={"quadrants_visited": 4, "zoom_100": True},
            headers=self.auth,
        )
        assert scrub.status_code == 200, scrub.text
        scrub_id = scrub.json()["scrub_log_id"]
        acc = self.client.post(
            f"/api/v0/runs/{run['id']}/accept", json={"scrub_log_id": scrub_id}, headers=self.auth
        )
        assert acc.status_code == 200, acc.text
        return {
            "id": "e",
            "kind": "Edit",
            "label": "Edit",
            "params": {"engine": "fake"},
            "gateStatus": "pass",
            "approved": True,
            "approvedBy": "human",
            "scrubLogId": scrub_id,
            "outputHash": run["outputs"][0],
            "outputTier": "measured",
        }


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


def _plate(packet_id: str) -> dict[str, Any]:
    return {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}


def _locked_edit(key: str, **fields: Any) -> dict[str, Any]:
    return {"id": "e", "kind": "Edit", "label": "Edit", "outputHash": key, **fields}


# ---------------------------------------------------------------------------- wiring


def test_install_is_idempotent_and_falls_through_for_other_kinds(env):
    assert motion_runs.install(env.pipeline) is env.runner and env.pipeline.motion_runner is env.runner
    assert motion_runs.KINDS == {"Motion", "CropPair", "People"}
    assert not (motion_runs.KINDS & nodes.KINDS)
    # an Edit still quotes and runs through the original pipeline under both installs
    env.project()
    edit = env.approved_edit(env.packet(ROOM_A))
    assert edit["outputHash"] and env.cas.exists(edit["outputHash"])


# ---------------------------------------------------------------------------- the video lock


def test_a_motion_node_under_an_unapproved_edit_is_409_video_locked(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    key = env.plate_key(ROOM_A)
    for edit in (_locked_edit(key), _locked_edit(key, gateStatus="pass"), _locked_edit(key, approved=True)):
        env.save_graph(
            [_plate(packet_id), edit, {"id": "m", "kind": "Motion", "params": {}}], [("p", "e"), ("e", "m")]
        )
        r = env.quote("m")
        assert r.status_code == 409, r.text
        assert r.json()["error"] == "VIDEO_LOCKED"
    env.save_graph([_plate(packet_id), {"id": "c", "kind": "CropPair"}], [("p", "c")])
    assert env.quote("c").json()["error"] == "VIDEO_LOCKED"  # a Plate is not an approved still
    assert env.node_runs("m") == env.node_runs("c") == 0  # nothing to cancel, nothing ledgered


# ---------------------------------------------------------------------------- CropPair on Kling, end to end


@needs_ffmpeg
def test_a_croppair_after_a_server_approved_still_runs_to_a_gated_clip(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    edit = env.approved_edit(packet_id)
    env.put_graph(
        [_plate(packet_id), edit, {"id": "c", "kind": "CropPair", "params": {"duration": 2}}],
        [("p", "e"), ("e", "c")],
    )
    fake = FakeKling(env.tmp / "kling")
    env.runner.engines["kling-o3-4k"] = fake

    r = env.quote("c")
    assert r.status_code == 200, r.text
    q = r.json()
    run_id = q["run"]["id"]
    inputs = q["run"]["inputs"]
    assert q["run"]["state"] == "QUOTED" and q["run"]["node_id"] == "c"
    assert env.cas.exists(inputs["start_hash"]) and env.cas.exists(inputs["end_hash"])
    assert inputs["start_hash"] == inputs["image"] and inputs["end_hash"] == inputs["image_tail"]
    assert inputs["still"] == edit["outputHash"] and inputs["motion"]["task"] == "crop_pair_motion"
    route = q["run"]["route"]
    assert (route["engine_id"], route["provider"], route["custody_grade"]) == ("kling-o3-4k", "runway", "D")
    # a 2 s shot is a 3 s take (every route takes it, Wave 11 decision 63), priced on the runway route
    assert q["quote"]["model"] == "kling-o3-4k" and q["quote"]["price_usd"] == pytest.approx(3 * 0.43)
    assert is_pipeline_run(env.run(run_id)) and motion_runs.is_motion_run(env.run(run_id))

    run = env.approve(run_id)
    assert run["state"] == "DONE", run.get("error")
    assert run["approved_by"] == "human" and fake.job_count == 1

    # the clip, then the two stills, all in the CAS; the clip as an mp4
    clip, start, end = run["outputs"]
    assert env.cas.find(clip).suffix == ".mp4" and (start, end) == (inputs["start_hash"], inputs["end_hash"])
    assert pio.probe(env.cas.find(clip))["nb_frames"] == 48
    # the gate report is the video gate report, landing on the pinned frames
    report = run["gate_report"]
    assert report["landing"] is not None and report["landing"] == report["metrics"]["landing"]
    status = {c["metric_id"]: c["status"] for c in report["checks"]}
    assert status["video.first_frame"] == "pass" and status["video.last_frame"] == "pass"
    assert report["engine"]["id"] == "kling-o3-4k" and report["report_path"].startswith("gates/")
    tm = run["normalized"]["timemap"]
    assert tm == report["metrics"]["timemap"] and (tm["packet_fps"], tm["engine_fps"]) == ("24/1", "24/1")
    gate = env.client.get(f"/api/v0/runs/{run_id}/gate", headers=env.auth).json()
    assert gate["n"] == 1 and gate["metrics"]["landing"] == report["landing"]

    # the engine saw the two frames under opaque names, nothing else
    ins = fake.payloads[0]["inputs"]
    assert base64.b64decode(ins["image"]["b64"]) == env.cas.read_bytes(start)
    assert base64.b64decode(ins["image_tail"]["b64"]) == env.cas.read_bytes(end)
    assert all(re.fullmatch(r"[0-9a-f]{26}\.png", ins[k]["name"]) for k in ("image", "image_tail"))
    assert fake.payloads[0]["params"]["duration"] == 3

    # every move on the bus; the ledger carries the motion fields
    assert env.moves(run_id) == [*WORKER_STATES, "DONE"]
    kinds = [rec["kind"] for rec in env.ledger(run_id)]
    assert kinds == ["quoted", "approved", "submitted", "fetched", "done"]
    quoted = env.ledger(run_id)[0]
    assert quoted["data"]["task"] == "crop_pair_motion" and quoted["data"]["model"]
    assert quoted["data"]["shot_class"] and quoted["data"]["node_kind"] == "CropPair"


@needs_ffmpeg
def test_a_motion_run_left_queued_is_driven_by_the_boot_resume(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    key = env.plate_key(ROOM_A)
    edit = _locked_edit(key, gateStatus="pass", approved=True)
    env.save_graph(
        [_plate(packet_id), edit, {"id": "m", "kind": "Motion", "params": {"duration": 2}}],
        [("p", "e"), ("e", "m")],
    )
    env.runner.engines["kling-o3-4k"] = FakeKling(env.tmp / "kling")
    run_id = env.quote("m").json()["run"]["id"]
    assert env.approve(run_id, start=False)["state"] == "APPROVED"
    with env.pipeline.index.project(PROJECT) as conn:
        env.pipeline.queue(conn, PROJECT, run_id)
    assert run_id in env.pipeline.resume()  # the staging marker: resume picks it up
    run = env.get(run_id)
    assert run["state"] == "DONE", run.get("error")
    assert env.cas.find(run["outputs"][0]).suffix == ".mp4"


# ---------------------------------------------------------------------------- the Move policy and the lock at submit


def test_an_orbit_over_thirty_degrees_is_409_before_any_run(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    edit = _locked_edit(env.plate_key(ROOM_A), gateStatus="pass", approved=True)
    motion = {"id": "m", "kind": "Motion", "params": {"move": "orbit_le_30", "orbitDeg": 35, "duration": 2}}
    env.save_graph([_plate(packet_id), edit, motion], [("p", "e"), ("e", "m")])
    r = env.quote("m")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"] == "HIGH_RISK_MOVE" and body["move"]["risk"] == "highest" and body["move"]["blocked"]
    assert env.node_runs("m") == 0

    # an override with a real reason lets it through, labelled
    motion["params"]["override"] = "client asked for the orbit; we accept guessed geometry on the far side"
    env.save_graph([_plate(packet_id), edit, motion], [("p", "e"), ("e", "m")])
    r = env.quote("m")
    assert r.status_code == 200, r.text
    assert r.json()["run"]["inputs"]["motion"]["label"] == "unseen geometry guessed"


def test_the_lock_is_checked_again_before_the_submit(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    key = env.plate_key(ROOM_A)
    pairs = [("p", "e"), ("e", "m")]
    motion = {"id": "m", "kind": "Motion", "params": {"duration": 2}}
    env.save_graph([_plate(packet_id), _locked_edit(key, gateStatus="pass", approved=True), motion], pairs)
    fake = FakeKling(env.tmp / "kling")
    env.runner.engines["kling-o3-4k"] = fake
    run_id = env.quote("m").json()["run"]["id"]
    env.approve(run_id, start=False)
    # the still loses its approval after the quote (a client revision, say)
    env.save_graph([_plate(packet_id), _locked_edit(key, gateStatus="pass"), motion], pairs)
    with env.pipeline.index.project(PROJECT) as conn:
        env.pipeline.queue(conn, PROJECT, run_id)
    env.pipeline.execute(run_id, PROJECT)
    run = env.run(run_id)
    assert run.state.value == "FAILED" and run.reason_code == "submit_error"
    assert "MotionLocked" in (run.error or "") and fake.submit_count == 0


def test_cancel_reaches_the_video_engine(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    edit = _locked_edit(env.plate_key(ROOM_A), gateStatus="pass", approved=True)
    env.save_graph(
        [_plate(packet_id), edit, {"id": "m", "kind": "Motion", "params": {"duration": 2}}],
        [("p", "e"), ("e", "m")],
    )
    fake = FakeKling(env.tmp / "kling", polls_until_done=1000)
    env.runner.engines["kling-o3-4k"] = fake
    run_id = env.quote("m").json()["run"]["id"]
    env.approve(run_id, start=False)
    with env.pipeline.index.project(PROJECT) as conn:
        env.pipeline.queue(conn, PROJECT, run_id)
    for _ in range(3):
        env.step(run_id)
    assert env.run(run_id).state.value in ("SUBMITTED", "RUNNING") and fake.submit_count == 1
    r = env.client.post(f"/api/v0/runs/{run_id}/cancel", json={"reason": "wrong move"}, headers=env.auth)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "CANCELLED" and fake.cancel_count == 1
    assert env.moves(run_id)[-1] == "CANCELLED"


def test_custody_refuses_the_atlas_route_on_an_nda_project(env):
    env.project(nda=True)
    packet_id = env.packet(ROOM_A)
    edit = _locked_edit(env.plate_key(ROOM_A), gateStatus="pass", approved=True)
    env.save_graph([_plate(packet_id), edit, {"id": "c", "kind": "CropPair"}], [("p", "e"), ("e", "c")])
    r = env.quote("c")
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "custody" and env.node_runs("c") == 0


# ---------------------------------------------------------------------------- People


def _people_node(**params: Any) -> dict[str, Any]:
    base = {
        "cast": "woman in beige linen",
        "behavior": "walks toward the entrance",
        "placement": {"x": 320, "y": 330},
        "personDepthM": 4.0,
    }
    return {"id": "n", "kind": "People", "label": "People", "params": {**base, **params}}


def test_people_on_an_nda_project_is_403_before_anything_is_staged(env):
    env.project(nda=True)
    packet_id = env.packet(ROOM_A)
    env.put_graph([_plate(packet_id), _people_node()], [("p", "n")])
    r = env.quote("n")
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "custody" and env.node_runs("n") == 0
    assert not (env.settings.home / "cache" / "staffage").exists()


def test_people_runs_over_staffage_to_a_gated_still(env):
    fake_mod = _load_fake_staffage()
    env.project()
    packet_id = env.packet(ROOM_A)
    env.put_graph([_plate(packet_id), _people_node(duration=3)], [("p", "n")])
    fake = fake_mod.FakeStaffage(env.tmp, person_box=(300, 230, 340, 330))
    env.runner.people_client = StaffageClient(transport=fake.transport())

    r = env.quote("n")
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["quote"]["price_usd"] == pytest.approx(0.30) and q["quote"]["custody_grade"] == "D"
    assert (
        q["run"]["route"]["engine_id"] == motion_runs.PEOPLE_ENGINE_ID and not fake.calls
    )  # nothing sent yet

    run = env.approve(q["run"]["id"])
    assert run["state"] in ("DONE", "GATE_FAIL"), run.get("error")
    assert run["provider_job_id"].startswith("ppl_")
    out = decode_png16(env.cas.read_bytes(run["outputs"][0]))
    assert out.shape == (360, 640, 3)
    assert run["normalized"]["people"]["footprint_px"] > 0 and run["normalized"]["people"]["grade"] == "D"
    assert any(c[1] == "/api/person" for c in fake.calls)
    status = {c["metric_id"]: c["status"] for c in run["gate_report"]["checks"]}
    assert status["outside_mask.de00"] == "pass"
    assert run["gate_report"]["engine"]["id"] == "staffage"
    assert env.moves(run["id"]) == [*WORKER_STATES, run["state"]]


# ---------------------------------------------------------------------------- previs


@needs_ffmpeg
def test_a_previs_motion_node_runs_to_a_watermarked_take(env):
    env.project()
    hero = env.packet(DOLLY_HERO)
    env.packet(DOLLY)
    edit = env.approved_edit(hero)
    env.put_graph(
        [_plate(hero), edit, {"id": "m", "kind": "Motion", "params": {"previs": True}}],
        [("p", "e"), ("e", "m")],
    )
    fake = FakeR2V(env.tmp / "r2v")
    env.runner.engines[previs.ENGINE_ID] = fake

    r = env.quote("m")
    assert r.status_code == 200, r.text
    q = r.json()
    run_id = q["run"]["id"]
    assert q["run"]["inputs"]["previs"] is True and q["run"]["packet_id"] == hero
    assert q["run"]["route"]["engine_id"] == previs.ENGINE_ID and q["quote"]["custody_grade"] == "D"
    assert q["run"]["params"]["resolution"] == "480P" and q["quote"]["price_usd"] > 0

    run = env.approve(run_id)
    assert run["state"] == "DONE", run.get("error")
    (clip,) = run["outputs"]
    assert env.cas.find(clip).suffix == ".mp4" and run["normalized"]["previs"] is True
    assert run["normalized"]["watermark"]["text"] == "PREVIS"
    clean = blake3(next(iter(fake.raw.values()))).hexdigest()
    assert env.cas.find(clean) is None  # the clean take never enters the CAS
    graph = Project.load(env.pipeline.project_dir(PROJECT)).graph
    node = next(n for n in graph["nodes"] if n["id"] == "m")
    assert node["previs"] is True and node["outputHash"] == clip
    assert all(rec["data"].get("previs") is True for rec in env.ledger(run_id) if rec["kind"] != "approved")
    from plate_daemon.delivery import guard

    prov = guard.Provenance.from_run(env.run(run_id), project_dir=env.pipeline.project_dir(PROJECT))
    assert prov.previs is True
    with pytest.raises(guard.DeliveryRefused, match="previs"):
        guard.authorize(prov)


# ---------------------------------------------------------------------------- the router preview


def _preview(env: Env, monkeypatch, params: dict[str, Any]) -> dict[str, Any]:
    monkeypatch.setitem(node_runner.PREVIEWS, "router", motion_runs.router_preview)
    return env.pipeline.node_runner.preview(PROJECT, "m", "router", params)


CARD_KEYS = {"verdict", "mode", "D_px_median", "disocclusion_fraction", "orbit_deg", "explanation"}


def test_router_preview_on_a_still_keeps_the_camera(env, monkeypatch):
    env.project()
    packet_id = env.packet(ROOM_A)
    edit = _locked_edit(env.plate_key(ROOM_A), gateStatus="pass", approved=True)
    env.save_graph([_plate(packet_id), edit, {"id": "m", "kind": "Motion"}], [("p", "e"), ("e", "m")])
    card = _preview(env, monkeypatch, {"move": "push_in", "duration": 5})
    assert CARD_KEYS <= set(card) and card["source"] == "still" and card["mode"] == "advisory"
    assert card["verdict"] == "ai_interpolate" and card["orbit_deg"] == pytest.approx(0.0, abs=1e-6)
    assert card["disocclusion_fraction"] == pytest.approx(0.0, abs=1e-3)
    assert card["numbers"]["orbit_deg"] == card["orbit_deg"] and card["blocks"] is False
    json.dumps(card, allow_nan=False)


def test_router_preview_reads_the_motion_packet_of_the_plate(env, monkeypatch):
    env.project()
    hero = env.packet(DOLLY_HERO)
    dolly = env.packet(DOLLY)
    edit = _locked_edit(env.plate_key(DOLLY_HERO), gateStatus="pass", approved=True)
    env.save_graph([_plate(hero), edit, {"id": "m", "kind": "Motion"}], [("p", "e"), ("e", "m")])
    monkeypatch.setenv("PLATE_ROUTER_MODE", "blocking")
    card = _preview(env, monkeypatch, {"move": "push_in"})
    assert CARD_KEYS <= set(card) and card["source"] == "motion_packet" and card["motion_packet_id"] == dolly
    assert card["mode"] == "blocking"
    assert card["verdict"] in ("ai_interpolate", "rendered_endpoints_plus_clay", "render_move", "unknown")
    assert isinstance(card["disocclusion_fraction"], float) and isinstance(card["orbit_deg"], float)
    assert card["blocks"] is (card["verdict"] == "render_move")
