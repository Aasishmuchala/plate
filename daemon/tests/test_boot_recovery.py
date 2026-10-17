"""Boot recovery and poll faults (router plan T64).

`worker.recover_on_boot(conn, engine, *, skip=is_pipeline_run)` reconciles seed runs against the one engine
it is given and leaves pipeline runs alone: they are driven by `Pipeline.resume`, which resolves each run's
real engine, so a pipeline run stuck in STAGING with a submit intent is never re-submitted to the FakeEngine.
A poll that raises (an unknown job, a transport fault) ends the run FAILED as `provider_poll_error` and does
not unwind the worker; the pipeline ledgers the move as `failed`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from plate_daemon import __main__ as daemon_main
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.db import tx
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.index import Index, apply_packet_entry
from plate_daemon.machine import annotate, create_run, get_run, transition
from plate_daemon.models import Route, RunCreate
from plate_daemon.pipeline import STAGING_KEY, Pipeline, is_pipeline_run
from plate_daemon.quotes import approve, quote_run
from plate_daemon.states import RunState
from plate_daemon.worker import Runner, recover_on_boot, step

S = RunState
PID = "boot"
PACKET = "pkt_boot"
ROUTE = Route(engine_id="fake", provider="fake", region="local")
T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


class UnknownJob(Exception):
    """What an adapter raises when the provider has no such job (a purged job id)."""


class PollRaises(FakeEngine):
    """A FakeEngine whose poll raises: the provider forgot the job, or the transport failed."""

    def __init__(self, exc: Exception | None = None, **kwargs):
        super().__init__(**kwargs)
        self.exc = exc or UnknownJob("no such job at the provider")

    def poll(self, provider_job_id):
        raise self.exc


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


def cas_of(home: Path) -> CAS:
    return CAS(home / "projects" / PID / "assets")


def queued(idx: Index, *, pipeline: bool) -> str:
    inputs = {"plate": uuid.uuid4().hex, "prompt": "low sun"}
    if pipeline:
        inputs[STAGING_KEY] = {"node_kind": "Edit", "region_crop": False}
    with idx.project(PID) as conn:
        run = create_run(
            conn, RunCreate(project_id=PID, packet_id=PACKET, inputs=inputs, route=ROUTE), now=T0
        )
        quote_run(conn, run.id, now=T0)
        approve(conn, run.id, "human", now=T0)
        transition(conn, run.id, S.QUEUED, actor="human")
    idx.remember(run.id, PID)
    return run.id


def staging_with_intent(idx: Index, home: Path, *, pipeline: bool) -> str:
    """A run a dead daemon left in STAGING after it wrote its submit intent: it may or may not have sent."""
    rid = queued(idx, pipeline=pipeline)
    with idx.project(PID) as conn:
        assert step(conn, FakeEngine(), rid, cas=cas_of(home)) is S.STAGING  # QUEUED -> STAGING
        annotate(conn, rid, expect=S.STAGING, submit_started_utc=T0)
        assert is_pipeline_run(get_run(conn, rid)) is pipeline
    return rid


def snapshot(idx: Index, rid: str) -> dict:
    with idx.project(PID) as conn:
        r = get_run(conn, rid)
    return {
        "state": r.state,
        "provider_job_id": r.provider_job_id,
        "submit_started_utc": r.submit_started_utc,
        "history": len(r.history),
        "reason_code": r.reason_code,
        "updated_utc": r.updated_utc,
    }


# ---------------------------------------------------------------------------- boot recovery skips pipeline runs


def test_a_staging_pipeline_run_with_a_submit_intent_survives_boot_recovery(tmp_path):
    idx = make_index(tmp_path)
    rid = staging_with_intent(idx, tmp_path, pipeline=True)
    before = snapshot(idx, rid)
    assert before["state"] is S.STAGING and before["provider_job_id"] is None
    engine = FakeEngine()  # idempotent_submit: left to itself, recovery would re-stage and submit
    with idx.project(PID) as conn:
        actions = recover_on_boot(conn, engine)
    assert actions == []
    assert snapshot(idx, rid) == before  # state, intent and history untouched
    assert (
        engine.submit_count == 0 and engine.call_count("submit") == 0
    )  # the FakeEngine journal has no submit
    assert engine.calls == []


def test_without_the_skip_the_same_run_is_resubmitted_to_the_fake(tmp_path):
    """The control: this is the bug the default skip closes (boot sent a real-adapter run to the FakeEngine)."""
    idx = make_index(tmp_path)
    rid = staging_with_intent(idx, tmp_path, pipeline=True)
    engine = FakeEngine()
    with idx.project(PID) as conn:
        actions = recover_on_boot(conn, engine, skip=None)
    assert [a["action"] for a in actions] == ["restaged"]
    assert engine.submit_count == 1 and snapshot(idx, rid)["state"] is S.SUBMITTED


def test_a_seed_run_is_still_restaged_beside_a_skipped_pipeline_run(tmp_path):
    idx = make_index(tmp_path)
    seed = staging_with_intent(idx, tmp_path, pipeline=False)
    pipe = staging_with_intent(idx, tmp_path, pipeline=True)
    pipe_before = snapshot(idx, pipe)
    engine = FakeEngine()
    with idx.project(PID) as conn:
        actions = recover_on_boot(conn, engine, skip=is_pipeline_run)  # the explicit form __main__ uses
    assert [(a["run_id"], a["action"]) for a in actions] == [(seed, "restaged")]
    assert snapshot(idx, seed)["state"] is S.SUBMITTED
    assert snapshot(idx, pipe) == pipe_before
    assert engine.submit_count == 1  # exactly the seed run's submit
    with idx.project(PID) as conn:
        assert get_run(conn, seed).provider_job_id == engine.calls[0]["job"]


@pytest.mark.parametrize("with_job_id", [False, True], ids=["no-job-id", "phase-one-job-id"])
def test_skip_covers_every_recovery_branch(tmp_path, with_job_id):
    idx = make_index(tmp_path)
    rid = staging_with_intent(idx, tmp_path, pipeline=True)
    engine = FakeEngine()
    if with_job_id:  # phase one committed: recovery would promote it to SUBMITTED
        with idx.project(PID) as conn:
            annotate(conn, rid, expect=S.STAGING, provider_job_id="job-1")
    before = snapshot(idx, rid)
    with idx.project(PID) as conn:
        assert recover_on_boot(conn, engine) == []
        assert recover_on_boot(conn, engine, skip=lambda run: run.id == rid) == []  # a custom predicate
    assert snapshot(idx, rid) == before and engine.calls == []


def test_the_default_skip_is_the_pipeline_predicate():
    import inspect

    from plate_daemon.worker import is_pipeline_run as worker_predicate

    default = inspect.signature(recover_on_boot).parameters["skip"].default
    assert default is worker_predicate

    class Seed:
        inputs = {"plate": "a"}

    class Piped:
        inputs = {STAGING_KEY: {"node_kind": "Edit"}}

    assert worker_predicate(Seed()) is False and worker_predicate(Piped()) is True
    assert is_pipeline_run(Seed()) is False and is_pipeline_run(Piped()) is True  # the two agree


def test_main_recovers_seed_runs_and_leaves_pipeline_runs(tmp_path, monkeypatch, capsys):
    """`python -m plate_daemon` wiring: boot reconciles the seed run and does not touch the pipeline run."""
    idx = make_index(tmp_path)
    seed = staging_with_intent(idx, tmp_path, pipeline=False)
    pipe = staging_with_intent(idx, tmp_path, pipeline=True)
    pipe_before = snapshot(idx, pipe)
    served: list[bool] = []

    class FakeServer:
        def __init__(self, config):
            pass

        def run(self, sockets=None):
            served.append(True)

    import uvicorn

    import plate_daemon.api as api

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(api, "create_app", lambda *a, **k: object())
    monkeypatch.setattr(daemon_main, "bind_sockets", lambda hosts, port: [])
    assert Settings.load().home == tmp_path
    assert daemon_main.main() == 0
    assert served == [True]
    assert snapshot(idx, seed)["state"] is S.SUBMITTED  # re-staged against the fake engine
    assert snapshot(idx, pipe) == pipe_before  # left for Pipeline.resume
    engine = FakeEngine(journal=tmp_path / "fake_engine.json")
    assert engine.submit_count == 1
    capsys.readouterr()


# ---------------------------------------------------------------------------- a poll that raises fails the run


def submitted_run(idx: Index, home: Path, *, pipeline: bool, good: FakeEngine | None = None) -> str:
    rid = queued(idx, pipeline=pipeline)
    good = good or FakeEngine()
    with idx.project(PID) as conn:
        assert step(conn, good, rid, cas=cas_of(home)) is S.STAGING
        assert step(conn, good, rid, cas=cas_of(home)) is S.SUBMITTED
    return rid


@pytest.mark.parametrize("state_to_poll", [S.SUBMITTED, S.RUNNING])
def test_a_poll_that_raises_unknown_job_ends_failed(tmp_path, state_to_poll):
    idx = make_index(tmp_path)
    good = FakeEngine()
    rid = submitted_run(idx, tmp_path, pipeline=False, good=good)
    if state_to_poll is S.RUNNING:
        with idx.project(PID) as conn:
            assert step(conn, good, rid, cas=cas_of(tmp_path)) is S.RUNNING
    engine = PollRaises()
    with idx.project(PID) as conn:
        assert step(conn, engine, rid, cas=cas_of(tmp_path)) is S.FAILED  # returned, not raised
        run = get_run(conn, rid)
    assert run.state is S.FAILED and run.reason_code == "provider_poll_error"
    assert run.error == "provider_poll_error: UnknownJob: no such job at the provider"
    assert run.provider_job_id  # a job may exist at the provider: kept for the console, and not refunded
    assert run.cost_usd_billed is not None and run.reserved_usd == 0  # settled at the quote, not refunded


def test_a_poll_fault_reason_is_capped_at_500_characters(tmp_path):
    idx = make_index(tmp_path)
    rid = submitted_run(idx, tmp_path, pipeline=False)
    with idx.project(PID) as conn:
        assert step(conn, PollRaises(RuntimeError("x" * 2000)), rid, cas=cas_of(tmp_path)) is S.FAILED
        run = get_run(conn, rid)
    assert run.history[-1].reason.startswith("RuntimeError: xxx") and len(run.history[-1].reason) <= 500


def test_a_simulated_crash_is_not_a_poll_fault(tmp_path):
    from plate_daemon.adapter import SimulatedCrash

    idx = make_index(tmp_path)
    rid = submitted_run(idx, tmp_path, pipeline=False)
    with idx.project(PID) as conn, pytest.raises(SimulatedCrash):
        step(conn, PollRaises(SimulatedCrash("process died")), rid, cas=cas_of(tmp_path))
    assert snapshot(idx, rid)["state"] is S.SUBMITTED  # a crash leaves the run for recovery


def test_the_runner_survives_a_poll_fault_and_publishes_the_failure(tmp_path):
    idx = make_index(tmp_path)
    rid = submitted_run(idx, tmp_path, pipeline=False)
    bus = Collector()
    actions = Runner(idx, bus, {"*": PollRaises()}).tick()
    assert [(a["run_id"], a["state"]) for a in actions if a.get("action") == "advanced"] == [(rid, "FAILED")]
    assert snapshot(idx, rid)["state"] is S.FAILED
    assert [e["to"] for _r, e, _p in bus.events if e.get("kind") == "run.state"] == ["FAILED"]


def test_the_pipeline_ledgers_a_poll_fault_as_failed(plate_env, tmp_path):
    idx = make_index(tmp_path)
    rid = submitted_run(idx, tmp_path, pipeline=True)
    bus = Collector()
    pipeline = Pipeline(
        settings=Settings.load(),
        index=idx,
        cas_for=lambda _pid: cas_of(tmp_path),
        bus=bus,
        engine=PollRaises(),
        manifests={},
        clock=lambda: T0,
    )
    with idx.project(PID) as conn:
        assert pipeline.step(conn, PID, rid) is S.FAILED
        run = get_run(conn, rid)
    assert run.reason_code == "provider_poll_error"
    events = [e for _r, e, _p in bus.events if e.get("kind") == "run.state"]
    assert events == [
        {
            "kind": "run.state",
            "run_id": rid,
            "from": "SUBMITTED",
            "to": "FAILED",
            "reason_code": "provider_poll_error",
        }
    ]
    ledger = pipeline.project_dir(PID) / "runs.jsonl"
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    failed = [r for r in records if r["kind"] == "failed" and r["run_id"] == rid]
    assert len(failed) == 1
    assert failed[0]["data"]["reason_code"] == "provider_poll_error"
    assert failed[0]["provider_job_id"] == run.provider_job_id
    assert "refunded" not in failed[0]["data"]  # a job may exist at the provider: no refund flag
