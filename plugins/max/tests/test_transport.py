"""transport.py against a stdlib http.server stub on 127.0.0.1 (no other network)."""

from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from plate_max import PACKET_VERSION, SENDER, transport

TOKEN = "0123456789abcdef0123456789abcdef"


class Stub:
    def __init__(self):
        self.health = {"ok": True, "daemon_version": "x", "packet_versions": ["0.1"]}
        self.post_status = 202
        self.post_body = {
            "packet_id": "pkt_X",
            "status": "ingesting",
            "events_url": "/api/v0/packets/pkt_X/events",
        }
        self.seen = []


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
            if self.path == "/api/v0/health":
                self._send(200, stub.health)
            else:
                self._send(404, {"error": "unknown route"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            stub.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            self._send(stub.post_status, stub.post_body)

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
