"""Shared fixtures for the pixels tests: stub-mode env, an isolated app home, a tiny PNG, an authed client."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from PIL import Image

TOKEN = "t" * 32
BASE_URL = "http://127.0.0.1:4611"


@pytest.fixture(autouse=True)
def stub_env(monkeypatch, tmp_path):
    """Every test starts in default stub mode with an isolated model cache and app home."""
    for name in (
        "PLATE_PIXELS_REAL",
        "PLATE_PIXELS_PORT",
        "PLATE_TOKEN_FILE",
        "PLATE_CONFIG",
        "PLATE_ALLOW_UNMANAGED_GPU",
        "PLATE_FORCE_LOCAL",
        "PLATE_GPU_WINDOWS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLATE_MODELS", str(tmp_path / "models"))
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "home"))


@pytest.fixture
def png(tmp_path):
    """A 48 x 32 PNG (width x height) so shapes come back as [32, 48]."""
    path = tmp_path / "plate.png"
    Image.new("RGB", (48, 32), (120, 130, 140)).save(path)
    return str(path)


@pytest.fixture
def make_client():
    """Build an authed TestClient for an app; the Host header is the loopback address the Guard allows."""

    def build(app, token: str = TOKEN) -> TestClient:
        return TestClient(app, base_url=BASE_URL, headers={"Authorization": f"Bearer {token}"})

    return build


@pytest.fixture
def client(make_client):
    """An authed client on a default stub-mode worker."""
    from plate_pixels.server import create_app

    return make_client(create_app(TOKEN, port=4611))
