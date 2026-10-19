"""Wave 12 stress fixes in the Max plugin's transport, job poller, dock and token (router/PLAN.md T142).

Each test encodes an attacker's repro from the max-plugin stress run against stdlib servers on
127.0.0.1 (no other network): a daemon that trickles one byte a second, an event stream that only ever
sends keep-alives, a /hash that answers 503 once while the daemon restarts, an HTML page answering 200
where the daemon should, a token file with a second "# rotated" line, a claim whose answer is lost, and
a scene guard that only compared .max file names. The dock half runs offscreen and skips without the
PySide6 wheel.
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_jobs_poller import FakeTimer, QueueDaemon, _scout_results, posted, utc_now  # noqa: E402
from test_jobs_poller import serve as serve_queue  # noqa: E402
from test_send_packet import Daemon, send, serve_daemon, vray_scene  # noqa: E402

from plate_max import PLUGIN_VERSION, config, jobs, transport
from plate_max.actions import light, scout, send_packet
from plate_max.ui import controller as ctl

TOKEN = "0123456789abcdef0123456789abcdef"
SECRET = "fedcba9876543210fedcba9876543210"
ROTATED = SECRET + "\n# rotated 2026-10-01"
HEALTH = {"ok": True, "daemon_version": PLUGIN_VERSION, "packet_versions": ["0.1"]}
JOBS = "/api/v0/plugin/jobs"


@pytest.fixture(autouse=True)
def _app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "plate-home"))


# --------------------------------------------------------------------------- stub servers


def answer(handler, status, body, content_type="application/json"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


@contextmanager
def http_stub(respond):
    """respond(handler, method, path, body_bytes) writes each answer; yields (url, seen)."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def _go(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            seen.append((method, self.path, body))
            try:
                respond(self, method, self.path, body)
            except OSError:  # the client gave up and hung up
                pass

        def do_GET(self):  # noqa: N802
            self._go("GET")

        def do_POST(self):  # noqa: N802
            self._go("POST")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def trickle_server(head: bytes, drip: bytes):
    """A raw TCP peer: reads the request, sends head, then drip one byte per second, forever."""
    stop = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                self.request.recv(65536)
                if head:
                    self.request.sendall(head)
                n = 0
                while not stop.is_set():
                    self.request.sendall(drip[n % len(drip) : n % len(drip) + 1])
                    n += 1
                    stop.wait(1.0)
            except OSError:
                pass

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- total deadlines


def test_a_one_byte_per_second_status_line_cannot_hold_handshake_past_its_timeout():
    with trickle_server(b"", b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n") as url:
        start = time.monotonic()
        with pytest.raises(transport.DaemonTimeout):
            transport.handshake(url, timeout_s=3)
        assert time.monotonic() - start < 4.0


def test_a_one_byte_per_second_body_cannot_hold_a_call_past_its_timeout():
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n"
    with trickle_server(head, b'{"ok": true}'.ljust(64)) as url:
        start = time.monotonic()
        with pytest.raises(transport.DaemonTimeout):
            transport.handshake(url, timeout_s=1.5)
        with pytest.raises(transport.DaemonTimeout):
            transport.get_bytes("/api/v0/assets/x", {"token": TOKEN, "daemon_url": url}, timeout_s=1.5)
        assert time.monotonic() - start < 4.5


@contextmanager
def keepalive_stream():
    """GET /api/v0/packets/x/events answers 200 text/event-stream and then only ': keep-alive' every 0.5 s."""
    stop = threading.Event()

    def respond(handler, method, path, body):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        handler.wfile.write(b"retry: 1000\n\n")
        while not stop.is_set():
            handler.wfile.write(b": keep-alive\n\n")
            handler.wfile.flush()
            stop.wait(0.5)

    with http_stub(respond) as (url, seen):
        try:
            yield url, seen
        finally:
            stop.set()


def test_a_never_ending_keepalive_stream_cannot_hold_follow_ingest_past_its_window():
    with keepalive_stream() as (url, _seen):
        cfg = {"token": TOKEN, "daemon_url": url}
        start = time.monotonic()
        out = transport.follow_ingest("/api/v0/packets/x/events", cfg, window_s=5)
        assert time.monotonic() - start < 6.0
        assert out["ingest"] == "ingesting" and out["problem"]  # unknown, said so; never a raise
        start = time.monotonic()
        out = transport.follow_ingest("/api/v0/packets/x/events", cfg, window_s=5, timeout_s=30)
        assert time.monotonic() - start < 6.0  # the window is checked as the stream comes in
        assert out == {
            "ingest": "ingesting",
            "problem": "the event log was still open when the follow window ended",
        }


def test_events_that_arrive_on_an_open_stream_are_read_as_they_come():
    def respond(handler, method, path, body):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        handler.wfile.write(b'id: 1\r\ndata: {"seq": 1, "type": "ingest.file"}\r\n\r\n: keep')
        handler.wfile.flush()
        time.sleep(0.3)
        handler.wfile.write(b'-alive\n\nid: 2\ndata: {"seq": 2, "type": "ingest.done"}\n\n')
        handler.wfile.flush()
        time.sleep(30)  # the stream stays open: the done event alone must end the follow

    with http_stub(respond) as (url, _seen):
        start = time.monotonic()
        out = transport.follow_ingest(
            "/api/v0/packets/x/events", {"token": TOKEN, "daemon_url": url}, timeout_s=10
        )
        assert out == {"ingest": "ingested", "problem": None} and time.monotonic() - start < 3.0


def test_an_answer_over_the_cap_is_response_too_large(monkeypatch):
    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", 1024)
    with http_stub(lambda h, m, p, b: answer(h, 200, {"jobs": [], "pad": "x" * 4096})) as (url, _seen):
        with pytest.raises(transport.ResponseTooLarge) as err:
            transport.get_json(JOBS, {"token": TOKEN, "daemon_url": url})
    assert isinstance(err.value, transport.UnexpectedStatus) and err.value.status_code == 200
    assert "over 1024 bytes" in str(err.value)


# --------------------------------------------------------------------------- /hash retries


def test_a_503_then_200_on_hash_sends_with_one_retry_and_one_render(install_scene, tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(transport, "_sleep", waits.append)
    packets = tmp_path / "packets"
    packets.mkdir()
    daemon = Daemon(packets)
    real = daemon.hasher.answer
    restarting = {"left": 1}

    def flaky(headers, body):
        if restarting["left"]:
            restarting["left"] -= 1
            daemon.hasher.requests.append(body)
            return 503, {"error": "daemon restarting"}
        return real(headers, body)

    daemon.hasher.answer = flaky
    scene, _vray = vray_scene(install_scene)
    with serve_daemon(daemon) as cfg:
        [result] = send(["CAM_front"], packets, cfg)
    assert result.ok, (result.stage, result.errors)
    assert len(scene.render_calls) == 1  # the render was kept: the hash was asked again, not the scene
    assert waits == [1.0] and restarting["left"] == 0
    assert len([p for p in packets.iterdir() if p.is_dir()]) == 1


def hash_stub(statuses):
    """/hash answering each of statuses in turn (the last repeats); 200 hashes every payload."""

    def respond(handler, method, path, body):
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        if status != 200:
            return answer(handler, status, {"error": "scripted"})
        names = json.loads(body.decode("utf-8")).get("payloads", {})
        answer(handler, 200, {"hashes": {n: {"algo": "blake3", "hex": "ab" * 32, "bytes": 1} for n in names}})

    return http_stub(respond)


def test_hash_asks_again_after_1_2_and_4_s_then_gives_up(monkeypatch):
    waits = []
    monkeypatch.setattr(transport, "_sleep", waits.append)
    with hash_stub([503]) as (url, seen):
        with pytest.raises(transport.UnexpectedStatus) as err:
            transport.hash_payloads({"a": b"x"}, {"token": TOKEN, "daemon_url": url})
    assert err.value.status_code == 503 and len(seen) == 4 and waits == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    ("status", "error"), [(403, transport.HashRefused), (422, transport.UnexpectedStatus)]
)
def test_a_4xx_on_hash_is_never_asked_again(monkeypatch, status, error):
    waits = []
    monkeypatch.setattr(transport, "_sleep", waits.append)
    with hash_stub([status, 200]) as (url, seen):
        with pytest.raises(error):
            transport.hash_payloads({"a": b"x"}, {"token": TOKEN, "daemon_url": url})
    assert len(seen) == 1 and waits == []


def test_an_unreachable_daemon_is_asked_again_before_hash_gives_up(monkeypatch):
    waits = []
    monkeypatch.setattr(transport, "_sleep", waits.append)
    with hash_stub([200]) as (url, _seen):
        pass  # the port is closed now
    with pytest.raises(transport.DaemonUnreachable):
        transport.hash_payloads({"a": b"x"}, {"token": TOKEN, "daemon_url": url}, timeout_s=1.0)
    assert waits == [1.0, 2.0, 4.0]
    with hash_stub([502, 200]) as (url, seen):
        out = transport.hash_payloads({"a": b"x"}, {"token": TOKEN, "daemon_url": url})
    assert out["a"]["hex"] == "ab" * 32 and len(seen) == 2


# --------------------------------------------------------------------------- pumped calls


def test_run_with_pump_pumps_the_calling_thread_while_fn_runs_elsewhere():
    pumped = []
    where = {}

    def fn():
        where["thread"] = threading.current_thread()
        time.sleep(1.0)
        return 42

    assert transport.run_with_pump(fn, lambda: pumped.append(threading.current_thread())) == 42
    assert len(pumped) >= 5 and set(pumped) == {threading.current_thread()}
    assert where["thread"] is not threading.current_thread()


def test_run_with_pump_reraises_and_runs_inline_without_a_pump():
    def boom():
        raise transport.DaemonTimeout("slow")

    with pytest.raises(transport.DaemonTimeout, match="slow"):
        transport.run_with_pump(boom, lambda: None)
    assert transport.run_with_pump(threading.current_thread) is threading.current_thread()

    def broken_pump():
        raise RuntimeError("the Qt application is gone")

    assert transport.run_with_pump(lambda: time.sleep(0.3) or "done", broken_pump) == "done"


def test_the_four_calls_pump_when_the_config_carries_one(tmp_path):
    pumped = []

    def respond(handler, method, path, body):
        time.sleep(0.5)  # a daemon busy hashing
        if path == "/api/v0/hash":
            names = json.loads(body.decode("utf-8"))["payloads"]
            return answer(
                handler, 200, {"hashes": {n: {"algo": "blake3", "hex": "cd" * 32, "bytes": 1} for n in names}}
            )
        if path == "/api/v0/packets":
            return answer(handler, 202, {"packet_id": "pkt_X", "status": "ingesting", "events_url": "/e"})
        answer(handler, 200, b'data: {"seq": 1, "type": "ingest.done"}\n\n', "text/event-stream")

    with http_stub(respond) as (url, _seen):
        cfg = {"token": TOKEN, "daemon_url": url, transport.PUMP_KEY: lambda: pumped.append(1)}
        assert transport.hash_payloads({"scene": b"{}"}, cfg)["scene"]["hex"] == "cd" * 32
        after_hash = len(pumped)
        assert transport.post_packet(tmp_path, cfg)["status_code"] == 202
        after_post = len(pumped)
        assert transport.follow_ingest("/e", cfg)["ingest"] == "ingested"
    # The contract is that each call pumps while it waits; the count depends on the runner's scheduling
    # (a loaded CI box saw 2 pumps in a 0.5 s wait), so each phase only has to pump at least once.
    assert after_hash >= 1 and after_post - after_hash >= 1 and len(pumped) - after_post >= 1
    calls = len(pumped)
    with http_stub(respond) as (url, _seen):
        transport.hash_payloads({"scene": b"{}"}, {"token": TOKEN, "daemon_url": url})
    assert len(pumped) == calls  # no pump in the config: nothing pumped, nothing on a thread


# --------------------------------------------------------------------------- honest 200s

HTML = b"<!doctype html><html><body>Sign in to the captive portal</body></html>"


def test_an_html_200_is_never_the_daemons_answer(tmp_path):
    with http_stub(lambda h, m, p, b: answer(h, 200, HTML, "text/html")) as (url, _seen):
        cfg = {"token": TOKEN, "daemon_url": url}
        with pytest.raises(transport.UnexpectedStatus) as err:
            transport.post_packet(tmp_path, cfg)
        assert err.value.status_code == 200 and "captive portal" in err.value.body["raw"]
        for call in (
            lambda: transport.session(cfg),
            lambda: transport.handshake(url),
            lambda: transport.get_json(JOBS, cfg),
            lambda: transport.post_json(f"{JOBS}/j/result", {"ok": True}, cfg),
        ):
            with pytest.raises(transport.UnexpectedStatus):
                call()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, "known"),
        (200, {"packet_id": "pkt_X"}),
        (200, {"packet_id": "pkt_X", "status": "ingesting"}),
        (202, {"packet_id": "pkt_X", "status": "known"}),
        (202, {"packet_id": 7, "status": "ingesting"}),
        (202, ["pkt_X"]),
    ],
)
def test_a_200_or_202_without_the_daemons_keys_is_unexpected(tmp_path, status, body):
    with http_stub(lambda h, m, p, b: answer(h, status, body)) as (url, _seen):
        with pytest.raises(transport.UnexpectedStatus):
            transport.post_packet(tmp_path, {"token": TOKEN, "daemon_url": url})


def test_a_session_or_job_list_without_its_keys_is_unexpected():
    with http_stub(lambda h, m, p, b: answer(h, 200, {"ok": True})) as (url, _seen):
        cfg = {"token": TOKEN, "daemon_url": url}
        with pytest.raises(transport.UnexpectedStatus):
            transport.session(cfg)
        with pytest.raises(transport.UnexpectedStatus):
            transport.get_json(JOBS, cfg, keys=("jobs",))
        assert transport.get_json(JOBS, cfg) == {"ok": True}  # no keys named: any JSON object


def test_a_send_answered_by_an_html_200_never_reads_already_sent(install_scene, tmp_path):
    packets = tmp_path / "packets"
    packets.mkdir()
    daemon = Daemon(packets)
    daemon.post_status, daemon.post_body = 200, "<html><body>proxy login</body></html>"
    vray_scene(install_scene)
    with serve_daemon(daemon) as cfg:
        [result] = send(["CAM_front"], packets, cfg)
    assert not result.ok and result.stage == "post"
    line, err = ctl.result_message("Send Packet", ctl.result_dict(result))
    assert err and "Already sent" not in line and "HTTP 200" in line


# --------------------------------------------------------------------------- the token


def health_and_session(handler, method, path, body):
    if path == "/api/v0/health":
        return answer(handler, 200, HEALTH)
    answer(handler, 200, {**HEALTH, "packets_dirs": ["/plate/packets"]})


@pytest.mark.parametrize("bad", [ROTATED, SECRET + "\r\n", SECRET + "é", "﻿" + SECRET, SECRET + " x"])
def test_a_token_no_header_can_carry_is_unauthorized_and_never_shown(bad):
    with http_stub(health_and_session) as (url, seen):
        cfg = {"token": bad, "daemon_url": url}
        for call in (
            lambda: transport.session(cfg),
            lambda: transport.get_json("/api/v0/projects", cfg),
            lambda: transport.hash_payloads({"a": b"x"}, cfg),
        ):
            with pytest.raises(transport.Unauthorized) as err:
                call()
            assert str(err.value) == transport.MALFORMED_TOKEN and SECRET not in str(err.value)
        gate = send_packet.daemon_gate(cfg)  # never raises for the daemon
        assert not gate.ok and gate.code == send_packet.PREFLIGHT_TOKEN and SECRET not in gate.reason
    assert [path for _m, path, _b in seen] == ["/api/v0/health"]  # the bearer never left


def test_a_header_http_client_refuses_is_unauthorized_without_its_text():
    import urllib.request

    with http_stub(health_and_session) as (url, seen):
        req = urllib.request.Request(url + "/api/v0/session", headers={"Authorization": f"Bearer {ROTATED}"})
        with pytest.raises(transport.Unauthorized) as err:
            transport._request(req, 2.0)
    assert SECRET not in str(err.value) and err.value.__context__ is None and seen == []


def test_a_token_file_with_a_rotated_line_refuses_the_send_without_the_token(tmp_path, monkeypatch, caplog):
    token_file = tmp_path / "token.txt"
    token_file.write_text(ROTATED + "\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("PLATE_TOKEN_FILE", str(token_file))
    config._reported.clear()
    with http_stub(health_and_session) as (url, seen), caplog.at_level("WARNING", logger="plate_max.config"):
        cfg = {**config.load_config(), "daemon_url": url}
        assert cfg["token"] is None
        with pytest.raises(transport.Unauthorized) as err:
            transport.session(cfg)
        gate = send_packet.daemon_gate(cfg)
    assert SECRET not in str(err.value) and "PLATE_TOKEN_FILE" in str(err.value)
    assert gate.code == send_packet.PREFLIGHT_TOKEN and SECRET not in gate.reason
    assert "token source: PLATE_TOKEN_FILE" in caplog.text and SECRET not in caplog.text
    assert all(path == "/api/v0/health" for _m, path, _b in seen)


def test_the_poller_backs_off_a_malformed_token_and_says_so_once(scene):
    logged = []
    with http_stub(lambda h, m, p, b: answer(h, 200, {"jobs": [], "next": 0})) as (url, seen):
        poller = jobs.Poller({"token": ROTATED, "daemon_url": url}, log=lambda m, err=False: logged.append(m))
        assert [poller.tick() for _ in range(20)] == [[]] * 20
    assert seen == []
    assert len(logged) == 1 and transport.MALFORMED_TOKEN in logged[0] and SECRET not in logged[0]
    assert poller._failures <= 6  # asked less and less often


# --------------------------------------------------------------------------- lost claims


@pytest.fixture
def queue(tmp_path):
    stub = QueueDaemon()
    with serve_queue(stub) as url:
        packets = tmp_path / "packets"
        packets.mkdir()
        yield (
            stub,
            {"token": "feedfacefeedfacefeedfacefeedface", "daemon_url": url, "packets_dir": str(packets)},
        )


@pytest.fixture
def previews(monkeypatch):
    ran = []
    monkeypatch.setattr(
        light,
        "vantage_preview",
        lambda camera: ran.append(camera) or {"ok": True, "camera": camera, "how": "ok"},
    )
    return ran


def recovery_polls(stub):
    return [q for m, p, q in stub.seen if m == "GET" and p == JOBS and q.get("state") == "claimed"]


def test_a_job_claimed_for_an_answer_the_dock_never_got_is_run_on_the_next_tick(
    scene, queue, previews, monkeypatch
):
    stub, cfg = queue
    monkeypatch.setattr(jobs, "POLL_TIMEOUT_S", 0.5)
    job = stub.add("light.vantage_preview", camera="CAM_A")
    stub.claim_delays = [1.5]  # the daemon claims it, then answers after the dock gave up
    poller = jobs.Poller(cfg)
    assert poller.tick() == []
    assert stub.job(job["id"])["state"] == "claimed" and previews == []
    [record] = poller.tick()
    assert record["id"] == job["id"] and record["posted"] == "posted" and previews == ["CAM_A"]
    assert posted(stub)[job["id"]]["ok"] is True and stub.job(job["id"])["state"] == "done"
    assert recovery_polls(stub) == [{"state": "claimed", "since": "0", "limit": str(jobs.RECOVER_LIMIT)}]
    assert poller.tick() == [] and previews == ["CAM_A"]  # never twice


def test_on_start_claims_since_the_dock_started_are_taken_up_and_older_ones_left_to_the_lease(
    scene, queue, previews
):
    stub, cfg = queue
    poller = jobs.Poller(cfg)
    old = stub.add("light.vantage_preview", camera="CAM_old")
    ours = stub.add("light.vantage_preview", camera="CAM_ours")
    old["state"], old["claimed_utc"] = (
        "claimed",
        (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    ours["state"], ours["claimed_utc"] = "claimed", utc_now()
    timer = FakeTimer()
    poller.start(timer)
    [handled] = timer.fire()  # one tick: the recovered claim, before any new one
    assert [r["id"] for r in handled] == [ours["id"]] and previews == ["CAM_ours"]
    assert stub.job(ours["id"])["state"] == "done" and stub.job(old["id"])["state"] == "claimed"
    assert len(recovery_polls(stub)) == 1


def test_a_recovered_claim_this_dock_already_ran_is_answered_ok_false(scene, queue, previews):
    stub, cfg = queue
    job = stub.add("light.vantage_preview", camera="CAM_A")
    stub.result_script = [500]  # the answer is refused: the job stays claimed
    poller = jobs.Poller(cfg)
    [first] = poller.tick()
    assert first["posted"].startswith("refused") and stub.job(job["id"])["state"] == "claimed"
    poller.start(FakeTimer())  # e.g. the dock shown again
    [again] = poller.tick()
    assert again["id"] == job["id"] and previews == ["CAM_A"]  # not run twice
    assert posted(stub)[job["id"]] == {"ok": False, "detail": jobs.RECOVERED_DETAIL}
    assert stub.job(job["id"])["state"] == "failed"


# --------------------------------------------------------------------------- the scene guard


def write_manifest(cfg, packet_id, max_name):
    folder = Path(cfg["packets_dir"]) / packet_id
    folder.mkdir()
    scene_hash = {"max_file": {"name": max_name}} if max_name else {"max_file": None}
    manifest = {"packet_id": packet_id, "scene_hash": scene_hash}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")


def test_the_scene_guard_refuses_a_job_for_another_project_than_the_scene_is_bound_to(
    clubhouse, queue, tmp_path, monkeypatch
):
    stub, cfg = queue
    scenes = tmp_path / "client-a"
    scenes.mkdir()
    clubhouse.maxFilePath, clubhouse.maxFileName = str(scenes), "master.max"
    ran = []
    monkeypatch.setattr(scout, "run", lambda *a, **k: ran.append(1) or _scout_results(1))
    job = stub.add("scout", count=1, keep=False, project_id="client-b")
    write_manifest(cfg, job["packet_id"], "master.max")  # client-b's master.max: the same file name
    poller = jobs.Poller(cfg)
    poller.tick()
    body = posted(stub)[job["id"]]
    assert body["ok"] is False and ran == []
    assert "bound to project master" in body["detail"] and "for project client-b" in body["detail"]
    (scenes / "master.plate.json").write_text(
        json.dumps({"project_id": "client-b"}), encoding="utf-8", newline="\n"
    )
    again = stub.add("scout", count=1, keep=False, project_id="client-b", packet_id=job["packet_id"])
    poller.tick()
    assert posted(stub)[again["id"]]["ok"] is True and ran == [1]


@pytest.mark.parametrize("case", ["no manifest", "sent unsaved"])
def test_a_scene_check_that_cannot_compare_says_so_and_the_job_runs(scene, queue, monkeypatch, case):
    stub, cfg = queue
    ran = []
    monkeypatch.setattr(scout, "run", lambda *a, **k: ran.append(1) or _scout_results(1))
    job = stub.add("scout", count=1, keep=False)
    if case == "sent unsaved":
        write_manifest(cfg, job["packet_id"], None)
    logged = []
    jobs.Poller(cfg, log=lambda m, err=False: logged.append(m)).tick()
    assert posted(stub)[job["id"]]["ok"] is True and ran == [1]
    [line] = [m for m in logged if "scene check skipped" in m]
    assert line.startswith(f"packet {job['packet_id']}: scene check skipped (")
    assert ("packets folder" if case == "no manifest" else "unsaved scene") in line


# --------------------------------------------------------------------------- the dock (offscreen)


@pytest.fixture
def qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    return widgets.QApplication.instance() or widgets.QApplication([])


def test_informational_dock_lines_render_their_entities(qt):
    from plate_max.ui import dock as dockmod

    dock = dockmod.PlateDock(ctl.FakeController())
    try:
        dock._log("it's the canvas's Project panel / A&B west")
        dock._log("{'error': 'unauthorized'} <b>not bold</b>", err=True)
        dock._log("two  spaces\nand a second line")
        text = dock.log.toPlainText()
        assert "it's the canvas's Project panel / A&B west" in text
        assert "{'error': 'unauthorized'} <b>not bold</b>" in text
        assert "&#x27;" not in text and "&amp;" not in text
        assert "two  spaces" in text
    finally:
        dock.close()
        dock.deleteLater()


class PumpedController(ctl.FakeController):
    """A FakeController with the LiveController's _options seam: send_packet records the config the
    action would get and makes one pumped call through transport, as send_packet's hashing does."""

    def __init__(self):
        super().__init__()
        self.configs = []
        self.pumped = 0

    def _options(self, cameras, progress, motion=False):
        return ctl.ActionOptions(recipes={c: {} for c in cameras}, progress=progress, backup=False)

    def send_packet(self, cameras, progress):
        cfg = dict(self._options(cameras, progress).get("config") or {})
        self.configs.append(cfg)
        pump = cfg.get(transport.PUMP_KEY)
        if callable(pump):

            def counting():
                self.pumped += 1
                pump()

            transport.run_with_pump(lambda: time.sleep(0.5), counting)
        return [
            {"camera": c, "ok": True, "packet_id": "pkt_1", "errors": [], "status_code": 202} for c in cameras
        ]


def test_the_send_flow_hands_the_action_a_pump_and_only_while_it_runs(qt):
    from plate_max.ui import dock as dockmod

    ctrl = PumpedController()
    dock = dockmod.PlateDock(ctrl)
    try:
        dock.cam_list.setCurrentRow(0)
        dock.cam_list.item(0).setSelected(True)
        dock._on_send()
        [cfg] = ctrl.configs
        # pumped at least once: the count depends on the runner (a loaded macos-latest saw 2 in the wait)
        assert cfg[transport.PUMP_KEY] == dock._pump_events and ctrl.pumped >= 1
        assert "Sent, ingesting (packet pkt_1)" in dock.log.toPlainText()
        assert "config" not in ctrl._options(["CAM_main"], None)  # outside an action: no pump
        second = dockmod.PlateDock(ctrl)  # a new dock re-wraps the original, never the wrapper
        try:
            assert ctrl._options.plate_original.__self__ is ctrl
        finally:
            second.close()
            second.deleteLater()
    finally:
        dock.close()
        dock.deleteLater()
    off_main = threading.Thread(target=dockmod.PlateDock._pump_events)  # off the main thread: a no-op
    off_main.start()
    off_main.join(timeout=5)
