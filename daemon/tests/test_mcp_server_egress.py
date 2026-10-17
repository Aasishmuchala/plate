"""The MCP server's daemon client is loopback-only (T80): built through the engines' `make_client`, pinned to
one `127.0.0.1`/`localhost` host and port, never following a redirect to anywhere else."""

from __future__ import annotations

import httpx
import pytest

from plate_daemon import mcp_server
from plate_daemon.config import Settings
from plate_daemon.mcp_server import LoopbackOnlyTransport, McpError, PlateBackend, default_client


class Recorder(httpx.BaseTransport):
    """The network leg: records every request that got past the loopback pin and answers with `reply`."""

    def __init__(self, reply=None):
        self.seen: list[httpx.Request] = []
        self.reply = reply or (lambda request: httpx.Response(200, json={"ok": True}))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.reply(request)


@pytest.fixture
def settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.delenv("PLATE_DAEMON_URL", raising=False)
    return Settings.load()


def test_default_client_targets_the_daemon_loopback_port_with_the_install_token(settings):
    net = Recorder()
    client = default_client(settings, transport=net)
    assert client.get("/api/v0/projects").status_code == 200
    (req,) = net.seen
    assert req.url.host in mcp_server.LOOPBACK_HOSTS
    assert req.url.port == settings.port
    assert req.headers["authorization"] == f"Bearer {settings.token}"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_both_loopback_names_work(settings, host):
    net = Recorder()
    client = default_client(settings, url=f"http://{host}:{settings.port}", transport=net)
    client.get("/api/v0/projects")
    assert net.seen[0].url.host == host


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:4610",
        "https://api.anthropic.com",
        "http://192.168.1.20:4610",
        "http://100.64.0.7:4610",
        "http://0.0.0.0:4610",
    ],
)
def test_a_non_loopback_daemon_url_is_refused_before_a_client_exists(settings, url):
    with pytest.raises(McpError) as e:
        default_client(settings, url=url)
    assert e.value.code == "not_loopback"


def test_the_env_url_is_held_to_the_same_rule(settings, monkeypatch):
    monkeypatch.setenv("PLATE_DAEMON_URL", "http://example.com:4610")
    with pytest.raises(McpError):
        default_client(settings)


def test_the_client_cannot_be_aimed_or_redirected_elsewhere(settings):
    net = Recorder()
    client = default_client(settings, transport=net)
    for target in (
        "http://example.com:4610/api/v0/projects",
        "http://127.0.0.1:9/api/v0/projects",  # loopback, wrong port
        f"http://192.168.1.20:{settings.port}/api/v0/projects",
        "https://api.anthropic.com/v1/messages",
    ):
        with pytest.raises(httpx.UnsupportedProtocol):
            client.get(target)
    assert net.seen == []  # nothing got past the pin


def test_a_redirect_off_loopback_is_not_followed(settings):
    net = Recorder(lambda r: httpx.Response(302, headers={"location": "http://example.com/steal"}))
    client = default_client(settings, transport=net)
    resp = client.get("/api/v0/projects")
    assert resp.status_code == 302 and client.follow_redirects is False
    assert [r.url.host for r in net.seen] == ["127.0.0.1"]


def test_environment_proxies_are_ignored(settings, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example:3128")
    net = Recorder()
    default_client(settings, transport=net).get("/api/v0/projects")
    assert [r.url.host for r in net.seen] == ["127.0.0.1"]


def test_the_backend_reports_a_refused_target_as_daemon_unreachable(settings):
    net = Recorder()
    client = default_client(settings, transport=net)
    assert PlateBackend(client).call("GET", "/projects") == {"ok": True}
    client.base_url = httpx.URL("http://example.com:4610")
    with pytest.raises(McpError) as e:
        PlateBackend(client).call("GET", "/projects")
    assert e.value.code == "daemon_unreachable"
    assert len(net.seen) == 1


def test_the_transport_pin_is_by_port_not_just_host():
    net = Recorder()
    pinned = LoopbackOnlyTransport(net, 4610)
    ok = httpx.Request("GET", "http://localhost:4610/x")
    assert pinned.handle_request(ok).status_code == 200
    with pytest.raises(httpx.UnsupportedProtocol):
        pinned.handle_request(httpx.Request("GET", "http://localhost:4611/x"))
    with pytest.raises(httpx.UnsupportedProtocol):
        pinned.handle_request(httpx.Request("GET", "http://[::2]:4610/x"))
    assert len(net.seen) == 1


def test_serving_with_a_non_loopback_daemon_url_stops_at_start(settings, capsys, monkeypatch):
    served = []
    monkeypatch.setattr(mcp_server, "build_server", lambda factory: served.append(factory))
    assert mcp_server.main(["--daemon-url", "http://example.com:4610"]) == 2
    assert "127.0.0.1" in capsys.readouterr().err and served == []
