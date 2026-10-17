"""Engine cards (Wilson bounds, ledger folding) and weekly canaries (fake adapters, drift, suspension)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from blake3 import blake3

from plate_engines.adapters.base import JobStatus
from plate_engines.canaries import (
    N_MOVES,
    N_PLATES,
    CanaryCase,
    CanaryError,
    CanaryReport,
    EngineCanary,
    compare,
    drift,
    drift_reasons,
    run,
)
from plate_engines.cards import CARD_MIN_N, Card, from_ledger, index_cards, wilson_lower
from plate_engines.health import AlertLog, HealthBoard, HealthEntry
from plate_engines.router import Refusal, choose

ALL_GRADES = {"C0", "C1", "C2", "D"}


# ---------------------------------------------------------------------------- Wilson


def test_wilson_vector_from_the_plan():
    assert wilson_lower(18, 20) == pytest.approx(0.6989, abs=0.001)


def test_wilson_edges():
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(0, 20) == 0.0
    assert 0.83 < wilson_lower(20, 20) < 0.85  # 20 for 20 is not 1.0
    with pytest.raises(ValueError):
        wilson_lower(21, 20)
    with pytest.raises(ValueError):
        wilson_lower(-1, 20)


def test_wilson_is_pessimistic_for_small_samples():
    assert wilson_lower(3, 3) < wilson_lower(180, 200)  # 3 for 3 does not beat 90%
    assert wilson_lower(18, 20) < 18 / 20
    assert wilson_lower(90, 100) > wilson_lower(18, 20)  # more evidence, same rate, higher floor


def test_wilson_matches_the_formula_written_out():
    z, n, passes = 1.96, 50, 41
    p = passes / n
    by_hand = (p + z * z / (2 * n) - z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / (1 + z * z / n)
    assert wilson_lower(passes, n) == pytest.approx(by_hand, abs=1e-12)


def test_card_make_and_steering():
    c = Card.make("nano-banana-2.1", "relight", "medium", 18, 20, mean_cost=0.04, custody="D")
    assert (c.n, c.passes, c.pass_rate) == (20, 18, 0.9)
    assert c.wilson_lower_95 == pytest.approx(0.6989, abs=0.001)
    assert c.key == ("nano-banana-2.1", "relight", "medium")
    assert c.steering() and CARD_MIN_N == 20
    assert not Card.make("nano-banana-2.1", "relight", "medium", 19, 19).steering()
    assert Card.from_dict(c.to_dict()) == c
    assert index_cards([c])[c.key] is c
    assert index_cards({"anything": c})[c.key] is c
    assert index_cards(None) == {}


# ---------------------------------------------------------------------------- from_ledger


def run_records(
    run_id,
    engine,
    *,
    task="relight",
    shot="medium",
    grade="D",
    repeat=None,
    quoted=0.034,
    billed=None,
    ref=None,
    summary=None,
):
    data = {"task": task, "shot_class": shot}
    if repeat:
        data["repeat_key"] = repeat
    recs = [
        {
            "kind": "quoted",
            "run_id": run_id,
            "engine_id": engine,
            "custody_grade": grade,
            "cost_usd_quoted": quoted,
            "data": data,
        },
        {"kind": "approved", "run_id": run_id, "cost_usd_quoted": quoted},
        {"kind": "submitted", "run_id": run_id},
    ]
    if billed is not None:
        recs.append({"kind": "billed", "run_id": run_id, "cost_usd_billed": billed})
    if ref is not None:
        recs.append({"kind": "gated", "run_id": run_id, "gate_report": ref})
    if summary is not None:
        recs.append({"kind": "gated", "run_id": run_id, "gate_report": summary})
    recs.append({"kind": "done", "run_id": run_id})
    return recs


def test_from_ledger_counts_verdicts_costs_and_custody():
    records, reports = [], {}
    for i in range(10):
        ref = f"gates/r{i}.1.json"
        reports[ref] = {"verdict": "pass" if i < 7 else "fail"}
        records += run_records(f"r{i}", "nano-banana-2.1", ref=ref, billed=0.04)
    records += run_records("adv", "nano-banana-2.1", ref="gates/adv.1.json")
    reports["gates/adv.1.json"] = {"verdict": "advisory"}
    records += run_records("nogate", "nano-banana-2.1")  # never gated: not counted
    records += run_records("notask", "nano-banana-2.1", task=None)  # no cell
    records += [{"kind": "quoted", "run_id": "noeng", "data": {"task": "relight"}}]
    records.append("not a record")  # tolerated
    cards = from_ledger(records, reports)
    assert len(cards) == 1
    c = cards[0]
    assert (c.engine_id, c.task, c.shot_class) == ("nano-banana-2.1", "relight", "medium")
    assert (c.n, c.passes, c.pass_rate) == (10, 7, 0.7)
    assert c.mean_cost == pytest.approx(0.04)  # billed beats the quote
    assert c.custody == "D"
    assert c.determinism_var is None
    assert c.wilson_lower_95 == pytest.approx(wilson_lower(7, 10))


def test_from_ledger_advisory_rule_and_inline_summaries_and_object_reports():
    records = []
    records += run_records("a", "gpt-image-2.5", summary={"verdict": "pass", "tier": "A"})
    records += run_records("b", "gpt-image-2.5", ref="b.json")
    records += run_records("c", "gpt-image-2.5")  # looked up by run_id
    reports = {"b.json": SimpleNamespace(verdict="advisory"), "c": {"verdict": "fail"}}
    excluded = from_ledger(records, reports)[0]
    assert (excluded.n, excluded.passes) == (2, 1)
    as_pass = from_ledger(records, reports, advisory="pass")[0]
    assert (as_pass.n, as_pass.passes) == (3, 2)
    as_fail = from_ledger(records, reports, advisory="fail")[0]
    assert (as_fail.n, as_fail.passes) == (3, 1)


def test_from_ledger_last_gate_report_wins_a_rescore():
    records = run_records("r1", "gpt-image-2.5", ref="first.json")
    records.insert(-1, {"kind": "gated", "run_id": "r1", "gate_report": "second.json"})
    reports = {"first.json": {"verdict": "fail"}, "second.json": {"verdict": "pass"}}
    assert from_ledger(records, reports)[0].passes == 1


def test_from_ledger_cost_falls_back_to_the_approved_quote_and_custody_is_the_worst_seen():
    records = run_records("a", "nano-banana-2.1", grade="C1", quoted=0.02, summary={"verdict": "pass"})
    records += run_records("b", "nano-banana-2.1", grade="D", quoted=0.06, summary={"verdict": "pass"})
    card = from_ledger(records)[0]
    assert card.mean_cost == pytest.approx(0.04)
    assert card.custody == "D"
    only_c1 = from_ledger(run_records("a", "qwen-image-edit-2509", grade="C0", summary={"verdict": "pass"}))[
        0
    ]
    assert only_c1.custody == "C0"


def test_from_ledger_determinism_variance_from_repeat_keys():
    records = []
    # key k1: 2 runs agree (var 0); key k2: 2 runs split (var 0.25); key k3 has a single run (ignored)
    for run_id, key, verdict in [
        ("a", "k1", "pass"),
        ("b", "k1", "pass"),
        ("c", "k2", "pass"),
        ("d", "k2", "fail"),
        ("e", "k3", "pass"),
    ]:
        records += run_records(run_id, "nano-banana-2.1", repeat=key, summary={"verdict": verdict})
    card = from_ledger(records)[0]
    assert card.determinism_var == pytest.approx(0.125)  # mean of 0 and 0.25


def test_from_ledger_separates_cells_by_engine_task_and_shot():
    records = []
    records += run_records("a", "nano-banana-2.1", shot="close", summary={"verdict": "pass"})
    records += run_records("b", "nano-banana-2.1", shot="wide", summary={"verdict": "fail"})
    records += run_records("c", "nano-banana-2.1", task="sky", shot="wide", summary={"verdict": "pass"})
    records += run_records("d", "gpt-image-2.5", shot=None, summary={"verdict": "pass"})
    keys = [c.key for c in from_ledger(records)]
    assert keys == [
        ("gpt-image-2.5", "relight", None),
        ("nano-banana-2.1", "relight", "close"),
        ("nano-banana-2.1", "relight", "wide"),
        ("nano-banana-2.1", "sky", "wide"),
    ]


def test_cards_from_the_real_ledger_steer_the_router(tmp_path, manifests):
    """Records written by plate_daemon.ledger feed from_ledger, and a 24-run cell then steers choose()."""
    ledger_mod = pytest.importorskip("plate_daemon.ledger")
    led = ledger_mod.Ledger(tmp_path / "proj")
    for i in range(24):
        run_id = f"run{i:02d}"
        verdict = "pass" if i < 23 else "fail"
        led.append(
            kind="quoted",
            run_id=run_id,
            engine_id="nano-banana-2.1",
            custody_grade="D",
            cost_usd_quoted=0.034,
            data={"task": "relight", "shot_class": "medium"},
        )
        led.append(kind="submitted", run_id=run_id)
        led.append(kind="gated", run_id=run_id, gate_report={"verdict": verdict})
    cards = from_ledger(led.records())
    assert [(c.n, c.passes) for c in cards] == [(24, 23)]
    cloud = {k: manifests[k] for k in ("nano-banana-2.1", "gpt-image-2.5")}
    cheap_gpt = dict(cloud)
    cheap_gpt["gpt-image-2.5"] = cloud["gpt-image-2.5"].model_copy(
        update={"price": cloud["gpt-image-2.5"].price.model_copy(update={"usd": 0.001})}
    )
    health = {p: HealthEntry("ok", 100.0) for m in cloud.values() for p in (r.provider for r in m.routes)}
    from plate_engines.router import Task

    task = Task("relight", shot_class="medium")
    assert choose(task, cheap_gpt, ALL_GRADES, None, None, health).engine_id == "gpt-image-2.5"
    assert choose(task, cheap_gpt, ALL_GRADES, None, cards, health).engine_id == "nano-banana-2.1"


# ---------------------------------------------------------------------------- canaries: fakes


class FakeAdapter:
    """Drives the same interface the pipeline uses: validate_params, submit(Staged), poll, fetch."""

    def __init__(
        self, engine_id, kind, *, version="model-1", usage_version=True, script=None, polls_before_done=0
    ):
        self.engine_id = engine_id
        self.route_id = engine_id.split("-")[0]
        self.manifest = SimpleNamespace(kind=kind)
        self.model_version = version
        self.usage_version = usage_version
        self.script = script or {}
        self.polls_before_done = polls_before_done
        self.jobs: dict[str, dict] = {}
        self.submitted: list = []
        self._polled: dict[str, int] = {}

    def validate_params(self, params):
        return dict(params)

    def submit(self, staged):
        self.submitted.append(staged)
        case = staged.inputs["image"]
        job = f"job-{len(self.submitted)}"
        self.jobs[job] = {"case": case, "staged": staged}
        if isinstance(self.script.get(case), Exception):
            raise self.script[case]
        return job

    def poll(self, job):
        case = self.jobs[job]["case"]
        n = self._polled[job] = self._polled.get(job, 0) + 1
        if n <= self.polls_before_done:
            return JobStatus(state="running")
        state = self.script.get(case, "done")
        if state in ("failed", "refused"):
            return JobStatus(state=state, message=f"{state} by vendor")
        usage = {"model_version": self.model_version} if self.usage_version else {}
        return JobStatus(state="done", outputs=[f"https://fixture.invalid/{job}"], usage=usage)

    def fetch(self, url):
        job = url.rsplit("/", 1)[-1]
        # output depends on input: the case image and the engine
        return f"{self.engine_id}|{self.jobs[job]['case']}".encode()


def plates(n=N_PLATES):
    return [
        CanaryCase(f"plate-{i + 1:02d}", "plate", {"image": f"plate-{i + 1:02d}.png", "prompt": "relight"})
        for i in range(n)
    ]


def moves(n=N_MOVES):
    return [
        CanaryCase(
            f"move-{i + 1:02d}",
            "move",
            {"image": f"move-{i + 1:02d}-a.png", "image_tail": f"move-{i + 1:02d}-b.png"},
        )
        for i in range(n)
    ]


def gates_failing(*case_ids, engines=None):
    bad = set(case_ids)

    def gates(case, data, engine_id):
        if engines is not None and engine_id not in engines:
            return "pass"
        return "fail" if case.case_id in bad else "pass"

    return gates


def no_sleep(_):
    pass


def test_run_sends_plates_to_image_engines_and_moves_to_video_engines():
    adapters = {
        "nano-banana-2.1": FakeAdapter("nano-banana-2.1", "image_edit"),
        "qwen-image-edit-2509": FakeAdapter("qwen-image-edit-2509", "local_image"),
        "kling-o3-4k": FakeAdapter("kling-o3-4k", "video_startend"),
        "real-esrgan": FakeAdapter("real-esrgan", "upscale"),
    }
    report = run(plates(), moves(), adapters, gates_failing(), sleep=no_sleep)
    assert set(report.engines) == {"nano-banana-2.1", "qwen-image-edit-2509", "kling-o3-4k"}
    assert report.engines["nano-banana-2.1"].n == 12
    assert report.engines["kling-o3-4k"].n == 4
    assert all(e.passes == e.n and e.pass_rate == 1.0 for e in report.engines.values())
    assert "real-esrgan" in report.skipped and "plate" in report.skipped["real-esrgan"]
    assert adapters["real-esrgan"].submitted == []  # an upscaler is never run on a plate
    assert [s.inputs["image"] for s in adapters["kling-o3-4k"].submitted] == [
        f"move-0{i}-a.png" for i in (1, 2, 3, 4)
    ]
    nano = report.engines["nano-banana-2.1"]
    assert nano.model_version == "model-1" and nano.route_id == "nano"
    ids = [r.output_id for r in nano.results]
    assert len(set(ids)) == 12  # outputs depend on inputs
    assert ids[0] == blake3(b"nano-banana-2.1|plate-01.png").hexdigest()[:16]


def test_run_requires_twelve_plates_and_four_moves():
    adapters = {"nano-banana-2.1": FakeAdapter("nano-banana-2.1", "image_edit")}
    with pytest.raises(CanaryError, match="12 owned plates and 4 moves"):
        run(plates(11), moves(), adapters, gates_failing(), sleep=no_sleep)
    with pytest.raises(CanaryError):
        run(plates(), moves(3), adapters, gates_failing(), sleep=no_sleep)
    report = run(plates(2), moves(1), adapters, gates_failing(), sleep=no_sleep, n_plates=2, n_moves=1)
    assert report.engines["nano-banana-2.1"].n == 2


def test_run_counts_gate_failures_and_adapter_errors_as_fails():
    boom = RuntimeError("unrecorded request")
    adapter = FakeAdapter(
        "nano-banana-2.1",
        "image_edit",
        script={"plate-02.png": boom, "plate-03.png": "failed", "plate-04.png": "refused"},
    )
    report = run(plates(), moves(), {"nano-banana-2.1": adapter}, gates_failing("plate-01"), sleep=no_sleep)
    e = report.engines["nano-banana-2.1"]
    assert (e.n, e.passes) == (12, 8)
    by_case = {r.case_id: r for r in e.results}
    assert by_case["plate-01"].verdict == "fail" and by_case["plate-01"].error is None
    assert "RuntimeError: unrecorded request" in by_case["plate-02"].error
    assert by_case["plate-03"].error == "failed: failed by vendor"
    assert by_case["plate-04"].error.startswith("refused")
    assert by_case["plate-02"].output_id is None


def test_run_polls_until_done_and_times_out():
    slow = FakeAdapter("nano-banana-2.1", "image_edit", polls_before_done=3)
    naps = []
    report = run(
        plates(), moves(), {"nano-banana-2.1": slow}, gates_failing(), sleep=naps.append, poll_interval_s=2.5
    )
    assert report.engines["nano-banana-2.1"].passes == 12
    assert naps == [2.5] * 36
    stuck = FakeAdapter("nano-banana-2.1", "image_edit", polls_before_done=99)
    report = run(plates(), moves(), {"nano-banana-2.1": stuck}, gates_failing(), sleep=no_sleep, max_polls=3)
    e = report.engines["nano-banana-2.1"]
    assert e.passes == 0 and "timeout" in e.results[0].error


def test_run_gate_bugs_are_not_swallowed():
    adapters = {"nano-banana-2.1": FakeAdapter("nano-banana-2.1", "image_edit")}

    def broken(case, data, engine_id):
        raise KeyError("harness bug")

    with pytest.raises(KeyError):
        run(plates(), moves(), adapters, broken, sleep=no_sleep)
    with pytest.raises(CanaryError, match="no verdict"):
        run(plates(), moves(), adapters, lambda c, d, e: "maybe", sleep=no_sleep)


def test_run_verdict_shapes_and_the_advisory_rule():
    adapters = {"nano-banana-2.1": FakeAdapter("nano-banana-2.1", "image_edit")}
    shapes = iter([True, False, {"verdict": "pass"}, SimpleNamespace(verdict="advisory")] + ["pass"] * 8)
    report = run(plates(), moves(), adapters, lambda c, d, e: next(shapes), sleep=no_sleep)
    e = report.engines["nano-banana-2.1"]
    assert [r.verdict for r in e.results[:4]] == ["pass", "fail", "pass", "advisory"]
    assert e.passes == 11  # advisory is not a hard failure
    strict = iter([True, False, {"verdict": "pass"}, SimpleNamespace(verdict="advisory")] + ["pass"] * 8)
    report = run(
        plates(), moves(), adapters, lambda c, d, e: next(strict), sleep=no_sleep, advisory_is_pass=False
    )
    assert report.engines["nano-banana-2.1"].passes == 10


def test_run_model_string_is_the_vendor_reported_one_and_mixed_when_it_varies():
    from_usage = FakeAdapter("nano-banana-2.1", "image_edit", version="2026-10-17-b")
    assert (
        run(plates(), moves(), {"a": from_usage}, gates_failing(), sleep=no_sleep).engines["a"].model_version
        == "2026-10-17-b"
    )
    configured = FakeAdapter("nano-banana-2.1", "image_edit", version="cfg-1", usage_version=False)
    assert (
        run(plates(), moves(), {"a": configured}, gates_failing(), sleep=no_sleep).engines["a"].model_version
        == "cfg-1"
    )

    class Rolling(FakeAdapter):
        def poll(self, job):
            status = super().poll(job)
            n = int(job.split("-")[1])
            return JobStatus(
                state="done", outputs=status.outputs, usage={"model_version": "v1" if n % 2 else "v2"}
            )

    mixed = run(
        plates(), moves(), {"a": Rolling("nano-banana-2.1", "image_edit")}, gates_failing(), sleep=no_sleep
    )
    assert mixed.engines["a"].model_version == "mixed:v1|v2"


def test_report_round_trips_through_json():
    adapters = {
        "nano-banana-2.1": FakeAdapter(
            "nano-banana-2.1", "image_edit", script={"plate-02.png": RuntimeError("x")}
        )
    }
    report = run(plates(), moves(), adapters, gates_failing(), sleep=no_sleep)
    again = CanaryReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert again == report
    assert report.to_dict()["engines"]["nano-banana-2.1"]["pass_rate"] == pytest.approx(11 / 12)


# ---------------------------------------------------------------------------- drift and suspension


def ec(engine="nano-banana-2.1", n=12, passes=12, version="model-1"):
    return EngineCanary(engine_id=engine, route_id="gemini", n=n, passes=passes, model_version=version)


def test_drift_suspends_when_the_pass_rate_drops_more_than_15_points():
    base = ec(passes=12)
    assert drift(base, ec(passes=10)) is True  # 16.7 points
    assert drift(base, ec(passes=11)) is False  # 8.3 points
    assert drift(base, ec(passes=12)) is False
    assert drift(ec(passes=6), ec(passes=12)) is False  # got better
    assert drift(ec(n=20, passes=20), ec(n=20, passes=17)) is False  # exactly 15 points is not more than 15
    assert drift(ec(n=20, passes=20), ec(n=20, passes=16)) is True  # 20 points
    reasons = drift_reasons(base, ec(passes=10))
    assert len(reasons) == 1 and "16.7 points" in reasons[0]


def test_drift_suspends_when_the_model_string_changes():
    assert drift(ec(version="model-1"), ec(version="model-2")) is True
    assert drift(ec(version="model-1"), ec(version=None)) is True  # the string went missing
    assert drift(ec(version=None), ec(version="model-2")) is False  # no baseline string
    assert drift(ec(version=None), ec(version=None)) is False
    assert drift(ec(version="model-1"), ec(version="model-1")) is False


def test_drift_without_a_previous_run_is_a_baseline_and_accepts_mappings():
    assert drift(None, ec(passes=0)) is False
    assert drift({"pass_rate": 1.0, "model_version": "a"}, {"pass_rate": 0.5, "model_version": "a"}) is True


def test_drift_writes_alerts_and_the_log_says_who_is_suspended(tmp_path):
    log = AlertLog(tmp_path / "alerts.jsonl")
    assert drift(ec(), ec(passes=12), alerts=log) is False
    assert not (tmp_path / "alerts.jsonl").exists()
    assert drift(ec(), ec(passes=9, version="model-2"), alerts=log) is True
    alerts = log.read()
    assert [a.kind for a in alerts] == ["canary_drift", "engine_suspended"]
    assert all(a.engine_id == "nano-banana-2.1" and a.severity == "critical" for a in alerts)
    assert "pass rate fell 25.0 points" in alerts[0].message and "model string changed" in alerts[0].message
    assert alerts[0].data["reasons"] and alerts[0].at.endswith("Z")
    assert log.suspended_engines() == {"nano-banana-2.1"}
    text = (tmp_path / "alerts.jsonl").read_bytes()
    assert text.endswith(b"\n") and b"\r" not in text
    log.resume("nano-banana-2.1", "checked by hand")
    assert log.suspended_engines() == set()


def test_compare_reports_and_suspension_reaches_the_router(tmp_path, manifests):
    adapters = {
        "nano-banana-2.1": FakeAdapter("nano-banana-2.1", "image_edit"),
        "gpt-image-2.5": FakeAdapter("gpt-image-2.5", "image_edit"),
    }
    week1 = run(plates(), moves(), adapters, gates_failing(), sleep=no_sleep)
    week2 = run(
        plates(),
        moves(),
        adapters,
        gates_failing("plate-01", "plate-02", "plate-03", engines={"nano-banana-2.1"}),
        sleep=no_sleep,
    )
    log = AlertLog(tmp_path / "alerts.jsonl")
    assert compare(None, week2, alerts=log) == []
    suspended = compare(week1, week2, alerts=log)
    assert suspended == ["nano-banana-2.1"]
    assert log.suspended_engines() == {"nano-banana-2.1"}

    cloud = {k: manifests[k] for k in ("nano-banana-2.1", "gpt-image-2.5")}
    health = {p: HealthEntry("ok", 100.0) for m in cloud.values() for p in (r.provider for r in m.routes)}
    board = HealthBoard(health).apply_alerts(log)
    # nano is the cheaper engine, but it is suspended, so the router skips it
    assert choose("relight", cloud, ALL_GRADES, None, None, health).engine_id == "nano-banana-2.1"
    assert choose("relight", cloud, ALL_GRADES, None, None, board.as_mapping()).engine_id == "gpt-image-2.5"
    only_nano = {"nano-banana-2.1": cloud["nano-banana-2.1"]}
    with pytest.raises(Refusal) as exc:
        choose("relight", only_nano, ALL_GRADES, None, None, board.as_mapping())
    assert exc.value.code == "health"


def test_alert_log_skips_a_torn_last_line_and_rejects_unknown_kinds(tmp_path):
    log = AlertLog(tmp_path / "sub" / "alerts.jsonl")
    log.write("schema_change", "one", engine_id="a")
    with open(log.path, "ab") as fh:
        fh.write(b'{"kind": "schema_ch')
    assert [a.message for a in log.read()] == ["one"]
    with pytest.raises(ValueError, match="unknown alert kind"):
        log.write("whatever", "x")
    with pytest.raises(ValueError, match="unknown severity"):
        log.write("schema_change", "x", severity="loud")
    with pytest.raises(ValueError, match="unknown health status"):
        HealthEntry("on fire")
