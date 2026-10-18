"""People in motion (router plan T105): a STAFFAGE video take composited frame by frame over an approved clip.

Every run here goes through the daemon's own routes after `people_video.install(pipeline)` (what `api.create_app`
will call, T107, after `motion_runs.install`). The base clip is a real Motion run on the T43 fake (an ffmpeg
cross-fade from the start crop to the end crop, as `test_motion_runs.py` builds), scrubbed and accepted through the
routes; the still under it is a numeric take (a Colour node), so Plate -> still -> Motion -> People stays inside the
generation depth of 2. STAFFAGE is the in-process fake (T45) whose person take is a moving disc with its matte, both
written as lossless video. Nothing is paid for and no socket is opened.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import motion_runs, nodes, people_video
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.machine import get_run
from plate_daemon.motion.staffage_client import StaffageClient
from plate_daemon.pipeline import encode_png16
from plate_daemon.projects import Project
from plate_daemon.sse import EventFilter
from plate_gates import io as pio

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
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
DISC_R = 70  # still pixels: a big disc keeps the width measure well inside 2 %
DISC_X0, DISC_Y, DISC_DX = 300.0, 190.0, 30.0  # the disc slides 30 px to the right over the take
TAKE_FRAMES = 36  # shorter than the 48-frame clip: the last frame is held

pytestmark = [
    pytest.mark.skipif(
        not (ROOM_A / "manifest.json").is_file(), reason="fixtures/packets not built yet (T2)"
    ),
    pytest.mark.skipif(
        pio.ffmpeg() is None or pio.ffprobe() is None, reason="ffmpeg and ffprobe are needed for clips"
    ),
]


def _load_fake_staffage():
    spec = importlib.util.spec_from_file_location("fake_staffage", HERE / "fake_staffage.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("fake_staffage", mod)
    spec.loader.exec_module(mod)
    return mod


fake_mod = _load_fake_staffage()


# ---------------------------------------------------------------------------- fakes


def _rgb(png: bytes) -> np.ndarray:
    return np.ascontiguousarray(cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)[..., ::-1])


class FakeKling(FakeEngine):
    """A FakeEngine whose result is a clip cross-faded from the start crop to the end crop it was sent (T43's)."""

    def __init__(self, tmp: Path, *, seconds: int = 2, fps: int = 24, **kwargs: Any):
        super().__init__(**kwargs)
        self.tmp, self.seconds, self.fps = Path(tmp), seconds, fps
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.frames: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        ins = payload["inputs"]
        tail = ins.get("image_tail") or ins["image"]
        self.frames[job] = (_rgb(base64.b64decode(ins["image"]["b64"])), _rgb(base64.b64decode(tail["b64"])))
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        a, b = (f.astype(np.float64) for f in self.frames[result_url[len(RESULT_SCHEME) :]])
        n = self.fps * self.seconds
        frames = [np.round(a * (1 - t) + b * t).astype(np.uint8) for t in np.linspace(0.0, 1.0, n)]
        path = self.tmp / "clip.mp4"
        pio.write_clip(path, frames, fps=self.fps)
        return path.read_bytes()


def disc_alpha(shape: tuple[int, int], cx: float, cy: float, r: float = DISC_R) -> np.ndarray:
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    return np.clip(r + 0.5 - np.hypot(xx + 0.5 - cx, yy + 0.5 - cy), 0.0, 1.0)


class MovingStaffage(fake_mod.FakeStaffage):
    """The T45 fake whose take is a red disc sliding right over the still it was sent, with its matte: a
    `TAKE_FRAMES`-frame video at 24 fps, written lossless so the test reads back what it painted."""

    def _write_result(self, job_id: str, still_path: Path) -> tuple[str, str]:
        raw = cv2.imdecode(np.fromfile(str(still_path), np.uint8), cv2.IMREAD_COLOR)
        still = np.ascontiguousarray(raw[..., ::-1]).astype(np.float32)
        colour = np.array(fake_mod.PERSON_COLOUR_RGB, np.float32)
        plates, mattes = [], []
        for i in range(TAKE_FRAMES):
            a = disc_alpha(still.shape[:2], DISC_X0 + DISC_DX * i / (TAKE_FRAMES - 1), DISC_Y)
            plates.append(
                np.clip(np.rint(still * (1 - a[..., None]) + colour * a[..., None]), 0, 255).astype(np.uint8)
            )
            m = np.clip(np.rint(a * 255.0), 0, 255).astype(np.uint8)
            mattes.append(np.repeat(m[..., None], 3, axis=2))
        plate_path = self.work / f"person_{job_id}.mp4"
        matte_path = self.work / f"person_{job_id}_pha.mp4"
        pio.write_clip(plate_path, plates, fps=24)
        pio.write_clip(matte_path, mattes, fps=24)
        return str(plate_path), str(matte_path)


# ---------------------------------------------------------------------------- the daemon around the nodes


@dataclass
class Env:
    client: TestClient
    auth: dict[str, str]
    settings: Settings
    pipeline: Any
    runner: people_video.PeopleVideoRunner
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
        """Write project.json directly, with the node status the server would have derived."""
        project = Project.load(self.pipeline.project_dir(PROJECT))
        with project.lock(timeout_s=5.0):
            project.save_graph({"nodes": nodes_, "edges": edges})

    @property
    def cas(self):
        return self.pipeline.cas_for(PROJECT)

    def plate_key(self) -> str:
        from plate_pixels import imageio

        rgb = np.rint(imageio.read(ROOM_A / "display.tif").rgb * 65535).astype(np.uint16)
        return self.cas.put_bytes(encode_png16(rgb), "png")

    def quote(self, node_id: str, body: dict[str, Any] | None = None):
        return self.client.post(
            f"/api/v0/projects/{PROJECT}/nodes/{node_id}/quote", json=body or {}, headers=self.auth
        )

    def approve(self, run_id: str) -> dict[str, Any]:
        r = self.client.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=self.auth)
        assert r.status_code == 200, r.text
        return self.get(run_id)

    def get(self, run_id: str) -> dict[str, Any]:
        r = self.client.get(f"/api/v0/runs/{run_id}", headers=self.auth)
        assert r.status_code == 200, r.text
        return r.json()

    def accept(self, run_id: str) -> str:
        scrub = self.client.post(
            f"/api/v0/runs/{run_id}/scrub", json={"quadrants_visited": 4, "zoom_100": True}, headers=self.auth
        )
        assert scrub.status_code == 200, scrub.text
        scrub_id = scrub.json()["scrub_log_id"]
        acc = self.client.post(
            f"/api/v0/runs/{run_id}/accept", json={"scrub_log_id": scrub_id}, headers=self.auth
        )
        assert acc.status_code == 200, acc.text
        return scrub_id

    def node_runs(self, node_id: str) -> int:
        with self.pipeline.index.project(PROJECT) as conn:
            return conn.execute("SELECT COUNT(*) FROM runs WHERE node_id = ?", (node_id,)).fetchone()[0]

    def run_row(self, run_id: str):
        with self.pipeline.index.project(PROJECT) as conn:
            return get_run(conn, run_id)

    def moves(self, run_id: str) -> list[str]:
        recs = self.client.app.state.bus.read(EventFilter(PROJECT, run_id), 0)
        return [r["data"]["to"] for r in recs if r["kind"] == "run.state"]

    def ledger(self, run_id: str) -> list[dict[str, Any]]:
        return [r for r in Ledger(self.pipeline.project_dir(PROJECT)).records() if r.get("run_id") == run_id]


@pytest.fixture
def env(plate_env, tmp_path, monkeypatch) -> Env:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    app = create_app(settings, FakeEngine(echo=True))
    with TestClient(app, base_url=BASE) as client:
        pipeline = client.app.state.pipeline
        pipeline.sleep = lambda _s: None
        nodes.install(pipeline)
        motion_runs.install(pipeline)
        runner = people_video.install(pipeline)
        yield Env(client, {"Authorization": f"Bearer {settings.token}"}, settings, pipeline, runner, tmp_path)


def _edge(a: str, b: str, handle: str | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {"id": f"{a}->{b}", "source": a, "target": b}
    if handle:
        e["targetHandle"] = handle
    return e


def _plate(packet_id: str) -> dict[str, Any]:
    return {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}


def _still(key: str) -> dict[str, Any]:
    """A numeric take of the plate the way the server leaves an accepted one (generation depth 0)."""
    return {
        "id": "e",
        "kind": "Colour",
        "label": "Colour",
        "outputHash": key,
        "gateStatus": "pass",
        "approved": True,
        "approvedBy": "human",
        "outputTier": "measured",
    }


def _motion(**fields: Any) -> dict[str, Any]:
    params = {"duration": 2, **fields.pop("params", {})}
    return {"id": "m", "kind": "Motion", "label": "Motion", "params": params, **fields}


def _people(**params: Any) -> dict[str, Any]:
    base = {
        "video": True,
        "cast": "woman in beige linen",
        "behavior": "walks toward the entrance",
        "placement": {"x": DISC_X0, "y": DISC_Y + DISC_R},
        "personDepthM": 0.5,  # in front of everything in room_a: nothing occludes the disc
    }
    return {"id": "n", "kind": "People", "label": "People", "params": {**base, **params}}


GRAPH_EDGES = [_edge("p", "e"), _edge("e", "m"), _edge("m", "n", "clip")]


def approved_clip(env: Env, **motion: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Plate -> Colour still -> Motion run on the fake Kling, to DONE, scrubbed and accepted. Returns (packet id,
    the still node, the Motion node as the server leaves an accepted take)."""
    packet_id = env.packet(ROOM_A)
    still = _still(env.plate_key())
    env.save_graph([_plate(packet_id), still, _motion(params=motion)], [_edge("p", "e"), _edge("e", "m")])
    env.pipeline.motion_runner.engines["kling-o3-4k"] = FakeKling(env.tmp / "kling")
    r = env.quote("m")
    assert r.status_code == 200, r.text
    run = env.approve(r.json()["run"]["id"])
    assert run["state"] == "DONE", run.get("error")
    scrub_id = env.accept(run["id"])
    node = _motion(
        params=motion,
        gateStatus="pass",
        approved=True,
        approvedBy="human",
        scrubLogId=scrub_id,
        outputHash=run["outputs"][0],
    )
    return packet_id, still, {**node, "run": run}


def _people_ready(env: Env, **motion: Any) -> tuple[dict[str, Any], MovingStaffage]:
    packet_id, still, clip = approved_clip(env, **motion)
    base_run = clip.pop("run")
    env.save_graph([_plate(packet_id), still, clip, _people()], GRAPH_EDGES)
    fake = MovingStaffage(env.tmp)
    env.runner.people_client = StaffageClient(transport=fake.transport())
    return base_run, fake


def read_frames(path: Path) -> list[np.ndarray]:
    info = pio.probe(path)
    w, h = info["width"], info["height"]
    cmd = [
        str(pio.ffmpeg()),
        "-v",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return list(np.frombuffer(out, np.uint8).reshape(-1, h, w, 3))


def _dilate(mask: np.ndarray, px: int = 8) -> np.ndarray:
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
    return cv2.dilate(mask.astype(np.uint8), kernel) > 0


# ---------------------------------------------------------------------------- wiring


def test_install_is_idempotent_and_a_still_people_node_falls_through(env):
    assert people_video.install(env.pipeline) is env.runner and env.pipeline.people_video is env.runner
    env.project()
    packet_id = env.packet(ROOM_A)
    still_people = _people(video=False)
    env.save_graph([_plate(packet_id), still_people], [_edge("p", "n")])
    r = env.quote("n")
    assert r.status_code == 200, r.text
    assert r.json()["run"]["inputs"]["motion_run"]["flavour"] == "people"  # the T66 still People take


def test_box_affine_maps_the_crop_box_onto_the_clip_frame():
    A = people_video.box_affine((64, 36, 512, 288), 512, 288)
    # the box's top-left pixel centre lands on the clip's, its bottom-right edge on the frame's
    assert A @ np.array([64.0, 36.0, 1.0]) == pytest.approx([0.0, 0.0])
    assert A @ np.array([575.5, 323.5, 1.0]) == pytest.approx([511.5, 287.5])
    half = people_video.box_affine((0, 0, 640, 360), 512, 288)
    assert half[0, 0] == pytest.approx(0.8) and half[1, 1] == pytest.approx(0.8)
    box = people_video.box_at([[0, 0, 640, 360], [64, 36, 512, 288]], 0.5)
    assert box == pytest.approx((32.0, 18.0, 576.0, 324.0))
    assert people_video.box_at([[0, 0, 640, 360], [0, 0, 640, 360]], 0.7) == (0.0, 0.0, 640.0, 360.0)


# ---------------------------------------------------------------------------- the lock and custody


def test_an_unapproved_clip_is_409_video_locked_before_any_run(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    still = _still(env.plate_key())
    env.save_graph([_plate(packet_id), still, _motion(), _people()], GRAPH_EDGES)
    r = env.quote("n")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "VIDEO_LOCKED" and r.json()["clip"] == "m"
    # approved on the canvas but with no accepted take in the index: still locked
    env.save_graph(
        [_plate(packet_id), still, _motion(gateStatus="pass", approved=True), _people()], GRAPH_EDGES
    )
    r = env.quote("n")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "VIDEO_LOCKED" and "accepted take" in r.json()["detail"]
    # no clip wired at all
    env.save_graph([_plate(packet_id), still, _people()], [_edge("p", "e"), _edge("e", "n")])
    assert env.quote("n").json()["error"] == "VIDEO_LOCKED"
    assert env.node_runs("n") == 0


def test_people_in_motion_on_an_nda_project_is_403_before_anything_is_staged(env):
    env.project(nda=True)
    packet_id = env.packet(ROOM_A)
    clip = _motion(gateStatus="pass", approved=True, approvedBy="human", outputHash="0" * 64)
    env.save_graph([_plate(packet_id), _still(env.plate_key()), clip, _people()], GRAPH_EDGES)
    fake = MovingStaffage(env.tmp)
    env.runner.people_client = StaffageClient(transport=fake.transport())
    r = env.quote("n")
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "custody" and env.node_runs("n") == 0
    assert not fake.calls and not (env.settings.home / "cache" / "staffage").exists()


# ---------------------------------------------------------------------------- end to end


def _outside_changed(base: np.ndarray, comp: np.ndarray, matte: np.ndarray) -> float:
    """Share of the frame that differs from the base by more than 1 LSB outside the matte dilated by 8 px."""
    moved = np.any(np.abs(comp.astype(np.int16) - base.astype(np.int16)) > 1, axis=-1)
    return float((moved & ~_dilate(matte > 2)).sum()) / float(moved.size)


def test_a_people_take_over_a_locked_clip_runs_to_a_gated_composite(env):
    env.project()
    base_run, fake = _people_ready(env, move="locked_tripod")
    r = env.quote("n")
    assert r.status_code == 200, r.text
    q = r.json()
    run_id, inputs = q["run"]["id"], q["run"]["inputs"]
    # the 2 s shot's Kling take is 3 s (Wave 11 decision 63), and the People pass is priced on the take
    assert q["quote"]["price_usd"] == pytest.approx(0.30) and q["quote"]["custody_grade"] == "D"
    assert q["run"]["route"]["engine_id"] == "staffage" and not fake.calls  # nothing sent at the quote
    assert inputs["clip"] == base_run["outputs"][0] and inputs["clip_run"] == base_run["id"]
    assert inputs["ground_point"] == [DISC_X0, DISC_Y + DISC_R] and inputs["behaviour"].startswith("walks")
    assert inputs["plate"] and inputs["still"] and inputs["cast"] == "woman in beige linen"

    run = env.approve(run_id)
    assert run["state"] == "DONE", run.get("error")
    assert run["provider_job_id"].startswith("pplv_") and any(c[1] == "/api/person" for c in fake.calls)
    (clip,) = run["outputs"]
    assert env.cas.find(clip).suffix == ".mp4"
    base_path = env.cas.find(base_run["outputs"][0])
    base_info, info = pio.probe(base_path), pio.probe(env.cas.find(clip))
    assert info["nb_frames"] == base_info["nb_frames"] == 48 and info["fps"] == base_info["fps"]

    norm = run["normalized"]
    assert norm["timemap"] == base_run["normalized"]["timemap"]
    ppl = norm["people"]
    assert ppl["frames"] == 48 and ppl["matte_px_mean"] > 0
    assert ppl["held_frames"] == 48 - TAKE_FRAMES and ppl["person_frames"] == TAKE_FRAMES

    # per frame: outside the dilated matte the composite is the base clip, inside it the person is there
    bases = read_frames(base_path)
    comps = read_frames(env.cas.find(clip))
    mattes = [f[..., 0] for f in read_frames(env.cas.find(norm["assets"]["person_matte_clip"]))]
    assert len(bases) == len(comps) == len(mattes) == 48
    for b, c, m in zip(bases, comps, mattes, strict=True):
        assert _outside_changed(b, c, m) <= 0.02
        inside = m > 128
        assert 0 < inside.mean() < 0.25
        assert np.abs(c[inside].astype(int) - np.array(fake_mod.PERSON_COLOUR_RGB)).mean() < 6

    report = run["gate_report"]
    status = {c["metric_id"]: c for c in report["checks"]}
    assert status["people.lock_delta_px"]["status"] == "pass"
    assert (
        status["people.lock_delta_px"]["threshold"] == 0.5 and status["people.lock_delta_px"]["unit"] == "px"
    )
    assert status["video.first_frame"]["status"] == "pass" and status["video.first_frame"]["value"] == 0.0
    assert status["video.last_frame"]["value"] == 0.0
    assert "video.camera_lock_median_flow_px" in status  # a locked clip scores the masked lock too
    metrics = report["metrics"]
    assert 0 < metrics["person.coverage"] < 0.25
    assert metrics["camera_lock"]["delta_px"] <= 0.5 and metrics["landing"]["against"] == "base_clip"
    assert (
        metrics["flicker"]["base"]["frames"] == 48 and metrics["outside_matte"]["changed_fraction_max"] == 0
    )
    assert report["engine"]["id"] == "staffage" and report["report_path"].startswith("gates/")
    assert report["verdict"] != "fail"

    assert env.moves(run_id) == [*WORKER_STATES, "DONE"]
    kinds = [rec["kind"] for rec in env.ledger(run_id)]
    assert kinds == ["quoted", "approved", "submitted", "fetched", "done"]
    quoted = env.ledger(run_id)[0]["data"]
    assert quoted["task"] == "people" and quoted["shot_class"] == "people_video" and quoted["video"] is True


def _person_width(matte: np.ndarray) -> float:
    """The disc's width in clip pixels, sub-pixel: the widest row of its alpha, summed."""
    return float((matte.astype(np.float64) / 255.0).sum(axis=1).max())


def test_over_a_push_in_the_person_grows_with_the_crop(env):
    env.project()
    base_run, _fake = _people_ready(env, z0=1.0, z1=1.25, cx=0.5, cy=0.5)
    boxes = base_run["normalized"]["motion"]["boxes"]
    (x0, y0, w0, h0), (x1, y1, w1, h1) = boxes
    assert w0 > w1  # a push-in
    q = env.quote("n").json()
    run = env.approve(q["run"]["id"])
    assert run["state"] == "DONE", run.get("error")
    comp_path = env.cas.find(run["outputs"][0])
    mattes = [f[..., 0] for f in read_frames(env.cas.find(run["normalized"]["assets"]["person_matte_clip"]))]
    first, last = _person_width(mattes[0]), _person_width(mattes[-1])
    assert last == pytest.approx(first * (w0 / w1), rel=0.02)
    # the clip frame shows the box: the disc's width is the still's scaled by the clip width over the box width
    clip_w = pio.probe(comp_path)["width"]
    assert first == pytest.approx(2 * DISC_R * clip_w / w0, rel=0.02)
    # the composite agrees with its matte: on the disc's widest row she is there exactly where the matte says
    comps, bases = read_frames(comp_path), read_frames(env.cas.find(base_run["outputs"][0]))
    colour = np.array(fake_mod.PERSON_COLOUR_RGB)
    for i in (0, -1):
        row = int(np.argmax(mattes[i].astype(np.int64).sum(axis=1)))
        solid, empty = mattes[i][row] == 255, mattes[i][row] == 0
        assert solid.sum() == pytest.approx(_person_width(mattes[i]), abs=2.0)
        assert np.abs(comps[i][row][solid].astype(int) - colour).max() <= 6
        assert np.array_equal(comps[i][row][empty], bases[i][row][empty])
        assert _outside_changed(bases[i], comps[i], mattes[i]) <= 0.02
    status = {c["metric_id"]: c["status"] for c in run["gate_report"]["checks"]}
    assert status["people.lock_delta_px"] == "pass"
    assert "video.camera_lock_median_flow_px" not in status  # a push-in moves by design


def test_a_clip_that_lost_its_approval_is_refused_at_submit(env):
    env.project()
    base_run, fake = _people_ready(env, move="locked_tripod")
    run_id = env.quote("n").json()["run"]["id"]
    project = Project.load(env.pipeline.project_dir(PROJECT))
    graph = project.graph
    for node in graph["nodes"]:
        if node["id"] == "m":
            node["approved"] = False
    with project.lock(timeout_s=5.0):
        project.save_graph(graph)
    r = env.client.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=env.auth)
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "VIDEO_LOCKED" and not fake.calls
    assert env.run_row(run_id).state.value == "QUOTED"
