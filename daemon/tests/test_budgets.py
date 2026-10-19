"""budgets + quotes: who may approve without a click (envelopes, auto cap, daily cap, unattended cap and its
alert), the spend journal (reserved at APPROVED, settled or refunded at the terminal state), and quotes
built from an engine manifest with the requote rule."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plate_daemon.budgets import (
    Decision,
    Envelope,
    Policy,
    Spend,
    authorize,
    committed_since,
    day_start,
    is_external,
    is_unattended,
    publish_alert,
    spend_snapshot,
    spend_snapshot_all,
)
from plate_daemon.db import tx
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.index import Index, apply_packet_entry
from plate_daemon.machine import create_run, get_run, transition
from plate_daemon.models import QUOTE_TTL, Quote, Route, Run, RunCreate
from plate_daemon.quotes import (
    AutoApprovalDenied,
    EnvelopeExceeded,
    EnvelopeRequired,
    approve,
    approve_group,
    build_quote,
    create_envelope,
    get_envelope,
    insert_quote,
    quote_run,
    requote_needed,
)
from plate_daemon.states import Priority, RunState
from plate_daemon.worker import Runner, cancel_run, fan_out

S = RunState
PID = "budget"
PACKET = "pkt_budget"
ROUTE = Route(engine_id="fake", provider="fake", region="local")
EXTERNAL = Route(engine_id="import:manual", provider="external", region="local")
T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def manifest(price: dict | None = None, **over):
    from plate_engines.models import EngineManifest

    doc = {
        "id": "test-edit",
        "name": "Test edit",
        "version": "unpinned",
        "pinned": False,
        "kind": "image_edit",
        "model_id": "test-edit-001",
        "inputs": [{"name": "image", "type": "image"}],
        "outputs": [{"name": "image", "type": "image"}],
        "params": [],
        "price": price or {"model": "per_image", "usd": 0.04, "by": {"2K": 0.08}},
        "determinism": "nondeterministic",
        "licence": {"vendor_terms_url": "unknown"},
        "allowed_tasks": ["relight"],
        "sizes": {"legal": [{"w": 1024, "h": 1024, "label": "1K"}, {"w": 2048, "h": 2048, "label": "2K"}]},
        "routes": [
            {
                "provider": "vendor",
                "endpoint": "https://api.vendor.test",
                "region": "us",
                "custody_grade": "C1",
                "custody_status": "pending_written_terms",
                "terms_hash": "unknown",
                "upload_visibility": "private",
                "allowed_hosts": ["api.vendor.test"],
                "model_slug": "vendor/test-edit-v1",
            },
            {
                "provider": "local",
                "endpoint": "local",
                "region": "local",
                "custody_grade": "C0",
                "custody_status": "confirmed",
                "terms_hash": "unknown",
                "upload_visibility": "local",
            },
        ],
    }
    doc.update(over)
    return EngineManifest.model_validate(doc)


def quoted(price: float = 0.04, *, expires: datetime = T0 + QUOTE_TTL, priority=Priority.INTERACTIVE) -> Run:
    q = Quote(
        id="qt_t",
        run_id="run_t",
        provider="fake",
        model="fake",
        region="local",
        custody_grade="D",
        price_usd=price,
        eta_s=60,
        created_utc=expires - QUOTE_TTL,
        expires_utc=expires,
    )
    return Run(
        id="run_t",
        project_id=PID,
        packet_id=PACKET,
        state=S.QUOTED,
        priority=priority,
        route=ROUTE,
        attempt=1,
        created_utc=T0,
        updated_utc=T0,
        quote=q,
    )


def env(max_usd=10.0, spent=0.0, *, owner="agent", per_run=None, expires=T0 + timedelta(hours=1)) -> Envelope:
    return Envelope(
        envelope_id="env_T",
        owner=owner,
        max_usd=max_usd,
        spent_usd=spent,
        expires_utc=expires,
        max_per_run_usd=per_run,
    )


ATTENDED = T0 - timedelta(minutes=5)
POLICY = Policy()


# ---------------------------------------------------------------------------- authorize (pure)


def test_policy_defaults_are_the_plans():
    assert POLICY.model_dump() == {
        "per_run_auto_cap_usd": 2.0,
        "per_user_daily_cap_usd": 50.0,
        "unattended_cap_usd": 20.0,
        "unattended_after_min": 30,
    }


def test_agents_must_pass_an_envelope():
    d = authorize(quoted(), POLICY, None, ATTENDED, now=T0, requester="agent")
    assert (d.approve, d.code) == (False, "envelope_required")
    # An agent_batch run is an agent's whatever the caller says.
    d = authorize(quoted(priority=Priority.AGENT_BATCH), POLICY, None, ATTENDED, now=T0)
    assert (d.approve, d.code) == (False, "envelope_required")
    d = authorize(quoted(), POLICY, env(), ATTENDED, now=T0, requester="agent")
    assert (d.approve, d.by, d.alert) == (True, "agent:env_T", None)


@pytest.mark.parametrize(
    ("envelope", "code"),
    [
        (env(expires=T0), "envelope_expired"),
        (env(max_usd=1.0, spent=0.98), "envelope_exceeded"),
        (env(per_run=0.01), "envelope_exceeded"),
    ],
)
def test_the_envelope_bounds_an_agent(envelope, code):
    d = authorize(quoted(0.04), POLICY, envelope, ATTENDED, now=T0, requester="agent")
    assert (d.approve, d.code) == (False, code)


def test_agent_envelopes_may_exceed_the_auto_cap_but_not_the_daily_cap():
    d = authorize(quoted(5.0), POLICY, env(max_usd=100), ATTENDED, now=T0, requester="agent")
    assert d.approve and d.by == "agent:env_T"
    d = authorize(
        quoted(5.0), POLICY, env(max_usd=100), ATTENDED, now=T0, requester="agent", spent=Spend(46.0)
    )
    assert (d.approve, d.code) == (False, "daily_cap")


@pytest.mark.parametrize(("price", "ok"), [(0.04, True), (1.99, True), (2.0, False), (2.5, False)])
def test_auto_approves_only_below_the_per_run_cap(price, ok):
    d = authorize(quoted(price), POLICY, None, ATTENDED, now=T0)
    assert d.approve is ok and d.code == ("approved" if ok else "auto_cap")
    if ok:
        assert d.by == "auto"


def test_auto_stays_within_the_daily_cap():
    assert authorize(quoted(1.5), POLICY, None, ATTENDED, now=T0, spent=Spend(48.5, 0)).approve
    d = authorize(quoted(1.5), POLICY, None, ATTENDED, now=T0, spent=Spend(49.0, 0))
    assert (d.approve, d.code) == (False, "daily_cap")


def test_unattended_mode_lowers_the_cap_and_raises_an_alert():
    idle = T0 - timedelta(minutes=31)
    d = authorize(quoted(1.0), POLICY, None, idle, now=T0, spent=Spend(10.0, 10.0))
    assert d.approve and d.unattended and "unattended" in d.alert and "31 min" in d.alert
    d = authorize(quoted(1.0), POLICY, None, idle, now=T0, spent=Spend(19.5, 19.5))
    assert (d.approve, d.code, d.unattended) == (False, "unattended_cap", True) and d.alert
    never = authorize(quoted(1.0), POLICY, None, None, now=T0)
    assert never.unattended and "no human interaction recorded" in never.alert
    # Attended: the unattended cap does not apply and nothing is raised.
    d = authorize(quoted(1.0), POLICY, None, ATTENDED, now=T0, spent=Spend(30.0, 30.0))
    assert d.approve and not d.unattended and d.alert is None


@pytest.mark.parametrize(
    ("idle_s", "unattended"),
    [(0, False), (1799, False), (1799.999999, False), (1800, True), (1800.5, True), (3600, True)],
)
def test_unattended_starts_at_exactly_the_threshold_in_whole_seconds(idle_s, unattended):
    last = T0 - timedelta(seconds=idle_s)
    assert is_unattended(T0, last, POLICY) is unattended
    d = authorize(quoted(1.0), POLICY, None, last, now=T0)
    assert d.unattended is unattended and (d.alert is not None) is unattended
    assert is_unattended(T0, None, POLICY)  # nobody has ever interacted: unattended
    assert not is_unattended(T0, T0 + timedelta(seconds=5), POLICY)  # a clock skewed the other way


def test_no_quote_or_an_expired_quote_is_never_authorized():
    bare = quoted().model_copy(update={"quote": None})
    assert authorize(bare, POLICY, env(), ATTENDED, now=T0).code == "no_quote"
    assert authorize(quoted(expires=T0), POLICY, env(), ATTENDED, now=T0).code == "quote_expired"


# idle time is generated in whole seconds: as a float minute count 29.999999999999996 rounds to 30 min in a
# timedelta but compares below the threshold, which put the boundary on both sides of the assertion.
@settings(max_examples=300, deadline=None)
@given(
    price=st.floats(0, 30, allow_nan=False),
    today=st.floats(0, 60, allow_nan=False),
    since=st.floats(0, 30, allow_nan=False),
    idle_s=st.one_of(st.none(), st.integers(0, 36000)),
    agent=st.booleans(),
    envelope=st.one_of(
        st.none(), st.builds(lambda m, s: env(max_usd=m, spent=min(s, m)), st.floats(0, 40), st.floats(0, 40))
    ),
)
def test_every_approval_respects_every_cap(price, today, since, idle_s, agent, envelope):
    last = None if idle_s is None else T0 - timedelta(seconds=idle_s)
    d = authorize(
        quoted(price),
        POLICY,
        envelope,
        last,
        now=T0,
        spent=Spend(today, since),
        requester="agent" if agent else "auto",
    )
    assert isinstance(d, Decision)
    unattended = last is None or idle_s >= POLICY.unattended_after_min * 60
    assert d.unattended is unattended and (d.alert is not None) is unattended
    if not d.approve:
        assert d.by is None
        return
    assert today + price <= POLICY.per_user_daily_cap_usd + 1e-9
    if unattended:
        assert since + price <= POLICY.unattended_cap_usd + 1e-9
    if agent or (envelope is not None and envelope.is_agent):
        assert envelope is not None and d.by == f"agent:{envelope.envelope_id}"
    else:
        assert d.by == "auto" and price < POLICY.per_run_auto_cap_usd
    if envelope is not None:
        assert price <= envelope.remaining_usd + 1e-9


def test_envelope_owners_are_agent_or_user():
    assert env(owner="user:aasish").owner == "user:aasish" and not env(owner="user:aasish").is_agent
    assert env(owner="agent:mcp-1").is_agent
    with pytest.raises(ValueError):
        env(owner="bob")


# ---------------------------------------------------------------------------- the spend journal


@pytest.fixture
def idx(tmp_path: Path) -> Index:
    index = Index(tmp_path / "home")
    with index.project(PID, create=True) as conn, tx(conn):  # the fixture makes the project real (T144)
        apply_packet_entry(
            conn,
            PID,
            PACKET,
            {
                "shot_id": "s01",
                "kind": "still",
                "tier": "A",
                "packet_version": "0.1.1",
                "path": str(tmp_path / "packets" / PACKET),
                "manifest_blake3": "a" * 64,
            },
        )
    return index


def new_quoted(conn, *, manifests=None, route=ROUTE, now=None, params=None) -> str:
    run = create_run(
        conn,
        RunCreate(
            project_id=PID,
            packet_id=PACKET,
            inputs={"plate": uuid.uuid4().hex},
            params=params or {},
            route=route,
        ),
        now=now,
    )
    quote_run(conn, run.id, now=now, manifests=manifests)
    return run.id


def drive(idx: Index, run_ids: list[str], engine: FakeEngine) -> None:
    with idx.project(PID) as conn:
        for rid in run_ids:
            transition(conn, rid, S.QUEUED, actor="human")
    Runner(idx, None, {"*": engine}).run_until_idle()


def test_spend_is_reserved_at_approved_and_settled_at_done(idx):
    with idx.project(PID) as conn:
        rid = new_quoted(conn)
        run = approve(conn, rid, "human")
        assert (run.reserved_usd, run.cost_usd_billed, run.approved_by) == (0.04, None, "human")
        assert spend_snapshot(conn, datetime.now(UTC), None).today_usd == pytest.approx(0.04)
    drive(idx, [rid], FakeEngine())
    with idx.project(PID) as conn:
        run = get_run(conn, rid)
        assert run.state is S.DONE and run.cost_usd_billed == pytest.approx(0.04) and run.reserved_usd == 0
        kinds = [
            r["kind"]
            for r in conn.execute("SELECT kind FROM run_events WHERE run_id = ? ORDER BY seq", (rid,))
        ]
        assert kinds.count("spend.reserve") == 1 and kinds.count("spend.settle") == 1
        assert (
            kinds.index("spend.reserve") == kinds.index("transition", 1) + 1
        )  # right after QUOTED -> APPROVED
        assert spend_snapshot(conn, datetime.now(UTC), None).today_usd == pytest.approx(0.04)


def test_agent_approval_debits_the_envelope_and_a_failed_submit_refunds_it(idx):
    with idx.project(PID) as conn:
        e = create_envelope(conn, owner="agent", max_total_usd=1.0, max_per_run_usd=0.5, ttl_s=3600)
        rid = new_quoted(conn)
        with pytest.raises(EnvelopeRequired):
            approve(conn, rid, "agent")
        assert get_run(conn, rid).state is S.QUOTED
        run = approve(conn, rid, "agent", e.id)
        assert run.approved_by == f"agent:{e.id}" and get_envelope(conn, e.id).spent_usd == pytest.approx(
            0.04
        )
    drive(idx, [rid], FakeEngine(behaviour="fail_submit"))
    with idx.project(PID) as conn:
        run = get_run(conn, rid)
        assert run.state is S.FAILED and run.provider_job_id is None
        assert run.reserved_usd == 0 and run.cost_usd_billed is None
        assert get_envelope(conn, e.id).spent_usd == pytest.approx(0.0)
        assert spend_snapshot(conn, datetime.now(UTC), None).today_usd == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("engine", "state"),
    [(FakeEngine(behaviour="expire_url"), S.FAILED), (FakeEngine(behaviour="refuse"), S.REFUSED)],
)
def test_a_failure_after_submit_is_settled_not_refunded(idx, engine, state):
    with idx.project(PID) as conn:
        rid = new_quoted(conn)
        approve(
            conn,
            rid,
            "agent:" + create_envelope(conn, owner="agent", max_total_usd=1, max_per_run_usd=1, ttl_s=60).id,
        )
    drive(idx, [rid], engine)
    with idx.project(PID) as conn:
        run = get_run(conn, rid)
        assert run.state is state and run.provider_job_id and run.cost_usd_billed == pytest.approx(0.04)


def test_gate_fail_is_settled_and_a_cancel_before_submit_is_refunded(idx):
    with idx.project(PID) as conn:
        failing = create_run(
            conn,
            RunCreate(project_id=PID, packet_id=PACKET, params={"force_gate_fail": True}, route=ROUTE),
        ).id
        quote_run(conn, failing)
        approve(conn, failing, "human")
        dropped = new_quoted(conn)
        approve(conn, dropped, "human")
        transition(conn, dropped, S.QUEUED, actor="human")
        cancel_run(conn, FakeEngine(), dropped)
        assert get_run(conn, dropped).reserved_usd == 0 and get_run(conn, dropped).cost_usd_billed is None
    drive(idx, [failing], FakeEngine())
    with idx.project(PID) as conn:
        run = get_run(conn, failing)
        assert run.state is S.GATE_FAIL and run.cost_usd_billed == pytest.approx(0.04)


def test_auto_approval_follows_the_policy_and_the_daily_total(idx):
    tight = Policy(per_user_daily_cap_usd=0.10)
    with idx.project(PID) as conn:
        ids = [new_quoted(conn) for _ in range(3)]
        now = datetime.now(UTC)
        assert (
            approve(conn, ids[0], "auto", policy=tight, last_human_interaction_utc=now).approved_by == "auto"
        )
        approve(conn, ids[1], "auto", policy=tight, last_human_interaction_utc=now)
        with pytest.raises(AutoApprovalDenied, match="daily cap"):
            approve(conn, ids[2], "auto", policy=tight, last_human_interaction_utc=now)
        assert get_run(conn, ids[2]).state is S.QUOTED
        # A human can still approve past the auto policy.
        assert approve(conn, ids[2], "human").state is S.APPROVED
        priced = new_quoted(
            conn,
            manifests={"test-edit": manifest({"model": "per_image", "usd": 2.5})},
            route=Route(engine_id="test-edit", provider="local", region="local"),
        )
        assert get_run(conn, priced).cost_usd_quoted == pytest.approx(2.5)
        with pytest.raises(AutoApprovalDenied, match="auto-approve cap"):
            approve(conn, priced, "auto", last_human_interaction_utc=now)


def test_an_unattended_approval_reaches_the_bus_even_when_refused(idx):
    class Bus:
        events: list = []

        def publish(self, run_id, event, *, project_id=None):
            self.events.append((run_id, event, project_id))
            return len(self.events)

    bus = Bus()
    with idx.project(PID) as conn:
        rid = new_quoted(conn)
        sink = lambda d: publish_alert(bus, PID, rid, d)  # noqa: E731
        with pytest.raises(AutoApprovalDenied, match="unattended"):
            approve(conn, rid, "auto", policy=Policy(unattended_cap_usd=0.01), on_alert=sink)
        assert approve(conn, rid, "auto", on_alert=sink).state is S.APPROVED
    assert [e["kind"] for _, e, _ in bus.events] == ["alert", "alert"]
    assert [e["approved"] for _, e, _ in bus.events] == [False, True]
    assert all(p == PID and "unattended" in e["message"] for _, e, p in bus.events)


def test_a_best_of_n_group_is_approved_against_its_total(idx):
    with idx.project(PID) as conn:
        rid = new_quoted(conn)
        sibs = fan_out(conn, rid, 3)
        group = sibs[0].sibling_group
        now = datetime.now(UTC)
        with pytest.raises(AutoApprovalDenied):  # 3 x 0.04 = 0.12 is not below a 0.10 cap
            approve_group(
                conn, group, "auto", policy=Policy(per_run_auto_cap_usd=0.10), last_human_interaction_utc=now
            )
        e = create_envelope(conn, owner="user:aasish", max_total_usd=0.10, max_per_run_usd=1, ttl_s=60)
        with pytest.raises(EnvelopeExceeded):
            approve_group(conn, group, "human", e.id)
        approved = approve_group(conn, group, "auto", last_human_interaction_utc=now)
        assert [r.state for r in approved] == [S.APPROVED] * 3
        assert [r.reserved_usd for r in approved] == [pytest.approx(0.04)] * 3
        assert spend_snapshot(conn, now, None).today_usd == pytest.approx(0.12)


def test_the_caps_count_every_project_index_in_the_home(idx, tmp_path, caplog):
    """Decision 80 (Wave 12): `spend_snapshot_all` sums every project of the home, the refusal says how many it
    summed, and an index that cannot be opened is skipped with a warning (never a crash)."""
    now = datetime.now(UTC)
    with idx.project(PID) as conn:
        approve(conn, new_quoted(conn), "auto", last_human_interaction_utc=now)
    other = "budget-two"
    with idx.project(other, create=True) as conn:  # a second real project (T144)
        with tx(conn):
            apply_packet_entry(
                conn,
                other,
                PACKET,
                {
                    "shot_id": "s01",
                    "kind": "still",
                    "tier": "A",
                    "packet_version": "0.1.1",
                    "path": str(tmp_path / "packets" / PACKET),
                    "manifest_blake3": "a" * 64,
                },
            )
        runs = []
        for _ in range(2):
            run = create_run(
                conn,
                RunCreate(
                    project_id=other, packet_id=PACKET, inputs={"plate": uuid.uuid4().hex}, route=ROUTE
                ),
            )
            quote_run(conn, run.id)
            runs.append(run.id)
        approve(conn, runs[0], "auto", last_human_interaction_utc=now)
    (idx.home / "index" / "broken.sqlite").write_bytes(b"this is not a sqlite file at all, not even close")

    spent = spend_snapshot_all(idx, now, now)
    assert (spent.today_usd, spent.projects) == (pytest.approx(0.08), 2)
    assert "broken" in caplog.text
    with idx.project(other, create=True) as conn:  # a second real project (T144)
        assert spend_snapshot(conn, now, now).projects is None  # one connection: this project only
        with pytest.raises(AutoApprovalDenied, match="across 2 projects"):
            approve(
                conn,
                runs[1],
                "auto",
                policy=Policy(per_user_daily_cap_usd=0.1),
                last_human_interaction_utc=now,
                spent=spent,
            )
        assert get_run(conn, runs[1]).state is S.QUOTED
        # Without `spent` the cap reads this project alone (0.04 + 0.04 fits a 0.1 cap).
        assert approve(conn, runs[1], "auto", policy=Policy(per_user_daily_cap_usd=0.1)).state is S.APPROVED
    d = authorize(quoted(0.04), POLICY, None, None, now=T0, spent=Spend(0.0, 19.99, projects=1))
    assert d.code == "unattended_cap" and "across 1 project)" in d.reason


# ---------------------------------------------------------------------------- externally declared spend


def new_external(conn, usd: float, *, packet: str = PACKET, project: str = PID) -> str:
    """A QUOTED run on the import route (provider `external`) priced at the declared `usd`."""
    now = datetime.now(UTC)
    run = create_run(
        conn,
        RunCreate(project_id=project, packet_id=packet, inputs={"plate": uuid.uuid4().hex}, route=EXTERNAL),
        now=now,
    )
    q = Quote(
        run_id=run.id,
        provider="external",
        model="import:manual",
        model_version="manual",
        region="local",
        custody_grade="C0",
        price_usd=usd,
        eta_s=0.0,
        created_utc=now,
        expires_utc=now + QUOTE_TTL,
        price_status="declared",
    )
    with tx(conn):
        insert_quote(conn, q)
        transition(conn, run.id, S.QUOTED, actor="test", now=now, quote_id=q.id, quote_json=q.terms_json())
    return run.id


def reserve_body(conn, run_id: str) -> dict:
    import json

    (row,) = conn.execute(
        "SELECT reason FROM run_events WHERE run_id = ? AND kind = 'spend.reserve'", (run_id,)
    ).fetchall()
    return json.loads(row["reason"])


def test_is_external_reads_the_route_provider():
    assert is_external(quoted().model_copy(update={"route": EXTERNAL}))
    assert not is_external(quoted())  # provider "fake"
    assert not is_external(
        quoted().model_copy(update={"route": Route(engine_id="x", provider="vendor", region="us")})
    )


def test_an_external_reservation_is_marked_and_both_caps_leave_it_out(idx):
    """Decision 89: the marker is on the external run's `spend.reserve` body only; every other record keeps the
    old shape and reads as internal."""
    now = datetime.now(UTC)
    with idx.project(PID) as conn:
        imported = approve(conn, new_external(conn, 60.0), "human")
        internal = approve(conn, new_quoted(conn), "auto", last_human_interaction_utc=now)
        assert (imported.reserved_usd, internal.reserved_usd) == (60.0, 0.04)
        assert reserve_body(conn, imported.id) == {
            "usd": 60.0,
            "by": "human",
            "envelope_id": None,
            "external": True,
        }
        assert "external" not in reserve_body(conn, internal.id)
        start = day_start(now)
        assert committed_since(conn, start) == pytest.approx(0.04)
        assert committed_since(conn, start, include_external=True) == pytest.approx(60.04)
        assert committed_since(conn, start, unattended_only=True) == pytest.approx(0.04)
        assert committed_since(conn, start, unattended_only=True, include_external=True) == pytest.approx(
            0.04
        )
        snap = spend_snapshot(conn, now, None)
        assert snap.today_usd == pytest.approx(0.04) and snap.since_human_usd == pytest.approx(0.04)
        assert snap.external_today_usd == 60.0
        # the caps see 0.04, so a 49.9 USD agent run still fits a 50 USD day; with the 60 counted it would not
        assert authorize(quoted(49.9), POLICY, env(50.0), ATTENDED, now=T0, spent=snap).approve
        counted = Spend(snap.today_usd + 60.0, snap.since_human_usd)
        assert authorize(quoted(49.9), POLICY, env(50.0), ATTENDED, now=T0, spent=counted).code == "daily_cap"


def test_the_refund_of_an_external_run_is_left_out_with_it(idx):
    now = datetime.now(UTC)
    with idx.project(PID) as conn:
        rid = new_external(conn, 7.0)
        approve(conn, rid, "human")
        approve(conn, new_quoted(conn), "auto", last_human_interaction_utc=now)
        assert spend_snapshot(conn, now, None).external_today_usd == 7.0
        cancel_run(conn, FakeEngine(), rid)  # never reached a provider: the reservation is refunded
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM run_events WHERE run_id = ?", (rid,))]
        assert kinds.count("spend.refund") == 1
        start = day_start(now)
        assert committed_since(conn, start) == pytest.approx(0.04)
        assert committed_since(conn, start, include_external=True) == pytest.approx(0.04)  # 7 - 7
        assert spend_snapshot(conn, now, None).external_today_usd == 0.0


def test_external_spend_is_summed_across_the_projects_of_the_home(idx, tmp_path):
    now = datetime.now(UTC)
    other = "budget-two"
    with idx.project(PID) as conn:
        approve(conn, new_external(conn, 60.0), "human")
        approve(conn, new_quoted(conn), "auto", last_human_interaction_utc=now)
    with idx.project(other, create=True) as conn:
        with tx(conn):
            apply_packet_entry(
                conn,
                other,
                PACKET,
                {
                    "shot_id": "s01",
                    "kind": "still",
                    "tier": "A",
                    "packet_version": "0.1.1",
                    "path": str(tmp_path / "packets" / PACKET),
                    "manifest_blake3": "a" * 64,
                },
            )
        approve(conn, new_external(conn, 5.5, project=other), "human")
    spent = spend_snapshot_all(idx, now, None)
    assert spent.projects == 2 and spent.today_usd == pytest.approx(0.04)
    assert spent.external_today_usd == pytest.approx(65.5)
    assert Spend().external_today_usd == 0.0


# ---------------------------------------------------------------------------- quotes from manifests


def test_build_quote_from_a_manifest():
    m = manifest()
    q = build_quote(m, "vendor", {"image": "a" * 64}, {"image_size": "2K", "candidate_count": 2}, now=T0)
    assert (q.provider, q.model, q.model_version, q.region, q.route_id) == (
        "vendor",
        "test-edit",
        "vendor/test-edit-v1",
        "us",
        "vendor",
    )
    assert q.custody_grade == "D"  # C1 declared, written terms pending: the router acts on D
    assert q.price_usd == pytest.approx(0.16) and q.size == {"w": 2048, "h": 2048, "label": "2K"}
    assert q.expires_utc == T0 + timedelta(minutes=15) and q.price_status == "unconfirmed"
    local = build_quote(m, {"provider": "local", "region": "local"}, {}, {}, now=T0)
    assert (local.custody_grade, local.model_version, local.price_usd) == (
        "C0",
        "test-edit-001",
        pytest.approx(0.04),
    )
    assert local.size == {"w": 1024, "h": 1024, "label": "1K"}


def test_build_quote_bills_megapixels_and_seconds():
    mp = manifest({"model": "per_megapixel", "usd": 0.01})
    assert build_quote(mp, None, {}, {"width": 2000, "height": 1000}, now=T0).price_usd == pytest.approx(0.02)
    sec = manifest({"model": "per_second", "usd": 0.1}, kind="video_i2v")
    q = build_quote(sec, None, {}, {"duration_s": 5}, now=T0)
    assert q.price_usd == pytest.approx(0.5) and q.eta_s == 300.0
    with pytest.raises(ValueError, match="duration"):
        build_quote(sec, None, {}, {}, now=T0)


def _q(price: float, version: str = "v1") -> Quote:
    return Quote(
        provider="p",
        model="m",
        model_version=version,
        region="r",
        custody_grade="D",
        price_usd=price,
        eta_s=1,
        created_utc=T0,
        expires_utc=T0 + QUOTE_TTL,
    )


@pytest.mark.parametrize(
    ("new", "needed"),
    [
        (_q(1.0), False),
        (_q(1.05), False),
        (_q(0.95), False),
        (_q(1.051), True),
        (_q(0.94), True),
        (_q(1.0, "v2"), True),
    ],
)
def test_requote_needed(new, needed):
    assert requote_needed(_q(1.0), new) is needed


@settings(max_examples=200, deadline=None)
@given(
    old=st.floats(0.001, 100, allow_nan=False),
    new=st.floats(0, 100, allow_nan=False),
    same_version=st.booleans(),
)
def test_requote_iff_price_moves_over_5_percent_or_the_model_changes(old, new, same_version):
    a, b = _q(old), _q(new, "v1" if same_version else "v2")
    assert requote_needed(a, b) is ((not same_version) or abs(new - old) / old > 0.05 + 1e-12)
