"""Wave 12 stress fixes for Motion (router plan T138): the params agree with the route.

The attackers found three ways a Motion quote promised what the route could not do: a Seedance 2.5 i2v or Gen-4.5
take on the preferred Runway route always carried a `negative_prompt` (empty or the recipe's) that the adapter's
manifest schema refused at submit, after the approval; a Kling seed (`-1`, "random") quoted on Runway, whose Kling
models have no seed field, and failed at submit; and a Runway task that read SUCCEEDED a moment before its output
list was filled failed the take outright. Each repro is a daemon test here: the real routes after
`motion_runs.install(pipeline)`, the stills approved in `project.json`, the real `RunwayVideo` adapter on the
project's Egress whose network is the committed `contract/runway` recordings matched on method, host and path (as
`test_motion_runway.py` does), and a sleepless worker. No socket is opened and nothing is paid for.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import egress as egress_mod
from plate_daemon import motion_runs, nodes
from plate_daemon.adapter import PollResult
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.motion import kling_job
from plate_daemon.pipeline import encode_png16
from plate_daemon.projects import Project
from plate_engines.adapters.runway_schema import DOCUMENTED_OUTPUT_HOST, RUNWAY_HOST
from plate_engines.adapters.runway_video import RunwayVideo
from plate_engines.adapters.transport import (
    CONTRACT_DIR,
    FixtureTransport,
    UnrecordedRequest,
    load_recordings,
)
from plate_engines.loader import load_manifests
from plate_gates import io as pio

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"
KEY = "key_" + "0123456789abcdef" * 8  # the documented key shape, not a real key
TASK_ID = "0a6a3a0e-5a1e-4d7a-9f49-2b6f0a4c2d11"  # what every happy create recording answers
OUTPUT_URL = f"https://{DOCUMENTED_OUTPUT_HOST}/output.mp4?_jwt=test"
SEED_SKIP = (
    "prefer: runway skipped: kling-v3-pro: seed is not expressible on the runway route; pin the atlas route "
    "(kling3.0_pro has no seed field)"
)
EMPTY = "transient: runway SUCCEEDED without an output"

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file(), reason="fixtures/packets not built yet (T2)"
)
needs_ffmpeg = pytest.mark.skipif(
    pio.ffmpeg() is None or pio.ffprobe() is None, reason="ffmpeg and ffprobe are needed for the clip tests"
)


# ---------------------------------------------------------------------------- helpers


def _plate16(path: Path) -> np.ndarray:
    from plate_pixels import imageio

    return np.rint(imageio.read(path / "display.tif").rgb * 65535).astype(np.uint16)


def _rgb(png: bytes) -> np.ndarray:
    return np.ascontiguousarray(cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)[..., ::-1])


def _data_uri_rgb(uri: str) -> np.ndarray:
    head, _, b64 = uri.partition(",")
    assert head.startswith("data:image/png;base64"), head[:40]
    return _rgb(base64.b64decode(b64))


def _clip(tmp: Path, a: np.ndarray, b: np.ndarray, seconds: int) -> bytes:
    """A 24 fps clip cross-faded from `a` to `b` (a held frame when they are one)."""
    fa, fb = a.astype(np.float64), b.astype(np.float64)
    n = 24 * int(seconds)
    frames = [np.round(fa * (1 - t) + fb * t).astype(np.uint8) for t in np.linspace(0.0, 1.0, n)]
    path = Path(tmp) / "clip.mp4"
    pio.write_clip(path, frames, fps=24)
    return path.read_bytes()


def _edge(a: str, b: str) -> dict[str, Any]:
    return {"id": f"{a}->{b}", "source": a, "target": b}


def _still(nid: str, key: str) -> dict[str, Any]:
    """An Edit node the way the server leaves an accepted take: gate passed, approved by a human, its output."""
    return {
        "id": nid,
        "kind": "Edit",
        "label": "Edit",
        "outputHash": key,
        "gateStatus": "pass",
        "approved": True,
        "approvedBy": "human",
        "outputTier": "measured",
    }


class Replay(httpx.MockTransport):
    """The vendor side of a Runway run from the committed recordings, matched on method, host and path (the happy
    create and the THROTTLED -> SUCCEEDED poll where a path has several). `outputs`, when given, replaces the poll
    answers: look n reads SUCCEEDED with `outputs[n]` as its output list (the last one repeated). The result download
    on the documented output host answers a clip of the frames the create sent. Unrecorded requests raise."""

    PREFER = ("kling_v3_submit", "kling_o3_poll")

    def __init__(self, tmp: Path, outputs: list[list[str]] | None = None):
        self.tmp = Path(tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.recordings = load_recordings(CONTRACT_DIR / "runway")
        self.outputs = outputs
        self.served: list[tuple[str, str, str, bytes]] = []
        self._count: dict[str, int] = {}
        super().__init__(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        method, host, path = request.method.upper(), (request.url.host or "").lower(), request.url.path or "/"
        body = request.read()
        self.served.append((method, host, path, body))
        if host == DOCUMENTED_OUTPUT_HOST:
            return httpx.Response(200, content=self.clip())
        if self.outputs is not None and method == "GET" and path.startswith("/v1/tasks/"):
            n = self.looks() - 1
            out = self.outputs[min(n, len(self.outputs) - 1)]
            return httpx.Response(200, json={"id": TASK_ID, "status": "SUCCEEDED", "output": out})
        found = [r for r in self.recordings if (r.method, r.host, r.path) == (method, host, path)]
        if not found:
            raise UnrecordedRequest(f"no recording for {method} {host}{path}")
        rec = next((r for name in self.PREFER for r in found if r.name == f"{name}.json"), found[0])
        n = self._count.get(rec.name, 0)
        self._count[rec.name] = n + 1
        return FixtureTransport._build(rec.responses[min(n, len(rec.responses) - 1)])

    def creates(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (path, json.loads(body))
            for method, host, path, body in self.served
            if host == RUNWAY_HOST and path in ("/v1/image_to_video", "/v1/text_to_video")
        ]

    def looks(self) -> int:
        return sum(
            1 for m, h, p, _ in self.served if m == "GET" and h == RUNWAY_HOST and p.startswith("/v1/tasks/")
        )

    def clip(self) -> bytes:
        _path, body = self.creates()[-1]
        frames = [_data_uri_rgb(p["uri"]) for p in body["promptImage"]]
        return _clip(self.tmp, frames[0], frames[-1], body["duration"])


class FakeVideo:
    """A worker-protocol video engine (the Atlas route's stand-in): records the payload, answers a cross-fade from
    the start still to the end still it was sent."""

    def __init__(self, tmp: Path):
        self.tmp = Path(tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.payloads: list[dict[str, Any]] = []

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        self.payloads.append(payload)
        return f"job-{idempotency_key[:12]}"

    def poll(self, job: str) -> PollResult:
        return PollResult("succeeded", result_url=f"fake://{job}")

    def fetch(self, url: str) -> bytes:
        ins, params = self.payloads[-1]["inputs"], self.payloads[-1]["params"]
        a = _rgb(base64.b64decode(ins["image"]["b64"]))
        b = _rgb(base64.b64decode(ins["image_tail"]["b64"])) if "image_tail" in ins else a
        return _clip(self.tmp, a, b, params["duration"])


@dataclass
class Env:
    client: TestClient
    auth: dict[str, str]
    pipeline: Any
    runner: motion_runs.MotionRunner
    tmp: Path

    def motion(self, **params: Any) -> None:
        """A project, the room_a packet, Plate -> an approved still -> Motion `m` with `params` (5 s by default)."""
        r = self.client.post(
            "/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=self.auth
        )
        assert r.status_code == 201, r.text
        manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
        hdr = {**self.auth, "X-Plate-Packet-Version": manifest["packet_version"]}
        body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
        r = self.client.post("/api/v0/packets", json=body, headers=hdr)
        assert r.status_code in (200, 202), r.text
        packet_id = r.json()["packet_id"]
        key = self.pipeline.cas_for(PROJECT).put_bytes(encode_png16(_plate16(ROOM_A)), "png")
        plate = {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}
        node = {"id": "m", "kind": "Motion", "label": "Motion", "params": {"duration": 5, **params}}
        project = Project.load(self.pipeline.project_dir(PROJECT))
        with project.lock(timeout_s=5.0):
            project.save_graph(
                {"nodes": [plate, _still("e", key), node], "edges": [_edge("p", "e"), _edge("e", "m")]}
            )

    def quote(self, body: dict[str, Any]):
        return self.client.post(f"/api/v0/projects/{PROJECT}/nodes/m/quote", json=body, headers=self.auth)

    def approve(self, run_id: str) -> dict[str, Any]:
        r = self.client.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=self.auth)
        assert r.status_code == 200, r.text
        r = self.client.get(f"/api/v0/runs/{run_id}", headers=self.auth)
        assert r.status_code == 200, r.text
        return r.json()

    def node_runs(self) -> int:
        with self.pipeline.index.project(PROJECT) as conn:
            return conn.execute("SELECT COUNT(*) FROM runs WHERE node_id = 'm'").fetchone()[0]


@pytest.fixture
def env(plate_env, tmp_path, monkeypatch) -> Env:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv("RUNWAYML_API_SECRET", KEY)
    monkeypatch.setattr(
        RunwayVideo, "MIN_POLL_GAP_S", 0.0
    )  # one recorded answer per look of the sleepless worker
    settings = Settings.load()
    with TestClient(create_app(settings, FakeEngine(echo=True)), base_url=BASE) as client:
        pipeline = client.app.state.pipeline
        pipeline.sleep = lambda _s: None
        nodes.install(pipeline)
        runner = motion_runs.install(pipeline)
        yield Env(client, {"Authorization": f"Bearer {settings.token}"}, pipeline, runner, tmp_path)


def _network(monkeypatch, tmp_path: Path, outputs: list[list[str]] | None = None) -> Replay:
    replay = Replay(tmp_path / "runway", outputs)
    monkeypatch.setattr(egress_mod, "default_transport", lambda: replay)
    return replay


# ---------------------------------------------------------------------------- the fold reaches the provider


@needs_ffmpeg
@pytest.mark.parametrize(("engine", "usd"), [("seedance-2.5-i2v", 5 * 0.68), ("gen-4.5", 5 * 0.12)])
def test_a_start_frame_engine_on_runway_folds_the_negative_prompt_into_the_prompt(
    env, monkeypatch, tmp_path, engine, usd
):
    replay = _network(monkeypatch, tmp_path)
    env.motion(move="locked_tripod")
    r = env.quote({"engine_id": engine})
    assert r.status_code == 200, r.text
    q = r.json()
    route = q["run"]["route"]
    assert route["provider"] == "runway" and route["reasons"] == ["prefer: runway first for video"]
    assert q["quote"]["price_usd"] == pytest.approx(usd)
    negative = q["run"]["params"]["negative_prompt"]  # the recipe's, which the manifest declares no field for
    assert negative and "negative_prompt" not in {p.name for p in env.pipeline.manifests[engine].params}

    run = env.approve(q["run"]["id"])
    assert run["state"] == "DONE", run.get("error")
    ((path, body),) = replay.creates()  # exactly one create
    assert path == "/v1/image_to_video" and "negative_prompt" not in body and "negativePrompt" not in body
    assert body["promptText"].endswith(f"\nAvoid: {negative}")
    assert len(body["promptImage"]) == 1  # a start frame only: the take holds the frame


def test_an_empty_negative_prompt_is_not_a_param_at_all():
    still16 = _plate16(ROOM_A)
    for engine in ("seedance-2.5-i2v", "gen-4.5", "kling-v3-pro"):
        kw = {"engine_id": engine, "shot_s": 5, "move_kind": "locked_tripod", "manifests": load_manifests()}
        empty = kling_job.plan_from_still(still16, negative_prompt="  ", **kw)
        assert "negative_prompt" not in empty.params() and empty.negative_prompt == ""
        assert kling_job.plan_from_still(still16, negative_prompt="flicker", **kw).params()[
            "negative_prompt"
        ] == ("flicker")


# ---------------------------------------------------------------------------- route around what Runway cannot express


@needs_ffmpeg
def test_a_kling_seed_quotes_on_atlas_with_the_skip_reason_and_runs(env, tmp_path):
    env.motion(seed=-1)
    r = env.quote({"engine_id": "kling-v3-pro"})
    assert r.status_code == 200, r.text
    q = r.json()
    route = q["run"]["route"]
    assert route["provider"] == "atlas" and route["route_id"] == "atlas"
    assert route["reasons"] == ["prefer: runway first for video", SEED_SKIP]
    assert q["run"]["inputs"]["motion_run"]["route_reasons"] == route["reasons"]
    assert q["quote"]["price_usd"] == pytest.approx(5 * 0.153) and q["quote"]["route_id"] == "atlas"
    assert q["run"]["params"]["seed"] == -1

    fake = FakeVideo(tmp_path / "atlas")
    env.runner.engines["kling-v3-pro"] = fake  # the Atlas route's stand-in for execution
    run = env.approve(q["run"]["id"])
    assert run["state"] == "DONE", run.get("error")
    assert len(fake.payloads) == 1 and fake.payloads[0]["params"]["seed"] == -1


def test_a_pinned_route_that_cannot_take_the_params_is_422_params_and_no_run(env):
    env.motion(seed=3)
    r = env.quote({"engine_id": "kling-v3-pro", "route_id": "runway"})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "params"
    assert body["detail"] == SEED_SKIP.removeprefix("prefer: runway skipped: ")
    assert body["routes"] == [{"route_id": "runway", "detail": body["detail"]}]
    assert env.node_runs() == 0


def test_a_param_no_route_can_take_is_422_params_and_no_run(env):
    # Runway cannot express camera_fixed; Atlas, BytePlus and WaveSpeed declare no negative_prompt for Seedance
    env.motion(move="locked_tripod", camera_fixed=True)
    r = env.quote({"engine_id": "seedance-2.5-i2v"})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "params"
    assert body["detail"] == (
        "seedance-2.5-i2v: camera_fixed true is not expressible on the runway route; pin the atlas route"
    )
    assert [x["route_id"] for x in body["routes"]] == ["runway", "atlas", "byteplus", "wavespeed"]
    assert all("negative_prompt" in x["detail"] for x in body["routes"][1:])
    assert env.node_runs() == 0


# ---------------------------------------------------------------------------- SUCCEEDED before the output list


@needs_ffmpeg
@pytest.mark.parametrize(
    ("outputs", "state"),
    [([[], [], [], [OUTPUT_URL]], "DONE"), ([[], [], [], []], "FAILED")],
)
def test_succeeded_without_an_output_waits_three_looks(env, monkeypatch, tmp_path, outputs, state):
    replay = _network(monkeypatch, tmp_path, outputs)
    env.motion()
    q = env.quote({"engine_id": "kling-v3-pro"}).json()
    assert q["run"]["route"]["provider"] == "runway"
    run = env.approve(q["run"]["id"])
    assert run["state"] == state, run.get("error")
    assert replay.looks() == 4 and len(replay.creates()) == 1
    downloads = [s for s in replay.served if s[1] == DOCUMENTED_OUTPUT_HOST]
    if state == "DONE":
        assert len(downloads) == 1 and run["provider_job_id"] == TASK_ID
    else:
        assert downloads == [] and EMPTY in json.dumps(run)
