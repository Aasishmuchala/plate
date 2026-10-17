"""The hash-chained run ledger: chain, tamper detection, torn tails, locking, summaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from blake3 import blake3

from plate_daemon.ledger import (
    GENESIS,
    KINDS,
    LOCK_NAME,
    RUNS_JSONL,
    Ledger,
    LedgerError,
    canonical_json,
    fold_runs,
)
from plate_daemon.lock import ProjectLocked, project_lock

DAEMON_DIR = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 16, 9, 0, 0, tzinfo=UTC)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(DAEMON_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@pytest.fixture
def led(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "proj")


def fill(led: Ledger, n: int = 5) -> list[dict]:
    kinds = ["quoted", "approved", "submitted", "fetched", "done", "gated"]
    return [
        led.append(kind=kinds[i % len(kinds)], run_id="r1", at=at(i), cost_usd_quoted=0.04) for i in range(n)
    ]


def lines(led: Ledger) -> list[str]:
    return led.path.read_bytes().decode("ascii").split("\n")[:-1]


def write_lines(led: Ledger, new: list[str]) -> None:
    led.path.write_bytes(("\n".join(new) + "\n").encode("ascii"))


# ---------------------------------------------------------------------------- chain


def test_chain_fields_and_independent_hash(led: Ledger):
    recs = fill(led, 4)
    assert [r["seq"] for r in recs] == [1, 2, 3, 4]
    assert recs[0]["prev_hash"] == GENESIS == "0" * 64
    prev = GENESIS
    for ln in lines(led):
        rec = json.loads(ln)
        body = {k: v for k, v in rec.items() if k != "hash"}
        canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        assert rec["prev_hash"] == prev
        assert rec["hash"] == blake3((prev + canon).encode("utf-8")).hexdigest()
        assert rec["v"] == 1
        prev = rec["hash"]
    assert led.verify() == (True, None)
    assert led.head() == (4, prev)


def test_empty_ledger_verifies_and_has_genesis_head(led: Ledger):
    assert led.read() == ([], [])
    assert led.verify() == (True, None)
    assert led.head() == (0, GENESIS)
    assert led.summarize()["generations"] == 0


def test_golden_chain_is_stable_across_platforms(tmp_path: Path):
    """Frozen hashes: any change to the canonical form, field names or hash recipe fails here."""
    led = Ledger(tmp_path / "golden")
    led.append(
        kind="quoted",
        at="2026-10-16T09:00:00Z",
        run_id="r1",
        engine_id="nano-banana-2.1",
        custody_grade="C1",
        cost_usd_quoted=0.067,
    )
    led.append(kind="approved", at="2026-10-16T09:00:05Z", run_id="r1", cost_usd_quoted=0.067, by="human")
    last = led.append(
        kind="refused",
        at="2026-10-16T09:00:09Z",
        run_id="r1",
        reason="café — blocked",
        data={"b": [1, 2], "a": None},
    )
    assert [json.loads(x)["hash"] for x in lines(led)] == [
        "66a9bdac564cb24ba8fa3ff45b94bdf7c83f990d7359d330bd6b4ab49953be5e",
        "6ceefa66ffb2d0e362abebde4cf43779e462d32ac39134db9291e7f09438ea4d",
        "815b3b095d8e988e6a8562314a18695dfebd04dc61c7a0c086120b0db7d73ab3",
    ]
    assert last["hash"] == "815b3b095d8e988e6a8562314a18695dfebd04dc61c7a0c086120b0db7d73ab3"
    # the recipe written out by hand for record 1: prev_hash + sorted-key compact JSON without "hash"
    by_hand = (
        "0" * 64
        + '{"at":"2026-10-16T09:00:00.000000Z","cost_usd_quoted":0.067,"custody_grade":"C1",'
        + '"engine_id":"nano-banana-2.1","kind":"quoted","prev_hash":"'
        + "0" * 64
        + '","run_id":"r1","seq":1,"v":1}'
    )
    assert blake3(by_hand.encode("utf-8")).hexdigest() == json.loads(lines(led)[0])["hash"]


def test_file_is_ascii_lf_and_survives_non_ascii_text(led: Ledger):
    led.append(kind="override", reason="résumé — తెలుగు", by="aasish")
    raw = led.path.read_bytes()
    assert raw.isascii() and b"\r" not in raw and raw.endswith(b"\n")
    assert led.records()[0]["reason"] == "résumé — తెలుగు"
    assert led.verify() == (True, None)


def test_append_accepts_dict_or_keywords_and_fills_at(led: Ledger):
    a = led.append({"kind": "quoted", "run_id": "r1"})
    b = led.append(kind="approved", run_id="r1")
    assert a["at"].endswith("Z") and b["seq"] == 2
    t = datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)
    assert led.append(kind="done", at=t)["at"] == "2026-01-02T03:04:05.000006Z"
    assert led.append(kind="done", at="2026-01-02T03:04:05+05:30")["at"] == "2026-01-01T21:34:05.000000Z"


def test_new_instance_continues_the_chain_and_sees_other_writers(led: Ledger):
    fill(led, 3)
    other = Ledger(led.project_dir)
    assert other.append(kind="done", run_id="r1")["seq"] == 4
    assert led.append(kind="done", run_id="r1")["seq"] == 5  # led's cached tail is stale: it must re-read
    assert Ledger(led.project_dir).verify() == (True, None)
    assert led.head()[0] == 5


def test_none_fields_are_dropped_and_floats_round_trip(led: Ledger):
    rec = led.append(kind="billed", run_id="r1", node_id=None, cost_usd_billed=0.1 + 0.2, data=None)
    assert "node_id" not in rec and "data" not in rec
    assert led.records()[0]["cost_usd_billed"] == pytest.approx(0.30000000000000004, abs=0)
    assert led.verify() == (True, None)


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "nope"},
        {},
        {"kind": "done", "wat": 1},
        {"kind": "done", "seq": 7},
        {"kind": "done", "hash": "x"},
        {"kind": "done", "cost_usd_quoted": float("nan")},
        {"kind": "done", "cost_usd_quoted": -0.01},
        {"kind": "done", "cost_usd_quoted": "0.04"},
        {"kind": "done", "cost_usd_billed": True},
        {"kind": "done", "run_id": 7},
        {"kind": "done", "run_id": ""},
        {"kind": "done", "data": [1]},
        {"kind": "done", "data": {"x": object()}},
        {"kind": "done", "data": {"x": float("inf")}},
        {"kind": "done", "gate_report": 3},
        {"kind": "done", "at": "yesterday"},
        {"kind": "done", "at": 5},
    ],
)
def test_bad_records_are_refused_and_nothing_is_written(led: Ledger, bad: dict):
    with pytest.raises(LedgerError):
        led.append(bad)
    assert not led.path.exists()


def test_credit_is_a_negative_bill_but_never_a_negative_quote(led: Ledger):
    led.append(kind="billed", run_id="r1", cost_usd_billed=-0.5)
    assert led.records()[0]["cost_usd_billed"] == -0.5


def test_gate_report_may_be_a_path_or_a_summary(led: Ledger):
    led.append(kind="gated", run_id="r1", gate_report="gates/r1.1.json")
    led.append(kind="gated", run_id="r1", gate_report={"verdict": "pass", "score": 0.93})
    assert led.verify() == (True, None)


# ---------------------------------------------------------------------------- tamper evidence


def test_editing_a_middle_line_fails_at_that_seq(led: Ledger):
    fill(led, 5)
    ls = lines(led)
    ls[1] = ls[1].replace('"cost_usd_quoted":0.04', '"cost_usd_quoted":0.01')
    assert ls[1] != lines(led)[1]
    write_lines(led, ls)
    assert led.verify() == (False, 2)
    assert led.check().reason == "hash does not match the record's content"


def test_resealing_one_edited_line_breaks_at_the_next(led: Ledger):
    fill(led, 5)
    ls = lines(led)
    rec = json.loads(ls[1])
    rec["cost_usd_quoted"] = 9.99
    body = {k: v for k, v in rec.items() if k != "hash"}
    rec["hash"] = blake3((rec["prev_hash"] + canonical_json(body)).encode()).hexdigest()
    ls[1] = json.dumps(rec, separators=(",", ":"))
    write_lines(led, ls)
    assert led.verify() == (False, 3)
    assert "prev_hash" in led.check().reason


def test_deleting_a_middle_line_fails_where_it_was(led: Ledger):
    fill(led, 5)
    ls = lines(led)
    del ls[2]
    write_lines(led, ls)
    assert led.verify() == (False, 3)


def test_swapping_lines_fails(led: Ledger):
    fill(led, 5)
    ls = lines(led)
    ls[1], ls[2] = ls[2], ls[1]
    write_lines(led, ls)
    assert led.verify() == (False, 2)


def test_unreadable_middle_line_fails_but_reads_skip_it(led: Ledger):
    fill(led, 4)
    ls = lines(led)
    ls[2] = ls[2][:30]
    write_lines(led, ls)
    assert led.verify() == (False, 3)
    recs, problems = led.read()
    assert [r["seq"] for r in recs] == [1, 2, 4] and len(problems) == 1


def test_terminated_garbage_as_the_last_line_is_damage_not_a_torn_write(led: Ledger):
    fill(led, 3)
    with open(led.path, "ab") as fh:
        fh.write(b"{not json}\n")
    assert led.verify() == (False, 4)


def test_forged_hash_and_wrong_version_and_extra_blank_lines(led: Ledger):
    fill(led, 3)
    ls = lines(led)
    assert led.verify() == (True, None)
    write_lines(led, [ls[0], "", ls[1], "  ", ls[2]])  # blank lines carry nothing
    assert led.verify() == (True, None)
    bad = json.loads(ls[1])
    bad["hash"] = "f" * 64
    write_lines(led, [ls[0], json.dumps(bad), ls[2]])
    assert led.verify() == (False, 2)
    bad = json.loads(ls[0])
    bad["v"] = 2
    write_lines(led, [json.dumps(bad), ls[1], ls[2]])
    assert led.verify() == (False, 1)


def test_cutting_off_the_tail_is_only_visible_against_a_saved_head(led: Ledger):
    fill(led, 5)
    saved = led.head()
    write_lines(led, lines(led)[:3])
    assert led.verify() == (True, None)  # a chain cannot see this by itself
    assert led.head() != saved and led.head()[0] == 3


def test_bom_and_crlf_from_a_windows_editor_are_tolerated(led: Ledger):
    fill(led, 3)
    led.path.write_bytes(b"\xef\xbb\xbf" + b"\r\n".join(x.encode() for x in lines(led)) + b"\r\n")
    assert led.verify() == (True, None)
    assert led.append(kind="done", run_id="r1")["seq"] == 4
    assert led.verify() == (True, None)


# ---------------------------------------------------------------------------- torn tail


def test_torn_last_line_is_skipped_reported_and_not_a_failure(led: Ledger):
    fill(led, 3)
    with open(led.path, "ab") as fh:
        fh.write(b'{"v":1,"seq":4,"prev_ha')
    recs, problems = led.read()
    assert [r["seq"] for r in recs] == [1, 2, 3]
    assert problems == [f"{RUNS_JSONL}:4: torn last line skipped"]
    v = led.check()
    assert v.ok and v.torn and v.count == 3 and led.verify() == (True, None)
    assert led.head()[0] == 3


def test_append_after_a_torn_tail_moves_it_aside_and_keeps_the_chain(led: Ledger):
    fill(led, 3)
    with open(led.path, "ab") as fh:
        fh.write(b'{"v":1,"seq":4,"prev_ha')
    fresh = Ledger(led.project_dir)  # a restarted daemon
    rec = fresh.append(kind="done", run_id="r1")
    assert rec["seq"] == 4 and rec["prev_hash"] == json.loads(lines(led)[2])["hash"]
    assert fresh.read()[1] == [] and fresh.verify() == (True, None)
    torn = led.path.with_name(RUNS_JSONL + ".torn").read_bytes()
    assert torn == b'{"v":1,"seq":4,"prev_ha\n'
    assert fresh.repairs and "torn" in fresh.repairs[0]
    assert len(lines(led)) == 4


def test_torn_first_line_of_a_new_file(led: Ledger):
    led.project_dir.mkdir(parents=True)
    led.path.write_bytes(b'{"v":1,"se')
    assert led.verify() == (True, None) and led.head() == (0, GENESIS)
    assert led.append(kind="quoted", run_id="r1")["seq"] == 1
    assert led.verify() == (True, None)


def test_complete_last_line_missing_only_its_newline_is_kept(led: Ledger):
    fill(led, 3)
    led.path.write_bytes(led.path.read_bytes().rstrip(b"\n"))
    fresh = Ledger(led.project_dir)
    assert fresh.verify() == (True, None) and not fresh.check().torn
    assert fresh.append(kind="done", run_id="r1")["seq"] == 4
    assert len(lines(led)) == 4 and fresh.verify() == (True, None)
    assert not led.path.with_name(RUNS_JSONL + ".torn").exists()


# ---------------------------------------------------------------------------- locking and concurrency


def test_threads_of_one_process_queue_up(led: Ledger):
    def work(n: int) -> None:
        mine = Ledger(led.project_dir)  # separate instances, one file
        for i in range(10):
            mine.append(kind="done", run_id=f"t{n}", data={"i": i})

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    recs = led.records()
    assert len(recs) == 60 and [r["seq"] for r in recs] == list(range(1, 61))
    assert led.verify() == (True, None)
    for n in range(6):
        assert [r["data"]["i"] for r in recs if r["run_id"] == f"t{n}"] == list(range(10))


CHILD_APPEND = (
    "import sys; from pathlib import Path; from plate_daemon.ledger import Ledger\n"
    "led = Ledger(Path(sys.argv[1]), lock_timeout_s=60)\n"
    "for i in range(int(sys.argv[3])):\n"
    "    led.append(kind='done', run_id=sys.argv[2], data={'i': i})\n"
)


def test_processes_append_without_corrupting_the_chain(led: Ledger):
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD_APPEND, str(led.project_dir), f"p{n}", "5"], env=child_env()
        )
        for n in range(3)
    ]
    assert [p.wait(120) for p in procs] == [0, 0, 0]
    recs = led.records()
    assert len(recs) == 15 and [r["seq"] for r in recs] == list(range(1, 16))
    assert led.verify() == (True, None)
    for n in range(3):
        assert [r["data"]["i"] for r in recs if r["run_id"] == f"p{n}"] == list(range(5))


CHILD_HOLD = (
    "import sys; from pathlib import Path; from plate_daemon.lock import project_lock\n"
    "with project_lock(Path(sys.argv[1]), 0, name=sys.argv[2]):\n"
    "    print('held', flush=True)\n"
    "    sys.stdin.readline()\n"
)


def test_a_writer_in_another_process_raises_project_locked(led: Ledger):
    holder = subprocess.Popen(
        [sys.executable, "-c", CHILD_HOLD, str(led.project_dir), LOCK_NAME],
        env=child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        quick = Ledger(led.project_dir, lock_timeout_s=0.1)
        with pytest.raises(ProjectLocked) as exc:
            quick.append(kind="done", run_id="r1")
        assert exc.value.holder_pid == holder.pid
        assert not led.path.exists()
    finally:
        holder.stdin.write("\n")
        holder.stdin.close()
        holder.wait(30)
    assert quick.append(kind="done", run_id="r1")["seq"] == 1  # released: the holder's exit freed it


def test_holding_the_lock_through_another_handle_is_named_not_a_timeout(led: Ledger):
    with project_lock(led.project_dir, 0, name=LOCK_NAME):
        with pytest.raises(LedgerError, match="lock=") as exc:
            Ledger(led.project_dir, lock_timeout_s=0.1).append(kind="done")
        assert "another handle" in str(exc.value)


def test_a_held_projects_lock_can_be_passed_in(led: Ledger):
    projects = pytest.importorskip("plate_daemon.projects")
    assert projects.LOCK_NAME == LOCK_NAME  # one lock file for the project
    with projects.Lock(led.project_dir) as lock:
        mine = Ledger(led.project_dir, lock=lock, lock_timeout_s=0.1)
        assert mine.append(kind="quoted", run_id="r1")["seq"] == 1
        assert mine.append(kind="approved", run_id="r1")["seq"] == 2
        with pytest.raises(LedgerError):  # the same folder without handing the lock over
            Ledger(led.project_dir, lock_timeout_s=0.1).append(kind="done")
    assert Ledger(led.project_dir).append(kind="done", run_id="r1")["seq"] == 3
    assert Ledger(led.project_dir).verify() == (True, None)


def test_kinds_are_the_spec_set():
    assert KINDS == {
        "quoted",
        "approved",
        "submitted",
        "fetched",
        "gated",
        "done",
        "failed",
        "refused",
        "cancelled",
        "expired",
        "billed",
        "deleted_vendor_side",
        "human_minutes",
        "export",
        "override",
    }


# ---------------------------------------------------------------------------- folding and summaries


def scenario(led: Ledger) -> None:
    """Five runs on two engines and two custody grades, plus a bill that names no run."""
    t = 0.0

    def step(**kw) -> None:
        nonlocal t
        t += 1
        led.append(at=at(t), **kw)

    # r1: nano, C1, quoted 0.067, accepted, billed 0.067
    step(kind="quoted", run_id="r1", engine_id="nano-banana-2.1", custody_grade="C1", cost_usd_quoted=0.067)
    step(kind="approved", run_id="r1", cost_usd_quoted=0.067, by="human")
    step(kind="submitted", run_id="r1", provider_job_id="g-1")
    step(kind="done", run_id="r1", data={"decision": "accepted"})
    step(kind="billed", run_id="r1", cost_usd_billed=0.067)
    # r2: nano, C1, rerolled; the approved price (0.07) wins over the quote (0.06)
    step(kind="quoted", run_id="r2", engine_id="nano-banana-2.1", custody_grade="C1", cost_usd_quoted=0.06)
    step(kind="approved", run_id="r2", cost_usd_quoted=0.07)
    step(kind="submitted", run_id="r2")
    step(kind="done", run_id="r2", data={"decision": "accepted"})
    step(kind="override", run_id="r2", data={"decision": "rerolled"})  # the last decision wins
    # r3: kling, C2, accepted, billed in two parts
    step(kind="quoted", run_id="r3", engine_id="kling-o3-4k", custody_grade="C2", cost_usd_quoted=2.5)
    step(kind="approved", run_id="r3", cost_usd_quoted=2.5)
    step(kind="submitted", run_id="r3", provider_job_id="pred_x")
    step(kind="done", run_id="r3", data={"decision": "accepted"})
    step(kind="billed", run_id="r3", cost_usd_billed=2.0)
    step(kind="billed", run_id="r3", cost_usd_billed=0.52)
    # r4: cancelled before it was submitted: spends nothing
    step(kind="quoted", run_id="r4", engine_id="kling-o3-4k", custody_grade="C2", cost_usd_quoted=2.5)
    step(kind="approved", run_id="r4", cost_usd_quoted=2.5)
    step(kind="cancelled", run_id="r4")
    # r5: submitted, failed; carries the vendor figure on a non-billed record
    step(kind="quoted", run_id="r5", engine_id="seedance-2.5-i2v", custody_grade="C2", cost_usd_quoted=1.2)
    step(kind="submitted", run_id="r5")
    step(kind="failed", run_id="r5", cost_usd_billed=0.0, reason="vendor error")
    # a vendor line nobody ledgered, written by reconcile
    step(kind="billed", cost_usd_billed=0.84, provider_job_id="pred_zzz", data={"unmatched": True})
    step(kind="human_minutes", data={"minutes": 12.5})


def test_fold_runs(led: Ledger):
    scenario(led)
    runs = fold_runs(led.records())
    assert set(runs) == {"r1", "r2", "r3", "r4", "r5"}
    assert str(runs["r2"].quoted_usd) == "0.07" and runs["r2"].decision == "rerolled"
    assert str(runs["r3"].billed_usd) == "2.52" and runs["r3"].provider_job_ids == ["pred_x"]
    assert runs["r4"].kinds == {"quoted", "approved", "cancelled"} and not runs["r4"].submitted
    assert runs["r5"].submitted and str(runs["r5"].billed_usd) == "0.0"


def test_summarize_totals(led: Ledger):
    scenario(led)
    s = led.summarize()
    assert s["generations"] == 4  # r1, r2, r3, r5; r4 never left the building
    assert s["spent_usd_quoted"] == pytest.approx(0.067 + 0.07 + 2.5 + 1.2, abs=1e-9)
    assert s["spent_usd_billed"] == pytest.approx(0.067 + 2.52 + 0.84, abs=1e-9)
    assert (s["accepted"], s["rerolled"]) == (2, 1)
    assert s["by_engine"]["nano-banana-2.1"] == {
        "generations": 2,
        "spent_usd_quoted": 0.137,
        "spent_usd_billed": 0.067,
        "accepted": 1,
        "rerolled": 1,
    }
    assert s["by_engine"]["kling-o3-4k"]["generations"] == 1  # r4 is not counted
    assert s["by_engine"]["seedance-2.5-i2v"]["spent_usd_billed"] == 0.0
    assert s["by_engine"]["unattributed"]["spent_usd_billed"] == 0.84
    assert s["by_custody"]["C1"]["generations"] == 2 and s["by_custody"]["C2"]["spent_usd_quoted"] == 3.7
    assert s["by_custody"]["unattributed"]["spent_usd_billed"] == 0.84
    assert sum(v["spent_usd_billed"] for v in s["by_engine"].values()) == pytest.approx(s["spent_usd_billed"])
    json.dumps(s)  # plain JSON all the way down


def test_summarize_since_keeps_only_later_records(led: Ledger):
    scenario(led)
    cut = at(11.5)  # after r1 and r2's bookkeeping, before r3
    s = led.summarize(since=cut)
    assert s["generations"] == 2  # r3 and r5
    assert s["spent_usd_quoted"] == pytest.approx(2.5 + 1.2)
    assert led.summarize(since=datetime(2026, 10, 17, tzinfo=UTC))["generations"] == 0
    assert led.summarize(since=at(0))["generations"] == 4
    assert led.summarize(since=None) == led.summarize()


def test_summarize_ignores_records_it_cannot_read(led: Ledger):
    scenario(led)
    with open(led.path, "ab") as fh:
        fh.write(b'{"v":1,"seq":99')
    assert led.summarize()["generations"] == 4


def test_a_decision_on_an_unsubmitted_run_counts_as_a_decision_not_a_generation(led: Ledger):
    """An externally made file imported by hand (manual-pipeline fallback) is accepted without a spend."""
    led.append(kind="override", run_id="imp1", engine_id="manual", at=at(1), data={"decision": "accepted"})
    led.append(kind="quoted", run_id="q1", engine_id="manual", cost_usd_quoted=3.0, at=at(2))
    s = led.summarize()
    assert (s["generations"], s["accepted"], s["spent_usd_quoted"]) == (0, 1, 0.0)
    assert s["by_engine"] == {
        "manual": {
            "generations": 0,
            "spent_usd_quoted": 0.0,
            "spent_usd_billed": 0.0,
            "accepted": 1,
            "rerolled": 0,
        }
    }
