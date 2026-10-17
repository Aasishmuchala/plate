"""Talks to the PLATE daemon over loopback HTTP, exactly as spec/PACKET.md section 9 says.

handshake() is GET /api/v0/health (the version check before Send); post_packet() is POST
/api/v0/packets with the bearer token and X-Plate-Packet-Version. Standard library urllib only,
with proxies switched off: the daemon is 127.0.0.1 and a system proxy must never see the token.
Status handling: 200/202/409/422/426 are returned for the dock to show; 401 raises Unauthorized;
a dead or silent daemon raises DaemonUnreachable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import PACKET_VERSION, SENDER

PASS_THROUGH_STATUS = (200, 202, 409, 422, 426)

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class TransportError(Exception):
    """Base class for transport failures."""


class Unauthorized(TransportError):
    """401: the token in config.json is missing or wrong."""


class DaemonUnreachable(TransportError):
    """Nothing answered on the daemon URL (not running, wrong port, firewall, timeout)."""


class UnexpectedStatus(TransportError):
    def __init__(self, status_code: int, body: Any):
        super().__init__(f"daemon answered HTTP {status_code}: {body}")
        self.status_code = status_code
        self.body = body


def major_minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text[:300]}


def _request(req: urllib.request.Request, timeout_s: float) -> tuple[int, Any]:
    try:
        with _opener.open(req, timeout=timeout_s) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a JSON body
        try:
            return exc.code, _decode(exc.read())
        finally:
            exc.close()
    except OSError as exc:  # URLError, timeouts and connection errors are all OSError
        raise DaemonUnreachable(f"{req.full_url}: {exc}") from exc


def _url(daemon_url: str, path: str) -> str:
    return daemon_url.rstrip("/") + path


def handshake(daemon_url: str, timeout_s: float = 3.0) -> dict[str, Any]:
    """GET /api/v0/health. problems is empty when this plugin's packet version is one the daemon reads."""
    status, body = _request(urllib.request.Request(_url(daemon_url, "/api/v0/health")), timeout_s)
    problems: list[str] = []
    versions = body.get("packet_versions") if isinstance(body, dict) else None
    if status != 200 or not isinstance(body, dict):
        problems.append(f"health check answered HTTP {status}")
    elif not body.get("ok"):
        problems.append("daemon reports ok=false")
    if isinstance(versions, list):
        if major_minor(PACKET_VERSION) not in versions:
            problems.append(f"{major_minor(PACKET_VERSION)} not in packet_versions {versions}")
    elif status == 200:
        problems.append("health response has no packet_versions list")
    info = body if isinstance(body, dict) else {}
    return {
        "ok": bool(info.get("ok")) and not problems,
        "daemon_version": info.get("daemon_version"),
        "packet_versions": versions if isinstance(versions, list) else [],
        "problems": problems,
    }


def post_packet(packet_path: Path | str, config: dict[str, Any], timeout_s: float = 10.0) -> dict[str, Any]:
    """POST /api/v0/packets. Returns {"status_code", "body"} for 200/202/409/422/426."""
    token = config.get("token")
    if not token:
        raise Unauthorized("no token in config.json: run the installer or start the daemon once")
    payload = json.dumps({"packet_path": str(Path(packet_path).resolve()), "sender": dict(SENDER)})
    req = urllib.request.Request(
        _url(config.get("daemon_url") or "http://127.0.0.1:4610", "/api/v0/packets"),
        data=payload.encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Plate-Packet-Version": PACKET_VERSION,
            "Content-Type": "application/json",
        },
    )
    status, body = _request(req, timeout_s)
    if status == 401:
        raise Unauthorized(f"daemon rejected the token ({body})")
    if status not in PASS_THROUGH_STATUS:
        raise UnexpectedStatus(status, body)
    return {"status_code": status, "body": body}
