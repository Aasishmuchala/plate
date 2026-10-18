"""Wave 12 worker and engine faults (router plan T130, decision 76), each test the attacker's repro.

Vendor faults are classified, never guessed: a submit whose answer was lost resolves through lookup or an
idempotent re-stage, else fails `submit_state_unknown`; a refusal on this side is `staging_error` and never
degrades the route; a poll survives up to four transport faults in a row; a lapsed result URL is re-polled once;
a result is checked before the CAS (empty, not an image, a declared decompression bomb); a cancel never waits on a
stalled step; a full disk refuses before anything is sent; egress streams a download instead of buffering it; and
the echoing fake keeps its sources beside its journal, so a daemon killed in FETCHING resumes to NORMALIZING.
The fake engine is scripted in-process; the last test kills and restarts a real `python -m plate_daemon`.
"""

from __future__ import annotations

import base64
import collections
import json
import subprocess
import sys
import threading
import time
import tracemalloc
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import cv2
import httpx
import numpy as np
import pytest

from plate_daemon import worker
from plate_daemon.adapter import PollResult, ResultExpired
from plate_daemon.cas import CAS
from plate_daemon.custody import CustodyViolation, Grade
from plate_daemon.db import tx
from plate_daemon.egress import Egress, EgressBlocked, EgressRun
from plate_daemon.engine_fake import FakeEngine, SubmitRejected
from plate_daemon.index import Index, apply_packet_entry
from plate_daemon.machine import IllegalTransition, create_run, get_run, run_events, transition
from plate_daemon.models import Route, RunCreate
from plate_daemon.outbound_log import OutboundLog
from plate_daemon.pipeline import PipelineError
from plate_daemon.quotes import approve, quote_run
from plate_daemon.route_health import Tracker
from plate_daemon.states import RunState
from plate_daemon.timeutil import utcnow
from plate_daemon.worker import CancelPending, cancel_run, run_to_completion, step
from plate_engines.adapters.base import Adapter, FetchError, JobStatus, ParamError
from plate_engines.loader import load_manifests
from plate_engines.models import EngineSize

S = RunState
PID = "wave12"
PACKET = "pkt_wave12"
ROUTE = Route(engine_id="fake", provider="fake", region="local")
T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
MiB = 1024 * 1024
REPO = Path(__file__).resolve().parents[2]
ROOM_A = REPO / "fixtures" / "packets" / "room_a"


# ---------------------------------------------------------------------------- harness


def make_index(home: Path) -> Index:
    idx = Index(home)
    with idx.project(PID) as conn, tx(conn):
        apply_packet_entry(
            conn,
            PID,
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


def queued(idx: Index) -> str:
    with idx.project(PID) as conn:
        run = create_run(
            conn,
            RunCreate(
                project_id=PID,
                packet_id=PACKET,
                inputs={"plate": uuid.uuid4().hex, "prompt": "low sun"},
                route=ROUTE,
            ),
            now=T0,
        )
        quote_run(conn, run.id, now=T0)
        approve(conn, run.id, "human", now=T0)
        transition(conn, run.id, S.QUEUED, actor="human")
    idx.remember(run.id, PID)
    return run.id


def drive(idx: Index, home: Path, rid: str, engine, **kw) -> RunState:
    with idx.project(PID) as conn:
        return run_to_completion(
            conn, engine, rid, cas=cas_of(home), sleep=lambda _s: None, poll_interval_s=0, **kw
        )


def one_step(idx: Index, home: Path, rid: str, engine, **kw) -> RunState:
    with idx.project(PID) as conn:
        return step(conn, engine, rid, cas=cas_of(home), **kw)


def run_of(idx: Index, rid: str):
    with idx.project(PID) as conn:
        return get_run(conn, rid)


def notes_of(idx: Index, rid: str, kind: str) -> list:
    with idx.project(PID) as conn:
        return [e for e in run_events(conn, rid) if e.kind == kind]


def to_staging(idx: Index, home: Path, engine) -> str:
    rid = queued(idx)
    assert one_step(idx, home, rid, engine) is S.STAGING
    return rid


class Calls(SimpleNamespace):
    """A route-health tracker that keeps what the worker told it."""

    def __init__(self):
        super().__init__(seen=[])
        self.note_failure = lambda *a: self.seen.append(("fail", *a))
        self.note_success = lambda *a: self.seen.append(("ok", *a))


def png(w: int = 32, h: int = 24) -> bytes:
    ok, enc = cv2.imencode(".png", np.full((h, w, 3), 90, np.uint8))
    assert ok
    return bytes(enc)


def png_bomb(w: int, h: int) -> bytes:
    """A valid 1-bit greyscale PNG of w x h zeros: tens of KB on disk, w * h pixels once decoded."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            len(data).to_bytes(4, "big")
            + tag
            + data
            + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    row = b"\x00" * (1 + (w + 7) // 8)
    z = zlib.compressobj(9)
    idat = b"".join(z.compress(row) for _ in range(h)) + z.flush()
    ihdr = w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([1, 0, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# ---------------------------------------------------------------------------- (1) ambiguous submits


class LostAnswer(FakeEngine):
    """The provider created the job, then the answer was lost on the way back (the first `times` submits)."""

    def __init__(self, exc: BaseException | None = None, *, times: int = 1, idempotent: bool = True, **kw):
        super().__init__(polls_until_done=1, **kw)
        self.exc = exc or httpx.ReadTimeout("the answer never came")
        self.times = times
        self.idempotent_submit = idempotent

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        if self.times > 0:
            self.times -= 1
            raise self.exc
        return job


def test_a_lost_submit_answer_without_lookup_fails_submit_state_unknown_and_never_resubmits(tmp_path):
    idx = make_index(tmp_path)
    engine = LostAnswer(idempotent=False)
    tracker = Calls()
    rid = to_staging(idx, tmp_path, engine)
    assert one_step(idx, tmp_path, rid, engine, route_tracker=tracker) is S.FAILED
    run = run_of(idx, rid)
    assert run.reason_code == "submit_state_unknown" and run.provider_job_id is None
    assert "not resubmitting blind" in run.error and "Check the provider" in run.error
    assert (
        engine.submit_count == 1 and engine.job_count == 1
    )  # the job exists at the provider; a human checks
    assert tracker.seen == []  # an unknown outcome is not a provider failure


def test_a_lost_submit_answer_is_adopted_by_lookup(tmp_path):
    idx = make_index(tmp_path)
    engine = LostAnswer(idempotent=False, supports_lookup=True)
    rid = to_staging(idx, tmp_path, engine)
    assert one_step(idx, tmp_path, rid, engine) is S.SUBMITTED
    run = run_of(idx, rid)
    (job,) = engine.snapshot()["jobs"]
    assert run.provider_job_id == job and "adopted provider job" in run.history[-1].reason
    assert drive(idx, tmp_path, rid, engine) is S.DONE
    assert engine.submit_count == 1 and engine.job_count == 1 and engine.call_count("lookup") == 1


def test_a_lost_submit_answer_is_restaged_on_an_idempotent_provider_and_the_job_is_kept(tmp_path):
    idx = make_index(tmp_path)
    engine = LostAnswer()  # the fake dedupes on the key: the re-stage finds the same job
    rid = to_staging(idx, tmp_path, engine)
    assert one_step(idx, tmp_path, rid, engine) is S.SUBMITTED
    assert "re-staged with the same idempotency key" in run_of(idx, rid).history[-1].reason
    assert engine.submit_count == 2 and engine.job_count == 1  # one job, adopted, never orphaned
    assert drive(idx, tmp_path, rid, engine) is S.DONE


def test_a_restage_whose_answer_is_lost_too_stops_instead_of_looping(tmp_path):
    idx = make_index(tmp_path)
    engine = LostAnswer(httpx.ConnectError("vendor unreachable"), times=10)
    rid = to_staging(idx, tmp_path, engine)
    assert one_step(idx, tmp_path, rid, engine) is S.FAILED
    run = run_of(idx, rid)
    assert run.reason_code == "submit_state_unknown" and "re-stage after a lost submit answer" in run.error
    assert engine.submit_count == 2 and engine.job_count == 1


class Status(Exception):
    def __init__(self, status: int):
        super().__init__(f"vendor answered {status}")
        self.status = status


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (httpx.ReadTimeout("t"), "submit_state_unknown"),
        (httpx.RemoteProtocolError("server hung up"), "submit_state_unknown"),
        (httpx.WriteError("broken pipe"), "submit_state_unknown"),
        (Status(503), "submit_state_unknown"),
        (json.JSONDecodeError("Expecting value", "<html>", 0), "submit_state_unknown"),
        (Status(400), "submit_error"),
        (SubmitRejected("HTTP 400"), "submit_error"),
    ],
)
def test_submit_faults_are_classified(tmp_path, exc, code):
    idx = make_index(tmp_path)
    engine = LostAnswer(exc, times=10, idempotent=False)
    tracker = Calls()
    rid = to_staging(idx, tmp_path, engine)
    assert one_step(idx, tmp_path, rid, engine, route_tracker=tracker) is S.FAILED
    assert run_of(idx, rid).reason_code == code
    assert bool(tracker.seen) is (code == "submit_error")


# ---------------------------------------------------------------------------- (5) staging errors


class RefusesAtStaging(FakeEngine):
    """A daemon-side refusal raised inside `engine.submit` before anything is sent (what AdapterBridge and
    StagedEngine raise: validate_params, pipeline.stage, custody, the egress guard)."""

    def __init__(self, exc: BaseException):
        super().__init__()
        self.exc = exc

    def submit(self, payload, idempotency_key):
        raise self.exc


def test_three_param_errors_at_staging_are_staging_errors_and_the_route_stays_ok(tmp_path):
    idx = make_index(tmp_path)
    tracker = Tracker(tmp_path)
    for _ in range(3):
        engine = RefusesAtStaging(ParamError("fake: seed -1 is outside 0 to 4294967295"))
        rid = to_staging(idx, tmp_path, engine)
        assert one_step(idx, tmp_path, rid, engine, route_tracker=tracker) is S.FAILED
        run = run_of(idx, rid)
        assert run.reason_code == "staging_error" and "ParamError: fake: seed -1" in run.error
        assert run.provider_job_id is None and engine.submit_count == 0
    assert tracker.degraded() == {}  # three daemon-side refusals are not a sick vendor

    # The control: three provider rejections do degrade it.
    for _ in range(3):
        engine = FakeEngine(behaviour="fail_submit")
        rid = to_staging(idx, tmp_path, engine)
        assert one_step(idx, tmp_path, rid, engine, route_tracker=tracker) is S.FAILED
        assert run_of(idx, rid).reason_code == "submit_error"
    assert tracker.degraded()


@pytest.mark.parametrize(
    "exc",
    [
        PipelineError("the plate is not the one this run was quoted on"),
        ValueError("bad input"),
        CustodyViolation("route atlas is D; the run allows C0, C1", grade=Grade.D),
        EgressBlocked("host 'evil.example' is not in route allowed_hosts"),
    ],
    ids=["PipelineError", "ValueError", "CustodyViolation", "EgressBlocked"],
)
def test_every_refusal_on_this_side_is_a_staging_error(tmp_path, exc):
    idx = make_index(tmp_path)
    engine = RefusesAtStaging(exc)
    tracker = Calls()
    rid = to_staging(idx, tmp_path, engine)
    assert one_step(idx, tmp_path, rid, engine, route_tracker=tracker) is S.FAILED
    assert run_of(idx, rid).reason_code == "staging_error" and tracker.seen == []


# ---------------------------------------------------------------------------- (7) disk preflight

Usage = collections.namedtuple("Usage", "total used free")


def test_a_full_disk_refuses_before_the_vendor_is_called(tmp_path, monkeypatch):
    idx = make_index(tmp_path)
    engine = FakeEngine()
    rid = to_staging(idx, tmp_path, engine)
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _p: Usage(100 * MiB, 99 * MiB, 1 * MiB))
    assert one_step(idx, tmp_path, rid, engine) is S.FAILED
    run = run_of(idx, rid)
    assert run.reason_code == "disk_full" and run.provider_job_id is None and run.submit_started_utc is None
    assert run.error.startswith("disk_full: 1 MiB free under ") and "the floor is 256 MiB" in run.error
    assert engine.submit_count == 0 and engine.calls == []  # nothing was sent


def test_the_floor_follows_plate_min_free_mb(tmp_path, monkeypatch):
    idx = make_index(tmp_path)
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _p: Usage(100 * MiB, 90 * MiB, 10 * MiB))
    monkeypatch.setenv("PLATE_MIN_FREE_MB", "8")
    engine = FakeEngine()
    rid = to_staging(idx, tmp_path, engine)
    assert one_step(idx, tmp_path, rid, engine) is S.SUBMITTED and engine.submit_count == 1
    monkeypatch.setenv("PLATE_MIN_FREE_MB", "64")
    rid = to_staging(idx, tmp_path, engine)
    assert one_step(idx, tmp_path, rid, engine) is S.FAILED
    assert "10 MiB free" in run_of(idx, rid).error and "the floor is 64 MiB" in run_of(idx, rid).error
    monkeypatch.setenv("PLATE_MIN_FREE_MB", "0")  # off
    rid = to_staging(idx, tmp_path, engine)
    assert one_step(idx, tmp_path, rid, engine) is S.SUBMITTED


# ---------------------------------------------------------------------------- (2) poll faults


class ScriptedPolls(FakeEngine):
    """Polls follow `script`: an exception is raised, "running" answers running, anything else asks the fake
    (which succeeds at once: polls_until_done=1)."""

    def __init__(self, script, **kw):
        super().__init__(polls_until_done=1, **kw)
        self.script = list(script)

    def poll(self, provider_job_id):
        item = self.script.pop(0) if self.script else "real"
        if isinstance(item, BaseException):
            raise item
        if item == "running":
            return PollResult("running")
        return super().poll(provider_job_id)


def connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused", request=httpx.Request("GET", "https://api.vendor.test/x"))


def test_one_transport_fault_between_polls_is_tolerated(tmp_path):
    idx = make_index(tmp_path)
    tracker = Calls()
    engine = ScriptedPolls(["running", connect_error(), "succeeded"])
    rid = queued(idx)
    assert drive(idx, tmp_path, rid, engine, route_tracker=tracker) is S.DONE
    (note,) = notes_of(idx, rid, "poll_fault")
    assert note.reason.startswith("poll fault 1 of 4 tolerated: ConnectError")
    assert [c[0] for c in tracker.seen] == ["ok"]  # a tolerated fault is not a provider failure


def test_five_transport_faults_in_a_row_fail_the_run(tmp_path):
    idx = make_index(tmp_path)
    engine = ScriptedPolls([connect_error() for _ in range(5)])
    rid = queued(idx)
    assert drive(idx, tmp_path, rid, engine) is S.FAILED
    run = run_of(idx, rid)
    assert run.reason_code == "provider_poll_error" and "5 consecutive poll faults" in run.error
    assert len(notes_of(idx, rid, "poll_fault")) == 4 and run.provider_job_id  # the job may exist: kept


def test_an_answer_resets_the_fault_count(tmp_path):
    idx = make_index(tmp_path)
    faults = [
        httpx.ReadTimeout("slow"),
        ConnectionResetError("reset"),
        OSError("EHOSTUNREACH"),
        connect_error(),
    ]
    engine = ScriptedPolls([*faults, "running", *faults, "succeeded"])
    rid = queued(idx)
    assert drive(idx, tmp_path, rid, engine) is S.DONE
    assert len(notes_of(idx, rid, "poll_fault")) == 8


def test_a_transport_fault_past_the_poll_timeout_is_not_tolerated(tmp_path):
    idx = make_index(tmp_path)
    engine = ScriptedPolls(["running", connect_error()])
    rid = queued(idx)
    assert one_step(idx, tmp_path, rid, engine) is S.STAGING
    assert one_step(idx, tmp_path, rid, engine) is S.SUBMITTED
    assert one_step(idx, tmp_path, rid, engine) is S.RUNNING  # "running"
    later = lambda: utcnow() + timedelta(hours=2)  # noqa: E731
    assert one_step(idx, tmp_path, rid, engine, clock=later, poll_timeout_s=60) is S.FAILED
    assert run_of(idx, rid).reason_code == "provider_poll_error" and notes_of(idx, rid, "poll_fault") == []


# ---------------------------------------------------------------------------- (3) stale result URLs


class StaleOnce(FakeEngine):
    """The first fetch finds the URL lapsed; every succeeded poll hands out a freshly signed URL."""

    def __init__(self, **kw):
        super().__init__(polls_until_done=1, **kw)
        self.signed = 0
        self.expired = False

    def poll(self, provider_job_id):
        res = super().poll(provider_job_id)
        if res.status != "succeeded":
            return res
        self.signed += 1
        return PollResult("succeeded", result_url=f"{res.result_url}#sig{self.signed}")

    def fetch(self, result_url):
        if not self.expired:
            self.expired = True
            raise ResultExpired(f"{result_url} expired (403 from the CDN)")
        return super().fetch(result_url.split("#", 1)[0])


def test_a_stale_result_url_is_repolled_once_for_a_fresh_one(tmp_path):
    idx = make_index(tmp_path)
    engine = StaleOnce()
    rid = queued(idx)
    assert drive(idx, tmp_path, rid, engine) is S.DONE
    run = run_of(idx, rid)
    # Two succeeded polls took it to FETCHING (#sig2); the lapse cost one re-poll (#sig3), stored and fetched.
    assert engine.signed == 3 and run.result_url.endswith("#sig3") and run.result_key
    assert engine.call_count("fetch") == 1  # the fake's own fetch ran once: for the fresh URL


def test_a_repoll_with_no_fresh_url_still_fails_result_url_expired(tmp_path):
    idx = make_index(tmp_path)
    engine = FakeEngine(behaviour="expire_url")  # poll hands back the same URL every time
    rid = queued(idx)
    assert drive(idx, tmp_path, rid, engine) is S.FAILED
    run = run_of(idx, rid)
    assert run.reason_code == "result_url_expired" and "a re-poll gave no fresh result URL" in run.error
    assert engine.call_count("fetch") == 1


# ---------------------------------------------------------------------------- (4) results checked before the CAS


class Serves(FakeEngine):
    """A provider whose result download returns `data`."""

    def __init__(self, data: bytes, **kw):
        super().__init__(polls_until_done=1, **kw)
        self.data = data

    def fetch(self, result_url):
        super().fetch(result_url)
        return self.data


@pytest.mark.parametrize(
    ("data", "code", "said"),
    [
        (b"", "result_empty", "the engine result is empty (0 bytes)"),
        (
            b"<!DOCTYPE html><html><body>502 Bad Gateway</body></html>",
            "result_not_image",
            "the result is 56 bytes of 3c21444f43545950, not an image or a clip",
        ),
        (b'{"error": {"code": 429}}', "result_not_image", "not an image or a clip"),
    ],
    ids=["empty", "html", "json-error"],
)
def test_a_result_that_is_not_one_fails_before_the_cas(tmp_path, data, code, said):
    idx = make_index(tmp_path)
    tracker = Calls()
    rid = queued(idx)
    assert drive(idx, tmp_path, rid, Serves(data), route_tracker=tracker) is S.FAILED
    run = run_of(idx, rid)
    assert run.reason_code == code and said in run.error and run.result_key is None
    assert tracker.seen[-1][0] == "fail" and tracker.seen[-1][-1] == code
    assert not list(cas_of(tmp_path).root.rglob("*.bin"))


def test_a_declared_decompression_bomb_is_refused_on_its_header(tmp_path):
    bomb = png_bomb(16000, 16000)
    assert len(bomb) < 1 * MiB
    idx = make_index(tmp_path)
    rid = queued(idx)
    engine = Serves(bomb)
    tracemalloc.start()
    try:
        assert drive(idx, tmp_path, rid, engine) is S.FAILED
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    run = run_of(idx, rid)
    assert run.reason_code == "result_too_large" and run.result_key is None
    assert "declares 16000x16000 = 256000000 px; the cap is 64000000 (PLATE_RESULT_MAX_PIXELS)" in run.error
    assert peak < 50 * MiB, f"peak {peak / MiB:.0f} MiB"  # a decode would be about 256 MB at one byte a pixel


def test_the_pixel_cap_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_RESULT_MAX_PIXELS", "500")
    idx = make_index(tmp_path)
    rid = queued(idx)
    assert drive(idx, tmp_path, rid, Serves(png(32, 24))) is S.FAILED
    assert "declares 32x24 = 768 px; the cap is 500" in run_of(idx, rid).error


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (png(32, 24), "bytes, png, 32x24"),
        (cv2.imencode(".jpg", np.zeros((10, 12, 3), np.uint8))[1].tobytes(), "bytes, jpeg, 12x10"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + bytes(64), "88 bytes, mp4"),
        (b'{"engine":"numeric","job":"num-1"}', "34 bytes, json marker"),
    ],
    ids=["png", "jpeg", "mp4", "marker"],
)
def test_a_good_result_records_its_size_and_format_on_the_way_in(tmp_path, data, reason):
    idx = make_index(tmp_path)
    rid = queued(idx)
    assert drive(idx, tmp_path, rid, Serves(data)) is S.DONE
    run = run_of(idx, rid)
    moved = [h for h in run.history if h.from_ is S.FETCHING and h.to is S.NORMALIZING]
    assert len(moved) == 1 and moved[0].reason.endswith(reason)
    assert cas_of(tmp_path).read_bytes(run.result_key) == data


# ---------------------------------------------------------------------------- (6) cancel never waits on a stalled step


class Stalls(FakeEngine):
    """`at` ("submit", "poll" or "fetch") blocks until `release` is set (20 s at most), after `entered` is set.
    For "poll" the first poll answers running (SUBMITTED -> RUNNING) and the second stalls, in RUNNING, then
    answers `poll_answer`: "running" (no move) or "succeeded" (the fake's own answer, on to FETCHING)."""

    def __init__(self, at: str, *, poll_answer: str = "running", **kw):
        super().__init__(polls_until_done=1, **kw)
        self.at, self.poll_answer = at, poll_answer
        self.entered, self.release = threading.Event(), threading.Event()
        self.polls_seen = 0

    def _stall(self) -> None:
        self.entered.set()
        self.release.wait(20)

    def submit(self, payload, idempotency_key):
        if self.at == "submit":
            self._stall()
        return super().submit(payload, idempotency_key)

    def poll(self, provider_job_id):
        self.polls_seen += 1
        if self.at == "poll" and self.polls_seen == 1:
            return PollResult("running")
        if self.at == "poll" and self.polls_seen == 2:
            self._stall()
            if self.poll_answer == "running":
                return PollResult("running")
        return super().poll(provider_job_id)

    def fetch(self, result_url):
        if self.at == "fetch":
            self._stall()
        return super().fetch(result_url)


def _drive_in_thread(idx: Index, home: Path, rid: str, engine) -> tuple[threading.Thread, dict]:
    out: dict = {}

    def go():
        try:
            out["state"] = drive(idx, home, rid, engine)
        except BaseException as e:  # pragma: no cover - surfaced by the assertion below
            out["error"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


def _cancel(idx: Index, rid: str, engine, **kw):
    with idx.project(PID) as conn:
        return cancel_run(conn, engine, rid, actor="human", **kw)


@pytest.mark.parametrize("at", ["submit", "poll"])
def test_a_cancel_during_a_stalled_vendor_call_answers_at_once_and_lands_after_the_stall(tmp_path, at):
    idx = make_index(tmp_path)
    engine = Stalls(at)
    rid = queued(idx)
    t, out = _drive_in_thread(idx, tmp_path, rid, engine)
    try:
        assert engine.entered.wait(10), "the vendor call never started"
        stalled_in = run_of(idx, rid).state
        assert stalled_in is (S.STAGING if at == "submit" else S.RUNNING)  # both have a CANCELLED exit
        t0 = time.monotonic()
        with pytest.raises(CancelPending) as e:
            _cancel(idx, rid, engine, reason="wrong camera")
        assert time.monotonic() - t0 < 3.0 and e.value.run_id == rid
        assert run_of(idx, rid).state is stalled_in  # recorded, not yet done
    finally:
        engine.release.set()
    t.join(15)
    assert out.get("state") is S.CANCELLED, out
    run = run_of(idx, rid)
    assert run.history[-1].to is S.CANCELLED and "wrong camera" in run.history[-1].reason
    assert engine.cancel_count == 1 and "provider cancel requested" in run.history[-1].reason
    assert engine.submit_count == 1 and engine.fetch_count == 0  # nothing ran after the cancel landed
    if at == "submit":  # the move the stalled step made is in the record first, then the cancel
        assert [h.to for h in run.history[-2:]] == [S.SUBMITTED, S.CANCELLED]
    else:  # the stalled poll made no move: the cancel landed at the end of that same step
        assert [h.to for h in run.history[-2:]] == [S.RUNNING, S.CANCELLED] and engine.polls_seen == 2


def test_a_cancel_during_a_stalled_fetch_is_refused_at_once_and_the_take_completes(tmp_path):
    """FETCHING has no CANCELLED exit (the result is paid for): the refusal is immediate, not after the stall."""
    idx = make_index(tmp_path)
    engine = Stalls("fetch")
    rid = queued(idx)
    t, out = _drive_in_thread(idx, tmp_path, rid, engine)
    try:
        assert engine.entered.wait(10)
        assert run_of(idx, rid).state is S.FETCHING
        t0 = time.monotonic()
        with pytest.raises(IllegalTransition) as e:
            _cancel(idx, rid, engine)
        assert time.monotonic() - t0 < 0.5 and e.value.from_state is S.FETCHING
    finally:
        engine.release.set()
    t.join(15)
    assert out.get("state") is S.DONE and engine.cancel_count == 0


def test_a_recorded_cancel_the_run_outgrew_is_dropped_with_a_note(tmp_path):
    idx = make_index(tmp_path)
    engine = Stalls("poll", poll_answer="succeeded")  # the stalled poll comes back with the result
    rid = queued(idx)
    t, out = _drive_in_thread(idx, tmp_path, rid, engine)
    try:
        assert engine.entered.wait(10)
        with pytest.raises(CancelPending):
            _cancel(idx, rid, engine)
    finally:
        engine.release.set()
    t.join(15)
    assert out.get("state") is S.DONE and engine.cancel_count == 0
    (note,) = notes_of(idx, rid, "cancel_dropped")
    assert "had reached FETCHING" in note.reason and note.actor == "human"
    assert worker._cancel_requested == {}


def test_an_idle_run_is_still_cancelled_on_the_spot(tmp_path):
    idx = make_index(tmp_path)
    engine = FakeEngine(polls_until_done=5)
    rid = queued(idx)
    for _ in range(3):
        one_step(idx, tmp_path, rid, engine)
    assert run_of(idx, rid).state is S.RUNNING
    assert _cancel(idx, rid, engine).state is S.CANCELLED and engine.cancel_count == 1


# ---------------------------------------------------------------------------- (8) egress streams downloads


class Probe(Adapter):
    def quote(self, inputs, params):  # pragma: no cover - not exercised
        return self.make_quote(0.0, 0.0, EngineSize(w=0, h=0, label="auto"))

    def submit(self, staged):  # pragma: no cover - not exercised
        return "job"

    def poll(self, provider_job_id):  # pragma: no cover - not exercised
        return JobStatus(state="done")


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunk: bytes, n: int):
        self.chunk, self.n, self.yielded = chunk, n, 0
        self.closed = threading.Event()

    def __iter__(self):
        for _ in range(self.n):
            self.yielded += 1
            yield self.chunk

    def close(self):
        self.closed.set()


def _egress_adapter(settings, tmp_path, body, **limits) -> tuple[Probe, OutboundLog]:
    manifest = load_manifests()["kling-o3-4k"]
    route = next(r for r in manifest.routes if r.route_id == "atlas")
    log = OutboundLog(tmp_path / "outbound.jsonl")
    egress = Egress(settings, log, httpx.MockTransport(lambda request: httpx.Response(200, stream=body)))
    run = EgressRun(run_id="run_stream", allowed_grades=frozenset({"C0", "C1", "C2", "D"}))
    a = Probe(manifest, route, transport=egress.transport(run, route))
    for k, v in limits.items():
        setattr(a, k, v)
    return a, log


def test_egress_streams_a_300_mb_body_to_the_adapter_cap_instead_of_buffering_it(settings, tmp_path):
    body = Chunks(b"\x00" * MiB, 300)
    a, log = _egress_adapter(settings, tmp_path, body, fetch_max_bytes=64 * MiB)
    tracemalloc.start()
    try:
        with pytest.raises(FetchError, match=r"^download exceeded 67108864 bytes$"):
            a.fetch("https://img.atlascloud.ai/out/big300.png")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 100 * MiB, f"peak {peak / MiB:.0f} MiB"
    assert body.closed.wait(5) and body.yielded < 80
    (entry,) = log.entries()
    assert entry["status"] == 200 and 64 * MiB < entry["bytes_in"] < 80 * MiB  # what really came in
    assert entry["error"].startswith("the body was closed after")


def test_egress_still_logs_a_whole_download_with_its_bytes(settings, tmp_path):
    body = Chunks(png(), 1)
    a, log = _egress_adapter(settings, tmp_path, body)
    assert a.fetch("https://img.atlascloud.ai/out/o1.png") == png()
    (entry,) = log.entries()
    assert entry["status"] == 200 and entry["bytes_in"] == len(png()) and entry["error"] is None


# ---------------------------------------------------------------------------- (9) echo fake: kill in FETCHING, restart


needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built",
)


class Daemon:
    """`python -m plate_daemon` on this test's home, its output in a file (never a pipe that can fill)."""

    def __init__(self, home: Path, port: int, token: str, child_env, fake: dict):
        self.home, self.port, self.token = home, port, token
        self.base = f"http://127.0.0.1:{port}/api/v0"
        self.out = home / f"daemon-{uuid.uuid4().hex[:6]}.log"
        env = child_env(
            PLATE_HOME=str(home),
            PLATE_PORT=str(port),
            PLATE_FAKE_ENGINE=json.dumps(fake),
            PLATE_SYNC_INGEST="1",
            PLATE_NIGHTLY="0",
            PLATE_PIXELS_INPROC="1",
            PLATE_LOG_LEVEL="warning",
        )
        self.fh = open(self.out, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "plate_daemon"], stdout=self.fh, stderr=subprocess.STDOUT, env=env
        )
        deadline = time.monotonic() + 60
        while True:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"daemon exited {self.proc.returncode}: {self.out.read_text(encoding='utf-8')[-2000:]}"
                )
            try:
                if self.call("GET", "/session").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError("daemon did not answer within 60 s")
            time.sleep(0.2)

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}", **kw.pop("headers", {})}
        return httpx.request(method, self.base + path, headers=headers, timeout=30, **kw)

    def kill(self) -> None:
        self.proc.kill()  # SIGKILL / TerminateProcess: nothing runs after it
        self.proc.wait(30)
        self.fh.close()


@needs_room_a
def test_an_echo_run_killed_in_fetching_resumes_to_normalizing(tmp_path, settings, child_env, free_port):
    """The crash attacker's FETCHING/before case: the echo source lived in memory only, so the restarted daemon
    failed the paid run `result_url_expired`. It is beside the journal now and the fetch succeeds."""
    port = free_port()
    hang = {"echo": True, "hang_at": "FETCHING", "hang_phase": "before"}
    d = Daemon(tmp_path, port, settings.token, child_env, hang)
    try:
        assert (
            d.call(
                "POST", "/projects", json={"project_id": "plate-fixture", "custody": {"nda": False}}
            ).status_code
            == 201
        )
        manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
        r = d.call(
            "POST",
            "/packets",
            json={"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}},
            headers={"X-Plate-Packet-Version": manifest["packet_version"]},
        )
        assert r.status_code == 202, r.text
        graph = {
            "nodes": [
                {
                    "id": "p",
                    "kind": "Plate",
                    "packetId": r.json()["packet_id"],
                    "tier": "A",
                    "label": "Plate",
                },
                {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
            ],
            "edges": [{"id": "p->e", "source": "p", "target": "e"}],
        }
        assert d.call("PUT", "/projects/plate-fixture/graph", json=graph).status_code == 200
        r = d.call("POST", "/projects/plate-fixture/nodes/e/quote", json={"engine_id": "fake"})
        assert r.status_code == 200, r.text
        run_id = r.json()["run"]["id"]
        assert d.call("POST", f"/runs/{run_id}/approve", json={"by": "human"}).status_code == 200
        marker = tmp_path / "fake_engine.json.hang"
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert time.monotonic() < deadline, "the run never reached the FETCHING hang"
            time.sleep(0.05)
        assert d.call("GET", f"/runs/{run_id}").json()["state"] == "FETCHING"
        assert list((tmp_path / "fake_engine.json.echo").glob("fake-*.png"))  # persisted at submit
    finally:
        d.kill()
    marker.unlink()

    d = Daemon(tmp_path, port, settings.token, child_env, {"echo": True})
    try:
        deadline = time.monotonic() + 60
        while True:
            run = d.call("GET", f"/runs/{run_id}").json()
            if run["state"] not in ("FETCHING", "NORMALIZING", "GATING"):
                break
            assert time.monotonic() < deadline, f"stuck in {run['state']}"
            time.sleep(0.2)
        events = d.call("GET", f"/runs/{run_id}/events").json()
    finally:
        d.kill()
    assert run["state"] in ("DONE", "GATE_FAIL"), run
    assert run.get("reason_code") != "result_url_expired"
    moves = [(e.get("from_state"), e.get("to_state")) for e in events if e.get("kind") == "transition"]
    assert ("FETCHING", "NORMALIZING") in moves
    journal = json.loads((tmp_path / "fake_engine.json").read_text(encoding="utf-8"))
    assert journal["submit_count"] == 1 and len(journal["jobs"]) == 1  # resumed, never resubmitted


def test_the_live_daemon_echo_payload_is_what_the_fake_persists(tmp_path):
    """The unit half of (9): what `submit` persisted is what a fresh instance over the same journal echoes."""
    sent = png(40, 30)
    payload = {"inputs": {"image": {"b64": base64.b64encode(sent).decode("ascii")}}, "params": {}}
    first = FakeEngine(echo=True, journal=tmp_path / "fake_engine.json")
    job = first.submit(payload, "k")
    assert FakeEngine(echo=True, journal=tmp_path / "fake_engine.json").fetch(f"fake://results/{job}") == sent
