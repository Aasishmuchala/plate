"""Wave 12 stress fixes in the API shell (router plan T132), each test the attacker's repro.

- A route that lets an `OSError`, `ProjectError`, `IllegalTransition`, `sqlite3.OperationalError` or a NUL-byte
  `ValueError` escape answers typed JSON (422 `filesystem` / `project_unreadable` / `bad_path`, 409, 503 `busy`,
  507 `disk_full`), and anything else a 500 `internal` that names the class and never carries the traceback.
- `BodyCap`: a JSON body above `PLATE_JSON_MAX_MB` is 413 `too_large` before the handler runs, a chunked write
  is 411 `length_required`, and the assets and hash routes keep their own caps.
- `HEAD /api/v0/health` is 200; HEAD, OPTIONS and TRACE on an unknown path, `/api` and `/api/` are the JSON 404.
- `check_project_id` uses `fullmatch` (`"nl1\\n"` is not a slug); a 255+ character doc name is the documented 404.

The tests that need a take run the real pipeline on `fixtures/packets/room_a` with the seed fake engine, as
`test_stress_wave12_routes.py` does.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from plate_daemon import config
from plate_daemon.api import API_METHODS, BodyCap, create_app
from plate_daemon.config import ConfigError, Settings, check_project_id, json_max_bytes
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.machine import IllegalTransition
from plate_daemon.projects import ProjectError
from plate_daemon.routes import require_token
from plate_daemon.routes.docs import resolve_doc
from plate_daemon.states import RunState

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
MIB = 1024 * 1024

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="a POSIX path or errno")


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent, so the take passes its gates."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        return self.sent[result_url[len(RESULT_SCHEME) :]]


def _client(settings: Settings, **kw) -> TestClient:
    kw.setdefault("raise_server_exceptions", False)  # a 500 is the answer under test, not a test error
    c = TestClient(create_app(settings, EchoEngine()), base_url=BASE, **kw)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


@pytest.fixture
def api(plate_env, monkeypatch):
    """(client, auth headers) on a daemon whose packets ingest inline."""
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    with _client(settings) as c:
        yield c, {"Authorization": f"Bearer {settings.token}"}


def _project(c: TestClient, auth: dict, project_id: str = PROJECT, **extra) -> None:
    body = {"project_id": project_id, "custody": {"nda": False}, **extra}
    r = c.post("/api/v0/projects", json=body, headers=auth)
    assert r.status_code == 201, r.text


def _take(c: TestClient, auth: dict) -> str:
    """Project + room_a + a graph of Plate -> Edit `e`, one auto-approved take run to DONE; its run id."""
    _project(c, auth)
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    graph = {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": r.json()["packet_id"], "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth)
    assert r.status_code == 200, r.text
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "DONE"
    return run_id


def _blocker(tmp_path: Path) -> Path:
    """A regular file, so that any path below it fails to open on every platform."""
    f = tmp_path / "blocker.txt"
    f.write_text("not a folder\n", encoding="utf-8", newline="\n")
    return f


# ---------------------------------------------------------------------------- OSError


@posix_only
def test_project_root_in_a_place_it_cannot_be_made_is_422_filesystem(api):
    c, auth = api
    if os.access("/etc", os.W_OK):  # a root shell can make it: nothing to refuse
        pytest.skip("/etc is writable here")
    r = c.post(
        "/api/v0/projects",
        json={"project_id": "etc-root", "custody": {"nda": False}, "root": "/etc/plate_x"},
        headers=auth,
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "filesystem" and body["errno"] in ("EACCES", "EPERM", "EROFS"), body
    assert "plate_x" in body["detail"]
    assert r.headers["content-type"].startswith("application/json")


def test_project_root_below_a_file_is_422_filesystem(api, tmp_path):
    c, auth = api
    r = c.post(
        "/api/v0/projects",
        json={"project_id": "below-file", "custody": {"nda": False}, "root": str(_blocker(tmp_path) / "p")},
        headers=auth,
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "filesystem" and r.json()["errno"] != "unknown", r.json()


@needs_room_a
@posix_only
def test_export_into_dev_null_is_a_json_422_not_text_plain(api):
    c, auth = api
    run_id = _take(c, auth)
    r = c.post(
        f"/api/v0/projects/{PROJECT}/export",
        json={"run_ids": [run_id], "out_dir": "/dev/null/x", "full_frame": True},
        headers=auth,
    )
    assert r.headers["content-type"].startswith("application/json"), r.text
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "filesystem" and r.json()["errno"] == "ENOTDIR", r.json()
    r = c.post(
        f"/api/v0/runs/{run_id}/export", json={"dest": "/dev/null/x/r.png", "full_frame": True}, headers=auth
    )
    assert r.status_code == 422 and r.json()["error"] == "filesystem", r.text


@needs_room_a
def test_export_below_a_file_is_a_json_422(api, tmp_path):
    c, auth = api
    run_id = _take(c, auth)
    below = str(_blocker(tmp_path) / "x")
    r = c.post(
        f"/api/v0/projects/{PROJECT}/export",
        json={"run_ids": [run_id], "out_dir": below, "full_frame": True},
        headers=auth,
    )
    assert r.status_code == 422, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["error"] == "filesystem", r.json()


@needs_room_a
def test_deliver_out_dir_with_a_nul_is_422_bad_path(api, tmp_path):
    c, auth = api
    run_id = _take(c, auth)
    out = str(tmp_path / "nul") + "\x00more"
    r = c.post(
        f"/api/v0/runs/{run_id}/deliver",
        json={"format": "exr_sequence", "out_dir": out, "full_frame": True},
        headers=auth,
    )
    assert r.status_code == 422, r.text
    assert r.json() == {"error": "bad_path", "detail": "a path cannot contain NUL"}


@posix_only
def test_a_packet_path_with_a_300_character_component_is_422_filesystem(api):
    c, auth = api
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8")) if ROOM_A.is_dir() else {}
    hdr = {**auth, "X-Plate-Packet-Version": manifest.get("packet_version", "0.1.1")}
    r = c.post(
        "/api/v0/packets",
        json={"packet_path": str(ROOM_A.parent / ("a" * 300)), "sender": {"name": "test", "version": "0"}},
        headers=hdr,
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "filesystem" and r.json()["errno"] == "ENAMETOOLONG", r.json()


# ---------------------------------------------------------------------------- the handler table


def _raising(c: TestClient, exc: BaseException, path: str = "/api/v0/__boom") -> None:
    """A token-gated route, put ahead of the catch-all on the live app, that raises `exc`."""

    def boom():
        raise exc

    route = APIRoute(path, boom, methods=["GET"], dependencies=[Depends(require_token)])
    c.app.router.routes.insert(0, route)


@pytest.mark.parametrize(
    ("exc", "status", "error"),
    [
        (sqlite3.OperationalError("database is locked"), 503, "busy"),
        (sqlite3.OperationalError("database or disk is full"), 507, "disk_full"),
        (OSError(28, "No space left on device"), 507, "disk_full"),
        (PermissionError(13, "Permission denied", "C-x"), 422, "filesystem"),
        (ProjectError("project.json: truncated"), 422, "project_unreadable"),
        (ValueError("embedded null byte"), 422, "bad_path"),
        (ValueError("embedded null character"), 422, "bad_path"),
        (RuntimeError("secret detail at /home/x"), 500, "internal"),
        (ValueError("an ordinary value error"), 500, "internal"),
        (KeyError("k"), 500, "internal"),
    ],
)
def test_handler_table(api, exc, status, error):
    c, auth = api
    _raising(c, exc)
    r = c.get("/api/v0/__boom", headers=auth)
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["error"] == error, body
    assert r.headers["x-content-type-options"] == "nosniff"
    if error == "internal":
        assert body == {"error": "internal", "exception": type(exc).__name__}
        assert "secret" not in r.text and "Traceback" not in r.text
    if error == "busy":
        assert body["retry"] is True and "locked" in body["detail"]
    if error == "filesystem":
        assert body["errno"] == "EACCES" and body["detail"] == "Permission denied: C-x"


def test_illegal_transition_is_409(api):
    c, auth = api
    _raising(c, IllegalTransition(RunState.DONE, RunState.QUEUED))
    r = c.get("/api/v0/__boom", headers=auth)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"] == "illegal_transition" and body["from"] == "DONE" and body["to"] == "QUEUED", body


def test_an_unhandled_error_is_still_raised_to_the_server_for_its_log(plate_env):
    settings = Settings.load()
    auth = {"Authorization": f"Bearer {settings.token}"}
    with _client(settings, raise_server_exceptions=True) as c:
        _raising(c, RuntimeError("logged by the server"))
        with pytest.raises(RuntimeError, match="logged by the server"):
            c.get("/api/v0/__boom", headers=auth)


def test_the_error_shapes_leave_validation_and_api_errors_alone(api):
    c, auth = api
    r = c.post("/api/v0/projects", json={"project_id": "x"}, headers=auth)  # missing custody
    assert r.status_code == 422 and "errors" in r.json()
    r = c.get("/api/v0/projects/nope/graph", headers=auth)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


# ---------------------------------------------------------------------------- the body cap


def test_json_max_bytes_reads_the_environment():
    assert json_max_bytes({}) == config.JSON_MAX_BYTES == 8 * MIB
    assert json_max_bytes({"PLATE_JSON_MAX_MB": "2"}) == 2 * MIB
    assert json_max_bytes({"PLATE_JSON_MAX_MB": "0.5"}) == MIB // 2
    for bad in ("zero", "0", "-3", "nan", "inf"):
        with pytest.raises(ConfigError):
            json_max_bytes({"PLATE_JSON_MAX_MB": bad})


def test_settings_load_carries_the_cap(plate_env, monkeypatch):
    assert Settings.load().json_max_bytes == 8 * MIB
    monkeypatch.setenv("PLATE_JSON_MAX_MB", "3")
    assert Settings.load().json_max_bytes == 3 * MIB


def test_a_50_mib_graph_put_is_413_before_the_handler_runs(api, monkeypatch):
    c, auth = api
    _project(c, auth)
    from plate_daemon.routes import projects as project_routes

    called = []
    monkeypatch.setattr(project_routes, "_project", lambda *a, **k: called.append(a) or None)
    body = b'{"nodes": ["' + b"a" * (50 * MIB) + b'"], "edges": []}'
    r = c.put(
        f"/api/v0/projects/{PROJECT}/graph",
        content=body,
        headers={**auth, "Content-Type": "application/json"},
    )
    assert r.status_code == 413, r.text[:200]
    assert r.json() == {"error": "too_large", "limit_bytes": 8 * MIB}
    assert r.headers["x-content-type-options"] == "nosniff"
    assert not called, "the handler ran"


def test_the_cap_follows_the_setting(plate_env, monkeypatch):
    monkeypatch.setenv("PLATE_JSON_MAX_MB", "0.001")  # 1048 bytes
    settings = Settings.load()
    auth = {"Authorization": f"Bearer {settings.token}"}
    with _client(settings) as c:
        small = c.post(
            "/api/v0/projects", json={"project_id": "small", "custody": {"nda": False}}, headers=auth
        )
        assert small.status_code == 201, small.text
        r = c.post(
            "/api/v0/projects",
            json={"project_id": "big", "custody": {"nda": False, "pad": "x" * 2000}},
            headers=auth,
        )
        assert r.status_code == 413 and r.json() == {"error": "too_large", "limit_bytes": 1048}


def test_the_cap_answers_before_the_token_check(api):
    c, _auth = api
    r = c.post("/api/v0/projects", content=b"x" * (9 * MIB), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_a_chunked_write_is_411_length_required(api):
    c, auth = api
    for method in ("post", "put", "patch"):
        r = getattr(c, method)(
            "/api/v0/projects",
            content=iter([b'{"project_id": "chunked", ', b'"custody": {"nda": false}}']),
            headers={**auth, "Content-Type": "application/json"},
        )
        assert r.status_code == 411, (method, r.text)
        assert r.json() == {"error": "length_required"}


def test_a_chunked_get_and_a_bodiless_post_pass_the_cap(api):
    c, auth = api
    assert c.get("/api/v0/session", headers=auth).status_code == 200
    r = c.request("GET", "/api/v0/session", content=iter([b"x"]), headers=auth)  # only writes need a length
    assert r.status_code == 200, r.text
    r = c.post("/api/v0/__nothing", headers=auth)  # no body, no length: not the cap's business
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_the_assets_and_hash_routes_keep_their_own_caps():
    assert not BodyCap.covers("/api/v0/assets")
    assert not BodyCap.covers("/api/v0/assets/resolve")
    assert not BodyCap.covers("/api/v0/hash")
    assert BodyCap.covers("/api/v0/projects")
    assert BodyCap.covers("/api/v0/packets")
    assert not BodyCap.covers("/")  # the UI mount is not the API


def test_a_big_hash_post_is_not_the_json_cap(api):
    c, auth = api
    r = c.post(
        "/api/v0/hash",
        content=b'{"paths": [], "payloads": {"a": "' + b"x" * (9 * MIB) + b'"}}',
        headers={**auth, "Content-Type": "application/json"},
    )
    assert r.json().get("error") != "too_large", r.text[:200]


# ---------------------------------------------------------------------------- methods and unknown paths


def test_head_health_is_200(api):
    c, _auth = api
    assert c.head("/api/v0/health").status_code == 200
    assert c.get("/api/v0/health").status_code == 200


def test_api_methods_cover_head_options_and_trace():
    assert {"HEAD", "OPTIONS", "TRACE"} <= set(API_METHODS)


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
@pytest.mark.parametrize("path", ["/api/v0/x", "/api", "/api/", "/api/v0"])
def test_every_method_on_an_unknown_path_is_the_json_404(api, method, path):
    c, auth = api
    r = c.request(method, path, headers=auth)
    assert r.status_code == 404, (method, path, r.status_code)
    assert r.headers["content-type"].startswith("application/json")
    if method != "HEAD":
        body = r.json()
        assert body["error"] == "not_found" and body["detail"].startswith("no route /api/")


def test_trace_is_404_json(api):
    c, auth = api
    r = c.request("TRACE", "/api/v0/x", headers=auth)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


# ---------------------------------------------------------------------------- ids and docs


@pytest.mark.parametrize("bad", ["nl1\n", "a\n", "x\n\n", "", "A", "-a", "a" * 65, "a b", "a/b", "a\x00"])
def test_check_project_id_uses_fullmatch(bad):
    with pytest.raises(ValueError):
        check_project_id(bad)


def test_check_project_id_accepts_slugs():
    for good in ("a", "plate-fixture", "p_1", "0x", "a" * 64):
        assert check_project_id(good) == good


def test_a_project_id_with_a_trailing_newline_is_422(api):
    c, auth = api
    r = c.post("/api/v0/projects", json={"project_id": "nl1\n", "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 422, r.text
    assert c.get("/api/v0/projects", headers=auth).json() == []


def test_a_260_character_doc_name_is_the_404(api):
    c, auth = api
    r = c.get(f"/api/v0/docs/{'a' * 260}.md", headers=auth)
    assert r.status_code == 404, r.text
    assert r.json()["error"] == "not_found"
    assert resolve_doc("a" * 260 + ".md") is None
    assert resolve_doc("/".join(["d" * 300, "x.md"])) is None


# ---------------------------------------------------------------------------- an unreadable project


def test_a_truncated_project_json_is_422_on_its_graph_and_the_list_still_answers(api):
    c, auth = api
    _project(c, auth, "good-one")
    _project(c, auth, "cut-short")
    home = Settings.load().projects_dir
    pj = home / "cut-short" / "project.json"
    text = pj.read_text(encoding="utf-8")
    pj.write_text(text[: len(text) // 2], encoding="utf-8", newline="\n")
    r = c.get("/api/v0/projects/cut-short/graph", headers=auth)
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "project_unreadable" and r.json()["detail"]
    r = c.get("/api/v0/projects", headers=auth)
    assert r.status_code == 200, r.text
    assert "good-one" in [p["project_id"] for p in r.json()]
