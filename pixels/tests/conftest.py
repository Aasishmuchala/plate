"""Shared fixtures for the pixels tests: stub-mode env, a tiny PNG, and a server on a free port."""

from __future__ import annotations

import threading

import pytest
from PIL import Image


@pytest.fixture(autouse=True)
def stub_env(monkeypatch, tmp_path):
    """Every test starts in default stub mode with an isolated model cache."""
    monkeypatch.delenv("PLATE_PIXELS_REAL", raising=False)
    monkeypatch.delenv("PLATE_PIXELS_PORT", raising=False)
    monkeypatch.setenv("PLATE_MODELS", str(tmp_path / "models"))


@pytest.fixture
def png(tmp_path):
    """A 48 x 32 PNG (width x height) so shapes come back as [32, 48]."""
    path = tmp_path / "plate.png"
    Image.new("RGB", (48, 32), (120, 130, 140)).save(path)
    return str(path)


@pytest.fixture
def pixels_server():
    """Yield (host, port) of a stub server on a free loopback port; shut it down afterwards."""
    from plate_pixels.server import make_server

    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
