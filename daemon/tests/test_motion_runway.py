"""Runway as the preferred video route, wired into the daemon (router plan T120, Wave 11 decisions 59 to 70).

Daemon-level tests drive the real routes after `motion_runs.install(pipeline)`, as `test_motion_wave9.py` does: the
stills are PNG16s put in the project CAS and marked gate-passed and approved in `project.json` (the shape the server
derives for an accepted take). Quotes need no vendor at all. The two full runs (a Kling v3 Pro CropPair, a Seedance
r2v previs draft) build the real `RunwayVideo` adapter on the project's Egress, whose network is `RunwayReplay`:
the committed `contract/runway` recordings matched on method, host and path (the frames of a daemon run are not the
contract test's, so the body is not matched here; `engines/tests/test_adapters_runway.py` pins the bodies), with the
result download answering a real clip cut from the frames the submit sent, so the video gates and the PREVIS
watermark run on it. No socket is opened and nothing is paid for.
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
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.custody import CustodyViolation, Grade
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.motion import kling_job, previs
from plate_daemon.outbound_log import OutboundLog
from plate_daemon.pipeline import PipelineError, adapter_class, encode_png16
from plate_daemon.projects import Project
from plate_engines import prefer
from plate_engines.adapters.runway_schema import DOCUMENTED_OUTPUT_HOST, RUNWAY_HOST
from plate_engines.adapters.runway_video import RunwayVideo
from plate_engines.adapters.seedance_fallbacks import BytePlusSeedance, WaveSpeedSeedance
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
PACKETS = ROOT / "fixtures" / "packets"
ROOM_A, DOLLY, DOLLY_HERO = PACKETS / "room_a", PACKETS / "dolly_12f", PACKETS / "dolly_hero"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"
KEY = "key_" + "0123456789abcdef" * 8  # the documented key shape, not a real key
TASK_ID = "0a6a3a0e-5a1e-4d7a-9f49-2b6f0a4c2d11"  # what every happy create recording answers
ALLOWED_D = {Grade.C0, Grade.C1, Grade.C2, Grade.D}
NDA_GRADES = {Grade.C0, Grade.C1}

have_packets = pytest.mark.skipif(
    not all((p / "manifest.json").is_file() for p in (ROOM_A, DOLLY, DOLLY_HERO)),
    reason="fixtures/packets not built yet (T2)",
)
needs_ffmpeg = pytest.mark.skipif(
    pio.ffmpeg() is None or pio.ffprobe() is None, reason="ffmpeg and ffprobe are needed for the clip tests"
)


# ---------------------------------------------------------------------------- helpers


def textured(w: int, h: int) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w]
    img = np.stack([x / max(w - 1, 1), y / max(h - 1, 1), ((x // 16 + y // 16) % 2) * 0.4 + 0.3], axis=-1)
    return (img * 65535).astype(np.uint16)


def _plate16(path: Path) -> np.ndarray:
    from plate_pixels import imageio

    return np.rint(imageio.read(path / "display.tif").rgb * 65535).astype(np.uint16)


def _data_uri_rgb(uri: str) -> np.ndarray:
    head, _, b64 = uri.partition(",")
    assert head.startswith("data:image/png;base64"), head[:40]
    bgr = cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), cv2.IMREAD_COLOR)
    return np.ascontiguousarray(bgr[..., ::-1])


def _edge(a: str, b: str, handle: str | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {"id": f"{a}->{b}" + (f":{handle}" if handle else ""), "source": a, "target": b}
    if handle:
        e["targetHandle"] = handle
    return e


def _plate(packet_id: str) -> dict[str, Any]:
    return {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}


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


@dataclass
class Served:
    method: str
    host: str
    path: str
    body: bytes
    authorization: str | None


class RunwayReplay(httpx.MockTransport):
    """The vendor side of a Runway run from the committed `contract/runway` recordings, matched on method, host and
    path; `prefer` names the recording to answer where a path has several (the happy one). The result download on
    the documented output host answers a real clip: the frames the last create sent (start to end, or the first
    reference held), for the seconds it asked, at 24 fps. Unrecorded requests raise: nothing reaches a socket."""

    PREFER = (
        "kling_v3_submit",
        "seedance_r2v_submit",
        "uploads_init",
        "uploads_put",
        "kling_o3_poll",
    )

    def __init__(self, tmp: Path):
        self.tmp = Path(tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.recordings = load_recordings(CONTRACT_DIR / "runway")
        self.served: list[Served] = []
        self._count: dict[str, int] = {}
        super().__init__(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        method, host, path = request.method.upper(), (request.url.host or "").lower(), request.url.path or "/"
        body = request.read()
        self.served.append(Served(method, host, path, body, request.headers.get("authorization")))
        if host == DOCUMENTED_OUTPUT_HOST:
            return httpx.Response(200, content=self.clip())
        found = [r for r in self.recordings if (r.method, r.host, r.path) == (method, host, path)]
        if not found:
            raise UnrecordedRequest(f"no recording for {method} {host}{path}")
        rec = next((r for name in self.PREFER for r in found if r.name == f"{name}.json"), found[0])
        n = self._count.get(rec.name, 0)
        self._count[rec.name] = n + 1
        return FixtureTransport._build(rec.responses[min(n, len(rec.responses) - 1)])

    def creates(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (s.path, json.loads(s.body))
            for s in self.served
            if s.host == RUNWAY_HOST and s.path in ("/v1/image_to_video", "/v1/text_to_video")
        ]

    def clip(self) -> bytes:
        path, body = self.creates()[-1]
        if path == "/v1/image_to_video":
            frames = [_data_uri_rgb(p["uri"]) for p in body["promptImage"]]
            a = frames[0].astype(np.float64)
            b = frames[-1].astype(np.float64)
        else:
            a = b = _data_uri_rgb(body["references"][0]["uri"]).astype(np.float64)
        n = 24 * int(body["duration"])
        out = [np.round(a * (1 - t) + b * t).astype(np.uint8) for t in np.linspace(0.0, 1.0, n)]
        clip = self.tmp / "runway-clip.mp4"
        pio.write_clip(clip, out, fps=24)
        return clip.read_bytes()


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
    def cas(self):
        return self.pipeline.cas_for(PROJECT)

    def still_key(self, rgb16: np.ndarray) -> str:
        return self.cas.put_bytes(encode_png16(rgb16), "png")

    def one_still(self, packet: Path, rgb16: np.ndarray | None = None, **motion: Any) -> str:
        """Plate -> an approved still -> Motion `m` with `motion` as its params; returns the still's CAS key."""
        packet_id = self.packet(packet)
        key = self.still_key(_plate16(packet) if rgb16 is None else rgb16)
        node = {"id": "m", "kind": "Motion", "label": "Motion", "params": {"duration": 5, **motion}}
        self.save_graph([_plate(packet_id), _still("e", key), node], [_edge("p", "e"), _edge("e", "m")])
        return key

    def quote(self, body: dict[str, Any] | None = None, node_id: str = "m"):
        return self.client.post(
            f"/api/v0/projects/{PROJECT}/nodes/{node_id}/quote", json=body or {}, headers=self.auth
        )

    def put_settings(self, body: dict[str, Any]):
        return self.client.put(f"/api/v0/projects/{PROJECT}/settings", json=body, headers=self.auth)

    def approve(self, run_id: str) -> dict[str, Any]:
        r = self.client.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=self.auth)
        assert r.status_code == 200, r.text
        r = self.client.get(f"/api/v0/runs/{run_id}", headers=self.auth)
        assert r.status_code == 200, r.text
        return r.json()

    def node_runs(self, node_id: str = "m") -> int:
        with self.pipeline.index.project(PROJECT) as conn:
            return conn.execute("SELECT COUNT(*) FROM runs WHERE node_id = ?", (node_id,)).fetchone()[0]

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
        runner = motion_runs.install(pipeline)
        yield Env(client, {"Authorization": f"Bearer {settings.token}"}, settings, pipeline, runner, tmp_path)


@pytest.fixture
def runway(monkeypatch, tmp_path) -> RunwayReplay:
    """The Egress network for this test is the Runway replay; the key is a test key; the 5 s look rule is lifted so
    the recorded THROTTLED, PENDING, RUNNING, SUCCEEDED answers come one per look of the (sleepless) worker."""
    replay = RunwayReplay(tmp_path / "runway")
    monkeypatch.setattr(egress_mod, "default_transport", lambda: replay)
    monkeypatch.setenv("RUNWAYML_API_SECRET", KEY)
    monkeypatch.setattr(RunwayVideo, "MIN_POLL_GAP_S", 0.0)
    return replay


# ---------------------------------------------------------------------------- the library


def test_motion_engines_are_every_start_frame_video_manifest():
    manifests = load_manifests()
    assert kling_job.motion_engines(manifests) == {
        "gen-4.5": False,
        "kling-o3-4k": True,
        "kling-v3-pro": True,
        "seedance-2.5-i2v": False,
        "veo-3.1": True,
    }
    assert kling_job.START_END_ENGINES == ("kling-o3-4k", "kling-v3-pro", "veo-3.1")
    assert kling_job.KLING_ENGINES == kling_job.START_END_ENGINES
    assert kling_job.DEFAULT_RESOLUTION["seedance-2.5-i2v"] == "1080P"
    assert kling_job.DEFAULT_RESOLUTION["gen-4.5"] is None
    with pytest.raises(PipelineError) as e:
        kling_job.plan_from_still(
            textured(480, 270), engine_id="seedance-2.5-r2v", shot_s=4, manifests=manifests
        )
    assert e.value.code == "engine_kind" and "gen-4.5" in str(e.value) and "seedance-2.5-i2v" in str(e.value)


@pytest.mark.parametrize(
    ("engine_id", "shot", "seconds"),
    [
        ("kling-o3-4k", 0.5, 3),
        ("kling-o3-4k", 2, 3),
        ("kling-v3-pro", 4.2, 5),
        ("veo-3.1", 3, 4),
        ("veo-3.1", 5, 6),
        ("veo-3.1", 7, 8),
        ("veo-3.1", 12, 8),
        ("seedance-2.5-i2v", 2, 4),
        ("gen-4.5", 1, 2),
        ("gen-4.5", 11, 10),
    ],
)
def test_the_duration_is_one_every_active_route_takes(engine_id, shot, seconds):
    manifest = load_manifests()[engine_id]
    assert kling_job.clamp_duration(shot, manifest) == seconds


def test_pick_route_orders_by_the_rule_and_says_why():
    manifests = load_manifests()
    kling = manifests["kling-o3-4k"]
    r = kling_job.pick_route(kling, ALLOWED_D, frame=(1920, 1080), tier="4K")
    assert (r["provider"], r["model_version"]) == ("runway", "klingO3_4k")
    assert r["reasons"] == ["prefer: runway first for video"] and r["custody_status"] == "confirmed"
    assert kling_job.pick_route(kling, ALLOWED_D, prefer_provider="none")["provider"] == "atlas"
    assert kling_job.pick_route(kling, ALLOWED_D, prefer_provider=None)["provider"] == "atlas"
    off = kling_job.pick_route(kling, ALLOWED_D, frame=(1920, 804), tier="4K")
    assert off["provider"] == "atlas"
    assert off["reasons"] == ["prefer: runway skipped: no legal ratio for 1920x804 on klingO3_4k"]
    # klingO3_4k renders at 4K only: a 1080P plan of this engine stays on Atlas (the adapter would refuse it)
    assert kling_job.pick_route(kling, ALLOWED_D, frame=(1920, 1080), tier="1080P")["provider"] == "atlas"
    # a pinned runway route the frame does not fit is refused at the quote, not at the vendor
    with pytest.raises(PipelineError) as e:
        kling_job.pick_route(kling, ALLOWED_D, "runway", frame=(1920, 804), tier="4K")
    assert e.value.code == "route_ratio" and "3840:2160" in str(e.value)
    # gen-4.5 has the runway route alone: an off-ratio frame has nowhere to go
    with pytest.raises(PipelineError) as e:
        kling_job.pick_route(manifests["gen-4.5"], ALLOWED_D, frame=(1000, 400), tier=None)
    assert e.value.code == "route_ratio"
    assert kling_job.pick_route(manifests["gen-4.5"], ALLOWED_D, frame=(960, 960))["provider"] == "runway"
    with pytest.raises(CustodyViolation):  # NDA: no route at all, Runway included
        kling_job.pick_route(kling, NDA_GRADES, frame=(1920, 1080), tier="4K")


def test_the_pipeline_finds_each_routes_own_adapter():
    manifests = load_manifests()
    for engine_id in RunwayVideo.ENGINE_IDS:
        route = next(r for r in manifests[engine_id].routes if r.provider == "runway")
        assert adapter_class(manifests[engine_id], route) is RunwayVideo, engine_id
    seedance = manifests["seedance-2.5-i2v"]
    by = {r.provider: adapter_class(seedance, r) for r in seedance.routes}
    assert (
        by["byteplus"] is BytePlusSeedance and by["wavespeed"] is WaveSpeedSeedance
    )  # not their shared base
    assert by["atlas"].__name__ == "SeedanceI2V"


def test_previs_picks_runway_on_the_hero_frame_and_records_why():
    m = load_manifests()["seedance-2.5-r2v"]
    r, route = previs._pick_route(m, ALLOWED_D, None, frame=(320, 180), tier="480P")
    assert r.route_id == "runway" and route["model_version"] == "seedance2_5"
    assert route["reasons"] == ["prefer: runway first for video"]
    r, route = previs._pick_route(m, ALLOWED_D, None, prefer_provider="none")
    assert r.route_id == "atlas" and route["reasons"] == ["prefer: manifest order"]
    r, route = previs._pick_route(m, ALLOWED_D, None, frame=(1000, 200), tier="480P")
    assert r.route_id == "atlas" and route["reasons"][0].startswith("prefer: runway skipped: no legal ratio")
    with pytest.raises(previs.PrevisError) as e:
        previs._pick_route(m, ALLOWED_D, "runway", frame=(1000, 200), tier="480P")
    assert e.value.code == "route_ratio"
    assert prefer.PREFERRED_VIDEO_PROVIDER == "runway"


# ---------------------------------------------------------------------------- quotes through the daemon


@have_packets
def test_a_croppair_on_kling_o3_quotes_runway_first(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    key = env.still_key(_plate16(ROOM_A))
    env.save_graph(
        [_plate(packet_id), _still("e", key), {"id": "c", "kind": "CropPair", "params": {"duration": 5}}],
        [_edge("p", "e"), _edge("e", "c")],
    )
    r = env.quote(node_id="c")
    assert r.status_code == 200, r.text
    q = r.json()
    route = q["run"]["route"]
    assert route["provider"] == "runway" and route["model_version"] == "klingO3_4k"
    assert route["reasons"][0].startswith("prefer: runway")
    assert q["quote"]["price_usd"] == pytest.approx(2.15) and q["quote"]["custody_grade"] == "D"
    assert q["quote"]["price_status"] == "confirmed" and q["quote"]["route_id"] == "runway"
    assert q["run"]["params"]["duration"] == 5 and q["run"]["params"]["resolution"] == "4K"
    assert q["run"]["inputs"]["motion_run"]["route_reasons"] == route["reasons"]


@have_packets
def test_an_nda_project_gets_no_video_route_at_all(env):
    env.project(nda=True)
    env.one_still(ROOM_A)
    r = env.quote({"engine_id": "kling-o3-4k"})
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "custody" and env.node_runs() == 0
    r = env.quote({"engine_id": "kling-o3-4k", "route_id": "runway"})
    assert r.status_code == 403 and env.node_runs() == 0


@have_packets
def test_project_settings_override_the_preference_in_order(env):
    env.project()
    env.one_still(ROOM_A)
    assert env.quote().json()["run"]["route"]["provider"] == "runway"

    r = env.put_settings({"preferred_video_provider": "none"})
    assert r.status_code == 200, r.text
    assert r.json()["preferred_video_provider"] == "none" and r.json()["video_route_pins"] == {}
    q = env.quote().json()
    assert q["run"]["route"]["provider"] == "atlas" and q["run"]["route"]["reasons"] == [
        "prefer: manifest order"
    ]
    assert q["quote"]["price_usd"] == pytest.approx(5 * 0.357)

    r = env.put_settings({"preferred_video_provider": "runway", "video_route_pins": {"kling-o3-4k": "atlas"}})
    assert r.status_code == 200, r.text
    assert r.json()["video_route_pins"] == {"kling-o3-4k": "atlas"}
    assert env.quote().json()["run"]["route"]["provider"] == "atlas"
    v3 = env.quote({"engine_id": "kling-v3-pro"}).json()
    assert v3["run"]["route"]["provider"] == "runway" and v3["quote"]["price_usd"] == pytest.approx(5 * 0.12)

    # the quote body's route_id wins over both the pin and the preference
    assert env.put_settings({"video_route_pins": {"kling-o3-4k": "runway"}}).status_code == 200
    assert env.quote().json()["run"]["route"]["provider"] == "runway"
    assert env.quote({"route_id": "atlas"}).json()["run"]["route"]["provider"] == "atlas"

    stored = Project.load(env.pipeline.project_dir(PROJECT)).doc["settings"]
    assert stored["preferred_video_provider"] == "runway"
    assert stored["video_route_pins"] == {"kling-o3-4k": "runway"}


@have_packets
def test_bad_route_settings_are_422_and_write_nothing(env):
    env.project()
    root = env.pipeline.project_dir(PROJECT) / "project.json"
    before = root.read_bytes()
    r = env.put_settings({"preferred_video_provider": "nowhere"})
    assert r.status_code == 422, r.text
    body = r.json()
    assert "runway" in body["known_providers"] and "atlas" in body["known_providers"]
    assert "nowhere" in json.dumps(body["errors"])
    r = env.put_settings({"video_route_pins": {"kling-o3-4k": "vertex"}})
    assert r.status_code == 422 and r.json()["known_routes"] == {"kling-o3-4k": ["atlas", "runway"]}
    r = env.put_settings({"video_route_pins": {"no-such-engine": "runway"}})
    assert r.status_code == 422 and "no-such-engine" in json.dumps(r.json()["errors"])
    r = env.put_settings({"video_route_pins": {"kling-o3-4k": 3}})
    assert r.status_code == 422
    assert root.read_bytes() == before
    got = env.client.get(f"/api/v0/projects/{PROJECT}/settings", headers=env.auth).json()
    assert got["preferred_video_provider"] == "runway" and got["video_route_pins"] == {}


@have_packets
def test_an_off_ratio_start_crop_goes_to_atlas_with_the_skip_reason(env):
    env.project()
    env.one_still(ROOM_A, textured(1920, 804), move="locked_tripod")
    r = env.quote()
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["run"]["route"]["provider"] == "atlas"
    assert q["run"]["route"]["reasons"] == [
        "prefer: runway skipped: no legal ratio for 1920x804 on klingO3_4k"
    ]
    assert q["quote"]["price_usd"] == pytest.approx(5 * 0.357)
    pinned = env.quote({"route_id": "runway"})  # Runway would centre-crop it: refused before a run exists
    assert pinned.status_code == 422 and pinned.json()["error"] == "route_ratio"
    assert env.node_runs() == 1


@have_packets
def test_veo_plans_six_seconds_for_a_five_second_shot_on_either_route(env):
    env.project()
    env.one_still(ROOM_A)
    runway = env.quote({"engine_id": "veo-3.1"}).json()
    assert runway["run"]["route"]["provider"] == "runway" and runway["run"]["params"]["duration"] == 6
    assert runway["quote"]["price_usd"] == pytest.approx(6 * 0.20)
    vertex = env.quote({"engine_id": "veo-3.1", "route_id": "vertex"}).json()
    assert vertex["run"]["route"]["provider"] == "vertex" and vertex["run"]["params"]["duration"] == 6
    assert vertex["quote"]["price_usd"] == pytest.approx(6 * 0.21)
    assert runway["run"]["inputs"]["motion_run"]["shot_s"] == 5.0  # the take outlasts the shot


@have_packets
def test_seedance_i2v_is_a_single_image_motion_engine_on_runway(env):
    env.project()
    env.one_still(ROOM_A, move="locked_tripod")
    r = env.quote({"engine_id": "seedance-2.5-i2v"})
    assert r.status_code == 200, r.text
    q = r.json()
    inputs = q["run"]["inputs"]
    assert "image_tail" not in inputs and inputs["motion"]["single_image"] is True
    assert q["run"]["route"]["provider"] == "runway" and q["run"]["route"]["model_version"] == "seedance2_5"
    assert q["run"]["params"]["resolution"] == "1080P" and q["run"]["params"]["duration"] == 5
    assert (
        q["quote"]["price_usd"] == pytest.approx(5 * 0.68) and q["quote"]["price_tier"] == "resolution=1080P"
    )
    # a push-in needs its end frame pinned, which a start-frame-only engine cannot take
    env.one_still(ROOM_A, move="push_in")
    r = env.quote({"engine_id": "seedance-2.5-i2v"})
    assert r.status_code == 422 and r.json()["error"] == "single_image_move"


@have_packets
def test_an_end_still_wired_to_gen45_is_422_end_unsupported(env):
    env.project()
    packet_id = env.packet(ROOM_A)
    plate = _plate16(ROOM_A)
    k1 = env.still_key(plate)
    k2 = env.still_key(np.clip(plate.astype(np.float64) * 1.12, 0, 65535).astype(np.uint16))
    motion = {"id": "m", "kind": "Motion", "label": "Motion", "params": {"duration": 5}}
    env.save_graph(
        [_plate(packet_id), _still("e1", k1), _still("e2", k2), motion],
        [_edge("p", "e1"), _edge("p", "e2"), _edge("e1", "m"), _edge("e2", "m", "end")],
    )
    r = env.quote({"engine_id": "gen-4.5"})
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "end_unsupported" and "start frame only" in r.json()["detail"]
    assert env.node_runs() == 0
    assert env.quote({"engine_id": "kling-v3-pro"}).json()["run"]["route"]["provider"] == "runway"


# ---------------------------------------------------------------------------- full runs on the recordings


@have_packets
@needs_ffmpeg
def test_a_kling_v3_pro_take_on_the_runway_recordings_reaches_done(env, runway):
    env.project()
    env.one_still(ROOM_A, duration=3)
    q = env.quote({"engine_id": "kling-v3-pro"}).json()
    assert q["run"]["route"]["provider"] == "runway" and q["quote"]["price_usd"] == pytest.approx(3 * 0.12)
    run_id = q["run"]["id"]

    run = env.approve(run_id)
    assert run["state"] == "DONE", run.get("error")
    clip = run["outputs"][0]
    assert env.cas.find(clip).suffix == ".mp4" and run["provider_job_id"] == TASK_ID
    assert pio.probe(env.cas.find(clip))["nb_frames"] == 72
    status = {c["metric_id"]: c["status"] for c in run["gate_report"]["checks"]}
    assert status["video.first_frame"] == "pass" and status["video.last_frame"] == "pass"

    # exactly one create, its body the kling3.0_pro schema; the key only on the vendor's own host
    ((path, body),) = runway.creates()
    assert path == "/v1/image_to_video" and body["model"] == "kling3.0_pro"
    assert body["ratio"] == "1920:1080" and body["duration"] == 3 and body["audio"] is False
    assert [p["position"] for p in body["promptImage"]] == ["first", "last"]
    assert "Avoid:" in body["promptText"] and "negativePrompt" not in body  # folded, never dropped
    for s in runway.served:
        assert s.authorization == (f"Bearer {KEY}" if s.host == RUNWAY_HOST else None), (s.host, s.path)
    hosts = [s.host for s in runway.served]
    assert (
        hosts.count(RUNWAY_HOST) == 1 + 4 and hosts[-1] == DOCUMENTED_OUTPUT_HOST
    )  # create, 4 looks, the clip

    submitted = next(r for r in env.ledger(run_id) if r["kind"] == "submitted")
    assert submitted["data"]["model"] == "kling3.0_pro" and submitted["provider_job_id"] == TASK_ID
    log = OutboundLog.for_project(env.pipeline.project_dir(PROJECT))
    records, problems = log.records()
    assert not problems
    assert {r.get("host") for r in records if r.get("host")} == {RUNWAY_HOST, DOCUMENTED_OUTPUT_HOST}
    text = log.path.read_text(encoding="utf-8")
    assert "Authorization" not in text and "authorization" not in text and KEY not in text
    assert KEY not in (env.pipeline.project_dir(PROJECT) / "runs.jsonl").read_text(encoding="utf-8")


@have_packets
@needs_ffmpeg
def test_a_previs_draft_on_seedance_r2v_goes_to_runway_as_a_draft(env, runway):
    env.project()
    hero = env.packet(DOLLY_HERO)
    env.packet(DOLLY)
    key = env.still_key(_plate16(DOLLY_HERO))
    node = {"id": "m", "kind": "Motion", "label": "Motion", "params": {"previs": True}}
    env.save_graph([_plate(hero), _still("e", key), node], [_edge("p", "e"), _edge("e", "m")])

    r = env.quote()
    assert r.status_code == 200, r.text
    q = r.json()
    route = q["run"]["route"]
    assert route["engine_id"] == previs.ENGINE_ID and route["provider"] == "runway"
    assert route["reasons"] == ["prefer: runway first for video"]
    assert q["run"]["params"]["resolution"] == "480P" and q["quote"]["price_usd"] == pytest.approx(0.80)
    assert q["run"]["inputs"]["motion_run"]["route_reasons"] == route["reasons"]

    run = env.approve(q["run"]["id"])
    assert run["state"] == "DONE", run.get("error")
    assert run["normalized"]["previs"] is True and run["normalized"]["watermark"]["text"] == "PREVIS"
    assert (
        "reference roles are not a Runway field: first_frame sent as a plain reference"
        in (run["normalized"]["notes"])
    )

    ((path, body),) = runway.creates()
    assert path == "/v1/text_to_video" and body["model"] == "seedance2_5"
    assert body["draft"] is True and body["ratio"] == "854:480" and body["audio"] is False
    assert body["duration"] == q["run"]["params"]["duration"]
    assert body["references"][0]["uri"].startswith("data:image/png;base64,")
    assert body["referenceVideos"] == [{"type": "video", "uri": "runway://ephemeral/clay-test"}]
    upload = next(s for s in runway.served if s.path == "/" and s.host.endswith(".amazonaws.com"))
    assert upload.authorization is None  # the presigned hop carries no Runway key
    assert all(s.authorization is None for s in runway.served if s.host != RUNWAY_HOST)
