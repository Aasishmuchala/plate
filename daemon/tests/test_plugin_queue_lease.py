"""Claim leases on the plugin queue (router plan T139).

A dock that takes a job and never answers (it died, or the answer was lost) must not strand the run that waits
for it: a claim older than `CLAIM_LEASE_S` goes back to the queue with `claim_count + 1`, and after
`CLAIM_LIMIT_PER_JOB` lapsed claims the job is failed, which the run's poll sees. Every call here passes `now=`,
so no test sleeps and none depends on the wall clock.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from plate_daemon.nodes.plugin_queue import (
    CLAIM_LEASE_S,
    CLAIM_LIMIT_PER_JOB,
    FILE_NAME,
    JobStateError,
    PluginQueue,
)
from plate_daemon.nodes.runner import PluginEngine
from plate_daemon.timeutil import iso

T0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=UTC)
LATE = f"no Max dock finished this job after {CLAIM_LIMIT_PER_JOB} claims"


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def make(tmp_path, **kw):
    q = PluginQueue(tmp_path)
    return q, q.enqueue(action="scout", project_id="p", now=T0, **kw)


def test_the_lease_constants():
    assert CLAIM_LEASE_S == 120 and CLAIM_LIMIT_PER_JOB == 3


def test_a_claim_three_minutes_old_is_offered_again(tmp_path):
    q, job = make(tmp_path)
    [first] = q.list_jobs(0, claim=True, now=T0)
    assert first.id == job.id and first.state == "claimed" and first.claim_count == 0

    [again] = q.list_jobs(0, claim=True, now=at(180))
    assert again.id == job.id and again.state == "claimed" and again.claim_count == 1
    assert again.claimed_utc == iso(at(180))  # the new claim is stamped, so its own lease starts now
    assert q.get(job.id).claim_count == 1


def test_a_fresh_claim_is_never_offered_twice(tmp_path):
    q, job = make(tmp_path)
    assert [j.id for j in q.list_jobs(0, claim=True, now=T0)] == [job.id]
    assert q.list_jobs(0, claim=True, now=at(1)) == []
    assert q.list_jobs(0, claim=True, now=at(CLAIM_LEASE_S - 1)) == []  # still inside the lease
    assert q.list_jobs(0, claim=True, now=at(CLAIM_LEASE_S)) == []  # the lease runs through its last second
    got = q.get(job.id)
    assert got.state == "claimed" and got.claim_count == 0 and got.claimed_utc == iso(T0)
    assert [j.id for j in q.list_jobs(0, claim=True, now=at(CLAIM_LEASE_S + 1))] == [job.id]


def test_a_listing_that_does_not_claim_leaves_the_lease_alone(tmp_path):
    q, job = make(tmp_path)
    q.list_jobs(0, claim=True, now=T0)
    assert q.list_jobs(0, states=("queued", "claimed"), now=at(600))[0].state == "claimed"
    assert q.get(job.id).claim_count == 0  # only a dock polling (claim=True) takes a claim back


def test_after_three_lapsed_claims_the_job_fails(tmp_path):
    q, job = make(tmp_path)
    t = 0
    for taken in (1, 2, 3):  # three docks take it and say nothing
        [got] = q.list_jobs(0, claim=True, now=at(t))
        assert got.id == job.id and got.claim_count == taken - 1
        t += 180
    assert q.list_jobs(0, claim=True, now=at(t)) == []  # the third lapse fails it instead of offering it
    failed = q.get(job.id)
    assert failed.state == "failed" and failed.claim_count == CLAIM_LIMIT_PER_JOB
    assert failed.result == {"detail": LATE} and failed.finished_utc == iso(at(t))
    assert q.list_jobs(0, claim=True, now=at(t + 600)) == []  # and it stays failed
    assert q.counts(now=at(t)) == {"queued": 0, "claimed": 0, "expired_claims": 0}
    with pytest.raises(JobStateError):
        q.finish(job.id, ok=True, now=at(t))  # a failed job takes no more answers


def test_the_run_waiting_for_the_job_sees_it_fail(tmp_path):
    q, job = make(tmp_path, run_id="run_1", key="k")
    engine = PluginEngine(SimpleNamespace(queue=q), run=SimpleNamespace(id="run_1"))
    assert engine.poll(job.id).status == "queued"
    for k in range(CLAIM_LIMIT_PER_JOB):
        q.list_jobs(0, claim=True, now=at(180 * k))
        assert engine.poll(job.id).status == "running"
    q.list_jobs(0, claim=True, now=at(180 * CLAIM_LIMIT_PER_JOB))
    polled = engine.poll(job.id)
    assert polled.status == "failed" and polled.detail == LATE


def test_a_late_answer_still_closes_a_job_that_is_waiting_again(tmp_path):
    q, job = make(tmp_path)
    q.list_jobs(0, claim=True, now=T0)
    # A second dock polls and the lease has lapsed: the job is claimed again (claim_count 1) ...
    [again] = q.list_jobs(0, claim=True, now=at(300))
    assert again.claim_count == 1
    # ... and the first dock's answer arrives after all: one answer per job, whoever sends it first.
    done = q.finish(job.id, ok=True, result={"packet_id": "pkt_x"}, now=at(301))
    assert done.state == "done" and done.claim_count == 1
    assert q.list_jobs(0, claim=True, now=at(900)) == []  # done jobs are never offered again
    assert q.get(job.id).state == "done" and q.get(job.id).claim_count == 1


def test_an_answered_job_is_not_touched_by_the_sweep(tmp_path):
    q, job = make(tmp_path)
    other = q.enqueue(action="scout", project_id="p", now=T0)
    q.list_jobs(0, claim=True, now=T0, limit=2)
    q.finish(job.id, ok=True, result={}, now=at(5))
    q.cancel(other.id, reason="run cancelled", now=at(6))
    assert q.list_jobs(0, claim=True, now=at(1000)) == []
    assert (q.get(job.id).state, q.get(other.id).state) == ("done", "cancelled")
    assert q.get(job.id).claim_count == 0 and q.get(other.id).claim_count == 0


def test_only_the_stale_claims_are_taken_back(tmp_path):
    q = PluginQueue(tmp_path)
    old = q.enqueue(action="scout", project_id="p", now=T0)
    q.list_jobs(0, claim=True, now=T0)
    fresh = q.enqueue(action="scout", project_id="p", now=at(100))
    waiting = q.enqueue(action="scout", project_id="p", now=at(100))
    q.list_jobs(old.seq, claim=True, now=at(100), limit=1)  # takes `fresh` only
    assert [j.state for j in (q.get(old.id), q.get(fresh.id), q.get(waiting.id))] == [
        "claimed",
        "claimed",
        "queued",
    ]
    got = q.list_jobs(0, claim=True, now=at(200), limit=5)  # `old` lapsed at 120, `fresh` has until 220
    assert [j.id for j in got] == [old.id, waiting.id]
    assert q.get(fresh.id).state == "claimed" and q.get(fresh.id).claimed_utc == iso(at(100))
    assert (q.get(old.id).claim_count, q.get(fresh.id).claim_count, q.get(waiting.id).claim_count) == (
        1,
        0,
        0,
    )


def test_a_dock_past_the_jobs_seq_is_still_offered_a_lapsed_job(tmp_path):
    """The dock keeps a cursor (`next`): the job it lost the answer to is below it, and must still come back."""
    q = PluginQueue(tmp_path)
    lost = q.enqueue(action="scout", project_id="p", now=T0)
    newer = q.enqueue(action="scout", project_id="p", now=T0)
    q.list_jobs(0, claim=True, now=T0, limit=2)
    q.finish(newer.id, ok=True, result={}, now=at(1))
    got = q.list_jobs(newer.seq, claim=True, now=at(200))  # cursor already past `lost`
    assert [j.id for j in got] == [lost.id] and got[0].claim_count == 1
    assert q.list_jobs(newer.seq, claim=True, now=at(201)) == []  # claimed again: not a third time over


def test_counts_report_claimed_and_expired_claims(tmp_path):
    q = PluginQueue(tmp_path)
    assert q.counts(now=T0) == {"queued": 0, "claimed": 0, "expired_claims": 0}
    for _ in range(3):
        q.enqueue(action="scout", project_id="p", now=T0)
    assert q.counts(now=T0) == {"queued": 3, "claimed": 0, "expired_claims": 0}
    q.list_jobs(0, claim=True, now=T0, limit=2)
    assert q.counts(now=at(CLAIM_LEASE_S)) == {"queued": 1, "claimed": 2, "expired_claims": 0}
    assert q.counts(now=at(CLAIM_LEASE_S + 1)) == {"queued": 1, "claimed": 2, "expired_claims": 2}
    q.list_jobs(0, claim=True, now=at(CLAIM_LEASE_S + 1), limit=10)  # a dock polls: all three claimed afresh
    assert q.counts(now=at(CLAIM_LEASE_S + 1)) == {"queued": 0, "claimed": 3, "expired_claims": 0}


def test_the_wire_form_carries_the_claim_count(tmp_path):
    q, job = make(tmp_path)
    assert job.to_dict()["claim_count"] == 0
    q.list_jobs(0, claim=True, now=T0)
    [again] = q.list_jobs(0, claim=True, now=at(500))
    assert again.to_dict()["claim_count"] == 1


def test_a_queue_file_made_before_leases_gains_the_column(tmp_path):
    path = tmp_path / FILE_NAME
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE plugin_queue (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, idempotency_key TEXT UNIQUE,
                action TEXT NOT NULL, project_id TEXT NOT NULL, packet_id TEXT, run_id TEXT, node_id TEXT,
                payload TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('queued', 'claimed', 'done', 'failed', 'cancelled')),
                result TEXT, created_utc TEXT NOT NULL, claimed_utc TEXT, finished_utc TEXT)"""
        )
        conn.execute(
            "INSERT INTO plugin_queue (id, action, project_id, payload, state, created_utc, claimed_utc) "
            "VALUES ('pj_old', 'scout', 'p', '{}', 'claimed', ?, ?)",
            (iso(T0), iso(T0)),
        )
    conn.close()
    q = PluginQueue(tmp_path)
    assert q.get("pj_old").claim_count == 0  # existing rows start at 0
    [got] = q.list_jobs(0, claim=True, now=at(300))  # and the lease applies to the job the old daemon left
    assert got.id == "pj_old" and got.claim_count == 1
    assert PluginQueue(tmp_path).get("pj_old").claim_count == 1  # opening it again does not migrate twice
