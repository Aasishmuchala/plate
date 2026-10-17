"""AtlasCloud client contract: upload, image generation, prediction polling, allowlisted download.

Ported from ~/Desktop/staffage/src/lib/atlas.ts (behaviours, not the fetch code) so every quirk of that file
is pinned by a contract test that runs against an injected Transport; nothing here opens a socket. Tests use
FakeTransport; adapters reach the client through adapters.transport.HttpxAtlasTransport (httpx, so recorded
fixtures and the host allowlist apply). Adapters see the client as adapters.base.AtlasClient (a re-export).

CUSTODY: uploadMedia returns public Alibaba OSS URLs, so every Atlas route is grade D and the router refuses
Atlas for NDA projects. Nothing in this module changes that.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlparse

ATLAS_BASE = "https://api.atlascloud.ai/api/v1"
RESULT_HOSTS = (".aliyuncs.com", ".atlascloud.ai")

# Polling cadence (atlas.ts / personJobs.ts): images every 3 s for at most 120 s, video every 5 s for at most 12 min.
IMAGE_POLL_INTERVAL_S = 3.0
IMAGE_POLL_TIMEOUT_S = 120.0
VIDEO_POLL_INTERVAL_S = 5.0
VIDEO_POLL_TIMEOUT_S = 12 * 60.0

# Atlas never says "moderated": a failed prediction (or a rejected submit) whose message reads like one is a refusal.
REFUSAL_PATTERN = re.compile(r"(moderation|safety|policy|nsfw|blocked)", re.IGNORECASE)
# Pace words fight an end-frame pin (the model picks the slow pace and never arrives); personJobs.ts buildMotionPrompt.
_PACE_PATTERN = re.compile(r"\b(?:slowly|slow|unhurried|leisurely|gently)\b(?:-\w+)*\s*", re.IGNORECASE)
# Keys whose string values can carry a vendor's reason; deliberately not input / prompt echoes.
_MESSAGE_KEYS = ("status", "message", "msg", "error", "error_message", "reason", "detail", "failure_reason")


class AtlasError(RuntimeError):
    """An Atlas call failed."""


class AtlasTerminal(AtlasError):
    """The prediction ended in a failed, error or cancelled status."""


class AtlasRefused(AtlasTerminal):
    """Terminal failure whose message reads like moderation / safety / policy (REFUSAL_PATTERN)."""


class AtlasInputError(AtlasError):
    """The request body breaks an Atlas rule; raised client-side and never sent."""


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


def unwrap(data: dict) -> dict:
    """`data.data ?? data`: the payload sits under `data` or at the top level; anything not an object is {}."""
    inner = data.get("data")
    d = inner if inner is not None else data
    return d if isinstance(d, dict) else {}


def failure_text(d: dict) -> str:
    """The vendor's reason as one string: status / message-like fields (and the string values of an `error` object)."""
    parts: list[str] = []
    for key in _MESSAGE_KEYS:
        v = d.get(key)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            parts.extend(x for x in v.values() if isinstance(x, str))
    return " ".join(parts)


def is_refusal(text: str) -> bool:
    return REFUSAL_PATTERN.search(text) is not None


def atlas_resolution(value: str | int) -> str:
    """Atlas wants "480P" / "720P" / "1080P" in upper case; accepts 1080, "1080", "1080p"."""
    m = re.fullmatch(r"(\d{3,4})\s*[pP]?", str(value).strip())
    if m is None or isinstance(value, bool):
        raise AtlasInputError(f"resolution {value!r}: expected something like '1080P'")
    return f"{int(m.group(1))}P"


def atlas_duration(
    seconds: float, lo: int = 1, hi: int = 15, rounding: Literal["ceil", "floor"] = "ceil"
) -> int:
    """Integer seconds clamped to [lo, hi]; ceil so a take outlasts the shot, floor when the source is the limit."""
    n = math.ceil(seconds) if rounding == "ceil" else math.floor(seconds)
    return max(lo, min(hi, n))


def atlas_ratio(width: int, height: int) -> str:
    """personJobs.ts: landscape and square sources are "16:9", portrait ones "9:16"."""
    return "16:9" if width >= height else "9:16"


def strip_pace_words(prompt: str) -> str:
    """Remove slow / slowly / gently / leisurely / unhurried (and "slow-motion" style compounds) from a prompt."""
    return re.sub(r"[ \t]{2,}", " ", _PACE_PATTERN.sub("", prompt)).strip()


def check_image_body(body: Mapping[str, Any]) -> None:
    """generateImage takes `images`, always an array. A lone `image` is silently ignored by the vendor (the model
    free-generates), and an empty body is text-to-image, which PLATE never does: both fail here, before sending."""
    if "image" in body:
        raise AtlasInputError(
            "generateImage takes `images` (an array), never `image`: a lone `image` is ignored"
        )
    images = body.get("images")
    if not isinstance(images, list) or not images or not all(isinstance(u, str) and u for u in images):
        raise AtlasInputError("generateImage needs `images`: a non-empty array of URLs (no text-to-image)")


def prepare_video_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a generateVideo body: integer `duration`, upper-case `resolution`, pace words stripped when an
    `image_tail` pins the end frame. Returns a copy; the caller's dict is not touched."""
    out = dict(body)
    if "duration" in out:
        d = out["duration"]
        if isinstance(d, bool) or not isinstance(d, (int, float)) or (isinstance(d, float) and d != int(d)):
            raise AtlasInputError(f"duration {d!r}: Atlas takes an integer (use atlas_duration to round)")
        out["duration"] = int(d)
    if "resolution" in out:
        out["resolution"] = atlas_resolution(out["resolution"])
    if out.get("image_tail"):
        if not out.get("image"):
            raise AtlasInputError("image_tail pins the end frame and needs `image` as the first frame")
        if isinstance(out.get("prompt"), str):
            out["prompt"] = strip_pace_words(out["prompt"])
    return out


@dataclass(frozen=True)
class AtlasPrediction:
    """One look at /model/prediction/{id}, classified. `outputs` holds the http URLs; `message` the failure text."""

    state: Literal["queued", "running", "done", "failed", "refused"]
    outputs: tuple[str, ...] = ()
    message: str = ""
    status: str = ""


_QUEUED_WORDS = ("queue", "pend", "wait", "creat", "submit")


def classify_prediction(d: dict) -> AtlasPrediction:
    """outputs[0] http string wins over everything; fail / error / cancel in the status is terminal; else running."""
    raw_outputs = d.get("outputs")
    out = raw_outputs[0] if isinstance(raw_outputs, list) and raw_outputs else None
    if isinstance(out, str) and out.startswith("http"):
        urls = tuple(u for u in raw_outputs if isinstance(u, str) and u.startswith("http"))
        return AtlasPrediction("done", urls, status=str(_coalesce(d.get("status"), "")).lower())
    status = str(_coalesce(d.get("status"), "")).lower()
    if "fail" in status or "error" in status or "cancel" in status:
        refused = is_refusal(failure_text(d))
        return AtlasPrediction(
            "refused" if refused else "failed", (), f"generation {status}: {_dump(d)}", status
        )
    state = "queued" if any(w in status for w in _QUEUED_WORDS) else "running"
    return AtlasPrediction(state, (), status=status)


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

    def _submit(self, endpoint: str, label: str, body: dict) -> str:
        resp = self._transport.request(
            "POST",
            f"{ATLAS_BASE}/model/{endpoint}",
            headers={**self._auth(), "Content-Type": "application/json"},
            json=body,
        )
        data = _body(resp)
        pid = _coalesce(_dig(data, "data", "id"), _dig(data, "id"))
        if not _ok(resp) or not pid:
            message = f"{label} failed: {resp.status} {_dump(data)}"
            if not _ok(resp) and is_refusal(f"{failure_text(data)} {failure_text(unwrap(data))}"):
                raise AtlasRefused(message)
            raise AtlasError(message)
        return str(pid)

    def generate_image(self, body: dict) -> str:
        """POST /model/generateImage (JSON); returns the prediction id; atlas.ts:32-42.

        `images` must be an array of URLs: a lone `image` or an empty body raises AtlasInputError before any request.
        """
        check_image_body(body)
        return self._submit("generateImage", "generateImage", body)

    def generate_video(self, body: dict) -> str:
        """POST /model/generateVideo (JSON); returns the prediction id; personJobs.ts startVideoGen.

        The body is normalised first (integer duration, upper-case resolution, pace words stripped under an image_tail).
        """
        return self._submit("generateVideo", "generateVideo", prepare_video_body(body))

    def poll_once(self, prediction_id: str) -> AtlasPrediction:
        """One GET of /model/prediction/{id}, classified, no sleeping. An unparsable body means still running."""
        headers = self._auth()
        url = f"{ATLAS_BASE}/model/prediction/{quote(str(prediction_id), safe='')}"
        return classify_prediction(unwrap(_body(self._transport.request("GET", url, headers=headers))))

    def poll(
        self,
        prediction_id: str,
        timeout_s: float = IMAGE_POLL_TIMEOUT_S,
        interval_s: float = IMAGE_POLL_INTERVAL_S,
    ) -> str:
        """Sleep, then GET /model/prediction/{id}, until an output URL, a terminal status or the deadline.

        Quirks kept from atlas.ts:44-61: the sleep comes before the first GET; the HTTP status is never
        checked (an unparsable or error body just means "keep polling"); the payload may sit under `data`
        or at the top level; outputs[0] wins over a failure status; a status containing fail, error or
        cancel is terminal. The key is read once up front, so a missing key fails before any sleep.
        A terminal message that reads like moderation / safety / policy raises AtlasRefused (an AtlasTerminal).
        """
        headers = self._auth()
        url = f"{ATLAS_BASE}/model/prediction/{quote(str(prediction_id), safe='')}"
        deadline = self._now() + timeout_s
        while self._now() < deadline:
            self._sleep(interval_s)
            pred = classify_prediction(unwrap(_body(self._transport.request("GET", url, headers=headers))))
            if pred.state == "done":
                return pred.outputs[0]
            if pred.state == "refused":
                raise AtlasRefused(pred.message)
            if pred.state == "failed":
                raise AtlasTerminal(pred.message)
        raise AtlasTimeout(f"prediction {prediction_id} timed out")

    def poll_image(self, prediction_id: str) -> str:
        """poll() at the image cadence: 3 s steps, 120 s cap."""
        return self.poll(prediction_id, IMAGE_POLL_TIMEOUT_S, IMAGE_POLL_INTERVAL_S)

    def poll_video(self, prediction_id: str) -> str:
        """poll() at the video cadence: 5 s steps, 12 min cap."""
        return self.poll(prediction_id, VIDEO_POLL_TIMEOUT_S, VIDEO_POLL_INTERVAL_S)

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
