"""plate_max.jobs: the dock's job poller against a stdlib http.server standing in for the daemon's
plugin queue (nodes/routes.py), on the fake scene.

The stub keeps a real queue (queued -> claimed -> done | failed, seq cursor, claim=1 marks claimed,
limit) and serves GET /assets/{hash}, so the claim, the reference download, the action and the
answer are exercised over real loopback HTTP. MaxGaffer is a fake maxgaffer.api in sys.modules, so
actions.light.match and apply really run; only the re-render (a V-Ray render) is replaced. The Qt
half drives the poller from a real QTimer offscreen and skips without the PySide6 wheel.
What this cannot prove: MaxGaffer's own match inside Max, and a re-render on the box (verify-on-box).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from plate_max import jobs, transport
from plate_max.actions import light, scout, send_packet
from plate_max.packet_id import new_packet_id

TOKEN = "feedfacefeedfacefeedfacefeedface"
REF_KEY = "ab" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "plate-home"))


class QueueDaemon:
    """The daemon's plugin queue, its assets route and a projects list, in memory."""

    def __init__(self):
        self.jobs: list[dict] = []
        self.assets: dict[str, tuple[bytes, str]] = {REF_KEY: (PNG, "image/png")}
        self.results: list[tuple[str, dict]] = []
        self.seen: list[tuple[str, str, dict]] = []  # (method, path, query)
        self.result_status: int | None = None  # force an answer to the result POST
        self.result_script: list[int] = []  # statuses for the next result POSTs, in turn
        self.seq = 0

    def add(self, action, **args):
        self.seq += 1
        job = {
            "id": f"job_{self.seq}",
            "seq": self.seq,
            "action": action,
            "state": "queued",
            "project_id": args.pop("project_id", "clubhouse"),
            "packet_id": args.pop("packet_id", new_packet_id()),
            "run_id": f"run_{self.seq}",
            "node_id": "n1",
            "created_utc": "2026-10-18T09:00:00Z",
            "claimed_utc": None,
            "finished_utc": None,
            "result": None,
            **args,
        }
        self.jobs.append(job)
        return job

    def job(self, job_id):
        return next(j for j in self.jobs if j["id"] == job_id)


@contextmanager
def serve(daemon: QueueDaemon):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, body):
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorised(self):
            if self.headers.get("Authorization") == f"Bearer {TOKEN}":
                return True
            self._json(401, {"error": "unauthorized"})
            return False

        def do_GET(self):  # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(url.query))
            daemon.seen.append(("GET", url.path, query))
            if not self._authorised():
                return
            if url.path == "/api/v0/plugin/jobs":
                since, limit = int(query.get("since", "0")), int(query.get("limit", "50"))
                found = [j for j in daemon.jobs if j["state"] == "queued" and j["seq"] > since][:limit]
                if query.get("claim") == "1":
                    for j in found:
                        j["state"] = "claimed"
                return self._json(200, {"jobs": found, "next": max([j["seq"] for j in found], default=since)})
            if url.path.startswith("/api/v0/assets/"):
                key = url.path.rsplit("/", 1)[1]
                if key not in daemon.assets:
                    return self._json(404, {"error": "not_found", "hash": key})
                data, ctype = daemon.assets[key]
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return None
            return self._json(404, {"error": "not_found"})

        def do_POST(self):  # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            daemon.seen.append(("POST", url.path, {}))
            if not self._authorised():
                return
            if url.path.startswith("/api/v0/plugin/jobs/") and url.path.endswith("/result"):
                job_id = urllib.parse.unquote(url.path.split("/")[-2])
                body = json.loads(raw.decode("utf-8"))
                status = daemon.result_script.pop(0) if daemon.result_script else daemon.result_status
                if status:
                    return self._json(status, {"error": "forced", "detail": "scripted"})
                job = daemon.job(job_id)
                if job["state"] not in ("queued", "claimed"):
                    return self._json(409, {"error": "job_not_active", "state": job["state"]})
                daemon.results.append((job_id, body))
                job["state"] = "done" if body.get("ok") else "failed"
                job["result"] = body
                return self._json(200, job)
            return self._json(404, {"error": "not_found"})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def daemon(tmp_path):
    """(QueueDaemon, config) with config.json pointing at it, and a packets folder."""
    stub = QueueDaemon()
    with serve(stub) as url:
        packets = tmp_path / "packets"
        packets.mkdir()
        cfg = {"token": TOKEN, "daemon_url": url, "packets_dir": str(packets)}
        path = Path(os.environ["PLATE_CONFIG"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg), encoding="utf-8", newline="\n")
        yield stub, cfg


class FakeGaffer:
    def __init__(self):
        self.match_calls: list[dict] = []
        self.apply_calls: list[str] = []
        self.answer = {
            "score": 96.5,
            "iterations": 4,
            "stop_reason": "target",
            "de00": 1.8,
            "nan": float("nan"),
        }
        self.raise_on_match: Exception | None = None

    def match_camera(self, camera_name, reference_path="", log=None, should_cancel=None, **kw):
        self.match_calls.append({"camera": camera_name, "reference_path": reference_path, **kw})
        if self.raise_on_match:
            raise self.raise_on_match
        return dict(self.answer)

    def apply_camera_state(self, camera_name):
        self.apply_calls.append(camera_name)
        return ["dome restored"]


@pytest.fixture
def gaffer(monkeypatch):
    fake = FakeGaffer()
    pkg = types.ModuleType("maxgaffer")
    api = types.ModuleType("maxgaffer.api")
    api.match_camera = fake.match_camera
    api.apply_camera_state = fake.apply_camera_state
    pkg.api = api
    monkeypatch.setitem(sys.modules, "maxgaffer", pkg)
    monkeypatch.setitem(sys.modules, "maxgaffer.api", api)
    return fake


@pytest.fixture
def rerender(monkeypatch):
    """actions.light.rerender_packet replaced: no V-Ray render, a new packet id per call."""
    calls = []

    def fake(camera, out_root, daemon_base=None, options=None, supersedes=None):
        pid = new_packet_id()
        calls.append(
            {
                "camera": camera,
                "out_root": out_root,
                "base": daemon_base,
                "options": options,
                "supersedes": supersedes,
            }
        )
        return send_packet.SendResult(
            camera=camera, ok=True, packet_id=pid, status_code=202, ingest="ingested"
        )

    monkeypatch.setattr(light, "rerender_packet", fake)
    return calls


class RecordingController:
    """What the poller asks of the dock's controller: io, job_options, note_packet."""

    def __init__(self):
        self.io_calls = 0
        self.noted: list[tuple[str, str]] = []
        self.option_calls: list[tuple] = []

    def io(self, fn):
        self.io_calls += 1
        return fn()

    def job_options(self, cameras, project_id, progress=None):
        self.option_calls.append((list(cameras), project_id))
        return {
            "project_id": project_id,
            "custody": {"nda": True, "allowed_grades": ["C0", "C1"]},
            "progress": progress,
        }

    def note_packet(self, camera, packet_id):
        self.noted.append((camera, packet_id))


class FakeTimer:
    """A QTimer stand-in: timeout.connect, start(ms), stop(), isActive(); fire() runs the slots."""

    def __init__(self):
        self.slots = []
        self.interval = None
        self.active = False
        self.timeout = types.SimpleNamespace(connect=self.slots.append)

    def start(self, ms):
        self.interval, self.active = ms, True

    def stop(self):
        self.active = False

    def isActive(self):  # noqa: N802 Qt naming
        return self.active

    def fire(self):
        return [slot() for slot in self.slots]


def light_job(stub, **extra):
    packet_id = extra.pop("packet_id", new_packet_id())
    return stub.add(
        "light.match",
        camera=extra.pop("camera", "CAM_front"),
        reference_hash=extra.pop("reference_hash", REF_KEY),
        locks=extra.pop("locks", ["sun_azimuth"]),
        deep=extra.pop("deep", True),
        supersedes=packet_id,
        packet_id=packet_id,
        **extra,
    )


def posted(stub):
    return {job_id: body for job_id, body in stub.results}


# ===================================================================== light.match
def test_a_queued_light_match_is_claimed_matched_with_the_saved_reference_and_answered_with_the_packet(
    clubhouse, daemon, gaffer, rerender, tmp_path
):
    stub, _cfg = daemon
    job = light_job(stub, project_id="tula-wot")
    controller = RecordingController()
    poller = jobs.Poller(None, controller)
    [record] = poller.tick()

    claim = next(q for m, p, q in stub.seen if m == "GET" and p == "/api/v0/plugin/jobs")
    assert claim == {"claim": "1", "since": "0", "limit": "1"}
    reference = tmp_path / "plate-home" / "cache" / "references" / f"{REF_KEY}.png"
    assert reference.read_bytes() == PNG  # GET /assets/{hash} saved to app-data/cache
    [call] = gaffer.match_calls
    assert call["camera"] == "CAM_front" and call["reference_path"] == str(reference)
    assert call["locks"] == {"sun_azimuth"} and call["deep"] is True
    assert gaffer.apply_calls == ["CAM_front"]
    [sent] = rerender
    assert sent["supersedes"] == job["packet_id"]  # the re-render says which packet it replaces
    assert sent["options"]["project_id"] == "tula-wot"  # it lands in the job's project
    assert sent["options"]["custody"] == {"nda": True, "allowed_grades": ["C0", "C1"]}
    assert sent["options"]["config"]["token"] == TOKEN
    assert "reference_path" not in sent["options"] and "locks" not in sent["options"]

    body = posted(stub)[job["id"]]
    assert body["ok"] is True and body["packet_id"] == record["body"]["packet_id"]
    assert body["metrics"] == {"de00": 1.8, "score": 96.5, "iterations": 4}  # NaN never reaches the daemon
    assert "supersedes" in body["detail"]
    assert stub.job(job["id"])["state"] == "done" and record["posted"] == "posted"
    assert controller.noted == [("CAM_front", body["packet_id"])]
    assert controller.option_calls == [(["CAM_front"], "tula-wot")]
    assert controller.io_calls >= 3  # claim, asset, result all went through the io runner


def test_the_reference_is_downloaded_once_per_hash(clubhouse, daemon, gaffer, rerender):
    stub, cfg = daemon
    light_job(stub)
    light_job(stub)
    poller = jobs.Poller(cfg)
    poller.tick()
    poller.tick()
    assert len(gaffer.match_calls) == 2
    assert sum(1 for m, p, _q in stub.seen if p.startswith("/api/v0/assets/")) == 1


def test_the_reference_extension_follows_the_content_type_or_the_bytes(clubhouse, daemon, gaffer, rerender):
    stub, cfg = daemon
    jpeg_key = "cd" * 32
    stub.assets[jpeg_key] = (b"\xff\xd8\xff\xe0 jpeg", "application/octet-stream")
    light_job(stub, reference_hash=jpeg_key)
    jobs.Poller(cfg).tick()
    assert gaffer.match_calls[0]["reference_path"].endswith(f"{jpeg_key}.jpg")
    assert jobs.extension_for("image/tiff; charset=binary", b"") == ".tif"
    assert jobs.extension_for("", b"v/1\x01....") == ".exr"


def test_a_failing_match_posts_ok_false_with_the_text(clubhouse, daemon, gaffer, rerender):
    stub, cfg = daemon
    gaffer.raise_on_match = RuntimeError("V-Ray license lost")
    job = light_job(stub)
    jobs.Poller(cfg).tick()
    body = posted(stub)[job["id"]]
    assert body["ok"] is False and "V-Ray license lost" in body["detail"]
    assert "MAXGAFFER_FAILED" in body["detail"]
    assert rerender == [] and stub.job(job["id"])["state"] == "failed"


def test_an_action_that_raises_posts_ok_false_and_the_poller_keeps_going(clubhouse, daemon, monkeypatch):
    stub, cfg = daemon
    monkeypatch.setattr(light, "available", lambda: (True, ""))
    monkeypatch.setattr(light, "run", lambda *a, **k: 1 / 0)
    first = light_job(stub)
    second = stub.add("light.vantage_preview", camera="CAM_front")
    poller = jobs.Poller(cfg)
    poller.tick()
    assert posted(stub)[first["id"]] == {
        "ok": False,
        "detail": "light.match: ZeroDivisionError: division by zero",
    }
    monkeypatch.setattr(
        light, "vantage_preview", lambda camera: {"ok": True, "camera": camera, "how": "live link"}
    )
    poller.tick()
    assert posted(stub)[second["id"]]["ok"] is True


def test_without_maxgaffer_a_light_job_fails_with_maxgaffer_missing(clubhouse, daemon, monkeypatch):
    stub, cfg = daemon
    monkeypatch.setitem(sys.modules, "maxgaffer", None)
    monkeypatch.setitem(sys.modules, "maxgaffer.api", None)
    job = light_job(stub)
    jobs.Poller(cfg).tick()
    body = posted(stub)[job["id"]]
    assert body["ok"] is False and "MAXGAFFER_MISSING" in body["detail"]
    assert not any(p.startswith("/api/v0/assets/") for _m, p, _q in stub.seen)  # nothing downloaded


def test_a_camera_that_is_not_in_the_scene_fails_before_the_download(clubhouse, daemon, gaffer, rerender):
    stub, cfg = daemon
    job = light_job(stub, camera="CAM_nope")
    jobs.Poller(cfg).tick()
    body = posted(stub)[job["id"]]
    assert body["ok"] is False and "CAM_nope" in body["detail"] and "not in the open scene" in body["detail"]
    assert gaffer.match_calls == []


def test_a_bad_or_missing_reference_fails_the_job(clubhouse, daemon, gaffer, rerender):
    stub, cfg = daemon
    bad = light_job(stub, reference_hash="../../etc/passwd")
    gone = light_job(stub, reference_hash="ef" * 32)
    poller = jobs.Poller(cfg)
    poller.tick()
    poller.tick()
    answers = posted(stub)
    assert answers[bad["id"]]["ok"] is False and "not a BLAKE3 key" in answers[bad["id"]]["detail"]
    assert answers[gone["id"]]["ok"] is False and "HTTP 404" in answers[gone["id"]]["detail"]
    assert gaffer.match_calls == []


def test_a_job_whose_packet_came_from_another_scene_fails_instead_of_relighting_it(
    clubhouse, daemon, gaffer, rerender
):
    stub, cfg = daemon
    clubhouse.maxFilePath, clubhouse.maxFileName = "D:/jobs", "terrace.max"
    job = light_job(stub)
    folder = Path(cfg["packets_dir"]) / job["packet_id"]
    folder.mkdir()
    manifest = {"packet_id": job["packet_id"], "scene_hash": {"max_file": {"name": "clubhouse.max"}}}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    jobs.Poller(cfg).tick()
    body = posted(stub)[job["id"]]
    assert body["ok"] is False and "terrace.max" in body["detail"] and "clubhouse.max" in body["detail"]
    assert gaffer.match_calls == []
    clubhouse.maxFileName = "Clubhouse.MAX"  # the same file, as Windows spells it
    again = light_job(stub, packet_id=job["packet_id"])
    jobs.Poller(cfg).tick()
    assert posted(stub)[again["id"]]["ok"] is True


# ===================================================================== the other actions
def test_vantage_preview_answers_the_live_link(scene, daemon, monkeypatch):
    stub, cfg = daemon
    seen = []
    monkeypatch.setattr(
        light,
        "vantage_preview",
        lambda camera: (
            seen.append(camera)
            or {"ok": True, "camera": camera, "how": "vrayVantageLiveLink", "warnings": []}
        ),
    )
    job = stub.add("light.vantage_preview", camera="CAM_x")
    jobs.Poller(cfg).tick()
    assert seen == ["CAM_x"]
    assert posted(stub)[job["id"]] == {"ok": True, "detail": "vrayVantageLiveLink"}
    monkeypatch.setattr(
        light,
        "vantage_preview",
        lambda camera: {"ok": False, "code": "LIVE_LINK_FAILED", "error": "no Vantage"},
    )
    failing = stub.add("light.vantage_preview", camera="CAM_x")
    jobs.Poller(cfg).tick()
    assert posted(stub)[failing["id"]] == {"ok": False, "detail": "LIVE_LINK_FAILED: no Vantage"}


def _scout_results(count, packets=False):
    out = []
    for n in range(count):
        r = scout.ScoutResult(
            n, f"corner_{n}", f"__PLATE_scout_{n}", ok=True, preview=f"scouts/scout_{n}.png"
        )
        r.pose = {"pos": [0.0, 0.0, 1600.0], "look_at": [1.0, 0.0, 1600.0], "fov_mm": 24.0, "up": [0, 0, 1]}
        if packets:
            r.packet_id = new_packet_id()
        out.append(r)
    return out


def test_scout_runs_the_poses_and_sends_packets_only_when_keep(scene, daemon, monkeypatch):
    stub, cfg = daemon
    calls = []

    def fake_run(out_root, daemon_base=None, options=None):
        calls.append({"out_root": out_root, "base": daemon_base, **options})
        return _scout_results(len(options["views"]), packets=bool(options.get("packets")))

    monkeypatch.setattr(scout, "run", fake_run)
    look = stub.add("scout", count=3, keep=False, scene_hash="")
    keep = stub.add("scout", count=40, keep=True, scene_hash="")
    poller = jobs.Poller(cfg)
    poller.tick()
    poller.tick()
    first, second = calls
    assert first["views"] == [0, 1, 2] and first["keep"] is False and first["packets"] is False
    assert second["views"] == list(range(24)) and second["keep"] is True and second["packets"] is True
    assert second["project_id"] == "clubhouse" and str(second["out_root"]) == cfg["packets_dir"]
    look_body, keep_body = posted(stub)[look["id"]], posted(stub)[keep["id"]]
    assert look_body["ok"] is True and len(look_body["poses"]) == 3 and "packets" not in look_body
    assert (
        look_body["poses"][0]["camera"] == "__PLATE_scout_0"
        and look_body["poses"][0]["pose"]["fov_mm"] == 24.0
    )
    assert keep_body["ok"] is True and len(keep_body["packets"]) == 24


def test_a_scout_that_places_nothing_fails_with_the_reason(scene, daemon, monkeypatch):
    stub, cfg = daemon
    monkeypatch.setattr(
        scout,
        "run",
        lambda *a, **k: [
            scout.ScoutResult(-1, "scene", "", code="NO_GEOMETRY", errors=["no visible geometry"])
        ],
    )
    job = stub.add("scout", count=2, keep=False)
    jobs.Poller(cfg).tick()
    body = posted(stub)[job["id"]]
    assert body["ok"] is False and "no visible geometry" in body["detail"]


def test_an_unknown_action_is_answered_ok_false(scene, daemon):
    stub, cfg = daemon
    job = stub.add("teleport", camera="CAM_x")
    jobs.Poller(cfg).tick()
    body = posted(stub)[job["id"]]
    assert body["ok"] is False and "does not handle 'teleport'" in body["detail"]


# ===================================================================== the loop
def test_one_job_at_a_time_and_the_cursor_moves_on(scene, daemon, monkeypatch):
    stub, cfg = daemon
    monkeypatch.setattr(light, "vantage_preview", lambda camera: {"ok": True, "camera": camera, "how": "ok"})
    a = stub.add("light.vantage_preview", camera="A")
    b = stub.add("light.vantage_preview", camera="B")
    poller = jobs.Poller(cfg)
    [first] = poller.tick()
    assert first["id"] == a["id"] and stub.job(b["id"])["state"] == "queued"  # the other stays queued
    assert poller.cursor == a["seq"]
    [second] = poller.tick()
    assert second["id"] == b["id"] and poller.cursor == b["seq"]
    assert poller.tick() == []
    sinces = [q["since"] for m, p, q in stub.seen if p == "/api/v0/plugin/jobs"]
    assert sinces == ["0", str(a["seq"]), str(b["seq"])]


def test_busy_skips_the_tick_and_a_running_tick_is_not_entered_again(scene, daemon, monkeypatch):
    stub, cfg = daemon
    stub.add("light.vantage_preview", camera="A")
    busy = {"now": True}
    reentered = []
    poller = None

    def preview(camera):
        reentered.append(poller.tick())  # a timer firing inside the job's event processing
        return {"ok": True, "camera": camera, "how": "ok"}

    monkeypatch.setattr(light, "vantage_preview", preview)
    poller = jobs.Poller(cfg, busy=lambda: busy["now"])
    assert poller.tick() == [] and stub.seen == []  # busy: not even a poll
    busy["now"] = False
    [record] = poller.tick()
    assert record["body"]["ok"] is True and reentered == [[]]
    assert poller.active is False


def test_start_and_stop_drive_the_timer_and_on_start_on_finish_wrap_each_job(scene, daemon, monkeypatch):
    stub, cfg = daemon
    monkeypatch.setattr(light, "vantage_preview", lambda camera: {"ok": True, "camera": camera, "how": "ok"})
    events = []
    poller = jobs.Poller(
        cfg,
        interval_s=5,
        on_start=lambda job: events.append(("start", job["action"])),
        on_finish=lambda job, body: events.append(("finish", body["ok"])),
    )
    timer = FakeTimer()
    assert poller.start(timer) is True and poller.running and timer.interval == 5000
    assert poller.start(timer) is True and len(timer.slots) == 1  # restarting never connects twice
    stub.add("light.vantage_preview", camera="A")
    timer.fire()
    assert events == [("start", "light.vantage_preview"), ("finish", True)]
    poller.stop()
    assert not poller.running and poller.start(None) is False


def test_an_unreachable_daemon_backs_off_and_never_raises(scene, tmp_path, monkeypatch):
    logged, polls = [], []
    real_get = transport.get_json
    monkeypatch.setattr(transport, "get_json", lambda *a, **k: polls.append(1) or real_get(*a, **k))
    cfg = {"token": TOKEN, "daemon_url": "http://127.0.0.1:9", "packets_dir": str(tmp_path)}
    poller = jobs.Poller(cfg, log=lambda message, err=False: logged.append((message, err)))
    ticks = [poller.tick() for _ in range(30)]
    assert ticks == [[]] * 30
    assert poller.last_error.startswith("job poll: ")
    assert len(logged) == 1 and logged[0][1] is True  # said once, not every five seconds
    assert len(polls) <= 6  # asked less and less often: 1, 2, 4, 8, then every 13th tick


def test_a_bad_token_is_a_logged_error_not_a_crash(scene, daemon):
    _stub, cfg = daemon
    logged = []
    poller = jobs.Poller({**cfg, "token": "wrong"}, log=lambda m, err=False: logged.append(m))
    assert poller.tick() == []
    assert "rejected the token" in logged[0]


def test_an_answer_the_daemon_missed_is_posted_first_on_the_next_tick(scene, daemon, monkeypatch):
    stub, cfg = daemon
    monkeypatch.setattr(light, "vantage_preview", lambda camera: {"ok": True, "camera": camera, "how": "ok"})
    job = stub.add("light.vantage_preview", camera="A")
    real_post = transport.post_json
    away = {"left": 1}

    def flaky(path, body, config, timeout_s=5.0):
        if away["left"]:
            away["left"] -= 1
            raise transport.DaemonUnreachable("connection reset")
        return real_post(path, body, config, timeout_s)

    monkeypatch.setattr(transport, "post_json", flaky)
    poller = jobs.Poller(cfg)
    [record] = poller.tick()
    assert record["posted"] == "queued" and posted(stub) == {}
    poller.tick()
    assert posted(stub)[job["id"]]["ok"] is True and poller._unsent == []


def test_a_finished_job_drops_the_answer_and_a_refused_extra_is_retried_slim(scene, daemon, monkeypatch):
    stub, cfg = daemon
    monkeypatch.setattr(light, "vantage_preview", lambda camera: {"ok": True, "camera": camera, "how": "ok"})
    monkeypatch.setattr(scout, "run", lambda *a, **k: _scout_results(2))
    stub.add("light.vantage_preview", camera="A")
    stub.result_script = [409]  # the run was cancelled meanwhile
    poller = jobs.Poller(cfg)
    [record] = poller.tick()
    assert record["posted"] == "dropped" and posted(stub) == {}
    job = stub.add("scout", count=2, keep=False)
    stub.result_script = [422]  # the poses are refused; the slim answer {ok} is taken
    [again] = poller.tick()
    assert again["posted"] == "posted" and posted(stub)[job["id"]] == {"ok": True}


def test_clean_body_clips_the_detail_and_drops_non_finite_numbers():
    body = jobs.clean_body(
        {"ok": 1, "detail": "x" * 5000, "metrics": {"de00": float("inf")}, "packet_id": None}
    )
    assert body["ok"] is True and len(body["detail"]) == jobs.DETAIL_MAX and body["detail"].endswith("…")
    assert body["metrics"] == {"de00": None} and "packet_id" not in body
    json.dumps(body, allow_nan=False)


# ===================================================================== on a real QTimer (offscreen)
def test_the_poller_runs_on_a_qtimer_offscreen(scene, daemon, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    stub, cfg = daemon
    monkeypatch.setattr(light, "vantage_preview", lambda camera: {"ok": True, "camera": camera, "how": "ok"})
    job = stub.add("light.vantage_preview", camera="A")
    timer = QtCore.QTimer()
    poller = jobs.Poller(cfg, interval_s=0.02)
    poller.start(timer)
    deadline = time.monotonic() + 10
    while job["id"] not in posted(stub) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    poller.stop()
    assert posted(stub)[job["id"]]["ok"] is True and not timer.isActive()


def test_on_finish_follows_on_start_even_when_posting_breaks(scene, daemon, monkeypatch):
    stub, cfg = daemon
    monkeypatch.setattr(light, "vantage_preview", lambda camera: {"ok": True, "camera": camera, "how": "ok"})
    stub.add("light.vantage_preview", camera="A")
    events = []
    poller = jobs.Poller(
        cfg,
        log=lambda m, err=False: events.append(("log", err)),
        on_start=lambda job: events.append(("start",)),
        on_finish=lambda job, body: events.append(("finish", body["ok"])),
    )
    monkeypatch.setattr(poller, "_post_result", lambda *a: 1 / 0)
    assert poller.tick() == []  # the tick swallows it and logs
    assert events[:2] == [("start",), ("finish", True)] and events[-1] == ("log", True)
    assert poller.handled[-1]["posted"] == "not posted"


# ===================================================================== the transport calls it uses
def test_api_paths_queries_and_the_bearer_calls(daemon):
    stub, cfg = daemon
    assert transport.api_path("plugin/jobs") == "/api/v0/plugin/jobs"
    assert transport.api_path("/api/v0/projects") == "/api/v0/projects"
    assert transport._with_query("/x", {"claim": True, "since": 0, "skip": None}) == "/x?claim=1&since=0"
    answer = transport.get_json("plugin/jobs", cfg, params={"claim": False})
    assert answer == {"jobs": [], "next": 0}
    data, ctype = transport.get_bytes(f"/api/v0/assets/{REF_KEY}", cfg)
    assert data == PNG and ctype == "image/png"
    with pytest.raises(transport.TransportError, match="over 10 bytes"):
        transport.get_bytes(f"/api/v0/assets/{REF_KEY}", cfg, max_bytes=10)
    with pytest.raises(transport.UnexpectedStatus) as missing:
        transport.get_bytes("/api/v0/assets/" + "00" * 32, cfg)
    assert missing.value.status_code == 404 and missing.value.body["error"] == "not_found"
    with pytest.raises(transport.Unauthorized):
        transport.get_json("/api/v0/plugin/jobs", {**cfg, "token": "nope"})
    with pytest.raises(transport.Unauthorized):
        transport.get_bytes("/api/v0/assets/x", {**cfg, "token": ""})  # no token never touches the network
    with pytest.raises(ValueError):
        transport.post_json("/api/v0/plugin/jobs/j/result", {"metrics": {"de00": float("nan")}}, cfg)
    job = stub.add("scout", count=1)
    done = transport.post_json(f"/api/v0/plugin/jobs/{job['id']}/result", {"ok": True}, cfg)
    assert done["state"] == "done"
    with pytest.raises(transport.UnexpectedStatus) as again:
        transport.post_json(f"/api/v0/plugin/jobs/{job['id']}/result", {"ok": True}, cfg)
    assert again.value.status_code == 409
    with pytest.raises(transport.DaemonUnreachable):
        transport.get_json("/api/v0/projects", {**cfg, "daemon_url": "http://127.0.0.1:9"}, timeout_s=2)
