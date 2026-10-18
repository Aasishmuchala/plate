"""`plate smoke` on the six Runway routes (router plan T121): the cases, the nightly contract list, the live gate.

The fixture smoke replays `plate_engines/contract/runway` (T118's synthetic recordings) through `RunwayVideo` with
the socket layer blocked; `smoke --live` is exercised with an injected transport and never reaches a network. Every
test calls `cli.main(argv, env=...)` in process with an explicit environment, so a key exported in a developer's
shell cannot change what a test means.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

import pytest

from plate_daemon import cli, scheduler

ROOT = Path(__file__).resolve().parents[2]
RUNWAY_ENGINES = ("gen-4.5", "kling-o3-4k", "kling-v3-pro", "seedance-2.5-i2v", "seedance-2.5-r2v", "veo-3.1")
ATLAS_ENGINES = ("kling-o3-4k", "kling-v3-pro", "seedance-2.5-i2v", "seedance-2.5-r2v")
ENGINES_ARG = "kling-o3-4k,kling-v3-pro,veo-3.1,seedance-2.5-i2v,seedance-2.5-r2v,gen-4.5"
SECRET = "rwk-test-secret-value"
# The Runway model id each engine sends, and the happy-path create recording the replay must pick for it.
MODEL = {
    "gen-4.5": "gen4.5",
    "kling-o3-4k": "klingO3_4k",
    "kling-v3-pro": "kling3.0_pro",
    "seedance-2.5-i2v": "seedance2_5",
    "seedance-2.5-r2v": "seedance2_5",
    "veo-3.1": "veo3.1",
}
CREATE = {
    "gen-4.5": ("/v1/image_to_video", "gen45_submit"),
    "kling-o3-4k": ("/v1/image_to_video", "kling_o3_submit"),
    "kling-v3-pro": ("/v1/image_to_video", "kling_v3_submit"),
    "seedance-2.5-i2v": ("/v1/image_to_video", "seedance_i2v_submit"),
    "seedance-2.5-r2v": ("/v1/text_to_video", "seedance_r2v_submit"),
    "veo-3.1": ("/v1/image_to_video", "veo_submit"),
}
UNHAPPY_NAMES = re.compile(
    r"safety|issues|_401|_429|_503|_404|_403|failed|cancel|offlist|draft_enhance|upload"
)
# Switches that would change what a test means if a developer's shell had them exported.
SCRUBBED = ("PLATE_LIVE", "PLATE_FAKE_ENGINE", "PLATE_ROUTER_MODE", "PLATE_DEBUG")
KEY_NAMES = (
    "RUNWAYML_API_SECRET",
    "ATLASCLOUD_KEY",
    "VERTEX_ACCESS_TOKEN",
    "GOOGLE_CLOUD_PROJECT",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "BFL_API_KEY",
    "ARK_API_KEY",
    "WAVESPEED_API_KEY",
    "MAGNIFIC_API_KEY",
    "FREEPIK_API_KEY",
)


def env_with(**extra: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k not in SCRUBBED and k not in KEY_NAMES}
    return {**base, **extra}


def run_cli(capsys, *argv: str, env: dict[str, str] | None = None, **kw) -> tuple[int, str, str]:
    code = cli.main(list(argv), env=env if env is not None else env_with(), **kw)
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the smoke opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def runway_live(monkeypatch):
    """PLATE_LIVE=1 and a Runway key, in os.environ (the adapter reads it there) and in the CLI's environment."""
    for name in KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RUNWAYML_API_SECRET", SECRET)
    monkeypatch.setenv("PLATE_LIVE", "1")
    return env_with(PLATE_LIVE="1", RUNWAYML_API_SECRET=SECRET)


class Spy(cli.ReplayTransport):
    """The replay, plus the JSON body of every request sent to the Runway API (what the adapter really asked for)."""

    def __init__(self, folders, prefer=()):
        super().__init__(folders, prefer)
        self.bodies: list[tuple[str, dict]] = []

    def _answer(self, request):
        if request.method == "POST" and request.url.host == "api.dev.runwayml.com" and request.content:
            self.bodies.append((request.url.path, json.loads(request.content)))
        return super()._answer(request)


# ---------------------------------------------------------------------------- the cases


def test_the_six_runway_cases_are_in_the_table_with_their_key_and_recordings():
    for engine in RUNWAY_ENGINES:
        case = cli.SMOKE_CASE_BY_ROUTE[(engine, "runway")]
        assert case.contract == ("runway",) and case.keys == cli.RUNWAY_KEY == (("RUNWAYML_API_SECRET",),)
        assert case.live_only is None  # recorded, so the fixture smoke runs it
    prefer = {e: cli.SMOKE_CASE_BY_ROUTE[(e, "runway")].prefer for e in RUNWAY_ENGINES}
    assert prefer["seedance-2.5-i2v"] == ("seedance_i2v_submit", "kling_o3_poll", "kling_o3_result")
    assert prefer["seedance-2.5-r2v"] == ("seedance_r2v",)
    assert prefer["kling-o3-4k"] == ("kling_o3",)
    assert prefer["kling-v3-pro"] == ("kling_v3", "kling_o3_poll")
    assert prefer["veo-3.1"] == ("veo",)
    assert prefer["gen-4.5"] == ("gen45", "kling_o3_poll")


def test_the_smoke_docstring_names_the_runway_cases_and_the_key():
    doc = cli.__doc__ or ""
    assert "six Runway cases" in doc and "RUNWAYML_API_SECRET" in doc


# ---------------------------------------------------------------------------- the fixture smoke


def test_fixture_smoke_runs_all_six_runway_routes_and_the_four_atlas_routes(capsys, no_network, tmp_path):
    report = tmp_path / "smoke.json"
    code, out, err = run_cli(capsys, "smoke", "--engines", ENGINES_ARG, "--out", str(report))
    assert code == cli.EXIT_OK, (out, err)
    for engine in RUNWAY_ENGINES:
        assert f"{engine} via runway: ok" in out, engine
    for engine in ATLAS_ENGINES:
        assert f"{engine} via atlas: ok" in out, engine
    assert out.count("via runway: ok") == 6
    doc = json.loads(report.read_text(encoding="utf-8"))
    rows = {(r["engine_id"], r["route_id"]): r for r in doc["rows"]}
    for engine in RUNWAY_ENGINES:
        row = rows[(engine, "runway")]
        assert row["status"] == "ok" and row["custody"] == "D", row
        assert row["model"] == MODEL[engine] and row["price_usd"] > 0 and row["result_bytes"], row
    assert rows[("kling-o3-4k", "runway")]["price_usd"] == pytest.approx(1.29)  # 3 s: Kling's shortest clip
    assert rows[("kling-v3-pro", "runway")]["price_usd"] == pytest.approx(0.36)  # 3 s
    assert rows[("seedance-2.5-i2v", "runway")]["price_usd"] == pytest.approx(
        0.80
    )  # the 4 s 480P draft floor
    assert rows[("seedance-2.5-r2v", "runway")]["price_usd"] == pytest.approx(0.80)
    assert rows[("veo-3.1", "runway")]["price_usd"] == pytest.approx(0.80)  # 4 s, audio off
    assert rows[("gen-4.5", "runway")]["price_usd"] == pytest.approx(0.24)  # 2 s


def test_fixture_smoke_of_a_runway_only_pair_counts_two(capsys, no_network):
    code, out, err = run_cli(capsys, "smoke", "--engines", "kling-o3-4k,gen-4.5")
    assert code == cli.EXIT_OK, (out, err)
    assert len(re.findall(r"via runway: ok", out)) == 2


def test_the_replay_serves_the_happy_recordings_and_only_those(capsys, no_network):
    built: dict[tuple[str, str], Spy] = {}

    def factory(case, manifest, route, live):
        assert live is False
        built[(manifest.id, route.route_id)] = spy = Spy(case.contract, case.prefer)
        return spy

    code, out, err = run_cli(capsys, "smoke", "--engines", ENGINES_ARG, transport_factory=factory)
    assert code == cli.EXIT_OK, (out, err)
    for engine in RUNWAY_ENGINES:
        served = built[(engine, "runway")].served
        path, create = CREATE[engine]
        served = [
            (m, h, p, n.removesuffix(".json")) for m, h, p, n in served
        ]  # recording names carry the suffix
        names = [name for _method, _host, _path, name in served]
        assert ("POST", "api.dev.runwayml.com", path, create) in served, (engine, served)
        poll = "veo_poll" if engine == "veo-3.1" else "kling_o3_poll"
        assert any(m == "GET" and p.startswith("/v1/tasks/") and n == poll for m, _h, p, n in served), served
        assert any(h.endswith("cloudfront.net") and n == "kling_o3_result" for _m, h, _p, n in served), served
        assert not any(UNHAPPY_NAMES.search(n) for n in names), (
            engine,
            names,
        )  # no refusal, no 429, no upload


def test_the_smoke_asks_for_the_cheapest_legal_runway_request(capsys, no_network):
    built: dict[str, Spy] = {}

    def factory(case, manifest, route, live):
        spy = Spy(case.contract, case.prefer)
        if route.route_id == "runway":
            built[manifest.id] = spy
        return spy

    code, out, err = run_cli(capsys, "smoke", "--engines", ENGINES_ARG, transport_factory=factory)
    assert code == cli.EXIT_OK, (out, err)
    body = {e: built[e].bodies[0][1] for e in RUNWAY_ENGINES}
    for engine in RUNWAY_ENGINES:
        assert body[engine]["model"] == MODEL[engine]
        assert built[engine].bodies[0][0] == CREATE[engine][0]
    assert body["kling-o3-4k"]["duration"] == 3 and body["kling-o3-4k"]["audio"] is False
    assert body["kling-v3-pro"]["duration"] == 3 and body["kling-v3-pro"]["audio"] is False
    assert body["veo-3.1"]["duration"] == 4 and body["veo-3.1"]["audio"] is False
    for engine in ("seedance-2.5-i2v", "seedance-2.5-r2v"):  # 480P is Runway's draft tier
        assert (
            body[engine]["duration"] == 4 and body[engine]["draft"] is True and body[engine]["audio"] is False
        )
        assert body[engine]["ratio"] == "854:480"
    assert body["gen-4.5"]["duration"] == 2 and body["gen-4.5"]["ratio"] == "1280:720"
    assert "audio" not in body["gen-4.5"]  # Gen-4.5 has no audio field
    assert "references" in body["seedance-2.5-r2v"] and "promptImage" in body["seedance-2.5-i2v"]


def test_min_cost_params_stay_the_manifest_minimums_for_the_runway_engines():
    from plate_engines.loader import load_manifests

    m = load_manifests()
    assert cli.min_cost_params(m["seedance-2.5-i2v"]) == {"duration": 4, "resolution": "480P"}
    assert cli.min_cost_params(m["seedance-2.5-r2v"]) == {"duration": 4, "resolution": "480P"}
    assert cli.min_cost_params(m["veo-3.1"]) == {"duration": 4, "resolution": "720p"}
    assert cli.min_cost_params(m["kling-v3-pro"]) == {
        "duration": 1,
        "resolution": "1080P",
    }  # Runway snaps 1 s to 3 s
    assert cli.min_cost_params(m["kling-o3-4k"]) == {"duration": 1}
    assert cli.min_cost_params(m["gen-4.5"]) == {"duration": 2}


def test_smoke_inputs_give_the_start_frame_engines_the_still_as_image_and_a_prompt(tmp_path):
    from plate_engines.loader import load_manifests

    m = load_manifests()
    still = tmp_path / "smoke_still.png"
    for engine in ("gen-4.5", "seedance-2.5-i2v", "kling-o3-4k", "kling-v3-pro"):
        inputs = cli.smoke_inputs(m[engine], b"", still)
        assert inputs == {"image": {"path": str(still)}, "prompt": cli.SMOKE_PROMPT}, engine
    assert cli.smoke_inputs(m["veo-3.1"], b"", still)["start_image"] == {"path": str(still)}
    refs = cli.smoke_inputs(m["seedance-2.5-r2v"], b"", still)
    assert refs["reference_images"] == [{"path": str(still), "role": "first_frame"}]


def test_the_fixture_clock_jumps_past_a_pollers_minimum_gap():
    clock = cli.FixtureClock()
    first, second = clock(), clock()
    assert second - first >= 5.0  # RunwayVideo.MIN_POLL_GAP_S


def test_a_fixture_run_does_not_leave_the_runway_key_behind(capsys, monkeypatch, no_network):
    monkeypatch.delenv("RUNWAYML_API_SECRET", raising=False)
    run_cli(capsys, "smoke", "--engines", "gen-4.5")
    assert "RUNWAYML_API_SECRET" not in os.environ


# ---------------------------------------------------------------------------- smoke --live


def test_live_runway_smoke_without_plate_live_is_refused_before_any_transport(capsys, monkeypatch):
    built: list[bool] = []

    def factory(case, manifest, route, live):
        built.append(live)
        raise AssertionError("no transport may be built before the refusal")

    monkeypatch.setenv("RUNWAYML_API_SECRET", SECRET)
    code, out, err = run_cli(
        capsys,
        "smoke", "--live", "--budget-usd", "5", "--engines", ENGINES_ARG,
        env=env_with(RUNWAYML_API_SECRET=SECRET),
        transport_factory=factory,
    )  # fmt: skip
    assert code == cli.EXIT_REFUSED and "PLATE_LIVE=1" in err
    assert built == [] and "via runway" not in out


def test_live_runway_smoke_without_a_key_names_the_variable_and_sends_nothing(
    capsys, monkeypatch, no_network
):
    for name in KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLATE_LIVE", "1")
    built: list[bool] = []

    def factory(case, manifest, route, live):
        built.append(live)
        return cli.ReplayTransport(case.contract, case.prefer)

    code, out, err = run_cli(
        capsys,
        "smoke", "--live", "--budget-usd", "1", "--engines", ENGINES_ARG,
        env=env_with(PLATE_LIVE="1"),
        transport_factory=factory,
    )  # fmt: skip
    assert code == cli.EXIT_OK, (out, err)
    for engine in RUNWAY_ENGINES:
        assert f"{engine} via runway: skipped (set RUNWAYML_API_SECRET)" in out, engine
    assert built == []  # zero transports, live or otherwise: nothing could be sent
    assert out.strip().splitlines()[-1].startswith("live smoke: 0 adapters ok, 0 failed")


def test_live_runway_smoke_stops_at_the_first_quote_over_budget(capsys, runway_live, no_network, tmp_path):
    spies: dict[str, Spy] = {}
    report = tmp_path / "live.json"

    def factory(case, manifest, route, live):
        assert live is True
        spies[manifest.id] = spy = Spy(case.contract, case.prefer)  # stands in for the network
        return spy

    code, out, err = run_cli(
        capsys,
        "smoke", "--live", "--budget-usd", "0.5", "--engines", "kling-o3-4k,kling-v3-pro",
        "--out", str(report),
        env=runway_live, transport_factory=factory, sleep=lambda _s: None,
    )  # fmt: skip
    # kling-o3-4k via atlas has no ATLASCLOUD_KEY here; via runway its 3 s quote (1.29) is over 0.5
    assert code == cli.EXIT_REFUSED, (out, err)
    assert "kling-o3-4k via atlas: skipped (set ATLASCLOUD_KEY)" in out
    head = next(ln for ln in out.splitlines() if ln.startswith("kling-o3-4k via runway: custody D"))
    assert "quote 1.2900 USD" in head and "of 0.5000 USD" in head
    assert "stopping: this call would take the running total past the budget" in out
    assert "kling-v3-pro via runway: skipped (not run: the budget was reached)" in out
    assert spies["kling-o3-4k"].served == [] and spies["kling-o3-4k"].bodies == []  # quoted, never sent
    assert "kling-v3-pro" not in spies  # and the next engine never started
    assert SECRET not in out and SECRET not in err
    rows = {
        (r["engine_id"], r["route_id"]): r for r in json.loads(report.read_text(encoding="utf-8"))["rows"]
    }
    assert rows[("kling-o3-4k", "runway")]["status"] == "stopped"
    assert rows[("kling-o3-4k", "runway")]["price_usd"] == pytest.approx(1.29)
    assert rows[("kling-v3-pro", "runway")]["status"] == "skipped"


def test_live_runway_smoke_paces_its_polls_by_the_adapter_and_stays_in_budget(
    capsys, runway_live, monkeypatch, no_network
):
    from plate_engines.adapters.runway_video import RunwayVideo

    monkeypatch.setattr(
        RunwayVideo, "MIN_POLL_GAP_S", 0.0
    )  # the recorded polls come one per look; do not spin
    sleeps: list[float] = []
    spies: list[Spy] = []

    def factory(case, manifest, route, live):
        spies.append(Spy(case.contract, case.prefer))
        return spies[-1]

    code, out, err = run_cli(
        capsys,
        "smoke", "--live", "--budget-usd", "0.5", "--engines", "gen-4.5",
        env=runway_live, transport_factory=factory, sleep=sleeps.append,
    )  # fmt: skip
    assert code == cli.EXIT_OK, (out, err)
    assert "gen-4.5 via runway: custody D" in out and "sending ..." in out
    assert "gen-4.5 via runway: ok" in out
    assert (
        out.strip()
        .splitlines()[-1]
        .startswith("live smoke: 1 adapters ok, 0 failed; quoted total 0.2400 USD")
    )
    assert (
        sleeps and min(sleeps) >= RunwayVideo.POLL_INTERVAL_S
    )  # the vendor asks for 5 s or more between looks
    assert [b["model"] for _path, b in spies[0].bodies] == ["gen4.5"]  # exactly one create
    assert SECRET not in out and SECRET not in err


# ---------------------------------------------------------------------------- the nightly contract list


def test_the_nightly_contract_list_includes_the_two_runway_suites():
    listed = scheduler.DEFAULT_CONTRACT_TESTS
    for name in ("engines/tests/test_adapters_runway.py", "engines/tests/test_runway_schema.py"):
        assert name in listed, name
        assert (ROOT / name).is_file(), name  # a listed suite that does not exist would fail every night
    assert len(set(listed)) == len(listed)
