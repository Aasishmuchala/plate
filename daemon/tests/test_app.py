"""create_app and `python -m plate_daemon`: the UI at /, the seed packet-to-export flow on a per-project
CAS, and the start-up line that carries the token."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import httpx
from fastapi.testclient import TestClient

import plate_spec
from plate_daemon.api import UI_NOT_BUILT, create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Paths, Settings

BASE = "http://127.0.0.1:4610"
ROUTE = {"engine_id": "fake", "provider": "fake", "region": "local"}


def test_root_says_the_ui_is_not_built(client):
    r = client.get("/")
    assert r.status_code == 200 and r.text == UI_NOT_BUILT
    assert r.headers["content-type"].startswith("text/plain")


def test_root_serves_ui_dist_when_present(tmp_path, monkeypatch, auth):
    dist = tmp_path / "ui-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>PLATE</title>\n", encoding="utf-8", newline="\n")
    (dist / "assets" / "app.js").write_text("console.log('plate')\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("PLATE_UI_DIST", str(dist))
    with TestClient(create_app(Settings.load()), base_url=BASE) as c:
        r = c.get("/")
        assert r.status_code == 200 and "<title>PLATE</title>" in r.text
        assert r.headers["content-type"].startswith("text/html")
        assert c.get("/assets/app.js").text == "console.log('plate')\n"
        # The mount does not shadow the API, and the Host check still covers the UI.
        assert c.get("/api/v0/session", headers=auth).status_code == 200
        assert c.get("/api/v0/session").status_code == 401
        assert c.get("/", headers={"Host": "evil:4610"}).status_code == 403


def test_packet_to_export_on_the_project_cas(tmp_path, monkeypatch, auth):
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv("PLATE_DEV", "1")  # the seed driver (POST /runs, /quote, /queue, /step): decision 36
    settings = Settings.load()
    packet = plate_spec.materialize_example("tierA_still_clubhouse", settings.packets_dirs[0] / "pkt")
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    body = {"packet_path": str(packet), "sender": {"name": "test", "version": "0"}}
    with TestClient(create_app(settings), base_url=BASE) as c:
        assert c.post("/api/v0/packets", json=body, headers=auth).status_code == 426
        v = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
        r = c.post("/api/v0/packets", json=body, headers=v)
        assert r.status_code == 202, r.text
        packet_id = r.json()["packet_id"]
        assert c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
        project_id = manifest["project_id"]
        r = c.post(
            "/api/v0/runs",
            json={"project_id": project_id, "packet_id": packet_id, "route": ROUTE},
            headers=auth,
        )
        run_id = r.json()["id"]
        assert c.post(f"/api/v0/runs/{run_id}/quote", headers=auth).status_code == 200
        assert (
            c.post(f"/api/v0/runs/{run_id}/approve", json={"actor": "human"}, headers=auth).status_code == 200
        )
        assert c.post(f"/api/v0/runs/{run_id}/queue", headers=auth).json()["state"] == "QUEUED"
        state = "QUEUED"
        for _ in range(20):
            state = c.post(f"/api/v0/runs/{run_id}/step", headers=auth).json()["state"]
            if state == "DONE":
                break
        assert state == "DONE"
        dest = tmp_path / "out" / "result.png"
        r = c.post(f"/api/v0/runs/{run_id}/export", json={"dest": str(dest)}, headers=auth)
        assert r.status_code == 200 and dest.is_file()
        counts = c.app.state.index.counts(project_id)
    cas = CAS(settings.projects_dir / project_id / "assets")
    objects = list(cas.iter_objects())
    assert objects and counts["assets"] == len(objects)
    assert not (settings.home / "cas").exists()  # no global CAS


def test_main_prints_the_token_url_then_serves(tmp_path, child_env, free_port):
    port = free_port()
    token = Paths.from_env().ensure_config()["token"]
    proc = subprocess.Popen(
        [sys.executable, "-m", "plate_daemon"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=child_env(PLATE_HOME=str(tmp_path), PLATE_PORT=str(port)),
        text=True,
        encoding="utf-8",
    )
    watchdog = threading.Timer(30, proc.kill)
    watchdog.start()
    try:
        line = proc.stdout.readline().strip()
        assert line == f"PLATE daemon http://127.0.0.1:{port}/#token={token}"
        # The line comes once the socket is bound; uvicorn starts listening a moment later.
        deadline = time.monotonic() + 15
        while True:
            try:
                r = httpx.get(
                    f"http://127.0.0.1:{port}/api/v0/session", headers={"Authorization": f"Bearer {token}"}
                )
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        assert r.status_code == 200 and r.json()["ok"] is True
    finally:
        watchdog.cancel()
        proc.terminate()
        proc.wait(timeout=15)
