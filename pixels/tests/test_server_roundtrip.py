"""Real HTTP round trip against a ThreadingHTTPServer on a free loopback port."""

from __future__ import annotations

import http.client
import json

import pytest


def post(addr, path, body=None, raw=None):
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=10)
    try:
        payload = raw if raw is not None else json.dumps(body if body is not None else {}).encode("utf-8")
        conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type"), json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


def test_health_roundtrip(pixels_server):
    assert pixels_server[0] == "127.0.0.1"
    status, ctype, data = post(pixels_server, "/health")
    assert status == 200
    assert ctype == "application/json"
    assert data["ok"] is True
    assert data["stub_mode"] is True
    assert data["torch"] is False
    assert data["models"]["depth_anything_v2_small"] == {
        "available": False,
        "licence": "Apache-2.0",
        "commercial_ok": True,
    }
    assert data["models"]["depth_anything_v2_large"]["commercial_ok"] is False


def test_depth_roundtrip(pixels_server, png):
    status, _, data = post(pixels_server, "/depth", {"png": png})
    assert status == 200
    assert data == {"stub": True, "model": "depth_anything_v2_small", "shape": [32, 48], "depth_png": None}


def test_plate_upscale_is_400(pixels_server, png):
    status, _, data = post(pixels_server, "/upscale", {"png": png, "scale": 2, "is_plate": True})
    assert status == 400
    assert data == {"error": "plate upscaling is forbidden (BUILD_PLAN section 1)"}


def test_unknown_route_is_404(pixels_server):
    status, _, data = post(pixels_server, "/nope")
    assert status == 404
    assert data == {"error": "unknown route /nope"}


@pytest.mark.parametrize(
    ("path", "body", "raw"),
    [
        ("/depth", {}, None),
        ("/depth", {"png": "missing-file.png"}, None),
        ("/segment", {"png": "x.png", "points": "nope"}, None),
        ("/health", None, b"{not json"),
        ("/health", None, b"[1, 2]"),
    ],
)
def test_bad_input_is_400(pixels_server, path, body, raw):
    status, _, data = post(pixels_server, path, body, raw)
    assert status == 400
    assert "error" in data


def test_empty_body_is_treated_as_empty_object(pixels_server):
    status, _, data = post(pixels_server, "/health", raw=b"")
    assert status == 200
    assert data["ok"] is True


def test_port_env(monkeypatch):
    from plate_pixels.server import PORT, configured_port

    assert configured_port() == PORT == 4611
    monkeypatch.setenv("PLATE_PIXELS_PORT", "4699")
    assert configured_port() == 4699
    monkeypatch.setenv("PLATE_PIXELS_PORT", "abc")
    with pytest.raises(SystemExit):
        configured_port()


def test_python_dash_m_starts_the_stub_server(tmp_path):
    """`python -m plate_pixels.server` (the Makefile target) serves /health on PLATE_PIXELS_PORT."""
    import os
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "PLATE_PIXELS_PORT": str(port),
        "PLATE_MODELS": str(tmp_path / "models"),
    }
    env.pop("PLATE_PIXELS_REAL", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "plate_pixels.server"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                status, _, data = post(("127.0.0.1", port), "/health")
                break
            except OSError:
                assert proc.poll() is None, "server exited early"
                assert time.monotonic() < deadline, "server did not come up"
                time.sleep(0.1)
        assert status == 200
        assert data["stub_mode"] is True
        assert data["torch"] is False
    finally:
        proc.terminate()
        proc.wait(timeout=10)
