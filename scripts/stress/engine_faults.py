#!/usr/bin/env python3
"""The engine-fault attacker's findings as checks of the fixed behaviour (router/PLAN.md Wave 12, T130/T131/T145).

    python scripts/stress/engine_faults.py [--keep] [--only <check>] [--list]

The attacker drove a fault-injecting subclass of the fake engine through a launcher. These checks keep the daemon
unpatched (`python -m plate_daemon`) and inject the same faults from outside, with the fake's own flags
(`PLATE_FAKE_ENGINE`), the daemon's settings and the file system:

- decode-bomb-crashloop: a result whose header declares more pixels than `PLATE_RESULT_MAX_PIXELS` fails
  `result_too_large` from its header, and the daemon boots and stays up across two restarts (the attacker's
  16000x16000 PNG is the same refusal at the default cap; the cap is lowered here so the fake's own 640x360 result
  plays the bomb without the fake allocating 768 MB to draw one).
- fetch-stall-cancel: with the vendor call stalled (`hang_at`), 45 cancel clicks answer at once (202 while the
  run sits in RUNNING, 409 in FETCHING) and /health stays fast.
- inflight-threads: 45 approvals of runs that never finish (`timeout`) answer at once and /health stays under 1 s.
- resume-starvation: after a SIGKILL with two runs in flight, the restarted daemon polls both, not one at a time.
- submit-timeout: the provider took the job and the daemon died before it saw the id
  (`crash_after_submit_before_persist`; the harness kills the process if the scripted crash does not): the
  restarted daemon adopts or re-stages it on the same key (one job at the provider), never records a refund,
  never resubmits blind. The attacker's variant, an httpx timeout raised by a non-idempotent provider after it
  created the job, cannot be injected into an unpatched daemon; its classification (`submit_state_unknown`,
  `billing_unknown`) is pinned by daemon/tests/test_stress_wave12_pipeline.py and test_stress_wave12_worker.py,
  and this check also passes on 1f272f2, whose boot recovery already re-staged a lost submit.
- poll-transient: transport faults between polls (the fake's journal swapped for a folder, so its writes raise
  OSError) are tolerated with `poll_fault` notes, and the run finishes once they stop.
- unbounded-download and undecodable-results: a result that is not an image (`output: text`) fails
  `result_not_image` at FETCHING, naming the bytes, and nothing of it is written into the CAS. (The streamed byte
  cap of a real download lives in the adapters and is unit-tested in engines/tests/test_adapter_fetch_stream.py.)
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import _lib
from _lib import Daemon, check, note

PROJECT = "ef"
TEXT_MAGIC = b"PLATE-FAKE-RESULT"
_TEXT: dict[str, Any] | None = None


def wait_file(path: Path, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.1)
    return path.exists()


def stays_up(d: Daemon, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not d.alive():
            return False
        time.sleep(0.25)
    return d.alive() and d.health_ms(5) < 5000


def swap_journal(d: Daemon) -> tuple[Path, Path]:
    """Replace the fake's journal by a folder: every write of the fake (submit, poll) raises OSError."""
    journal = d.home / "fake_engine.json"
    backup = d.home / "fake_engine.json.saved"
    if journal.is_file():
        journal.replace(backup)
    journal.mkdir()
    return journal, backup


# ---------------------------------------------------------------------------- checks


def decode_bomb_crashloop() -> None:
    with Daemon(name="ef-bomb", env={"PLATE_RESULT_MAX_PIXELS": "100000"}) as d:
        d.edit_project(PROJECT)
        rid = d.quote_run(PROJECT)
        d.approve(rid)
        run = d.wait_run(rid, 60)
        check(
            run.get("state") == "FAILED" and run.get("reason_code") == "result_too_large",
            f"an over-cap result ended {run.get('state')} {run.get('reason_code')} {str(run.get('error'))[:160]}",
        )
        check(
            "640x360" in str(run.get("error")),
            f"the refusal does not name the declared size: {run.get('error')}",
        )
        for boot in (1, 2):
            d.restart()
            check(stays_up(d), f"restart {boot}: the daemon did not stay up")
            again = d.run(rid)
            check(again.get("state") == "FAILED", f"restart {boot}: the poisoned run is {again.get('state')}")


def fetch_stall_cancel() -> None:
    with Daemon(name="ef-stall", fake={"echo": True, "hang_at": "RUNNING", "hang_phase": "before"}) as d:
        d.edit_project(PROJECT)
        rid = d.quote_run(PROJECT)
        d.approve(rid)
        if not check(wait_file(d.home / "fake_engine.json.hang", 60), "the vendor call never stalled"):
            return
        clients = [d.make_client(timeout=10) for _ in range(45)]
        t0 = time.monotonic()
        try:
            answers = _lib.concurrently(
                lambda i: clients[i].post(f"/runs/{rid}/cancel", json={}), 45, timeout_s=60
            )
        finally:
            for c in clients:
                c.close()
        dt = time.monotonic() - t0
        codes = Counter(_lib.status_of(r) for r in answers)
        check(set(codes) <= {202, 200, 409}, f"45 cancels during a stalled poll: {dict(codes)} in {dt:.1f} s")
        check(codes.get(202, 0) >= 1, f"no cancel was recorded as requested: {dict(codes)}")
        check(dt < 15, f"45 cancels took {dt:.1f} s")
        ms = d.health_ms(10)
        check(ms < 1000, f"/health took {ms:.0f} ms while the vendor call stalls")
        check(d.api("GET", f"/runs/{rid}").status_code == 200, "GET run did not answer during the stall")

    with Daemon(
        name="ef-stall-fetch", fake={"echo": True, "hang_at": "FETCHING", "hang_phase": "before"}
    ) as d:
        d.edit_project(PROJECT)
        rid = d.quote_run(PROJECT)
        d.approve(rid)
        if not check(wait_file(d.home / "fake_engine.json.hang", 60), "the download never stalled"):
            return
        t0 = time.monotonic()
        r = d.make_client(timeout=10).post(f"/runs/{rid}/cancel", json={})
        dt = time.monotonic() - t0
        check(
            r.status_code == 409 and dt < 3,
            f"cancel during a stalled download -> {r.status_code} in {dt:.1f} s",
        )
        check(d.health_ms(10) < 1000, "/health slowed down during the stalled download")


def inflight_threads() -> None:
    with Daemon(name="ef-inflight", fake={"echo": True, "timeout": True}) as d:
        d.edit_project(PROJECT)
        c = d.make_client(timeout=10)
        approved, slow = 0, []
        try:
            for i in range(45):
                t0 = time.monotonic()
                q = c.post(
                    f"/projects/{PROJECT}/nodes/edit/quote", json={"engine_id": "fake", "prompt": f"i{i}"}
                )
                a = c.post(f"/runs/{q.json()['run']['id']}/approve", json={"by": "human"})
                if a.status_code == 200:
                    approved += 1
                if time.monotonic() - t0 > 3:
                    slow.append(i)
        except Exception as e:
            check(False, f"the API stopped answering after {approved} approvals: {type(e).__name__}")
        finally:
            c.close()
        check(approved == 45, f"{approved} of 45 approvals answered 200")
        check(not slow, f"approvals {slow[:5]} took over 3 s")
        time.sleep(2)
        ms = d.health_ms(10)
        check(ms < 1000, f"/health took {ms:.0f} ms with 45 runs in flight")
        t0 = time.monotonic()
        e = d.api("GET", "/engines")
        check(e.status_code == 200 and time.monotonic() - t0 < 2, "/engines did not answer within 2 s")
        states = Counter(d.run_states(PROJECT).values())
        note(f"run states {dict(states)}")


def resume_starvation() -> None:
    with Daemon(name="ef-resume", fake={"echo": True, "timeout": True}) as d:
        d.edit_project(PROJECT)
        a = d.quote_run(PROJECT, prompt="a")
        d.approve(a)
        time.sleep(0.6)
        b = d.quote_run(PROJECT, prompt="b")
        d.approve(b)
        both = lambda: all(d.run(r).get("state") == "RUNNING" for r in (a, b))  # noqa: E731
        deadline = time.monotonic() + 30
        while not both() and time.monotonic() < deadline:
            time.sleep(0.3)
        if not check(
            both(), f"the runs never both reached RUNNING: {d.run(a).get('state')} {d.run(b).get('state')}"
        ):
            return
        jobs = {r: d.run(r).get("provider_job_id") for r in (a, b)}
        d.kill(_lib.sigkill())
        before = {r: d.journal()["jobs"][j]["polls"] for r, j in jobs.items()}
        d.start()  # the vendor still says "running" for both: both must be polled
        time.sleep(9)
        after = {r: d.journal()["jobs"][j]["polls"] for r, j in jobs.items()}
        grew = {r: after[r] - before[r] for r in jobs}
        check(
            all(n >= 2 for n in grew.values()),
            f"polls per run in 9 s after the restart: {grew} (one run starved)",
        )
        d.restart(env={"PLATE_FAKE_ENGINE": json.dumps({"echo": True})})  # the vendor finishes both
        states = {r: d.wait_run(r, 60).get("state") for r in (a, b)}
        check(set(states.values()) == {"DONE"}, f"after the vendor finished: {states}")


def submit_timeout() -> None:
    with Daemon(name="ef-submit", fake={"echo": True, "crash_after_submit_before_persist": True}) as d:
        d.edit_project(PROJECT)
        rid = d.quote_run(PROJECT)
        d.approve(rid)
        deadline = time.monotonic() + 10
        while d.alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        if (
            d.alive()
        ):  # the run executor logs a SimulatedCrash and carries on: end the process as a crash would
            note("the scripted crash did not end the process; the harness killed it")
            d.kill(_lib.sigkill())
        d.start(env={"PLATE_FAKE_ENGINE": "1"})
        run = d.wait_run(rid, 90)
        state, code = run.get("state"), run.get("reason_code")
        check(
            state == "DONE" or (state == "FAILED" and code == "submit_state_unknown"),
            f"a lost submit answer ended {state} {code} {str(run.get('error'))[:160]}",
        )
        jobs = d.journal().get("jobs") or {}
        check(len(jobs) == 1, f"the provider holds {len(jobs)} jobs for one approval (a blind resubmit)")
        if state == "DONE":
            check(
                run.get("provider_job_id") in jobs,
                f"the run finished on {run.get('provider_job_id')}, not the job it lost",
            )
        refunded = [
            x for x in d.ledger(PROJECT) if x.get("run_id") == rid and (x.get("data") or {}).get("refunded")
        ]
        check(not refunded, f"the ledger records a refund for a job the vendor holds: {refunded[:1]}")
        note(f"{state} {code or ''}".strip())


def poll_transient() -> None:
    with Daemon(name="ef-poll", fake={"echo": True, "polls_until_done": 6}) as d:
        d.edit_project(PROJECT)
        rid = d.quote_run(PROJECT)
        d.approve(rid)
        d.wait_run(rid, 30, until=lambda r: r.get("state") == "RUNNING")
        time.sleep(1.0)
        journal, backup = swap_journal(d)
        try:
            deadline = time.monotonic() + 15
            faults: list[Any] = []
            while time.monotonic() < deadline and len(faults) < 2:
                faults = [
                    e for e in d.api("GET", f"/runs/{rid}/events").json() if e.get("kind") == "poll_fault"
                ]
                time.sleep(0.3)
        finally:
            journal.rmdir()
            if backup.exists():
                backup.replace(journal)
        check(len(faults) >= 2, f"{len(faults)} poll faults noted while the transport failed")
        run = d.wait_run(rid, 60)
        check(
            run.get("state") == "DONE",
            f"after transient poll faults the run ended {run.get('state')} {run.get('reason_code')}",
        )


def text_result() -> dict[str, Any]:
    """One run whose result is a line of text (the fake's `output: text`), shared by the last two checks."""
    global _TEXT
    if _TEXT is not None:
        return _TEXT
    with Daemon(name="ef-text", fake={"output": "text"}) as d:
        d.edit_project(PROJECT)
        rid = d.quote_run(PROJECT)
        d.approve(rid)
        run = d.wait_run(rid, 60)
        assets = d.home / "projects" / PROJECT / "assets"
        blobs = [
            p for p in assets.rglob("*") if p.is_file() and p.read_bytes()[: len(TEXT_MAGIC)] == TEXT_MAGIC
        ]
        bins = [p.name for p in assets.rglob("*.bin")]
        _TEXT = {"run": run, "blobs": [p.name for p in blobs], "bins": bins}
    return _TEXT


def unbounded_download() -> None:
    got = text_result()
    check(not got["blobs"], f"the non-image result was written into the CAS: {got['blobs']}")
    check(not got["bins"], f"opaque .bin blobs in the CAS: {got['bins']}")


def undecodable_results() -> None:
    run = text_result()["run"]
    check(
        run.get("state") == "FAILED" and run.get("reason_code") == "result_not_image",
        f"a text result ended {run.get('state')} {run.get('reason_code')} {str(run.get('error'))[:160]}",
    )
    err = str(run.get("error") or "")
    check(
        "not an image" in err or "magic" in err or "PLATE-FAKE" in err,
        f"the error does not name the bytes: {err[:200]}",
    )


CHECKS = {
    "decode-bomb-crashloop": decode_bomb_crashloop,
    "fetch-stall-cancel": fetch_stall_cancel,
    "inflight-threads": inflight_threads,
    "resume-starvation": resume_starvation,
    "submit-timeout": submit_timeout,
    "poll-transient": poll_transient,
    "unbounded-download": unbounded_download,
    "undecodable-results": undecodable_results,
}

if __name__ == "__main__":
    sys.exit(_lib.main(CHECKS))
