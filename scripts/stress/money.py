#!/usr/bin/env python3
"""The money attacker's findings as checks of the fixed behaviour (router/PLAN.md Wave 12, T126/T129/T134/T145).

    python scripts/stress/money.py [--keep] [--only <check>] [--list]

The attacker's money/bin scripts (t2, t5, t10, t11, t13, t14, t17, ttl_attack), turned around, on the shipped
caps (auto 2.00, daily 50.00, unattended 20.00 USD: the caps have no setting, so the checks spend real quote prices
of nano-banana-pro instead of the attacker's patched policy):

- lost-submitted-record: the project lock held 7 s right after an approval loses no ledger record; the run's
  records arrive in order and /spend counts the generation.
- unattended-clock-reset: auto approvals stop at the unattended cap and a header-less timer, override or scrub
  does not lift it; a UI request does.
- daily-cap: the daily cap is the user's, summed across every project of the home ("across 3 projects").
- ledger-order-race and approve-500-on-cancel-race: 36 approve/cancel races give typed answers, one record per
  transition in transition order, and a rebuilt index agrees with the live one.
- run-stuck-after-db-locked: an index held by another writer for 12 s delays a RUNNING run; it does not kill it.
- concurrent-quote-same-attempt: ten concurrent quotes of one node number 1 to 10 and all ten run.
- expired-ledgered and quote-ttl-clock-back: an expired quote, and one made "in the future" (the clock went
  back), is refused 409 quote_expired, ledgered `expired`, and a rebuild keeps it EXPIRED.

The quote clock is moved by editing the quote rows of the SQLite index (the attacker's ttl_attack.py), the only
write a check makes outside HTTP besides the lock helper (`_lib.hold_project_lock`, portalocker).
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import _lib
from _lib import Daemon, check, error_of

ITERS = 36
DELAYS = [0, 0.002, 0.005, 0.01, 0.02, 0.04, 0.08, 0.15, 0.3]
TERMINAL_KINDS = {"done", "failed", "refused", "cancelled", "expired"}
_SHARED: Daemon | None = None
_RACE: dict[str, Any] | None = None


def shared() -> Daemon:
    global _SHARED
    if _SHARED is None or not _SHARED.alive():
        _SHARED = Daemon(name="money-shared", fake={"echo": True, "polls_until_done": 3}).start()
    return _SHARED


def kinds(d: Daemon, project: str, run_id: str) -> list[str]:
    return [x["kind"] for x in d.ledger(project) if x.get("run_id") == run_id]


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def set_quote_times(d: Daemon, project: str, run_id: str, created: datetime, expires: datetime) -> None:
    """Move a quote's clock in the index (the quote row and the run's copy of it)."""
    con = sqlite3.connect(str(d.index_db(project)), timeout=10)
    try:
        qid = con.execute("SELECT quote_id FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        con.execute(
            "UPDATE quotes SET created_utc = ?, expires_utc = ? WHERE id = ?",
            (iso(created), iso(expires), qid),
        )
        cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
        if "quote_json" in cols:
            con.execute(
                "UPDATE runs SET quote_json = json_set(quote_json, '$.created_utc', ?, '$.expires_utc', ?) "
                "WHERE id = ? AND quote_json IS NOT NULL",
                (iso(created), iso(expires), run_id),
            )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------- checks


def lost_submitted_record() -> None:
    d = shared()
    p = "money13"
    d.edit_project(p)
    q = d.quote(p).json()
    rid = q["run"]["id"]
    a = d.approve(rid)
    check(a.status_code == 200, f"approve -> {a.status_code}")
    holder = _lib.hold_project_lock(d.home / "projects" / p, 7.0)
    try:
        run = d.wait_run(rid, 90)
    finally:
        holder.wait(30)
    check(run.get("state") == "DONE", f"the run ended {run.get('state')} {run.get('reason_code')}")
    d.quote(p, prompt="drain")  # the next append drains whatever the held lock parked, in order
    got = kinds(d, p, rid)
    check(got == ["quoted", "approved", "submitted", "fetched", "done"], f"ledger kinds for the run: {got}")
    spend = d.api("GET", f"/projects/{p}/spend").json()
    check(
        abs(float(spend.get("spent_usd_quoted", 0)) - q["quote"]["price_usd"]) < 1e-9
        and spend.get("generations") == 1,
        f"/spend after a held lock: {spend.get('spent_usd_quoted')} USD, {spend.get('generations')} generations",
    )


def unattended_clock_reset() -> None:
    with Daemon(name="money-clock", fake={"echo": True, "shift_px": 8}) as d:
        p = "money11"
        assert d.project(p).status_code == 201
        rec = d.post_packet(d.stage_packet(p))
        g = _lib.edit_graph(rec["packet_id"], d.mask_id(rec["packet_id"]), "nano-banana-pro")
        assert d.api("PUT", f"/projects/{p}/graph", g).status_code == 200  # nobody at the UI
        take = d.quote_run(p, engine_id="fake", prompt="take")
        check(d.approve(take, by="auto").status_code == 200, "the first auto approval was refused")
        check(d.wait_run(take).get("state") == "GATE_FAIL", f"the shifted take is {d.run(take).get('state')}")

        def auto(i: int) -> Any:
            rid = d.quote_run(p, engine_id="nano-banana-pro", prompt=f"n{i}")
            return d.approve(rid, by="auto", start=False)

        n, refusal = 0, None
        for i in range(150):
            r = auto(i)
            if r.status_code != 200:
                refusal = r
                break
            n += 1
        if not check(
            refusal is not None and error_of(refusal) == "unattended_cap",
            f"auto approvals never met the unattended cap: {n} approved, last {refusal and refusal.text[:200]}",
        ):
            return
        sid = d.api("POST", f"/runs/{take}/scrub", {"quadrants_visited": 4, "zoom_100": True})
        lifts = {
            "timer start": lambda: d.api("POST", "/timer", {"deliverable_id": "hero", "action": "start"}),
            "override (by human in the body)": lambda: d.api(
                "POST", f"/runs/{take}/override", {"reason": "the client signed the drift off", "by": "human"}
            ),
            "scrub": lambda: d.api("POST", f"/runs/{take}/scrub", {"quadrants_visited": 4, "zoom_100": True}),
        }
        check(sid.status_code == 200, f"scrub -> {sid.status_code}")
        for what, call in lifts.items():
            r = call()
            check(r.status_code == 200, f"{what} without the ui header -> {r.status_code} {r.text[:160]}")
            after = auto(1000 + len(what))
            check(
                after.status_code == 409 and error_of(after) == "unattended_cap",
                f"after a header-less {what} the auto approval -> {after.status_code} {after.text[:160]}",
            )
        assert d.api("PUT", f"/projects/{p}/graph", g, ui=True).status_code == 200
        after = auto(2000)
        check(
            after.status_code == 200,
            f"after a ui request the auto approval -> {after.status_code} {after.text[:160]}",
        )


def daily_cap() -> None:
    with Daemon(name="money-daily") as d:
        projects = ["money100", "money101", "money102"]
        for p in projects:
            d.edit_project(p, "nano-banana-pro")  # the graph PUT carries the ui header: attended
        total, n, refusal = 0.0, 0, None
        for i in range(120):
            p = projects[i % 3]
            q = d.quote(p, engine_id="nano-banana-pro", prompt=f"d{i}", params={"candidate_count": 4})
            if q.status_code != 200:
                check(False, f"quote -> {q.status_code} {q.text[:160]}")
                return
            price = q.json()["quote"]["price_usd"]
            r = d.approve(q.json()["run"]["id"], by="auto", start=False)
            if r.status_code != 200:
                refusal = r
                break
            total += price
            n += 1
        body = refusal.json() if refusal is not None and _lib.is_json(refusal) else {}
        check(
            refusal is not None and body.get("error") == "daily_cap",
            f"{n} auto approvals ({total:.2f} USD) across 3 projects, then {refusal and refusal.text[:200]}",
        )
        check(
            "across 3 projects" in str(body.get("detail")),
            f"the refusal does not name the project count: {body}",
        )
        check(total <= 50.0 + 1e-6, f"approved {total:.2f} USD against the 50.00 USD daily cap")
        allp = d.api("GET", "/spend", params={"all_projects": "1"}).json()
        spent = float(allp.get("spent_usd_quoted") or 0)
        check(spent <= 50.0 + 1e-6, f"GET /spend?all_projects=1 shows {spent:.2f} USD against the 50.00 cap")


def _approve_into(out: dict[str, Any], client: Any, rid: str) -> None:
    out["ap"] = client.post(f"/runs/{rid}/approve", json={"by": "human"})


def _cancel_into(out: dict[str, Any], client: Any, rid: str, delay: float) -> None:
    time.sleep(delay)
    out["ca"] = client.post(f"/runs/{rid}/cancel", json={})


def race() -> dict[str, Any]:
    """36 approve/cancel races on one project (t2_cancel_race.py), run once and shared by two checks."""
    global _RACE
    if _RACE is not None:
        return _RACE
    d = shared()
    p = "money2"
    d.edit_project(p)
    results = []
    for it in range(ITERS):
        rid = d.quote_run(p, prompt=f"race{it}")
        delay = DELAYS[it % len(DELAYS)]
        out: dict[str, Any] = {}
        ap_client, ca_client = d.make_client(), d.make_client()
        ts = [
            threading.Thread(target=_approve_into, args=(out, ap_client, rid)),
            threading.Thread(target=_cancel_into, args=(out, ca_client, rid, delay)),
        ]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        ap_client.close()
        ca_client.close()
        results.append((rid, delay, out.get("ap"), out.get("ca")))
    ids = [r[0] for r in results]
    for rid in ids:
        d.wait_run(rid, 60)
    time.sleep(1.0)
    live = {rid: d.run(rid)["state"] for rid in ids}
    ledger = d.ledger(p)
    d.drop_index(p)
    rebuilt = d.run_states(p)
    _RACE = {"results": results, "live": live, "ledger": ledger, "rebuilt": rebuilt}
    return _RACE


def ledger_order_race() -> None:
    data = race()
    by_run: dict[str, list[str]] = defaultdict(list)
    for rec in data["ledger"]:
        if rec.get("run_id"):
            by_run[rec["run_id"]].append(rec["kind"])
    for rid, state in data["live"].items():
        ks = by_run[rid]
        c = Counter(ks)
        check(c["quoted"] == 1 and c["approved"] <= 1 and c["cancelled"] <= 1, f"{rid}: ledger kinds {ks}")
        if "cancelled" in ks and "approved" in ks:
            check(
                ks.index("approved") < ks.index("cancelled"),
                f"{rid}: approved ledgered after cancelled: {ks}",
            )
        ends = [i for i, k in enumerate(ks) if k in TERMINAL_KINDS]
        if ends:
            check(
                ends[0] == len(ks) - 1 or all(k == "export" for k in ks[ends[0] + 1 :]),
                f"{rid}: records after the end: {ks}",
            )
        if state == "CANCELLED":
            check("cancelled" in ks, f"{rid}: CANCELLED live, no cancelled record: {ks}")
        got = data["rebuilt"].get(rid)
        check(got == state, f"{rid}: live {state}, rebuilt {got} (ledger {ks})")


def approve_500_on_cancel_race() -> None:
    data = race()
    ap = Counter(_lib.status_of(r[2]) for r in data["results"])
    ca = Counter(_lib.status_of(r[3]) for r in data["results"])
    check(set(ap) <= {200, 409}, f"approve answers across {ITERS} races: {dict(ap)}")
    check(set(ca) <= {200, 202, 409}, f"cancel answers across {ITERS} races: {dict(ca)}")
    for _rid, _delay, a, _c in data["results"]:
        if _lib.status_of(a) == 409:
            check(
                _lib.is_json(a) and error_of(a) is not None,
                f"an approve 409 without a typed body: {a.text[:120]}",
            )


def run_stuck_after_db_locked() -> None:
    with Daemon(name="money-busy", fake={"echo": True, "polls_until_done": 4}) as d:
        p = "money17"
        d.edit_project(p)
        rid = d.quote_run(p)
        d.approve(rid)
        run = d.wait_run(rid, 30, until=lambda r: r.get("state") == "RUNNING")
        if not check(run.get("state") == "RUNNING", f"the run never reached RUNNING: {run.get('state')}"):
            return
        con = sqlite3.connect(str(d.index_db(p)), timeout=10, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")  # another writer (a CLI command, a reindex) holds the write lock
            time.sleep(12)
            con.execute("COMMIT")
        finally:
            con.close()
        run = d.wait_run(rid, 60)
        check(
            run.get("state") == "DONE",
            f"12 s after an index lock the run is {run.get('state')} {run.get('reason_code')}",
        )
        check(
            "stopped: database is locked" not in d.log_text(),
            "the run driver stopped on 'database is locked'",
        )


def concurrent_quote_same_attempt() -> None:
    d = shared()
    p = "money5"
    d.edit_project(p)
    clients = [d.make_client() for _ in range(10)]
    try:
        answers = _lib.concurrently(
            lambda i: clients[i].post(f"/projects/{p}/nodes/edit/quote", json={"engine_id": "fake"}), 10
        )
    finally:
        for c in clients:
            c.close()
    ok = [r.json()["run"] for r in answers if _lib.status_of(r) == 200]
    check(len(ok) == 10, f"concurrent quotes: {Counter(_lib.status_of(r) for r in answers)}")
    attempts = sorted(r["attempt"] for r in ok)
    check(attempts == list(range(1, 11)), f"attempts {attempts}")
    for r in ok:
        d.approve(r["id"])
    states = Counter(d.wait_run(r["id"], 120).get("state") for r in ok)
    check(states == Counter({"DONE": len(ok)}), f"after {len(ok)} human approvals: {dict(states)}")


def _expiry(created_shift: timedelta, expires_shift: timedelta, word: str | None) -> None:
    d = shared()
    p = f"money14{'b' if word else 'a'}"
    d.edit_project(p)
    rid = d.quote_run(p)
    now = datetime.now(UTC)
    set_quote_times(d, p, rid, now + created_shift, now + expires_shift)
    r = d.approve(rid, by="auto")
    body = r.json() if _lib.is_json(r) else {}
    check(
        r.status_code == 409 and body.get("error") == "quote_expired",
        f"approve -> {r.status_code} {r.text[:200]}",
    )
    if word:
        check(
            word in str(body.get("detail")), f"the refusal does not say the clock moved: {body.get('detail')}"
        )
    check(d.run(rid).get("state") == "EXPIRED", f"the run is {d.run(rid).get('state')}")
    got = kinds(d, p, rid)
    check(got == ["quoted", "expired"], f"ledger kinds {got}")
    d.drop_index(p)
    check(d.run_states(p).get(rid) == "EXPIRED", f"rebuilt state {d.run_states(p).get(rid)}")


def expired_ledgered() -> None:
    _expiry(timedelta(minutes=-16), timedelta(minutes=-1), None)


def quote_ttl_clock_back() -> None:
    _expiry(timedelta(hours=1), timedelta(hours=1, minutes=15), "clock")


CHECKS = {
    "lost-submitted-record": lost_submitted_record,
    "unattended-clock-reset": unattended_clock_reset,
    "daily-cap": daily_cap,
    "ledger-order-race": ledger_order_race,
    "approve-500-on-cancel-race": approve_500_on_cancel_race,
    "run-stuck-after-db-locked": run_stuck_after_db_locked,
    "concurrent-quote-same-attempt": concurrent_quote_same_attempt,
    "expired-ledgered": expired_ledgered,
    "quote-ttl-clock-back": quote_ttl_clock_back,
}

if __name__ == "__main__":
    sys.exit(_lib.main(CHECKS))
