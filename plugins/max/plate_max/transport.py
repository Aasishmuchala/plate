"""Talks to the PLATE daemon over loopback HTTP, exactly as spec/PACKET.md section 9 says.

handshake() is GET /api/v0/health (the version check before Send); post_packet() is POST
/api/v0/packets with the bearer token and X-Plate-Packet-Version. compatible() judges a health
answer (packet version and daemon version). hash_files() / hash_payloads() are POST /api/v0/hash:
Max's Python has no BLAKE3, so the loopback daemon hashes for the plugin (BUILD_PLAN decision 5b).
Standard library urllib only, with proxies switched off: the daemon is 127.0.0.1 and a system proxy
must never see the token. Status handling: 200/202/409/422/426 are returned for the dock to show; 401
raises Unauthorized; a dead or silent daemon raises DaemonUnreachable.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import PACKET_VERSION, PLUGIN_VERSION, SENDER

PASS_THROUGH_STATUS = (200, 202, 409, 422, 426)
HASH_PAYLOAD_LIMIT = 4 * 1024 * 1024  # the daemon's cap on in-memory payloads per request
HASH_TIMEOUT_S = 600.0  # a 400 MB EXR on a NAS takes a while to stream through BLAKE3

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class TransportError(Exception):
    """Base class for transport failures."""


class Unauthorized(TransportError):
    """401: the token in config.json is missing or wrong."""


class HashRefused(TransportError):
    """403 or 404 from /api/v0/hash: a path outside the packets folders, a missing file, or a daemon
    too old to have the route."""

    def __init__(self, status_code: int | None, detail: Any):
        super().__init__(f"daemon refused to hash (HTTP {status_code}): {detail}")
        self.status_code = status_code
        self.detail = detail


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


def compatible(health: dict[str, Any]) -> tuple[bool, str]:
    """(ok, reason) for a /api/v0/health answer (or handshake()'s result, which has the same keys).
    The daemon must read this plugin's packet version ("0.1" in packet_versions) and be the same
    major.minor release as the plugin; reason is empty when ok."""
    if not isinstance(health, dict):
        return False, "no health answer from the daemon"
    if "ok" in health and not health["ok"]:
        return False, "daemon reports ok=false"
    versions = health.get("packet_versions")
    if not isinstance(versions, list):
        return False, "health response has no packet_versions list"
    wanted = major_minor(PACKET_VERSION)
    if wanted not in {major_minor(str(v)) for v in versions}:
        return False, f"daemon reads packet versions {versions}, this plugin writes {wanted}"
    daemon_version = health.get("daemon_version")
    if not daemon_version:
        return False, "health response has no daemon_version"
    if major_minor(str(daemon_version)) != major_minor(PLUGIN_VERSION):
        return False, (
            f"daemon {daemon_version} and plugin {PLUGIN_VERSION} are different releases "
            f"({major_minor(str(daemon_version))} vs {major_minor(PLUGIN_VERSION)}): update the older one"
        )
    return True, ""


def _post_hash(body: dict[str, Any], config: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    token = config.get("token")
    if not token:
        raise Unauthorized("no token in config.json: run the installer or start the daemon once")
    req = urllib.request.Request(
        _url(config.get("daemon_url") or "http://127.0.0.1:4610", "/api/v0/hash"),
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    status, answer = _request(req, timeout_s)
    if status == 401:
        raise Unauthorized(f"daemon rejected the token ({answer})")
    if status in (403, 404):
        raise HashRefused(status, answer)
    if status != 200 or not isinstance(answer, dict) or not isinstance(answer.get("hashes"), dict):
        raise UnexpectedStatus(status, answer)
    return answer["hashes"]


def _pick(hashes: dict[str, Any], key: str, alt: str | None = None) -> dict[str, Any]:
    entry = hashes.get(key)
    if entry is None and alt is not None:
        entry = hashes.get(alt)
    if not isinstance(entry, dict) or not entry.get("hex"):
        raise HashRefused(None, f"daemon returned no hash for {key!r}")
    return entry


def hash_files(
    paths: Sequence[Path | str], config: dict[str, Any], timeout_s: float = HASH_TIMEOUT_S
) -> dict[str, dict[str, Any]]:
    """POST /api/v0/hash {"paths": [...]}: {str(path): {"algo": "blake3", "hex", "bytes"}} for every
    path, which must sit inside the daemon's packets folders. 401 raises Unauthorized, 403/404 raise
    HashRefused; a path the answer leaves out is HashRefused too."""
    sent = [os.path.abspath(str(p)) for p in paths]
    if not sent:
        return {}
    hashes = _post_hash({"paths": sent}, config, timeout_s)
    return {
        str(original): _pick(hashes, path, str(Path(path).resolve()))
        for original, path in zip(paths, sent, strict=True)
    }


def hash_payloads(
    payloads: Mapping[str, bytes], config: dict[str, Any], timeout_s: float = HASH_TIMEOUT_S
) -> dict[str, dict[str, Any]]:
    """POST /api/v0/hash {"payloads": {name: base64}}: the same answer for in-memory bytes (the
    scene and view digests). At most 4 MiB in total, the daemon's cap."""
    if not payloads:
        return {}
    if sum(len(v) for v in payloads.values()) > HASH_PAYLOAD_LIMIT:
        raise ValueError(f"hash payloads are over the {HASH_PAYLOAD_LIMIT // (1024 * 1024)} MiB limit")
    body = {"payloads": {name: base64.b64encode(data).decode("ascii") for name, data in payloads.items()}}
    hashes = _post_hash(body, config, timeout_s)
    return {name: _pick(hashes, name) for name in payloads}


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
