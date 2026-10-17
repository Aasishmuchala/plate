"""The Anthropic client the composer and the judge share: custody first, recorded fixtures by default.

Order of every call (BUILD_PLAN decision 8, router Decision 5a):

1. custody: `custody.llm_route()` is read on every call and its effective grade (`pending_written_terms`
   counts as D) must be in the run's allowed grades, else `CustodyViolation`. The transport is not touched, so
   an NDA project makes zero calls. Callers check `AnthropicClient.available` first and degrade; this is the
   backstop.
2. transport: `FixtureTransport` replays `<key>.json` recordings and raises `UnrecordedPrompt` for anything it
   has not seen, so no test and no default run can reach the API. `LiveTransport` exists only under
   `PLATE_LIVE=1` with a budget `Envelope`, and only over an `httpx.Client` the caller got from
   `Egress.client(run, route)` (so the egress guard, leak scan and outbound log see every request).
3. ledger: one `done` record per call with `engine_id: 'anthropic:<model>'`, the cost estimated from `usage` and
   `prices.yaml`, under a run id of its own (`llm_<hex>`) so a composer call never changes the quote of the
   generation run it served; the parent run is in `data.parent_run_id`. A refused call writes a `refused` record.

The fixture key is `blake3(model + US + system + US + canonical messages)[:12]` (US = the unit separator
character). Canonical messages are sorted-key compact JSON with each base64 image replaced by its media type,
size and the BLAKE3 of its decoded pixels, so a recording survives a different PNG encoder or zlib build.
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import secrets
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import yaml
from blake3 import blake3

from .. import custody
from ..custody import CustodyViolation, Grade, LlmRoute
from ..fsutil import write_json_atomic
from ..timeutil import utcnow

__all__ = [
    "AnthropicClient",
    "BudgetRefused",
    "DEFAULT_FIXTURE_DIR",
    "FixtureTransport",
    "LiveDisabled",
    "LiveTransport",
    "LlmError",
    "LlmRequest",
    "LlmResponse",
    "PRICES_YAML",
    "Prices",
    "Transport",
    "UnknownModel",
    "UnrecordedPrompt",
    "Usage",
    "canonical_json",
    "canonical_messages",
    "estimate_cost",
    "fixture_key",
    "load_prices",
]

PRICES_YAML = Path(__file__).resolve().parent / "prices.yaml"
# daemon/plate_daemon/llm/client.py -> daemon/tests/fixtures/llm
DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "llm"
KEY_LEN = 12
UNIT_SEP = "\x1f"
EPS = 1e-9
# Anthropic's published rule of thumb for image input: tokens = width * height / 750.
IMAGE_PX_PER_TOKEN = 750
CHARS_PER_TOKEN = 3.0


class UnrecordedPrompt(RuntimeError):
    """FixtureTransport met a request no recording matches. It never falls through to the network."""

    def __init__(self, key: str, model: str, where: Path):
        super().__init__(
            f"no recorded LLM response for key {key} (model {model}); expected {where}. "
            "Record it with FixtureTransport.record(); the default build never calls out."
        )
        self.key = key
        self.model = model


class LiveDisabled(RuntimeError):
    """LiveTransport needs PLATE_LIVE=1 and a budget envelope."""


class BudgetRefused(RuntimeError):
    """A live call that would break the envelope (expired, or the worst case does not fit what is left)."""

    code = "budget"


class LlmError(RuntimeError):
    """A transport-level failure worth one retry (overloaded, timeout, connection). Policy refusals
    (`CustodyViolation`, egress blocks, `UnrecordedPrompt`, `BudgetRefused`) are never wrapped in this."""


class UnknownModel(ValueError):
    """A model with no entry in prices.yaml: every call must be costed, so it is refused before it is sent."""


# ---------------------------------------------------------------------------- request, response, key


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LlmRequest:
    """One Messages API call. `messages` is the Anthropic wire shape: content blocks of type text or image
    (`{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ...}}`)."""

    model: str
    system: str
    messages: tuple[Mapping[str, Any], ...]
    max_tokens: int = 1024
    temperature: float | None = None
    purpose: str = ""

    @property
    def key(self) -> str:
        return fixture_key(self.model, self.system, self.messages)


@dataclass(frozen=True)
class LlmResponse:
    text: str
    usage: Usage
    model: str
    stop_reason: str | None = None
    cost_usd: float = 0.0
    key: str = ""
    transport: str = ""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _image_digest(block: Mapping[str, Any]) -> dict[str, Any]:
    source = block.get("source") or {}
    media_type = str(source.get("media_type", ""))
    raw = base64.b64decode(str(source.get("data", "")), validate=False)
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            rgb = im.convert("RGB")
            return {
                "type": "image",
                "media_type": media_type,
                "size": [rgb.width, rgb.height],
                "pixels_blake3": blake3(rgb.tobytes()).hexdigest(),
            }
    except Exception:  # not a decodable image: key on the bytes themselves
        return {"type": "image", "media_type": media_type, "bytes_blake3": blake3(raw).hexdigest()}


def _canonical(node: Any) -> Any:
    if isinstance(node, Mapping):
        if node.get("type") == "image" and isinstance(node.get("source"), Mapping):
            if node["source"].get("type") == "base64":
                return _image_digest(node)
        return {str(k): _canonical(v) for k, v in node.items()}
    if isinstance(node, list | tuple):
        return [_canonical(v) for v in node]
    return node


def canonical_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    """The messages as the fixture key sees them (images reduced to a pixel digest)."""
    return canonical_json(_canonical(list(messages)))


def fixture_key(model: str, system: str, messages: Sequence[Mapping[str, Any]]) -> str:
    """`blake3(model + system + canonical messages)[:12]`, the file name of a recording."""
    payload = UNIT_SEP.join((model, system, canonical_messages(messages)))
    return blake3(payload.encode("utf-8")).hexdigest()[:KEY_LEN]


# ---------------------------------------------------------------------------- prices


@dataclass(frozen=True)
class Prices:
    as_of: str
    confirmed: bool
    per_mtok_usd: Mapping[str, Mapping[str, float]]


@lru_cache(maxsize=4)
def load_prices(path: Path | str = PRICES_YAML) -> Prices:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"{path}: unreadable price table ({e})") from e
    if not isinstance(doc, dict) or doc.get("schema") != 1 or not doc.get("as_of"):
        raise ValueError(f"{path}: expected a mapping with schema: 1 and an as_of date")
    table: dict[str, dict[str, float]] = {}
    for model, row in (doc.get("per_mtok_usd") or {}).items():
        try:
            table[str(model)] = {"input": float(row["input"]), "output": float(row["output"])}
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"{path}: per_mtok_usd[{model!r}] needs numeric input and output") from e
    return Prices(str(doc["as_of"]), bool(doc.get("confirmed_against_console", False)), table)


def _price_row(model: str, prices: Prices) -> Mapping[str, float]:
    try:
        return prices.per_mtok_usd[model]
    except KeyError:
        raise UnknownModel(
            f"model {model!r} has no entry in the price table; known: {sorted(prices.per_mtok_usd)}"
        ) from None


def estimate_cost(model: str, usage: Usage, prices: Prices | None = None) -> float:
    """USD estimate for a call from its `usage` and prices.yaml (an estimate: billing is reconciled daily)."""
    row = _price_row(model, prices or load_prices())
    return (usage.input_tokens * row["input"] + usage.output_tokens * row["output"]) / 1_000_000


def _png_size(b64: str) -> tuple[int, int] | None:
    try:
        head = base64.b64decode(b64[:64] + "=" * (-len(b64[:64]) % 4))
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    except Exception:  # best effort: an unreadable size just falls back to a flat allowance
        pass
    return None


def estimate_input_tokens(request: LlmRequest) -> int:
    """A deliberately high guess at the input tokens (text at 3 chars/token, images at w*h/750)."""
    chars = len(request.system)
    images = 0
    for msg in request.messages:
        content = msg.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        for b in blocks:
            if isinstance(b, Mapping) and b.get("type") == "image":
                size = _png_size(str((b.get("source") or {}).get("data", "")))
                images += math.ceil(size[0] * size[1] / IMAGE_PX_PER_TOKEN) if size else 2000
            elif isinstance(b, Mapping):
                chars += len(str(b.get("text", "")))
    return math.ceil(chars / CHARS_PER_TOKEN) + images


# ---------------------------------------------------------------------------- transports


class Transport(Protocol):
    """Anything that can answer one LlmRequest. `name` lands in the ledger."""

    name: str

    def send(self, request: LlmRequest) -> LlmResponse: ...


class FixtureTransport:
    """Replays `<root>/<key>.json`. A recording is

        {"request": {"model": ..., "purpose": ..., "note": ...},          # informative only
         "responses": [{"text": ..., "usage": {"input_tokens": 1, "output_tokens": 2}, "stop_reason": ...}, ...]}

    (`"response": {...}` is accepted for a single answer). Responses for one key are served in order, one per
    call, the last one repeating, which is how two judge samples of the same prompt can differ. An entry
    `{"error": "overloaded"}` raises `LlmError`, to exercise the retry. `requests` keeps what was asked."""

    name = "fixture"

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else DEFAULT_FIXTURE_DIR
        self.requests: list[LlmRequest] = []
        self._served: Counter[str] = Counter()

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def send(self, request: LlmRequest) -> LlmResponse:
        key = request.key
        self.requests.append(request)
        path = self.path_for(key)
        if not path.is_file():
            raise UnrecordedPrompt(key, request.model, path)
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        responses = doc.get("responses") or ([doc["response"]] if "response" in doc else [])
        if not responses:
            raise ValueError(f"{path}: a recording needs 'responses' (or 'response')")
        item = responses[min(self._served[key], len(responses) - 1)]
        self._served[key] += 1
        if "error" in item:
            raise LlmError(str(item["error"]))
        usage = item.get("usage") or {}
        return LlmResponse(
            text=str(item["text"]),
            usage=Usage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))),
            model=request.model,
            stop_reason=item.get("stop_reason", "end_turn"),
            key=key,
            transport=self.name,
        )

    def record(self, request: LlmRequest, responses: Sequence[Mapping[str, Any]], note: str = "") -> Path:
        """Write a recording for `request` (used by the scripts and tests that build fixtures)."""
        doc = {
            "request": {"model": request.model, "purpose": request.purpose, "key": request.key, "note": note},
            "responses": [dict(r) for r in responses],
        }
        path = self.path_for(request.key)
        write_json_atomic(path, doc, indent=1)
        return path


class LiveTransport:
    """The real API, reachable only on purpose: `PLATE_LIVE=1`, a budget `envelope` (a `budgets.Envelope`) and
    an `http_client` from `Egress.client(run, route)`. Each call must fit what the envelope has left in the
    worst case (all of `max_tokens` produced), and what it actually costs is added to `spent_usd`.

    It speaks the Messages API over plain HTTP instead of the `anthropic` SDK: the SDK (1.15) is built on
    `httpx2` and rejects an `httpx.Client`, and the egress guard only wraps an `httpx` transport. A custody or
    egress refusal raised inside the transport reaches the caller untouched; a network or HTTP failure is
    an `LlmError` (worth the judge's one retry)."""

    name = "live"
    messages_url = "https://api.anthropic.com/v1/messages"
    api_version = "2023-06-01"

    def __init__(
        self,
        http_client: Any,
        envelope: Any,
        *,
        api_key: str | None = None,
        prices: Prices | None = None,
        clock: Any = utcnow,
    ):
        if os.environ.get("PLATE_LIVE") != "1":
            raise LiveDisabled("live LLM calls need PLATE_LIVE=1")
        if envelope is None:
            raise LiveDisabled("live LLM calls need a budget envelope")
        if http_client is None:
            raise LiveDisabled("live LLM calls need an httpx client from Egress.client(run, route)")
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LiveDisabled("live LLM calls need an API key (ANTHROPIC_API_KEY); never a consumer login")
        self.envelope = envelope
        self.prices = prices
        self.spent_usd = 0.0
        self._http = http_client
        self._key = key
        self._clock = clock

    def _worst_case(self, request: LlmRequest) -> float:
        row = _price_row(request.model, self.prices or load_prices())
        return (
            estimate_input_tokens(request) * row["input"] + request.max_tokens * row["output"]
        ) / 1_000_000

    def send(self, request: LlmRequest) -> LlmResponse:
        if self._clock() >= self.envelope.expires_utc:
            raise BudgetRefused(f"envelope {self.envelope.envelope_id} has expired")
        worst = self._worst_case(request)
        room = self.envelope.remaining_usd - self.spent_usd
        if worst > room + EPS:
            raise BudgetRefused(
                f"worst case ${worst:.4f} does not fit what envelope {self.envelope.envelope_id} has left "
                f"(${max(room, 0):.4f})"
            )
        body: dict[str, Any] = {
            "model": request.model,
            "system": request.system,
            "messages": list(request.messages),
            "max_tokens": request.max_tokens,
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        headers = {
            "x-api-key": self._key,
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }
        try:
            response = self._http.post(
                self.messages_url, content=canonical_json(body).encode("utf-8"), headers=headers
            )
        except (
            OSError,
            TimeoutError,
        ) as e:  # connection and timeout failures; policy refusals are not OSErrors
            raise LlmError(f"{type(e).__name__}: {e}") from e
        except Exception as e:
            if type(e).__module__.partition(".")[0] == "httpx":  # httpx.TransportError and friends
                raise LlmError(f"{type(e).__name__}: {e}") from e
            raise
        if response.status_code // 100 != 2:
            raise LlmError(f"HTTP {response.status_code}: {_error_text(response)}")
        try:
            doc = response.json()
            text = "".join(b["text"] for b in doc["content"] if b.get("type") == "text")
            usage = Usage(int(doc["usage"]["input_tokens"]), int(doc["usage"]["output_tokens"]))
        except (ValueError, KeyError, TypeError) as e:
            raise LlmError(f"unreadable Messages API reply ({type(e).__name__}: {e})") from e
        self.spent_usd += estimate_cost(request.model, usage, self.prices)
        return LlmResponse(
            text=text,
            usage=usage,
            model=str(doc.get("model", request.model)),
            stop_reason=doc.get("stop_reason"),
            key=request.key,
            transport=self.name,
        )


def _error_text(response: Any) -> str:
    try:
        err = response.json().get("error") or {}
        return str(err.get("message") or err.get("type") or response.text)[:200]
    except Exception:
        return str(getattr(response, "text", ""))[:200]


# ---------------------------------------------------------------------------- the client


@dataclass
class AnthropicClient:
    """`AnthropicClient(transport, allowed_grades=run.allowed_grades, ledger=ledger, run_id=run.id)`.

    `allowed_grades` is the run's (project custody intersect packet custody, NDA capped at C0/C1). The LLM
    route is re-read from `routes_path` on every call, so promoting it to a confirmed C1 needs no restart."""

    transport: Transport | None = None
    allowed_grades: Iterable[Grade | str] = field(default_factory=tuple)
    routes_path: Path | str = custody.ROUTES_YAML
    ledger: Any | None = None
    run_id: str | None = None
    node_id: str | None = None
    prices_path: Path | str = PRICES_YAML

    def __post_init__(self) -> None:
        self.allowed = frozenset(Grade.parse(g) for g in self.allowed_grades)
        if self.transport is None:
            self.transport = FixtureTransport()

    def route(self) -> LlmRoute:
        return custody.llm_route(self.routes_path)

    @property
    def available(self) -> bool:
        """False means composer and judge degrade (`custody.LLM_UNAVAILABLE`): nothing may be sent."""
        return custody.route_allowed(self.route(), self.allowed)

    def model_for(self, role: str) -> str:
        """The model id the route names for `composer` or `judge`."""
        try:
            return str(self.route().models[role])
        except KeyError:
            raise ValueError(f"custody_routes.yaml llm.models has no {role!r} model") from None

    def _ledger(self, kind: str, route: LlmRoute, model: str, **fields: Any) -> None:
        if self.ledger is None:
            return
        data = {"llm": True, "model": model, "parent_run_id": self.run_id, **fields.pop("data", {})}
        self.ledger.append(
            kind=kind,
            run_id=f"llm_{secrets.token_hex(8)}",
            node_id=self.node_id,
            engine_id=f"anthropic:{model}",
            route_id=route.route_id,
            custody_grade=route.effective.value,
            data=data,
            **fields,
        )

    def complete(
        self,
        *,
        purpose: str,
        model: str,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> LlmResponse:
        route = self.route()
        try:
            custody.require_route(route, self.allowed, what=f"LLM {purpose}")
        except CustodyViolation as e:
            self._ledger("refused", route, model, reason=str(e)[:300], data={"purpose": purpose})
            raise
        prices = load_prices(self.prices_path)
        _price_row(model, prices)  # an uncostable call is refused before it is sent
        request = LlmRequest(
            model=model,
            system=system,
            messages=tuple(messages),
            max_tokens=max_tokens,
            temperature=temperature,
            purpose=purpose,
        )
        assert self.transport is not None
        raw = self.transport.send(request)
        cost = estimate_cost(model, raw.usage, prices)
        response = LlmResponse(
            text=raw.text,
            usage=raw.usage,
            model=raw.model,
            stop_reason=raw.stop_reason,
            cost_usd=cost,
            key=raw.key or request.key,
            transport=raw.transport or getattr(self.transport, "name", ""),
        )
        self._ledger(
            "done",
            route,
            model,
            cost_usd_quoted=cost,
            data={
                "purpose": purpose,
                "input_tokens": raw.usage.input_tokens,
                "output_tokens": raw.usage.output_tokens,
                "transport": response.transport,
                "fixture_key": response.key,
                "prices_as_of": prices.as_of,
            },
        )
        return response
