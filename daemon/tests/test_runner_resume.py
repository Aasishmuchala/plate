"""worker.Runner and crash-resume: priority-then-age order, per-route max_inflight, fetch at once, best-of-N,
FakeEngine behaviours and outputs, and recovery that re-polls instead of resubmitting, proven both with
in-process SimulatedCrash points and by terminating a real runner process."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from plate_daemon.adapter import SimulatedCrash
from plate_daemon.cas import CAS
from plate_daemon.db import tx
from plate_daemon.engine_fake import SLOW_POLLS, FakeEngine, payload_digest, render
from plate_daemon.index import Index, apply_packet_entry
from plate_daemon.machine import annotate, create_run, get_run, transition
from plate_daemon.models import Route, RunCreate
from plate_daemon.quotes import approve, approve_group, quote_run
from plate_daemon.states import TERMINAL, WORKER_STATES, RunState
from plate_daemon.worker import FanOutError, GateOutcome, Runner, build_payload, recover_on_boot, step

S = RunState
PID = "resume"
PACKET = "pkt_resume"
ROUTE = Route(engine_id="fake", provider="fake", region="local")
OTHER_ROUTE = Route(engine_id="fake-b", provider="fake", region="local")
T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)

CHILD = r"""
import json, sys
from pathlib import Path
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.index import Index
from plate_daemon.worker import Runner

home = Path(sys.argv[1])
engine = FakeEngine(journal=home / "fake_engine.json", **json.loads(sys.argv[2]))
runner = Runner(Index(home), None, {"*": engine})
runner.resume_on_boot()
runner.run_until_idle(500)
print("IDLE", flush=True)
"""


class Collector:
    """A stand-in for sse.Bus: keeps what would have been published."""

    def __init__(self):
        self.events: list[tuple[str | None, dict, str | None]] = []

    def publish(self, run_id, event, *, project_id=None):
        self.events.append((run_id, event, project_id))
        return len(self.events)


def make_index(home: Path, pid: str = PID) -> Index:
    idx = Index(home)
    with idx.project(pid) as conn, tx(conn):
        apply_packet_entry(
            conn,
            pid,
            PACKET,
            {
                "shot_id": "s01",
                "kind": "still",
                "tier": "A",
                "packet_version": "0.1.1",
                "path": str(home / "packets" / PACKET),
                "manifest_blake3": "a" * 64,
            },
        )
    return idx


def queued_run(
    idx: Index, *, priority="interactive", route=ROUTE, now=None, inputs=None, pid: str = PID
) -> str:
    with idx.project(pid) as conn:
        run = create_run(
            conn,
            RunCreate(
                project_id=pid,
                packet_id=PACKET,
                priority=priority,
                inputs=inputs or {"plate": uuid.uuid4().hex, "prompt": "low sun"},
                route=route,
            ),
            now=now,
        )
        quote_run(conn, run.id, now=now)
        approve(conn, run.id, "human", now=now)
        transition(conn, run.id, S.QUEUED, actor="human")
    idx.remember(run.id, pid)
    return run.id


def state_of(idx: Index, run_id: str, pid: str = PID) -> RunState:
    with idx.project(pid) as conn:
        return get_run(conn, run_id).state


def run_of(idx: Index, run_id: str, pid: str = PID):
    with idx.project(pid) as conn:
        return get_run(conn, run_id)


# ---------------------------------------------------------------------------- the spec's process kill


def _wait_for(path: Path, proc: subprocess.Popen, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        if proc.poll() is not None:
            raise AssertionError(f"runner exited ({proc.returncode}) before reaching the kill point")
        if time.monotonic() > deadline:
            raise AssertionError("runner never reached the kill point")
        time.sleep(0.05)


KILL_POINTS = [
    # (hang_at, hang_phase, extra engine flags, state the kill leaves)
    ("SUBMITTED", "before", {}, S.SUBMITTED),  # between SUBMITTED persisted and RUNNING (the plan's case)
    ("STAGING", "after_persist", {}, S.STAGING),  # provider id persisted, SUBMITTED not yet committed
    ("STAGING", "after_effect", {"supports_lookup": True}, S.STAGING),  # accepted, id lost
    ("RUNNING", "before", {}, S.RUNNING),
    ("FETCHING", "before", {}, S.FETCHING),
]


@pytest.mark.parametrize(
    ("hang_at", "phase", "flags", "left"), KILL_POINTS, ids=[f"{k[0]}-{k[1]}" for k in KILL_POINTS]
)
def test_terminated_runner_resumes_with_exactly_one_submit(tmp_path, child_env, hang_at, phase, flags, left):
    idx = make_index(tmp_path)
    rid = queued_run(idx)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            CHILD,
            str(tmp_path),
            json.dumps({"hang_at": hang_at, "hang_phase": phase, **flags}),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env(PLATE_HOME=str(tmp_path)),
        text=True,
        encoding="utf-8",
    )
    watchdog = threading.Timer(120, proc.kill)
    watchdog.start()
    try:
        _wait_for(tmp_path / "fake_engine.json.hang", proc)
        proc.terminate()  # TerminateProcess on Windows, SIGTERM elsewhere: no signal Windows lacks
        proc.wait(timeout=30)
    finally:
        watchdog.cancel()
        if proc.poll() is None:  # pragma: no cover - only if terminate failed
            proc.kill()
        proc.communicate(timeout=30)
    assert state_of(idx, rid) is left

    engine = FakeEngine(journal=tmp_path / "fake_engine.json", **flags)
    runner = Runner(idx, Collector(), {"*": engine})
    runner.resume_on_boot()
    runner.run_until_idle()
    run = run_of(idx, rid)
    assert run.state is S.DONE, run.error
    snap = FakeEngine(journal=tmp_path / "fake_engine.json").snapshot()
    assert snap["submit_count"] == 1
    assert [c["op"] for c in snap["calls"]].count("submit") == 1
    assert len(snap["jobs"]) == 1 and run.provider_job_id in snap["jobs"]


# ---------------------------------------------------------------------------- in-process crash matrix

CRASHES = [(st, ph) for st in WORKER_STATES for ph in ("before", "after_effect", "after_commit")] + [
    (S.STAGING, "after_persist")
]


@pytest.mark.parametrize(("crash_at", "phase"), CRASHES, ids=[f"{s.value}-{p}" for s, p in CRASHES])
def test_a_crash_anywhere_resumes_without_a_second_job(tmp_path, crash_at, phase):
    idx = make_index(tmp_path)
    rid = queued_run(idx)
    engine = FakeEngine(crash_at=crash_at, crash_phase=phase)
    with pytest.raises(SimulatedCrash):
        Runner(idx, None, {"*": engine}).run_until_idle()
    restarted = engine.restarted()
    runner = Runner(idx, None, {"*": restarted})
    resumed = {a["action"] for a in runner.resume_on_boot() if a.get("run_id") == rid}
    runner.run_until_idle()
    assert state_of(idx, rid) is S.DONE
    snap = restarted.snapshot()
    assert len(snap["jobs"]) == 1
    # Only a crash after the provider accepted but before the id was written may submit again, and then
    # under the same idempotency key, which the provider dedupes into the same job.
    lost_id = (crash_at, phase) == (S.STAGING, "after_effect")
    assert snap["submit_count"] == (2 if lost_id else 1)
    if lost_id:
        assert "restaged" in resumed
    if (crash_at, phase) == (S.STAGING, "after_persist"):
        assert "promoted" in resumed
    keys = {c["key"] for c in snap["calls"] if c["op"] == "submit"}
    assert keys == {run_of(idx, rid).idempotency_key}


def test_a_lost_id_is_adopted_by_lookup(tmp_path):
    idx = make_index(tmp_path)
    rid = queued_run(idx)
    engine = FakeEngine(crash_after_submit_before_persist=True, supports_lookup=True)
    with pytest.raises(SimulatedCrash):
        Runner(idx, None, {"*": engine}).run_until_idle()
    restarted = engine.restarted(supports_lookup=True)
    actions = Runner(idx, None, {"*": restarted}).resume_on_boot()
    assert {"run_id": rid, "action": "adopted"}.items() <= next(
        a for a in actions if a.get("run_id") == rid
    ).items()
    Runner(idx, None, {"*": restarted}).run_until_idle()
    assert state_of(idx, rid) is S.DONE and restarted.submit_count == 1


class BlindEngine(FakeEngine):
    """A provider that does not dedupe on the key and cannot look jobs up."""

    idempotent_submit = False


def test_a_lost_id_without_lookup_or_idempotency_fails_for_a_human(tmp_path):
    idx = make_index(tmp_path)
    rid = queued_run(idx)
    engine = BlindEngine(crash_after_submit_before_persist=True)
    with pytest.raises(SimulatedCrash):
        Runner(idx, None, {"*": engine}).run_until_idle()
    restarted = BlindEngine()
    restarted._state = engine.snapshot()
    Runner(idx, None, {"*": restarted}).resume_on_boot()
    run = run_of(idx, rid)
    assert run.state is S.FAILED and run.reason_code == "submit_state_unknown"
    assert restarted.submit_count == 1  # never resubmitted blind
    assert run.cost_usd_billed is None and run.reserved_usd == 0  # nothing reached a known job: refunded


def test_recover_on_boot_promotes_a_phase_one_run_and_expires_stale_quotes(tmp_path):
    idx = make_index(tmp_path)
    rid = queued_run(idx)
    engine = FakeEngine()
    with idx.project(PID) as conn:
        cas = CAS(tmp_path / "projects" / PID / "assets")
        assert step(conn, engine, rid, cas=cas) is S.STAGING
        job = engine.submit(build_payload(get_run(conn, rid)), get_run(conn, rid).idempotency_key)
        annotate(conn, rid, expect=S.STAGING, provider_job_id=job)  # phase one only
        stale = create_run(conn, RunCreate(project_id=PID, packet_id=PACKET, route=ROUTE), now=T0)
        quote_run(conn, stale.id, now=T0)
        actions = recover_on_boot(conn, engine)
        assert get_run(conn, rid).state is S.SUBMITTED and get_run(conn, stale.id).state is S.EXPIRED
    assert {a["action"] for a in actions} == {"promoted", "expired"}
    Runner(idx, None, {"*": engine}).run_until_idle()
    assert state_of(idx, rid) is S.DONE and engine.submit_count == 1


def test_a_staging_run_without_intent_is_restaged_with_the_same_key(tmp_path):
    idx = make_index(tmp_path)
    rid = queued_run(idx)
    engine = FakeEngine()
    with idx.project(PID) as conn:
        assert step(conn, engine, rid, cas=CAS(tmp_path / "projects" / PID / "assets")) is S.STAGING
        key = get_run(conn, rid).idempotency_key
    actions = Runner(idx, None, {"*": engine}).resume_on_boot()
    assert [a["action"] for a in actions if a.get("run_id") == rid] == ["restage"]
    Runner(idx, None, {"*": engine}).run_until_idle()
    assert state_of(idx, rid) is S.DONE
    assert [c["key"] for c in engine.calls if c["op"] == "submit"] == [key]


# ---------------------------------------------------------------------------- the runner


def test_tick_order_is_priority_then_age(tmp_path):
    idx = make_index(tmp_path)
    now = datetime.now(UTC)
    calib = queued_run(idx, priority="calibration", now=now)
    inter_old = queued_run(idx, priority="interactive", now=now + timedelta(seconds=1))
    deadline = queued_run(idx, priority="client_deadline", now=now + timedelta(seconds=2))
    inter_new = queued_run(idx, priority="interactive", now=now + timedelta(seconds=3))
    batch = queued_run(idx, priority="agent_batch", now=now + timedelta(seconds=4))
    runner = Runner(idx, None, {"*": FakeEngine(polls_until_done=1)}, default_max_inflight=1)
    started = [
        a["run_id"]
        for a in runner.run_until_idle()
        if a.get("action") == "advanced" and a.get("from") == "QUEUED"
    ]
    assert started == [deadline, inter_old, inter_new, batch, calib]


def _manifest(max_inflight: int):
    from plate_engines.models import EngineManifest

    return EngineManifest.model_validate(
        {
            "id": "fake",
            "name": "Fake",
            "version": "1",
            "pinned": True,
            "kind": "image_edit",
            "inputs": [{"name": "image", "type": "image"}],
            "outputs": [{"name": "image", "type": "image"}],
            "params": [],
            "price": {"model": "per_image", "usd": 0.04},
            "determinism": "seeded",
            "licence": {"vendor_terms_url": "unknown"},
            "allowed_tasks": ["relight"],
            "routes": [
                {
                    "provider": "fake",
                    "endpoint": "local",
                    "region": "local",
                    "custody_grade": "C0",
                    "custody_status": "confirmed",
                    "terms_hash": "unknown",
                    "upload_visibility": "local",
                    "max_inflight": max_inflight,
                }
            ],
        }
    )


@pytest.mark.parametrize(("manifests", "cap"), [(None, 2), ({"fake": _manifest(3)}, 3)])
def test_max_inflight_per_route(tmp_path, manifests, cap):
    idx = make_index(tmp_path)
    ids = [queued_run(idx) for _ in range(5)]
    other = queued_run(idx, route=OTHER_ROUTE)
    engine = FakeEngine(behaviour="slow")
    runner = Runner(idx, None, {"*": engine}, manifests=manifests)
    for _ in range(3):
        runner.tick()
        states = [state_of(idx, r) for r in ids]
        assert sum(s in (S.STAGING, S.SUBMITTED, S.RUNNING) for s in states) == cap
        assert states.count(S.QUEUED) == 5 - cap
        assert state_of(idx, other) is not S.QUEUED  # another route has its own slots
    runner.run_until_idle()
    assert all(state_of(idx, r) is S.DONE for r in ids)


def test_fetch_happens_in_the_same_tick_as_the_poll_that_saw_success(tmp_path):
    idx = make_index(tmp_path)
    rid = queued_run(idx)
    engine = FakeEngine(polls_until_done=2)
    bus = Collector()
    runner = Runner(idx, bus, {"fake": engine})
    seen = []
    while state_of(idx, rid) not in TERMINAL:
        before = engine.fetch_count
        runner.tick()
        seen.append((state_of(idx, rid), engine.fetch_count - before))
    # STAGING, SUBMITTED, RUNNING, then the succeeded poll fetches, normalises and gates at once.
    assert seen == [(S.STAGING, 0), (S.SUBMITTED, 0), (S.RUNNING, 0), (S.DONE, 1)]
    moves = [(e["from"], e["to"]) for _, e, _ in bus.events if e["kind"] == "run.state"]
    assert moves[-4:] == [
        ("RUNNING", "FETCHING"),
        ("FETCHING", "NORMALIZING"),
        ("NORMALIZING", "GATING"),
        ("GATING", "DONE"),
    ]
    assert all(p == PID for _, _, p in bus.events)


def test_a_run_without_an_adapter_waits(tmp_path):
    idx = make_index(tmp_path)
    rid = queued_run(idx)
    actions = Runner(idx, None, {"other-engine": FakeEngine()}).tick()
    assert actions == [{"project_id": PID, "run_id": rid, "state": "QUEUED", "action": "no_adapter"}]


def test_a_duplicate_request_is_cancelled_not_submitted(tmp_path):
    idx = make_index(tmp_path)
    inputs = {"plate": "c" * 64, "prompt": "same request"}
    first = queued_run(idx, inputs=inputs)
    second = queued_run(idx, inputs=inputs)
    engine = FakeEngine()
    Runner(idx, None, {"*": engine}).run_until_idle()
    assert state_of(idx, first) is S.DONE
    dup = run_of(idx, second)
    assert (
        dup.state is S.CANCELLED
        and dup.reason_code == "duplicate_request"
        and first in dup.history[-1].reason
    )
    assert engine.submit_count == 1 and dup.reserved_usd == 0


# ---------------------------------------------------------------------------- best-of-N


def _bright(run, cas) -> GateOutcome:
    img = Image.open(io.BytesIO(cas.read_bytes(run.result_key))).convert("L")
    score = sum(img.tobytes()) / (img.width * img.height)
    return GateOutcome(
        passed=True,
        report={"metrics": {"mean_luma": score}},
        score=score,
        report_path=f"gates/{run.id}.1.json",
    )


def test_fan_out_runs_n_siblings_under_one_quote_and_ranks_them_by_gate_score(tmp_path):
    idx = make_index(tmp_path)
    with idx.project(PID) as conn:
        draft = create_run(
            conn, RunCreate(project_id=PID, packet_id=PACKET, inputs={"plate": "d" * 64}, route=ROUTE)
        )
        _, quote = quote_run(conn, draft.id)
    idx.remember(draft.id, PID)
    bus = Collector()
    engine = FakeEngine(seed=3)
    runner = Runner(idx, bus, {"*": engine}, gate_fn=_bright)
    sibs = runner.fan_out(draft.id, 4)
    assert [s.sibling_index for s in sibs] == [0, 1, 2, 3] and sibs[0].id == draft.id
    assert len({s.sibling_group for s in sibs}) == 1 and sibs[0].sibling_group.startswith("grp_")
    assert {s.quote.expires_utc for s in sibs} == {quote.expires_utc}
    assert {s.cost_usd_quoted for s in sibs} == {quote.price_usd} and {s.quote.siblings for s in sibs} == {4}
    with idx.project(PID) as conn:
        approved = approve_group(conn, sibs[0].sibling_group, "human")
        assert [r.state for r in approved] == [S.APPROVED] * 4
        for s in sibs:
            transition(conn, s.id, S.QUEUED, actor="human")
    actions = runner.run_until_idle()
    done = [run_of(idx, s.id) for s in sibs]
    assert all(r.state is S.DONE for r in done)
    assert len({r.idempotency_key for r in done}) == 4 and engine.job_count == 4
    assert len({r.result_key for r in done}) == 4  # each sibling is its own generation
    assert all(r.gate_report_path == f"gates/{r.id}.1.json" for r in done)
    ranked = [a for a in actions if a.get("action") == "ranked"]
    expected = [r.id for r in sorted(done, key=lambda r: -r.gate_score)]
    assert len(ranked) == 1 and ranked[0]["order"] == expected
    assert runner.rank(sibs[0].sibling_group, PID)[0].id == expected[0]
    published = [e for _, e, _ in bus.events if e["kind"] == "siblings.ranked"]
    assert len(published) == 1 and published[0]["order"] == expected
    with pytest.raises(FanOutError):
        runner.fan_out(draft.id, 2)  # DONE, and already in a group


def test_fan_out_refuses_bad_requests(tmp_path):
    idx = make_index(tmp_path)
    with idx.project(PID) as conn:
        draft = create_run(conn, RunCreate(project_id=PID, packet_id=PACKET, route=ROUTE), now=T0)
    runner = Runner(idx, None, {"*": FakeEngine()})
    with pytest.raises(FanOutError, match="QUOTED"):
        runner.fan_out(draft.id, 3, PID)
    with idx.project(PID) as conn:
        quote_run(conn, draft.id, now=T0)
    with pytest.raises(FanOutError, match="expired"):
        runner.fan_out(draft.id, 3, PID)
    with pytest.raises(FanOutError, match="integer"):
        runner.fan_out(draft.id, 0, PID)


# ---------------------------------------------------------------------------- the fake engine


def _pixels(png: bytes) -> bytes:
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.mode == "RGB"
    return img.tobytes()


def test_fake_output_is_a_png_that_depends_on_the_input():
    eng = FakeEngine(seed=7, size=(32, 16))
    a = {"engine_id": "fake", "inputs": {"plate": "a" * 64}, "params": {"k": 1}}
    b = {"engine_id": "fake", "inputs": {"plate": "b" * 64}, "params": {"k": 1}}
    assert Image.open(io.BytesIO(eng.render(a))).size == (32, 16)
    assert _pixels(eng.render(a)) == _pixels(eng.render(dict(a)))  # deterministic
    assert _pixels(eng.render(a)) != _pixels(eng.render(b))  # output depends on input
    assert _pixels(eng.render(a)) != _pixels(FakeEngine(seed=8, size=(32, 16)).render(a))
    assert _pixels(eng.render(a)) != _pixels(eng.render({**a, "variant": 1}))
    job = eng.submit(a, "key-a")
    for _ in range(eng.polls_until_done):
        res = eng.poll(job)
    assert res.status == "succeeded" and eng.fetch(res.result_url) == render(
        7, *payload_digest(a), size=(32, 16)
    )
    assert eng.submit(a, "key-a") == job and eng.job_count == 1  # dedupes on the idempotency key


@pytest.mark.parametrize(
    ("behaviour", "state", "code"),
    [
        ("ok", S.DONE, None),
        ("fail_submit", S.FAILED, "submit_error"),
        ("refuse", S.REFUSED, "refused"),
        ("slow", S.DONE, None),
        ("expire_url", S.FAILED, "result_url_expired"),
    ],
)
def test_behaviours(tmp_path, behaviour, state, code):
    idx = make_index(tmp_path)
    rid = queued_run(idx)
    engine = FakeEngine(behaviour=behaviour)
    Runner(idx, None, {"*": engine}).run_until_idle()
    run = run_of(idx, rid)
    assert run.state is state and run.reason_code == code
    if behaviour == "slow":
        assert engine.poll_count >= SLOW_POLLS
    if behaviour == "fail_submit":
        assert engine.job_count == 0 and run.provider_job_id is None
    if state is S.DONE:
        assert run.outputs == [run.result_key] and _pixels(
            CAS(tmp_path / "projects" / PID / "assets").read_bytes(run.result_key)
        )


def test_the_flags_load_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_FAKE_ENGINE", json.dumps({"seed": 5, "behaviour": "slow", "size": [8, 8]}))
    eng = FakeEngine.from_env(journal=tmp_path / "j.json")
    assert (eng.seed, eng.behaviour, eng.size, eng.polls_until_done) == (5, "slow", (8, 8), SLOW_POLLS)
    monkeypatch.setenv("PLATE_FAKE_ENGINE", json.dumps({"bogus": 1}))
    with pytest.raises(ValueError, match="unknown FakeEngine flags"):
        FakeEngine.from_env(journal=None)
