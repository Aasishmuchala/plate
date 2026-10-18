"""transport.py against a stdlib http.server stub on 127.0.0.1 (no other network)."""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import blake3
import pytest

from plate_max import PACKET_VERSION, PLUGIN_VERSION, SENDER, transport

TOKEN = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_retry_waits(monkeypatch):
    """/hash asks again after 1, 2 and 4 s (a daemon restart); the waits are not worth sitting through here."""
    waits: list[float] = []
    monkeypatch.setattr(transport, "_sleep", waits.append)
    return waits


class Stub:
    def __init__(self):
        self.health = {"ok": True, "daemon_version": "x", "packet_versions": ["0.1"]}
        self.token = TOKEN
        self.root = None  # the packets folder /hash confines paths to (None: any path is outside)
        self.hash_override = None  # (status, body) to answer /hash with, whatever the request
        self.post_status = 202
        self.post_body = {
            "packet_id": "pkt_X",
            "status": "ingesting",
            "events_url": "/api/v0/packets/pkt_X/events",
        }
        self.post_script = []  # (status, body, delay_s) per POST in turn; the last one repeats
        self.packets_dirs = ["/plate/packets"]
        self.events = []  # the SSE frames the events route serves, a list of event dicts
        self.events_status = 200
        self.seen = []

    def next_post(self):
        if not self.post_script:
            return self.post_status, self.post_body, 0.0
        return self.post_script.pop(0) if len(self.post_script) > 1 else self.post_script[0]


@contextmanager
def serve(stub: Stub):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body):
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            stub.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, None))
            authorised = self.headers.get("Authorization") == f"Bearer {stub.token}"
            if url.path == "/api/v0/health":
                self._send(200, stub.health)
            elif url.path == "/api/v0/session":
                if not authorised:
                    return self._send(401, {"error": "unauthorized"})
                self._send(200, {**stub.health, "packets_dirs": stub.packets_dirs})
            elif url.path.endswith("/events"):
                if not authorised:
                    return self._send(401, {"error": "unauthorized"})
                if stub.events_status != 200:
                    return self._send(stub.events_status, {"error": "nope"})
                after = int(dict(urllib.parse.parse_qsl(url.query)).get("after", "0"))
                text = "retry: 1000\n\n: keep-alive comment\n\n" + "".join(
                    f"id: {ev['seq']}\ndata: {json.dumps(ev)}\n\n" for ev in stub.events if ev["seq"] > after
                )
                raw = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self._send(404, {"error": "unknown route"})

        def _hash(self, body):
            if self.headers.get("Authorization") != f"Bearer {stub.token}":
                return self._send(401, {"error": "unauthorized"})
            if stub.hash_override:
                return self._send(*stub.hash_override)
            hashes = {}
            for path in body.get("paths", []):
                real = Path(path).resolve()
                if stub.root is None or not real.is_relative_to(Path(stub.root).resolve()):
                    return self._send(403, {"error": "outside the packets folders", "path": path})
                if not real.is_file():
                    return self._send(404, {"error": "no such file", "path": path})
                data = real.read_bytes()
                hashes[path] = {"algo": "blake3", "hex": blake3.blake3(data).hexdigest(), "bytes": len(data)}
            for name, text in body.get("payloads", {}).items():
                data = base64.b64decode(text)
                hashes[name] = {"algo": "blake3", "hex": blake3.blake3(data).hexdigest(), "bytes": len(data)}
            self._send(200, {"hashes": hashes})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            stub.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            if self.path == "/api/v0/hash":
                return self._hash(body)
            status, answer, delay = stub.next_post()
            if delay:
                time.sleep(delay)
            try:
                self._send(status, answer)
            except OSError:  # the client timed out and hung up
                pass

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


def test_handshake_with_a_matching_daemon():
    stub = Stub()
    with serve(stub) as url:
        out = transport.handshake(url)
    assert out == {"ok": True, "daemon_version": "x", "packet_versions": ["0.1"], "problems": []}


def test_handshake_reports_an_unknown_packet_version():
    stub = Stub()
    stub.health["packet_versions"] = ["1.0"]
    with serve(stub) as url:
        out = transport.handshake(url)
    assert out["ok"] is False
    assert out["problems"] == ["0.1 not in packet_versions ['1.0']"]


def test_handshake_flags_a_daemon_that_is_not_ok():
    stub = Stub()
    stub.health = {"ok": False, "packet_versions": ["0.1"]}
    with serve(stub) as url:
        assert transport.handshake(url)["problems"] == ["daemon reports ok=false"]


def test_post_packet_sends_the_contracted_request(tmp_path):
    stub = Stub()
    folder = tmp_path / "pkt_folder"
    folder.mkdir()
    with serve(stub) as url:
        out = transport.post_packet(folder, {"token": TOKEN, "daemon_url": url})
    assert out["status_code"] == 202
    assert out["body"]["status"] == "ingesting"
    path, headers, body = stub.seen[0]
    assert path == "/api/v0/packets"
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["x-plate-packet-version"] == PACKET_VERSION == "0.1.1"
    assert headers["content-type"] == "application/json"
    assert Path(body["packet_path"]).is_absolute()
    assert Path(body["packet_path"]) == folder.resolve()
    assert body["sender"] == SENDER and body["sender"]["name"] == "plate_max"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {"packet_id": "pkt_X", "status": "known"}),
        (409, {"error": "conflict"}),
        (422, {"errors": ["manifest.json missing"]}),
        (426, {"supported": ["0.1"]}),
    ],
)
def test_other_contract_statuses_pass_through(tmp_path, status, body):
    stub = Stub()
    stub.post_status, stub.post_body = status, body
    with serve(stub) as url:
        out = transport.post_packet(tmp_path, {"token": TOKEN, "daemon_url": url})
    assert out == {"status_code": status, "body": body}


def test_401_raises_unauthorized(tmp_path):
    stub = Stub()
    stub.post_status, stub.post_body = 401, {"error": "unauthorized"}
    with serve(stub) as url, pytest.raises(transport.Unauthorized):
        transport.post_packet(tmp_path, {"token": "wrong", "daemon_url": url})


def test_no_token_never_touches_the_network(tmp_path):
    with pytest.raises(transport.Unauthorized, match="no token"):
        transport.post_packet(tmp_path, {"token": "", "daemon_url": "http://127.0.0.1:9"})


def test_unexpected_status_raises(tmp_path):
    stub = Stub()
    stub.post_status, stub.post_body = 500, {"error": "boom"}
    with serve(stub) as url, pytest.raises(transport.UnexpectedStatus) as err:
        transport.post_packet(tmp_path, {"token": TOKEN, "daemon_url": url})
    assert err.value.status_code == 500


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_closed_port_is_daemon_unreachable(tmp_path):
    url = f"http://127.0.0.1:{_closed_port()}"
    with pytest.raises(transport.DaemonUnreachable):
        transport.handshake(url, timeout_s=1.0)
    with pytest.raises(transport.DaemonUnreachable):
        transport.post_packet(tmp_path, {"token": TOKEN, "daemon_url": url}, timeout_s=1.0)


def test_major_minor():
    assert transport.major_minor("0.1.0") == "0.1"
    assert transport.major_minor("1.0") == "1.0"


# ---- compatible


def _health(**kw):
    return {"ok": True, "daemon_version": PLUGIN_VERSION, "packet_versions": ["0.1"], **kw}


def test_compatible_accepts_a_matching_daemon():
    assert transport.compatible(_health()) == (True, "")


def test_compatible_ignores_patch_levels():
    major_minor = transport.major_minor(PLUGIN_VERSION)
    assert transport.compatible(_health(daemon_version=f"{major_minor}.99")) == (True, "")
    assert transport.compatible(_health(packet_versions=["0.1.1", "0.2"]))[0] is True


def test_compatible_refuses_a_daemon_that_does_not_read_0_1():
    ok, reason = transport.compatible(_health(packet_versions=["0.2"]))
    assert ok is False and "0.2" in reason and "0.1" in reason
    assert transport.compatible(_health(packet_versions=[]))[0] is False


def test_compatible_refuses_another_daemon_release():
    ok, reason = transport.compatible(_health(daemon_version="0.2.0"))
    assert ok is False and "0.2.0" in reason and PLUGIN_VERSION in reason
    assert transport.compatible(_health(daemon_version="1.1.0"))[0] is False


@pytest.mark.parametrize(
    "health",
    [
        None,
        "ok",
        {},
        {"ok": False, "daemon_version": PLUGIN_VERSION, "packet_versions": ["0.1"]},
        {"ok": True, "daemon_version": PLUGIN_VERSION},
        {"ok": True, "packet_versions": ["0.1"]},
        {"ok": True, "daemon_version": "", "packet_versions": ["0.1"]},
    ],
)
def test_compatible_refuses_incomplete_health(health):
    ok, reason = transport.compatible(health)
    assert ok is False and reason


def test_compatible_reads_a_handshake_result():
    stub = Stub()
    stub.health = {"ok": True, "daemon_version": PLUGIN_VERSION, "packet_versions": ["0.1"]}
    with serve(stub) as url:
        assert transport.compatible(transport.handshake(url)) == (True, "")
    stub.health["packet_versions"] = ["0.2"]
    with serve(stub) as url:
        assert transport.compatible(transport.handshake(url))[0] is False


# ---- hash_files and hash_payloads (POST /api/v0/hash)


def _stub_with_files(tmp_path):
    stub = Stub()
    stub.root = tmp_path / "packets"
    folder = stub.root / "pkt_1"
    folder.mkdir(parents=True)
    (folder / "beauty.exr").write_bytes(b"EXR" * 1000)
    (folder / "id_legend.json").write_bytes(b"{}\n")
    return stub, folder


def test_hash_files_returns_blake3_per_path(tmp_path):
    stub, folder = _stub_with_files(tmp_path)
    paths = [folder / "beauty.exr", folder / "id_legend.json"]
    with serve(stub) as url:
        out = transport.hash_files(paths, {"token": TOKEN, "daemon_url": url})
    assert set(out) == {str(p) for p in paths}
    assert out[str(paths[0])] == {
        "algo": "blake3",
        "hex": blake3.blake3(b"EXR" * 1000).hexdigest(),
        "bytes": 3000,
    }
    path, headers, body = stub.seen[0]
    assert path == "/api/v0/hash" and headers["authorization"] == f"Bearer {TOKEN}"
    assert body == {"paths": [os.path.abspath(str(p)) for p in paths]}


def test_hash_files_accepts_strings_and_an_empty_list(tmp_path):
    stub, folder = _stub_with_files(tmp_path)
    with serve(stub) as url:
        config = {"token": TOKEN, "daemon_url": url}
        out = transport.hash_files([str(folder / "id_legend.json")], config)
        assert out[str(folder / "id_legend.json")]["bytes"] == 3
        assert transport.hash_files([], config) == {}
    assert len(stub.seen) == 1  # the empty call never went out


def test_hash_files_401_is_unauthorized(tmp_path):
    stub, folder = _stub_with_files(tmp_path)
    with serve(stub) as url, pytest.raises(transport.Unauthorized):
        transport.hash_files([folder / "beauty.exr"], {"token": "wrong", "daemon_url": url})


def test_hash_files_403_is_hash_refused(tmp_path):
    stub, _ = _stub_with_files(tmp_path)
    outside = tmp_path / "elsewhere.exr"
    outside.write_bytes(b"x")
    with serve(stub) as url, pytest.raises(transport.HashRefused) as err:
        transport.hash_files([outside], {"token": TOKEN, "daemon_url": url})
    assert err.value.status_code == 403 and "outside" in str(err.value)


def test_hash_files_404_is_hash_refused(tmp_path):
    stub, folder = _stub_with_files(tmp_path)
    with serve(stub) as url, pytest.raises(transport.HashRefused) as err:
        transport.hash_files([folder / "missing.exr"], {"token": TOKEN, "daemon_url": url})
    assert err.value.status_code == 404


def test_hash_refused_and_unauthorized_are_transport_errors():
    assert issubclass(transport.HashRefused, transport.TransportError)
    assert not issubclass(transport.HashRefused, transport.Unauthorized)


def test_a_path_left_out_of_the_answer_is_hash_refused(tmp_path):
    stub, folder = _stub_with_files(tmp_path)
    stub.hash_override = (200, {"hashes": {}})
    with serve(stub) as url, pytest.raises(transport.HashRefused, match="no hash"):
        transport.hash_files([folder / "beauty.exr"], {"token": TOKEN, "daemon_url": url})


def test_a_malformed_hash_answer_is_unexpected(tmp_path, _no_retry_waits):
    stub, folder = _stub_with_files(tmp_path)
    stub.hash_override = (200, {"nope": 1})
    with serve(stub) as url, pytest.raises(transport.UnexpectedStatus):
        transport.hash_files([folder / "beauty.exr"], {"token": TOKEN, "daemon_url": url})
    assert _no_retry_waits == []  # a 200, however wrong, is never asked again
    stub.hash_override = (500, {"error": "boom"})
    with serve(stub) as url, pytest.raises(transport.UnexpectedStatus) as err:
        transport.hash_files([folder / "beauty.exr"], {"token": TOKEN, "daemon_url": url})
    assert err.value.status_code == 500
    assert _no_retry_waits == list(transport.HASH_RETRY_DELAYS_S)  # a 5xx is asked three more times


def test_hash_without_a_token_never_touches_the_network(tmp_path):
    config = {"token": "", "daemon_url": "http://127.0.0.1:9"}
    with pytest.raises(transport.Unauthorized, match="no token"):
        transport.hash_files([tmp_path / "x"], config)
    with pytest.raises(transport.Unauthorized, match="no token"):
        transport.hash_payloads({"a": b"x"}, config)


def test_hash_payloads_returns_blake3_per_name():
    stub = Stub()
    payloads = {"scene": b'{"a":1}', "view": b"\x00\xff binary"}
    with serve(stub) as url:
        out = transport.hash_payloads(payloads, {"token": TOKEN, "daemon_url": url})
    assert out["scene"]["hex"] == blake3.blake3(payloads["scene"]).hexdigest()
    assert out["view"] == {"algo": "blake3", "hex": blake3.blake3(payloads["view"]).hexdigest(), "bytes": 9}
    assert base64.b64decode(stub.seen[0][2]["payloads"]["view"]) == payloads["view"]


def test_hash_payloads_over_the_cap_is_refused_before_sending():
    stub = Stub()
    big = {
        "a": b"x" * (transport.HASH_PAYLOAD_LIMIT // 2 + 1),
        "b": b"y" * (transport.HASH_PAYLOAD_LIMIT // 2),
    }
    with serve(stub) as url, pytest.raises(ValueError, match="MiB"):
        transport.hash_payloads(big, {"token": TOKEN, "daemon_url": url})
    assert stub.seen == []
    with serve(stub) as url:
        assert transport.hash_payloads({}, {"token": TOKEN, "daemon_url": url}) == {}


def test_a_closed_port_on_hash_is_daemon_unreachable(tmp_path):
    url = f"http://127.0.0.1:{_closed_port()}"
    with pytest.raises(transport.DaemonUnreachable):
        transport.hash_payloads({"a": b"x"}, {"token": TOKEN, "daemon_url": url}, timeout_s=1.0)


# ---- proxies stay off


def test_system_proxies_are_never_used(monkeypatch, tmp_path):
    # a proxy that would swallow the request and the token if the opener honoured it
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    stub, folder = _stub_with_files(tmp_path)
    with serve(stub) as url:
        assert transport.handshake(url)["ok"] is True
        out = transport.hash_files([folder / "id_legend.json"], {"token": TOKEN, "daemon_url": url})
    assert len(out) == 1


# ---- session (GET /api/v0/session)


def test_session_returns_the_daemons_answer_with_the_bearer():
    stub = Stub()
    with serve(stub) as url:
        out = transport.session({"token": TOKEN, "daemon_url": url})
    assert out["packets_dirs"] == ["/plate/packets"] and out["packet_versions"] == ["0.1"]
    path, headers, _ = stub.seen[-1]
    assert path == "/api/v0/session" and headers["authorization"] == f"Bearer {TOKEN}"


def test_session_with_a_wrong_token_is_unauthorized():
    stub = Stub()
    with serve(stub) as url, pytest.raises(transport.Unauthorized, match="daemon rejected the token"):
        transport.session({"token": "wrong", "daemon_url": url})


def test_session_without_a_token_never_touches_the_network():
    with pytest.raises(transport.Unauthorized, match="no token"):
        transport.session({"token": "", "daemon_url": "http://127.0.0.1:9"})


def test_session_on_an_old_daemon_is_unexpected():
    stub = Stub()

    with serve(stub) as url:
        with pytest.raises(transport.UnexpectedStatus) as err:
            transport.session({"token": TOKEN, "daemon_url": url + "/nothing-here"})
    assert err.value.status_code == 404


# ---- post timeout and the re-post


def test_the_post_timeout_is_30_s_plus_15_s_per_gib(tmp_path, monkeypatch):
    folder = tmp_path / "pkt"
    (folder / "motion").mkdir(parents=True)
    (folder / "beauty.exr").write_bytes(b"x" * 1000)
    (folder / "motion" / "z.0000.exr").write_bytes(b"y" * 24)
    assert transport.folder_bytes(folder) == 1024
    assert transport.post_timeout_s(folder) == pytest.approx(30 + 15 * 1024 / 2**30)
    assert transport.folder_bytes(tmp_path / "missing") == 0
    seen = []

    def fake_request(req, timeout_s):
        seen.append(timeout_s)
        return 202, {"status": "ingesting"}

    monkeypatch.setattr(transport, "folder_bytes", lambda f: 3 * transport.GIB)
    monkeypatch.setattr(transport, "_request", fake_request)
    transport.post_packet(folder, {"token": TOKEN, "daemon_url": "http://127.0.0.1:9"})
    transport.post_packet(folder, {"token": TOKEN, "daemon_url": "http://127.0.0.1:9"}, timeout_s=7)
    assert seen == [pytest.approx(75.0), 7.0]


@pytest.mark.parametrize(
    ("status", "body"),
    [(200, {"packet_id": "pkt_X", "status": "known"}), (202, {"packet_id": "pkt_X", "status": "ingesting"})],
)
def test_a_timed_out_post_is_reposted_once_and_200_or_202_is_success(tmp_path, status, body):
    stub = Stub()
    stub.post_script = [(202, stub.post_body, 1.0), (status, body, 0.0)]
    with serve(stub) as url:
        out = transport.post_packet(tmp_path, {"token": TOKEN, "daemon_url": url}, timeout_s=0.3)
    assert out == {"status_code": status, "body": body, "reposted": True}
    posts = [entry for entry in stub.seen if entry[0] == "/api/v0/packets"]
    assert len(posts) == 2 and posts[0][2] == posts[1][2]  # the same folder, the same request


def test_a_repost_answering_anything_else_is_post_timed_out(tmp_path):
    stub = Stub()
    stub.post_script = [(202, stub.post_body, 1.0), (409, {"error": "conflict"}, 0.0)]
    with serve(stub) as url, pytest.raises(transport.PostTimedOut) as err:
        transport.post_packet(tmp_path, {"token": TOKEN, "daemon_url": url}, timeout_s=0.3)
    assert str(err.value).startswith("timed out; re-post answered 409")
    assert err.value.status_code == 409 and isinstance(err.value, transport.TransportError)


def test_a_repost_that_times_out_too_is_post_timed_out(tmp_path):
    stub = Stub()
    stub.post_script = [(202, stub.post_body, 1.0)]
    with serve(stub) as url, pytest.raises(transport.PostTimedOut) as err:
        transport.post_packet(tmp_path, {"token": TOKEN, "daemon_url": url}, timeout_s=0.3)
    assert err.value.status_code is None and "re-post answered nothing" in str(err.value)
    assert len([e for e in stub.seen if e[0] == "/api/v0/packets"]) == 2  # never a third


def test_a_silent_daemon_is_a_daemon_timeout(tmp_path):
    stub = Stub()
    stub.post_script = [(202, stub.post_body, 1.0)]
    with serve(stub) as url, pytest.raises(transport.DaemonTimeout):
        transport._request(
            transport.urllib.request.Request(url + "/api/v0/packets", data=b"{}", method="POST"), 0.3
        )
    assert issubclass(transport.DaemonTimeout, transport.DaemonUnreachable)


# ---- following the ingest (events_url)


def test_parse_sse_reads_data_frames_only():
    text = 'retry: 500\n\n: comment\n\nid: 1\ndata: {"seq": 1, "type": "ingest.file"}\n\ndata: not json\n\n'
    assert transport.parse_sse(text) == [{"seq": 1, "type": "ingest.file"}]
    assert transport.parse_sse('id: 2\r\ndata: {"seq": 2}\r\n\r\n') == [{"seq": 2}]


def test_follow_ingest_reports_ingested():
    stub = Stub()
    stub.events = [
        {"seq": 1, "type": "ingest.file", "data": {"relpath": "beauty.exr"}},
        {"seq": 2, "type": "ingest.done", "data": {}},
    ]
    with serve(stub) as url:
        out = transport.follow_ingest("/api/v0/packets/pkt_X/events", {"token": TOKEN, "daemon_url": url})
    assert out == {"ingest": "ingested", "problem": None}
    path, headers, _ = stub.seen[-1]
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query))
    assert query == {"wait_s": "0", "after": "0"} and headers["authorization"] == f"Bearer {TOKEN}"


def test_follow_ingest_reports_the_failure_errors():
    stub = Stub()
    stub.events = [{"seq": 1, "type": "ingest.failed", "data": {"errors": ["a: bad", "b: worse"]}}]
    with serve(stub) as url:
        out = transport.follow_ingest(
            url + "/api/v0/packets/pkt_X/events", {"token": TOKEN, "daemon_url": url}
        )
    assert out["ingest"] == "failed: a: bad; b: worse", (
        out
    )  # the problem field says why if it stayed ingesting


def test_follow_ingest_polls_one_second_apart_until_the_window_ends():
    stub = Stub()
    stub.events = [{"seq": 3, "type": "ingest.file", "data": {}}]
    now = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    with serve(stub) as url:
        out = transport.follow_ingest(
            "/api/v0/packets/pkt_X/events?wait_s=60",
            {"token": TOKEN, "daemon_url": url},
            sleep=sleep,
            clock=lambda: now[0],
        )
    assert out == {"ingest": "ingesting", "problem": None}
    assert sleeps == [1.0] * 5  # polls at 0, 1, 2, 3, 4 and 5 s
    polls = [urllib.parse.urlsplit(p).query for p, _, _ in stub.seen if p.startswith("/api/v0/packets/pkt_X")]
    assert len(polls) == 6 and polls[0] == "wait_s=0&after=0" and polls[-1] == "wait_s=0&after=3"


def test_follow_ingest_never_raises_for_the_daemon():
    stub = Stub()
    stub.events_status = 404
    with serve(stub) as url:
        out = transport.follow_ingest("/api/v0/packets/pkt_X/events", {"token": TOKEN, "daemon_url": url})
    assert out["ingest"] == "ingesting" and "404" in out["problem"]
    gone = transport.follow_ingest(
        "/api/v0/packets/pkt_X/events", {"token": TOKEN, "daemon_url": f"http://127.0.0.1:{_closed_port()}"}
    )
    assert gone["ingest"] == "ingesting" and "unreadable" in gone["problem"]


def test_a_connect_timeout_is_unreachable_but_an_answer_timeout_is_a_timeout(monkeypatch):
    """Windows does not refuse a closed loopback port at once: the connect times out instead. urllib wraps a
    connect-phase timeout in URLError; a timeout waiting for the answer arrives unwrapped."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request("http://127.0.0.1:9/api/v0/health")

    def connect_timeout(*a, **k):
        raise urllib.error.URLError(TimeoutError("timed out"))

    def answer_timeout(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(transport._opener, "open", connect_timeout)
    with pytest.raises(transport.DaemonUnreachable, match="could not connect"):
        transport._request(req, 1.0)
    monkeypatch.setattr(transport._opener, "open", answer_timeout)
    with pytest.raises(transport.DaemonTimeout):
        transport._request(req, 1.0)
