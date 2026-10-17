"""AuthMiddleware: bearer token on the API, Host and Origin allowlists on everything."""

from __future__ import annotations

from fastapi.testclient import TestClient

from plate_daemon import __version__
from plate_daemon.api import create_app
from plate_daemon.config import Settings

BASE = "http://127.0.0.1:4610"


def test_session_needs_the_token(client, auth):
    assert client.get("/api/v0/session").status_code == 401
    r = client.get("/api/v0/session", headers={"Authorization": "Bearer not-the-token"})
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    assert r.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/v0/session", headers={"Authorization": "Basic x"}).status_code == 401
    ok = client.get("/api/v0/session", headers=auth)
    assert ok.status_code == 200
    # T59 added packets_dirs, projects_dir, dev and cas_copy; the original keys must stay.
    assert (
        ok.json().items() >= {"ok": True, "daemon_version": __version__, "packet_versions": ["0.1"]}.items()
    )


def test_seed_routes_are_behind_the_token_and_no_schema_is_served(client, auth):
    assert client.get("/api/v0/runs/run_x").status_code == 401
    assert client.get("/api/v0/runs/run_x", headers=auth).status_code == 404
    assert client.get("/api/v0/openapi.json").status_code == 401
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404


def test_health_is_exempt_from_the_token_but_not_from_host(client):
    assert client.get("/api/v0/health").status_code == 200
    r = client.get("/api/v0/health", headers={"Host": "evil:4610"})
    assert r.status_code == 403 and r.json() == {"error": "forbidden_host"}


def test_foreign_host_is_refused_even_with_the_token(client, auth):
    for host in ("evil:4610", "127.0.0.1:4611", "127.0.0.1", "localhost.evil.example:4610"):
        r = client.get("/api/v0/session", headers={**auth, "Host": host})
        assert r.status_code == 403, host
    assert client.get("/api/v0/session", headers={**auth, "Host": "localhost:4610"}).status_code == 200
    assert client.get("/", headers={"Host": "evil:4610"}).status_code == 403


def test_foreign_origin_is_refused(client, auth):
    r = client.get("/api/v0/session", headers={**auth, "Origin": "http://evil"})
    assert r.status_code == 403 and r.json() == {"error": "forbidden_origin"}
    assert client.get("/api/v0/session", headers={**auth, "Origin": "null"}).status_code == 403
    assert (
        client.get("/api/v0/session", headers={**auth, "Origin": "http://127.0.0.1:4610"}).status_code == 200
    )


def test_vite_dev_origins_need_plate_dev(monkeypatch, auth):
    dev_origins = ("http://localhost:5173", "http://127.0.0.1:5173")
    with TestClient(create_app(Settings.load()), base_url=BASE) as c:
        for origin in dev_origins:
            assert c.get("/api/v0/session", headers={**auth, "Origin": origin}).status_code == 403
    monkeypatch.setenv("PLATE_DEV", "1")
    with TestClient(create_app(Settings.load()), base_url=BASE) as c:
        for origin in dev_origins:
            assert c.get("/api/v0/session", headers={**auth, "Origin": origin}).status_code == 200
        assert (
            c.get("/api/v0/session", headers={**auth, "Origin": "http://localhost:5174"}).status_code == 403
        )


def test_token_file_token_is_the_one_that_works(tmp_path, monkeypatch):
    old = Settings.load().token
    tf = tmp_path / "token.txt"
    tf.write_text("token-from-file-abcdefghijklmnop", encoding="utf-8")
    monkeypatch.setenv("PLATE_TOKEN_FILE", str(tf))
    with TestClient(create_app(Settings.load()), base_url=BASE) as c:
        assert c.get("/api/v0/session", headers={"Authorization": f"Bearer {old}"}).status_code == 401
        good = {"Authorization": "Bearer token-from-file-abcdefghijklmnop"}
        assert c.get("/api/v0/session", headers=good).status_code == 200


def test_query_token_only_on_the_events_route(client, settings):
    assert client.get(f"/api/v0/session?token={settings.token}").status_code == 401
    # The events route accepts it (and then 404s on the unknown project rather than 401).
    r = client.get(f"/api/v0/events?project_id=nope&token={settings.token}")
    assert r.status_code == 404
    assert client.get("/api/v0/events?project_id=nope&token=wrong").status_code == 401
