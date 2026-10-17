"""AtlasCloud client contract: upload, image generation, prediction polling, allowlisted download.

Ported from ~/Desktop/staffage/src/lib/atlas.ts (behaviours, not the fetch code) so every quirk of that file
is pinned by a contract test that runs against an injected Transport; nothing here opens a socket. A real
transport (httpx) is added with the adapters; tests use FakeTransport.

CUSTODY: uploadMedia returns public Alibaba OSS URLs, so every Atlas route is grade D and the router refuses
Atlas for NDA projects. Nothing in this module changes that.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlparse

ATLAS_BASE = "https://api.atlascloud.ai/api/v1"
RESULT_HOSTS = (".aliyuncs.com", ".atlascloud.ai")


class AtlasError(RuntimeError):
    """An Atlas call failed."""


class AtlasTerminal(AtlasError):
    """The prediction ended in a failed, error or cancelled status."""


class AtlasTimeout(AtlasError):
    """The prediction did not produce an output before the deadline."""


@dataclass(frozen=True)
class TransportResponse:
    status: int
    json: dict | None = None
    body: bytes = b""


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        json: dict | None = None,
        files: dict | None = None,
    ) -> TransportResponse: ...


@dataclass(frozen=True)
class RecordedCall:
    method: str
    url: str
    headers: dict[str, str]
    json: dict | None
    files: dict | None


@dataclass
class FakeTransport:
    """Scripted transport: a list of responses consumed in order, or a callable taking a RecordedCall."""

    script: list[TransportResponse] | Callable[[RecordedCall], TransportResponse]
    calls: list[RecordedCall] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        json: dict | None = None,
        files: dict | None = None,
    ) -> TransportResponse:
        call = RecordedCall(method, url, dict(headers or {}), json, files)
        self.calls.append(call)
        if callable(self.script):
            return self.script(call)
        if not self.script:
            raise AssertionError(f"FakeTransport script exhausted at {method} {url}")
        return self.script.pop(0)


def _dump(value: Any) -> str:
    """JSON.stringify(value).slice(0, 300): compact separators, no ASCII escaping."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)[:300]


def _dig(obj: Any, *keys: str) -> Any:
    """Optional chaining: None as soon as a step is missing or not a mapping."""
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _coalesce(*values: Any) -> Any:
    """JS `??`: the first value that is not None."""
    for v in values:
        if v is not None:
            return v
    return None


def _body(resp: TransportResponse) -> dict:
    """res.json().catch(() => ({})): anything that is not a JSON object becomes {}."""
    return resp.json if isinstance(resp.json, dict) else {}


def _ok(resp: TransportResponse) -> bool:
    return 200 <= resp.status < 300


def _atomic_write(dest: Path, data: bytes, sleep: Callable[[float], None]) -> None:
    """Temp file beside dest, then os.replace with retry + backoff (5 tries, 50 ms doubling)."""
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix="atlas-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        delay = 0.05
        for attempt in range(5):
            try:
                os.replace(tmp, dest)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                sleep(delay)
                delay *= 2
    finally:
        tmp.unlink(missing_ok=True)


class AtlasClient:
    def __init__(
        self,
        transport: Transport,
        key_env: str = "ATLASCLOUD_KEY",
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._key_env = key_env
        self._sleep = sleep
        self._now = now

    def key(self) -> str:
        """atlas.ts:11-15. An empty value counts as missing."""
        k = os.environ.get(self._key_env)
        if not k:
            raise AtlasError(f"{self._key_env} missing — add it to the environment")
        return k

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key()}"}

    def upload(self, path: Path | str, mime: str = "application/octet-stream") -> str:
        """POST /model/uploadMedia (multipart, no Content-Type header); atlas.ts:17-30."""
        p = Path(path)
        buf = p.read_bytes()
        resp = self._transport.request(
            "POST",
            f"{ATLAS_BASE}/model/uploadMedia",
            headers=self._auth(),
            files={"file": (p.name, buf, mime)},
        )
        data = _body(resp)
        url = _coalesce(_dig(data, "data", "download_url"), _dig(data, "download_url"))
        if not _ok(resp) or not url:
            raise AtlasError(f"uploadMedia failed: {resp.status} {_dump(data)}")
        return str(url)

    def generate_image(self, body: dict) -> str:
        """POST /model/generateImage (JSON); returns the prediction id; atlas.ts:32-42."""
        resp = self._transport.request(
            "POST",
            f"{ATLAS_BASE}/model/generateImage",
            headers={**self._auth(), "Content-Type": "application/json"},
            json=body,
        )
        data = _body(resp)
        pid = _coalesce(_dig(data, "data", "id"), _dig(data, "id"))
        if not _ok(resp) or not pid:
            raise AtlasError(f"generateImage failed: {resp.status} {_dump(data)}")
        return str(pid)

    def poll(self, prediction_id: str, timeout_s: float = 120.0, interval_s: float = 3.0) -> str:
        """Sleep, then GET /model/prediction/{id}, until an output URL, a terminal status or the deadline.

        Quirks kept from atlas.ts:44-61: the sleep comes before the first GET; the HTTP status is never
        checked (an unparsable or error body just means "keep polling"); the payload may sit under `data`
        or at the top level; outputs[0] wins over a failure status; a status containing fail, error or
        cancel is terminal. The key is read once up front, so a missing key fails before any sleep.
        """
        headers = self._auth()
        url = f"{ATLAS_BASE}/model/prediction/{quote(str(prediction_id), safe='')}"
        deadline = self._now() + timeout_s
        while self._now() < deadline:
            self._sleep(interval_s)
            data = _body(self._transport.request("GET", url, headers=headers))
            inner = data.get("data")
            d = inner if inner is not None else data
            if not isinstance(d, dict):
                d = {}
            outputs = d.get("outputs")
            out = outputs[0] if isinstance(outputs, list) and outputs else None
            if isinstance(out, str) and out.startswith("http"):
                return out
            status = str(_coalesce(d.get("status"), "")).lower()
            if "fail" in status or "error" in status or "cancel" in status:
                raise AtlasTerminal(f"generation {status}: {_dump(d)}")
        raise AtlasTimeout(f"prediction {prediction_id} timed out")

    def download(self, url: str, dest: Path | str) -> Path:
        """GET an allowlisted result URL to dest; atlas.ts:63-71.

        The host check runs before any request. The request deliberately carries NO Authorization header
        (atlas.ts sends none): the URL points at a third-party OSS host and the key must not follow it.
        """
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            host = ""
        if not any(host.endswith(suffix) for suffix in RESULT_HOSTS):
            raise AtlasError(f"result host {host} not allowlisted")
        resp = self._transport.request("GET", url)
        if not _ok(resp):
            raise AtlasError(f"download failed: {resp.status}")
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(out, resp.body, self._sleep)
        return out
