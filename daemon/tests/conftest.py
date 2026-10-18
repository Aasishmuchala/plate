"""Shared fixtures for the daemon tests: every test runs with PLATE_HOME=tmp_path and a clean env.

`settings` is `Settings.load()` under that home (so config.json and the token are real), with the UI
pointed at a folder that does not exist unless a test builds one. `client` is a TestClient whose base URL
carries an allowlisted Host; `auth` is the matching bearer header. `live_server` runs the real app under
uvicorn on an ephemeral loopback port for the tests that need true streaming (SSE); `child_env` and
`free_port` help the tests that start a second Python process.

`PLATE_RUN_WORKERS=0` is set for every test (router plan T145): approved runs execute inline through a
BackgroundTask, so a TestClient call returns with the run at its end, as the suite expects. A test that wants
the run executor sets the variable itself before it builds the app (`live_server` with `PLATE_RUN_WORKERS=4`).
`live_server(on_app=fn, **create_app_kwargs)` calls `fn(app)` before serving, and the pipeline's executor is
closed when the server stops.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plate_daemon.api import create_app
from plate_daemon.config import Settings

ENV_VARS = (
    "PLATE_HOME",
    "PLATE_CONFIG",
    "PLATE_TOKEN_FILE",
    "PLATE_PORT",
    "PLATE_BIND",
    "PLATE_DEV",
    "PLATE_TAILSCALE_IP",
    "PLATE_UI_DIST",
    "PLATE_FAKE_ENGINE",
    "PLATE_SYNC_INGEST",
    "PLATE_CAS_COPY",
    "PLATE_RUN_WORKERS",
)
BASE_URL = "http://127.0.0.1:4610"
DAEMON_DIR = Path(__file__).resolve().parents[1]
REPO = DAEMON_DIR.parent


@pytest.fixture(autouse=True)
def plate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PLATE_HOME", str(tmp_path))
    monkeypatch.setenv("PLATE_UI_DIST", str(tmp_path / "ui-dist-absent"))
    monkeypatch.setenv("PLATE_RUN_WORKERS", "0")  # runs inline, as the suite expects (T145)
    return tmp_path


@pytest.fixture
def settings(plate_env: Path) -> Settings:
    return Settings.load()


@pytest.fixture
def auth(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.token}"}


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=BASE_URL) as c:
        yield c


def _child_env(**extra: str) -> dict[str, str]:
    """The current environment plus `extra`, for a child Python that must import plate_daemon and
    plate_spec from this checkout."""
    env = dict(os.environ)
    env.update(extra)
    env["PYTHONPATH"] = os.pathsep.join([str(DAEMON_DIR), str(REPO / "spec")])
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def child_env() -> Callable[..., dict[str, str]]:
    return _child_env


@pytest.fixture
def free_port() -> Callable[[], int]:
    return _free_port


@contextmanager
def _serve(
    make_settings: Callable[[int], Settings], on_app: Callable[[FastAPI], None] | None = None, **app_kwargs
) -> Iterator[tuple[Settings, str]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    settings = make_settings(port)
    app = create_app(settings, **app_kwargs)
    if on_app is not None:
        on_app(app)
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on", timeout_graceful_shutdown=2)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield settings, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        pipeline = getattr(app.state, "pipeline", None)
        if pipeline is not None and hasattr(pipeline, "close"):
            pipeline.close(2.0)  # the run executor's threads stop with the server


@pytest.fixture
def live_server(settings: Settings):
    """`with live_server(**create_app_kwargs) as (settings, url):` the real app on a real socket."""

    def start(on_app: Callable[[FastAPI], None] | None = None, **app_kwargs):
        return _serve(lambda port: settings.model_copy(update={"port": port}), on_app, **app_kwargs)

    return start
