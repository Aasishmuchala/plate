"""The Anthropic client and the two-sample consensus judge (T33), fixture-tested: no test reaches a network.

Recordings in `fixtures/llm/` are keyed by blake3(model + system + canonical messages)[:12]; see
`fixtures/llm/scenarios.py` for the inputs and `--record` to regenerate them after an intended prompt change."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
from datetime import timedelta
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
import yaml
from PIL import Image

from plate_daemon import custody
from plate_daemon.budgets import Envelope
from plate_daemon.custody import ALL_GRADES, CustodyViolation, Grade
from plate_daemon.egress import Egress, EgressRun
from plate_daemon.ledger import Ledger
from plate_daemon.llm import judge as judge_mod
from plate_daemon.llm.client import (
    DEFAULT_FIXTURE_DIR,
    AnthropicClient,
    BudgetRefused,
    FixtureTransport,
    LiveDisabled,
    LiveTransport,
    LlmError,
    LlmRequest,
    LlmResponse,
    UnknownModel,
    UnrecordedPrompt,
    Usage,
    fixture_key,
    load_prices,
)
from plate_daemon.llm.derivative import MAX_WIDTH_NDA, MAX_WIDTH_OPEN, build_pairs
from plate_daemon.llm.judge import Sample, Verdict, aggregate, judge, parse_sample
from plate_daemon.outbound_log import OutboundLog
from plate_daemon.timeutil import utcnow
from plate_pixels import icc

HERE = Path(__file__).resolve().parent


def _load_scenarios():
    name = "llm_scenarios"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / "fixtures" / "llm" / "scenarios.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SC = _load_scenarios()
NDA_GRADES = custody.NDA_GRADES
NO_SLEEP = lambda s: None  # noqa: E731


class CountingTransport:
    """A transport that records what it is sent and answers from a canned list. Proves 'zero calls'."""

    name = "counting"

    def __init__(self, replies: list[dict] | None = None):
        self.requests: list[LlmRequest] = []
        self._replies = replies or [SC._reply(9.0, "ship", [], "none")]

    def send(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        item = self._replies[min(len(self.requests) - 1, len(self._replies) - 1)]
        usage = item.get("usage", {"input_tokens": 100, "output_tokens": 20})
        return LlmResponse(
            text=item["text"],
            usage=Usage(usage["input_tokens"], usage["output_tokens"]),
            model=request.model,
            key=request.key,
            transport=self.name,
        )


def _confirmed_routes(tmp_path: Path) -> Path:
    """A copy of custody_routes.yaml with the Anthropic API promoted to a confirmed C1 (what filing the written
    terms would do)."""
    doc = yaml.safe_load(custody.ROUTES_YAML.read_text(encoding="utf-8"))
    doc["llm"]["status"] = "confirmed"
    doc["llm"]["terms_hash"] = "a" * 64
    path = tmp_path / "custody_routes.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    return path


def _png_size(b64: str) -> tuple[int, int]:
    with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
        return im.size


def _image_blocks(request: LlmRequest) -> list[dict]:
    out = []
    for msg in request.messages:
        for block in msg["content"]:
            if block.get("type") == "image":
                out.append(block)
    return out


def _fixture_client(**kw) -> AnthropicClient:
    return AnthropicClient(FixtureTransport(), allowed_grades=ALL_GRADES, **kw)


# ---------------------------------------------------------------------------- the committed fixtures


def test_agree_ship_uses_two_samples_and_the_mean(tmp_path):
    plate, cand = SC.write_pair(tmp_path, SC.JUDGE["agree_ship"]["seed"])
    v = judge(cand, plate, {"verdict": "pass"}, nda=False, client=_fixture_client(), sleep=NO_SLEEP)
    assert (v.verdict, v.samples, v.score) == ("ship", 2, 9.0)  # mean of 9.1 and 8.9
    assert v.spread == 0.2 and v.sample_scores == (9.1, 8.9)
    assert v.strongest_tell == "paving tone a touch uniform"
    assert v.unavailable is None and v.forced_by_gate is False
    assert v.cost_usd == pytest.approx(2 * (2400 * 5 + 90 * 25) / 1e6, abs=1e-6)


def test_disagreement_draws_a_third_sample_and_takes_the_median(tmp_path):
    sc = SC.JUDGE["disagree_third"]
    plate, cand = SC.write_pair(tmp_path, sc["seed"])
    v = judge(
        cand,
        plate,
        {"verdict": "pass"},
        nda=False,
        crops=sc["crops"],
        client=_fixture_client(),
        sleep=NO_SLEEP,
    )
    assert v.samples == 3
    assert v.sample_scores == (9.0, 6.0, 8.7)
    assert v.score == 8.7  # median, not the mean (7.9) and not the mean of the first two
    assert v.verdict == "ship"  # majority 2 of 3
    assert v.spread == 3.0
    assert v.strongest_tell == "glazing grid shifted by one bay"  # the lowest-scoring sample's tell
    assert v.issues[0] == "Glazing grid shifted" and len(v.issues) <= 4


def test_a_numeric_gate_failure_forces_fix(tmp_path):
    sc = SC.JUDGE["gate_fail_forces_fix"]
    plate, cand = SC.write_pair(tmp_path, sc["seed"])
    for gate in ({"verdict": "fail"}, _Obj("fail")):
        v = judge(cand, plate, gate, nda=False, client=_fixture_client(), sleep=NO_SLEEP)
        assert v.verdict == "fix" and v.forced_by_gate is True
        assert v.samples == 2 and v.score > judge_mod.SHIP_MIN_SCORE  # the judge liked it; the gate wins


class _Obj:
    def __init__(self, verdict: str):
        self.verdict = verdict


def test_an_unrecorded_prompt_never_reaches_the_network(tmp_path):
    plate, cand = SC.write_pair(tmp_path, 99)  # no recording for these pixels
    with pytest.raises(UnrecordedPrompt) as e:
        judge(cand, plate, None, nda=False, client=_fixture_client(), sleep=NO_SLEEP)
    assert "record" in str(e.value).lower() and len(e.value.key) == 12


def test_fixture_key_ignores_the_png_encoder(tmp_path):
    img = SC.make_image(5)
    ok, cv_png = cv2.imencode(".png", img[..., ::-1])
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG", compress_level=1)

    def msg(png: bytes) -> list[dict]:
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()},
        }
        return [{"role": "user", "content": [block]}]

    assert ok and cv_png.tobytes() != buf.getvalue()
    assert fixture_key("m", "s", msg(cv_png.tobytes())) == fixture_key("m", "s", msg(buf.getvalue()))
    other = img.copy()
    other[0, 0] ^= 1
    buf2 = io.BytesIO()
    Image.fromarray(other).save(buf2, format="PNG")
    assert fixture_key("m", "s", msg(buf2.getvalue())) != fixture_key("m", "s", msg(buf.getvalue()))
    assert fixture_key("m2", "s", msg(buf.getvalue())) != fixture_key("m", "s", msg(buf.getvalue()))


# ---------------------------------------------------------------------------- retry and malformed replies


def test_a_transport_error_is_retried_once_after_three_seconds(tmp_path):
    plate, cand = SC.write_pair(tmp_path, 11)
    good = SC._reply(9.0, "ship", [], "none")
    rec = SC.Recorder(tmp_path / "rec", [{"error": "overloaded"}, good, good, good], "retry")
    # The Recorder serves its own recording, so the error is raised for the first call and then served in order.
    sleeps: list[float] = []
    client = AnthropicClient(rec, allowed_grades=ALL_GRADES)
    v = judge(cand, plate, None, nda=False, client=client, sleep=sleeps.append)
    assert sleeps == [3.0]
    assert v.samples == 2 and v.verdict == "ship"


def test_a_malformed_reply_is_retried_then_the_judge_reports_unavailable(tmp_path):
    plate, cand = SC.write_pair(tmp_path, 12)
    sleeps: list[float] = []
    rec = SC.Recorder(tmp_path / "rec", [{"text": "I think it is fine!"}], "garbage")
    v = judge(
        cand,
        plate,
        None,
        nda=False,
        client=AnthropicClient(rec, allowed_grades=ALL_GRADES),
        sleep=sleeps.append,
    )
    assert sleeps == [3.0]  # one retry for the first sample, then the judge gives up
    assert v.unavailable == "error" and v.verdict is None and v.score is None
    assert v.notice.startswith("judge unavailable")


@pytest.mark.parametrize(
    "text, why",
    [
        ("not json", "no JSON"),
        ('{"score": 11, "verdict": "ship"}', "score"),
        ('{"score": "9", "verdict": "ship"}', "score"),
        ('{"score": 9, "verdict": "maybe"}', "verdict"),
        ('{"score": 9, "verdict": "ship", "issues": "none"}', "issues"),
    ],
)
def test_parse_sample_rejects_what_the_rubric_did_not_ask_for(text, why):
    with pytest.raises(judge_mod.SampleFormatError, match=why):
        parse_sample(text)


def test_parse_sample_normalises():
    s = parse_sample(
        '```json\n{"score": 8.0, "verdict": "ship", "issues": ["'
        + "x" * 300
        + '", "b", "c", "d", "e"], "strongest_tell": "None"}\n```'
    )
    assert s.verdict == "fix" and s.model_verdict == "ship"  # a ship below 8.5 is a fix
    assert len(s.issues) == 4 and len(s.issues[0]) == 140
    assert s.strongest_tell == ""
    assert parse_sample('{"score": 8.5, "verdict": "ship"}').verdict == "ship"


def test_consensus_arithmetic():
    def s(score, verdict):
        return Sample(score, verdict, (), "", verdict)

    assert judge_mod.disagree(s(9.0, "ship"), s(7.4, "ship")) is True  # 1.6 apart
    assert judge_mod.disagree(s(9.0, "ship"), s(7.5, "ship")) is True  # exactly 1.5
    assert judge_mod.disagree(s(9.0, "ship"), s(8.6, "ship")) is False
    assert judge_mod.disagree(s(9.0, "ship"), s(8.0, "fix")) is True
    v = aggregate([s(9.0, "ship"), s(8.5, "ship")])
    assert v.score == 8.75 and v.verdict == "ship"  # mean of two (never "the higher of two")
    v = aggregate([s(9.0, "ship"), s(6.0, "fix"), s(5.0, "fix")])
    assert v.score == 6.0 and v.verdict == "fix"  # median, majority fix
    assert aggregate([s(9, "ship"), s(9, "ship"), s(9, "ship")], "fail").forced_by_gate is True
    assert (
        aggregate([s(6, "fix"), s(6, "fix")], "fail").forced_by_gate is False
    )  # already fix: nothing forced
    with pytest.raises(ValueError):
        aggregate([s(9, "ship")])


# ---------------------------------------------------------------------------- custody and the NDA derivative


def test_nda_project_makes_zero_transport_calls_and_the_judge_is_unavailable(tmp_path):
    plate, cand = SC.write_pair(tmp_path, 1)
    transport = CountingTransport()
    client = AnthropicClient(transport, allowed_grades=NDA_GRADES)
    assert client.available is False
    v = judge(cand, plate, {"verdict": "pass"}, nda=True, client=client, sleep=NO_SLEEP)
    assert transport.requests == []
    assert (v.unavailable, v.samples, v.verdict, v.score) == ("custody", 0, None, None)
    assert v.notice == "judge unavailable on NDA until Anthropic terms are confirmed"
    assert v.as_dict()["status"] == custody.LLM_UNAVAILABLE


def test_the_client_refuses_a_direct_call_and_ledgers_the_refusal(tmp_path):
    transport = CountingTransport()
    ledger = Ledger(tmp_path / "proj")
    client = AnthropicClient(
        transport, allowed_grades=NDA_GRADES, ledger=ledger, run_id="run_1", node_id="n7"
    )
    with pytest.raises(CustodyViolation):
        client.complete(
            purpose="judge", model="claude-opus-5-5", system="s", messages=[{"role": "user", "content": "hi"}]
        )
    assert transport.requests == []
    (rec,) = ledger.records()
    assert rec["kind"] == "refused" and rec["engine_id"] == "anthropic:claude-opus-5-5"
    assert rec["custody_grade"] == "D" and rec["data"]["parent_run_id"] == "run_1"
    assert ledger.verify() == (True, None)


def test_confirmed_c1_route_lets_an_nda_run_send_only_1024_wide_derivatives(tmp_path):
    big_dir = tmp_path / "secret-project-name"
    big_dir.mkdir()
    plate, cand = SC.write_pair(big_dir, 4, width=2400, height=1350)
    transport = CountingTransport()
    routes = _confirmed_routes(tmp_path)
    client = AnthropicClient(transport, allowed_grades=NDA_GRADES, routes_path=routes)
    assert client.available is True
    crops = [[0.0, 0.0, 0.5, 0.5], [0.25, 0.25, 0.3125, 0.3125]]  # 1200 px wide and 150 px wide
    v = judge(cand, plate, {"verdict": "pass"}, nda=True, crops=crops, client=client, sleep=NO_SLEEP)
    assert v.verdict == "ship" and len(transport.requests) == 2
    for request in transport.requests:
        blocks = _image_blocks(request)
        assert len(blocks) == 4  # plate + candidate for each of two crops
        sizes = [_png_size(b["source"]["data"]) for b in blocks]
        assert all(w <= MAX_WIDTH_NDA for w, _ in sizes), sizes
        assert sizes[0] == (MAX_WIDTH_NDA, 576)  # the 1200 px crop shrank, aspect kept
        assert sizes[2] == (150, 85)  # the small crop is sent at native size: never upscaled
        for b in blocks:
            raw = base64.b64decode(b["source"]["data"])
            assert set(icc.png_chunk_types(raw)) <= {"IHDR", "IDAT", "IEND"}  # no text, EXIF, time or profile
        text = json.dumps(request.messages) + request.system
        assert big_dir.name not in text and "plate.png" not in text and "candidate.png" not in text
    # the same calls through the same client on a non-NDA run may be wider, up to 1536
    open_client = AnthropicClient(CountingTransport(), allowed_grades=ALL_GRADES, routes_path=routes)
    judge(cand, plate, None, nda=False, crops=[[0.0, 0.0, 1.0, 1.0]], client=open_client, sleep=NO_SLEEP)
    widths = [_png_size(b["source"]["data"])[0] for b in _image_blocks(open_client.transport.requests[0])]
    assert widths == [MAX_WIDTH_OPEN, MAX_WIDTH_OPEN]


def test_derivative_boxes_are_fractions_of_the_frame(tmp_path):
    plate, cand = SC.write_pair(tmp_path, 1)
    pairs = build_pairs(plate, cand, [[0.5, 0.5, 1.0, 1.0]], nda=True)
    assert [(d.label, d.width, d.height) for d in pairs] == [
        ("plate crop 1/1", 320, 180),
        ("candidate crop 1/1", 320, 180),
    ]
    assert [d.label for d in build_pairs(plate, cand, [], nda=True)] == ["plate frame", "candidate frame"]
    for bad in ([0.5, 0.5, 0.4, 0.9], [0, 0, 1.2, 1], [0.1, 0.1, 0.2]):
        with pytest.raises(ValueError):
            build_pairs(plate, cand, [bad], nda=True)


def test_every_call_is_ledgered_under_its_own_run(tmp_path):
    plate, cand = SC.write_pair(tmp_path, SC.JUDGE["agree_ship"]["seed"])
    ledger = Ledger(tmp_path / "proj")
    client = _fixture_client(ledger=ledger, run_id="run_gen", node_id="judge1")
    judge(cand, plate, None, nda=False, client=client, sleep=NO_SLEEP)
    recs = ledger.records()
    assert [r["kind"] for r in recs] == ["done", "done"]
    assert len({r["run_id"] for r in recs}) == 2 and all(r["run_id"].startswith("llm_") for r in recs)
    r = recs[0]
    assert r["engine_id"] == "anthropic:claude-opus-5-5" and r["route_id"] == "anthropic-api"
    assert r["cost_usd_quoted"] == pytest.approx((2400 * 5 + 90 * 25) / 1e6)
    assert r["data"]["parent_run_id"] == "run_gen" and r["data"]["purpose"] == "judge"
    assert r["data"]["transport"] == "fixture" and r["node_id"] == "judge1"
    assert ledger.verify() == (True, None)


def test_an_uncostable_model_is_refused_before_it_is_sent():
    transport = CountingTransport()
    client = AnthropicClient(transport, allowed_grades=ALL_GRADES)
    with pytest.raises(UnknownModel):
        client.complete(purpose="x", model="claude-mystery-9", system="s", messages=[])
    assert transport.requests == []
    p = load_prices()
    assert p.as_of == "2026-10-17" and set(p.per_mtok_usd) == {"claude-sonnet-5-5", "claude-opus-5-5"}


def test_default_transport_is_the_fixture_directory():
    client = AnthropicClient(allowed_grades=ALL_GRADES)
    assert isinstance(client.transport, FixtureTransport)
    assert client.transport.root == DEFAULT_FIXTURE_DIR and DEFAULT_FIXTURE_DIR.is_dir()
    assert len(list(DEFAULT_FIXTURE_DIR.glob("*.json"))) >= 3


# ---------------------------------------------------------------------------- the live path (mock network only)


def _anthropic_reply(text: str, tokens=(500, 50)) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }


def _envelope(max_usd: float, expires_in_s: float = 3600) -> Envelope:
    return Envelope(
        envelope_id="env_1",
        owner="agent:test",
        max_usd=max_usd,
        expires_utc=utcnow() + timedelta(seconds=expires_in_s),
    )


def test_live_transport_refuses_to_exist_without_plate_live(monkeypatch):
    monkeypatch.delenv("PLATE_LIVE", raising=False)
    with pytest.raises(LiveDisabled):
        LiveTransport(httpx.Client(), _envelope(5))
    monkeypatch.setenv("PLATE_LIVE", "1")
    with pytest.raises(LiveDisabled):
        LiveTransport(httpx.Client(), None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LiveDisabled, match="API key"):
        LiveTransport(httpx.Client(), _envelope(5))


def test_live_http_failures_are_retryable_llm_errors_and_cost_nothing(monkeypatch):
    monkeypatch.setenv("PLATE_LIVE", "1")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                529, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
            )
        if len(calls) == 2:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=_anthropic_reply(SC._reply(9.0, "ship", [], "none")["text"]))

    transport = LiveTransport(
        httpx.Client(transport=httpx.MockTransport(handler)), _envelope(5), api_key="sk-test"
    )
    req = LlmRequest("claude-opus-5-5", "s", ({"role": "user", "content": "hi"},), 100)
    with pytest.raises(LlmError, match="529.*Overloaded"):
        transport.send(req)
    with pytest.raises(LlmError, match="ConnectError"):
        transport.send(req)
    assert transport.spent_usd == 0.0
    assert transport.send(req).usage == Usage(500, 50)


def test_live_transport_over_a_mock_network_costs_and_obeys_the_envelope(monkeypatch):
    monkeypatch.setenv("PLATE_LIVE", "1")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_anthropic_reply(SC._reply(9.0, "ship", [], "none")["text"]))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    env = _envelope(0.05)
    transport = LiveTransport(http, env, api_key="sk-test")
    client = AnthropicClient(transport, allowed_grades=ALL_GRADES)
    req = dict(
        purpose="judge", model="claude-opus-5-5", system="s", messages=[{"role": "user", "content": "hi"}]
    )
    r = client.complete(**req, max_tokens=200)
    assert seen and seen[0].url.host == "api.anthropic.com" and seen[0].headers["x-api-key"] == "sk-test"
    assert r.transport == "live" and r.usage == Usage(500, 50)
    assert r.cost_usd == pytest.approx((500 * 5 + 50 * 25) / 1e6)
    assert transport.spent_usd == pytest.approx(r.cost_usd)
    with pytest.raises(BudgetRefused):  # worst case 10k output tokens x $25/M = $0.25 > what is left
        client.complete(**req, max_tokens=10_000)
    assert len(seen) == 1  # refused before any request
    expired = LiveTransport(http, _envelope(5, expires_in_s=-1), api_key="sk-test")
    with pytest.raises(BudgetRefused, match="expired"):
        AnthropicClient(expired, allowed_grades=ALL_GRADES).complete(**req)


def test_live_judge_through_egress_on_an_nda_project(tmp_path, monkeypatch, settings):
    """End to end on a mock network: the request passes custody (confirmed C1), the egress guard (host, PNG scrub,
    leak scan) and lands in the outbound log; the project's names never appear in the body."""
    monkeypatch.setenv("PLATE_LIVE", "1")
    routes = _confirmed_routes(tmp_path)
    route = custody.llm_route(routes)
    plate, cand = SC.write_pair(tmp_path, 6, width=1500, height=844)
    bodies: list[bytes] = []

    def vendor(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return httpx.Response(200, json=_anthropic_reply(SC._reply(9.0, "ship", [], "none")["text"]))

    log = OutboundLog(tmp_path / "outbound.jsonl")
    egress = Egress(settings, log, transport=httpx.MockTransport(vendor))
    manifest = json.loads((SC.EXAMPLE / "manifest.json").read_text(encoding="utf-8"))
    legend = json.loads((SC.EXAMPLE / "id_legend.json").read_text(encoding="utf-8"))
    run = EgressRun.for_project(
        {"project_id": "tula-wot", "custody": {"nda": True, "allowed_grades": ["C0", "C1"]}},
        "run_9",
        packet=manifest,
        legend=legend,
    )
    assert run.allowed_grades == {Grade.C0, Grade.C1}
    transport = LiveTransport(egress.client(run, route), _envelope(5), api_key="sk-test")
    client = AnthropicClient(transport, allowed_grades=run.allowed_grades, routes_path=routes)
    v = judge(
        cand, plate, {"verdict": "pass"}, nda=True, crops=[[0, 0, 0.8, 0.8]], client=client, sleep=NO_SLEEP
    )
    assert v.verdict == "ship" and v.samples == 2
    entries = log.entries()
    assert len(entries) == 2 and all(
        e["host"] == "api.anthropic.com" and e["custody_grade"] == "C1" for e in entries
    )
    assert all(e["status"] == 200 for e in entries)
    for body in bodies:
        doc = json.loads(body)
        for block in doc["messages"][0]["content"]:
            if block.get("type") == "image":
                png = base64.b64decode(block["source"]["data"])
                assert _png_size(block["source"]["data"])[0] <= MAX_WIDTH_NDA
                assert not {"tEXt", "iTXt", "zTXt", "eXIf", "tIME"} & set(icc.png_chunk_types(png))
        assert b"tula" not in body.lower() and b"CAM_C03" not in body


def test_the_egress_guard_beats_the_sdk_error_wrapper(tmp_path, monkeypatch, settings):
    """A route the run may not use is refused by egress; the SDK's connection error must not hide that."""
    monkeypatch.setenv("PLATE_LIVE", "1")
    routes = _confirmed_routes(tmp_path)
    route = custody.llm_route(routes)
    egress = Egress(
        settings,
        OutboundLog(tmp_path / "outbound.jsonl"),
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    run = EgressRun(run_id="r", allowed_grades=frozenset({Grade.C0}))  # C0 only: the C1 route is out
    with pytest.raises(CustodyViolation):
        LiveTransport(egress.client(run, route), _envelope(5), api_key="sk-test")
    # A run that allows C1 but a leaked name in the prompt: LeakBlocked comes through as itself, not LlmError.
    from plate_daemon.egress import LeakBlocked

    run = EgressRun(run_id="r", allowed_grades=frozenset({Grade.C1}), slugs=("tula-wot",))
    transport = LiveTransport(egress.client(run, route), _envelope(5), api_key="sk-test")
    with pytest.raises(LeakBlocked):
        transport.send(
            LlmRequest("claude-opus-5-5", "s", ({"role": "user", "content": "about tula wot"},), 100)
        )
    assert LeakBlocked.code == "leak" and not issubclass(LeakBlocked, LlmError)


def test_verdict_as_dict_is_json_ready():
    v = Verdict(
        score=8.7, verdict="ship", issues=("a",), samples=3, spread=3.0, sample_scores=(9.0, 6.0, 8.7)
    )
    json.dumps(v.as_dict())
    assert np.isfinite(v.score)
