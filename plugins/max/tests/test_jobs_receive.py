"""plate_max.jobs: the `receive` job (router plan T96), a take pushed into Max by the canvas.

A stdlib http.server stands in for the daemon: the plugin queue (claim=1), `GET /runs/{id}` and
`GET /assets/{hash}`. The poller claims the job, reads the run's `outputs[0]`, saves that picture to
<app-data>/cache/receive/<hash>.png and calls actions.receive.load_result on the fake pymxs scene, so the
claim, the run read, the download, the VFB / viewport calls and the answer are all exercised over real
loopback HTTP. What this cannot prove: V-Ray's own vfbControl commands inside Max (verify-on-box, ONBOX.md).
"""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_pymxs import tiny_png

from plate_max import jobs

TOKEN = "feedfacefeedfacefeedfacefeedface"
TAKE_KEY = "cd" * 32
OTHER_KEY = "ef" * 32
PNG = tiny_png()


@pytest.fixture(autouse=True)
def _app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "plate-home"))


class Daemon:
    """The queue, the runs and the assets the receive job reads, in memory."""

    def __init__(self):
        self.jobs: list[dict] = []
        self.runs: dict[str, dict] = {}
        self.assets: dict[str, tuple[bytes, str]] = {TAKE_KEY: (PNG, "image/png")}
        self.results: list[tuple[str, dict]] = []
        self.seen: list[tuple[str, str, dict]] = []
        self.seq = 0

    def run(self, run_id="run_1", outputs=None, **extra):
        view = {
            "id": run_id,
            "run_id": run_id,
            "project_id": "clubhouse",
            "packet_id": "pkt_room_a",
            "state": "DONE",
            "outputs": [TAKE_KEY] if outputs is None else outputs,
            **extra,
        }
        self.runs[run_id] = view
        return view

    def receive(self, run_id="run_1", **args):
        """A `receive` job as nodes/plugin_queue.Job.to_dict() sends it."""
        self.seq += 1
        job = {
            "id": f"pj_{self.seq}",
            "seq": self.seq,
            "action": "receive",
            "state": "queued",
            "project_id": "clubhouse",
            "packet_id": "pkt_room_a",
            "run_id": run_id,
            "node_id": "edit-1",
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

    def answers(self):
        return {job_id: body for job_id, body in self.results}


@contextmanager
def serve(daemon: Daemon):
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
            if url.path.startswith("/api/v0/runs/"):
                run_id = urllib.parse.unquote(url.path.rsplit("/", 1)[1])
                if run_id not in daemon.runs:
                    return self._json(404, {"error": "not_found", "run_id": run_id})
                return self._json(200, daemon.runs[run_id])
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
    """(Daemon, config) with config.json pointing at it."""
    stub = Daemon()
    with serve(stub) as url:
        cfg = {"token": TOKEN, "daemon_url": url, "packets_dir": str(tmp_path / "packets")}
        path = Path(os.environ["PLATE_CONFIG"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg), encoding="utf-8", newline="\n")
        yield stub, cfg


def cached_take(tmp_path: Path, key: str = TAKE_KEY) -> Path:
    return tmp_path / "plate-home" / "cache" / "receive" / f"{key}.png"


# ===================================================================== receive


def test_a_queued_receive_job_is_claimed_saved_and_shown_in_the_vfb_history(scene, daemon, tmp_path):
    stub, cfg = daemon
    stub.run("run_1")
    job = stub.receive("run_1", mode="vfb_history")
    [record] = jobs.Poller(cfg).tick()

    claim = next(q for m, p, q in stub.seen if m == "GET" and p == "/api/v0/plugin/jobs")
    assert claim == {"claim": "1", "since": "0", "limit": "1"}
    assert ("GET", "/api/v0/runs/run_1", {}) in stub.seen
    asset = next(q for m, p, q in stub.seen if p == f"/api/v0/assets/{TAKE_KEY}")
    assert asset == {"project_id": "clubhouse"}  # the take is read from the job's project

    saved = cached_take(tmp_path)
    assert saved.read_bytes() == PNG  # GET /assets/{hash} saved under app-data/cache/receive
    calls = [(str(c[0]), *c[1:]) for c in scene.vfb_calls]
    assert calls[0] == ("loadimage", str(saved.resolve()))  # load_result ran on the fake pymxs
    assert calls[1] == ("historysave",)
    assert scene.render_calls == [] and scene.save_calls == []  # display only

    body = stub.answers()[job["id"]]
    assert body["ok"] is True and "VFB history" in body["detail"]
    assert set(body) == {"ok", "detail"}
    assert stub.job(job["id"])["state"] == "done" and record["posted"] == "posted"


def test_the_mode_defaults_to_the_vfb_and_viewport_background_sets_the_background(scene, daemon, tmp_path):
    stub, cfg = daemon
    redraws = []
    scene.viewport = SimpleNamespace(DispBkgImage=False)
    scene.redrawViews = lambda: redraws.append(1)
    stub.run("run_1")
    plain = stub.receive("run_1")  # no mode: the VFB history
    shown = stub.receive("run_1", mode="viewport_background")
    poller = jobs.Poller(cfg)
    poller.tick()
    poller.tick()

    answers = stub.answers()
    assert "VFB history" in answers[plain["id"]]["detail"]
    assert answers[shown["id"]]["ok"] is True and "viewport background" in answers[shown["id"]]["detail"]
    assert scene.backgroundImageFileName == str(cached_take(tmp_path).resolve())
    assert scene.viewport.DispBkgImage is True and redraws == [1]


def test_a_take_is_downloaded_once_per_hash(scene, daemon):
    stub, cfg = daemon
    stub.run("run_1")
    stub.run("run_2")
    stub.receive("run_1")
    stub.receive("run_2")
    poller = jobs.Poller(cfg)
    poller.tick()
    poller.tick()
    assert len(scene.vfb_calls) == 4  # two loads, each with a history save
    assert sum(1 for m, p, _q in stub.seen if p.startswith("/api/v0/assets/")) == 1


def test_the_first_output_is_the_take(scene, daemon, tmp_path):
    stub, cfg = daemon
    stub.assets[OTHER_KEY] = (tiny_png(rgb=(1, 2, 3)), "image/png")
    stub.run("run_1", outputs=[OTHER_KEY, TAKE_KEY])
    stub.receive("run_1")
    jobs.Poller(cfg).tick()
    assert cached_take(tmp_path, OTHER_KEY).is_file() and not cached_take(tmp_path).exists()


def test_a_run_without_a_take_a_missing_run_or_a_missing_asset_fail_with_the_reason(scene, daemon):
    stub, cfg = daemon
    stub.run("run_empty", outputs=[])
    stub.run("run_gone", outputs=[OTHER_KEY])  # the daemon lists a hash it no longer serves
    empty = stub.receive("run_empty")
    unknown = stub.receive("run_unknown")
    gone = stub.receive("run_gone")
    nameless = stub.receive(None)
    poller = jobs.Poller(cfg)
    for _ in range(4):
        poller.tick()

    answers = stub.answers()
    assert answers[empty["id"]]["ok"] is False and "has no take" in answers[empty["id"]]["detail"]
    assert answers[unknown["id"]]["ok"] is False and "HTTP 404" in answers[unknown["id"]]["detail"]
    assert answers[gone["id"]]["ok"] is False and "HTTP 404" in answers[gone["id"]]["detail"]
    assert answers[nameless["id"]]["ok"] is False and "names no run" in answers[nameless["id"]]["detail"]
    assert scene.vfb_calls == []  # nothing reached Max


def test_a_bad_mode_or_output_key_fails_before_anything_is_loaded(scene, daemon):
    stub, cfg = daemon
    stub.run("run_bad", outputs=["../../etc/passwd"])
    stub.run("run_1")
    bad_mode = stub.receive("run_1", mode="screen")
    bad_key = stub.receive("run_bad")
    poller = jobs.Poller(cfg)
    poller.tick()
    poller.tick()
    answers = stub.answers()
    assert (
        answers[bad_mode["id"]]["ok"] is False and "unknown receive mode" in answers[bad_mode["id"]]["detail"]
    )
    assert answers[bad_key["id"]]["ok"] is False and "not a BLAKE3 key" in answers[bad_key["id"]]["detail"]
    assert scene.vfb_calls == [] and not any(p.startswith("/api/v0/assets/") for _m, p, _q in stub.seen)


def test_max_refusing_the_load_is_an_ok_false_answer_not_a_crash(scene, daemon):
    stub, cfg = daemon
    scene.vfbControl = None  # no V-Ray frame buffer in this Max
    stub.run("run_1")
    job = stub.receive("run_1")
    [record] = jobs.Poller(cfg).tick()
    body = stub.answers()[job["id"]]
    assert body["ok"] is False and "vfbControl" in body["detail"]
    assert record["posted"] == "posted" and stub.job(job["id"])["state"] == "failed"


def test_the_dock_lists_receive_among_its_actions():
    assert "receive" in jobs.ACTIONS
    assert jobs.ACTION_RECEIVE == "receive"
