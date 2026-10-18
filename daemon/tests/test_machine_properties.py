"""states + machine: the transition table is the law (every pair, random walks), the guards hold
(provider id before SUBMITTED, approver and unexpired quote before APPROVED), and idempotency keys are a
pure function of the request."""

from __future__ import annotations

import itertools
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from plate_daemon.db import dumps, tx
from plate_daemon.idempotency import idempotency_key, input_hashes, route_id, ulid
from plate_daemon.index import Index, apply_packet_entry
from plate_daemon.machine import (
    DuplicateRequest,
    IllegalTransition,
    MissingApprover,
    MissingProviderJobId,
    QuoteExpired,
    annotate,
    apply,
    create_run,
    get_run,
    staging_key,
    transition,
)
from plate_daemon.models import QUOTE_TTL, Quote, Route, Run, RunCreate
from plate_daemon.quotes import approve, insert_quote, quote_run
from plate_daemon.states import PROVIDER_STATES, TERMINAL, TRANSITIONS, Priority, RunState

S = RunState
PROJECT = "props"
PACKET = "pkt_props"
ROUTE = Route(engine_id="fake", provider="fake", region="local")
ALL = list(RunState)
PAIRS = list(itertools.product(ALL, ALL))
T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
FIXTURE_OK = settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])

# The plan's table, written out once more by hand: the code's table must equal it exactly.
SPEC_TABLE = {
    S.DRAFT: {S.QUOTED, S.CANCELLED},
    S.QUOTED: {S.APPROVED, S.EXPIRED, S.CANCELLED},
    S.APPROVED: {S.QUEUED, S.CANCELLED},
    S.QUEUED: {S.STAGING, S.CANCELLED},
    S.STAGING: {S.SUBMITTED, S.FAILED, S.CANCELLED},
    S.SUBMITTED: {S.RUNNING, S.FAILED, S.REFUSED, S.CANCELLED},
    S.RUNNING: {S.FETCHING, S.FAILED, S.REFUSED, S.CANCELLED},
    S.FETCHING: {S.NORMALIZING, S.FAILED},
    S.NORMALIZING: {S.GATING, S.FAILED},
    S.GATING: {S.DONE, S.GATE_FAIL, S.FAILED},
}


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    c = Index(home).connect(PROJECT, create=True)  # the fixture makes the project real (T144)
    with tx(c):
        apply_packet_entry(
            c,
            PROJECT,
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
    yield c
    c.close()


def _quote(run_id: str = "run_x", expires: datetime = T0 + QUOTE_TTL) -> Quote:
    return Quote(
        id=f"qt_{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        provider="fake",
        model="fake",
        region="local",
        custody_grade="D",
        price_usd=0.04,
        eta_s=60,
        created_utc=expires - QUOTE_TTL,
        expires_utc=expires,
    )


def pure_run(
    state: RunState, *, provider_job_id: str | None = "job-1", quote: Quote | None = None, **kw
) -> Run:
    base = {
        "id": "run_x",
        "project_id": PROJECT,
        "packet_id": PACKET,
        "state": state,
        "priority": Priority.INTERACTIVE,
        "inputs": {"plate": "b" * 64, "prompt": "late sun on the facade"},
        "params": {"image_size": "2K"},
        "route": ROUTE,
        "attempt": 1,
        "created_utc": T0,
        "updated_utc": T0,
        "provider_job_id": provider_job_id,
        "quote": quote if quote is not None else _quote(),
    }
    base.update(kw)
    return Run(**base)


def fresh_run(conn, **kw) -> Run:
    """A DRAFT run with unique inputs (so its idempotency key is its own)."""
    spec = RunCreate(
        project_id=PROJECT,
        packet_id=PACKET,
        inputs={"plate": uuid.uuid4().hex, **kw.pop("inputs", {})},
        route=kw.pop("route", ROUTE),
        **kw,
    )
    return create_run(conn, spec, now=T0)


def seeded_row(conn, state: RunState) -> str:
    """A run forced straight into `state` (bypassing the machine) with a provider id and a fresh quote."""
    run = fresh_run(conn)
    q = _quote(run.id)
    with tx(conn):
        insert_quote(conn, q)
        conn.execute(
            "UPDATE runs SET state = ?, provider_job_id = ?, quote_id = ?, quote_json = ? WHERE id = ?",
            (state.value, f"job-{run.id}", q.id, dumps(q.terms_json()), run.id),
        )
    return run.id


def _event_count(conn, run_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM run_events WHERE run_id = ?", (run_id,)).fetchone()[0]


# ---------------------------------------------------------------------------- the table


def test_the_table_is_the_plans_table_and_terminal_states_have_no_exits():
    assert set(TRANSITIONS) == set(RunState)
    assert {k: set(v) for k, v in TRANSITIONS.items() if v} == SPEC_TABLE
    assert TERMINAL == {S.DONE, S.GATE_FAIL, S.FAILED, S.REFUSED, S.CANCELLED, S.EXPIRED}
    assert all(not TRANSITIONS[t] for t in TERMINAL)
    assert [p.value for p in Priority] == [0, 1, 2, 3]
    assert [p.name for p in Priority] == ["CLIENT_DEADLINE", "INTERACTIVE", "AGENT_BATCH", "CALIBRATION"]


@pytest.mark.parametrize(("frm", "to"), PAIRS, ids=[f"{a.value}-{b.value}" for a, b in PAIRS])
def test_every_pair_pure(frm, to):
    run = pure_run(frm)
    if to in TRANSITIONS[frm]:
        new = apply(run, to, "why", now=T0, actor="human")
        assert new.state is to
        assert (new.history[-1].from_, new.history[-1].to, new.history[-1].reason) == (frm, to, "why")
        assert run.state is frm  # pure: the input is untouched
    else:
        with pytest.raises(IllegalTransition) as e:
            apply(run, to, now=T0, actor="human")
        assert (e.value.from_state, e.value.to_state) == (frm, to)


@FIXTURE_OK
@given(frm=st.sampled_from(ALL), to=st.sampled_from(ALL))
def test_every_pair_against_sqlite(conn, frm, to):
    rid = seeded_row(conn, frm)
    before = _event_count(conn, rid)
    if to in TRANSITIONS[frm]:
        run = transition(conn, rid, to, actor="human", now=T0)
        assert run.state is to and get_run(conn, rid).state is to
        assert [h.to for h in run.history][-1] is to
        assert _event_count(conn, rid) >= before + 1
    else:
        with pytest.raises(IllegalTransition):
            transition(conn, rid, to, actor="human", now=T0)
        assert get_run(conn, rid).state is frm
        assert _event_count(conn, rid) == before


@settings(max_examples=300, deadline=None)
@given(walk=st.lists(st.sampled_from(ALL), min_size=1, max_size=40))
def test_pure_random_walks_never_leave_a_terminal_state(walk):
    run = pure_run(S.DRAFT, quote=_quote(expires=T0 + timedelta(days=1)))
    terminal_at = None
    for to in walk:
        legal = to in TRANSITIONS[run.state]
        try:
            run = apply(run, to, now=T0, actor="human")
            moved = True
        except IllegalTransition:
            moved = False
        assert moved == legal
        if terminal_at is not None:
            assert not moved and run.state is terminal_at
        if run.state in TERMINAL:
            terminal_at = run.state


@FIXTURE_OK
@given(
    walk=st.lists(st.tuples(st.sampled_from(ALL), st.booleans()), min_size=1, max_size=30),
)
def test_sqlite_random_walks_never_leave_terminal_and_never_submit_without_an_id(conn, walk):
    rid = fresh_run(conn).id
    terminal_at = None
    for to, give_id in walk:
        cur = get_run(conn, rid)
        if cur.state is S.STAGING and to is S.SUBMITTED and give_id:
            annotate(conn, rid, expect=S.STAGING, provider_job_id=f"job-{rid}")
            cur = get_run(conn, rid)
        legal = to in TRANSITIONS[cur.state]
        needs_id = to is S.SUBMITTED and not cur.provider_job_id
        try:
            transition(conn, rid, to, actor="human", now=T0)
            moved = True
        except IllegalTransition:
            moved = False
        except MissingProviderJobId:
            assert legal and needs_id
            moved = False
        assert moved == (legal and not needs_id)
        after = get_run(conn, rid)
        if after.state in PROVIDER_STATES:
            assert after.provider_job_id  # a submitted run always carries the id recovery re-polls
        if terminal_at is not None:
            assert not moved and after.state is terminal_at
        if after.state in TERMINAL:
            terminal_at = after.state


# ---------------------------------------------------------------------------- SUBMITTED needs a persisted id


def test_submitted_needs_the_provider_id_persisted_first(conn):
    with pytest.raises(MissingProviderJobId):
        apply(pure_run(S.STAGING, provider_job_id=None), S.SUBMITTED, now=T0)
    # An id handed to the transition itself is not "persisted first" (two-phase).
    with pytest.raises(MissingProviderJobId):
        apply(pure_run(S.STAGING, provider_job_id=None), S.SUBMITTED, now=T0, provider_job_id="job-9")

    rid = seeded_row(conn, S.QUEUED)
    with tx(conn):
        conn.execute("UPDATE runs SET provider_job_id = NULL WHERE id = ?", (rid,))
    transition(conn, rid, S.STAGING, actor="worker")
    with pytest.raises(MissingProviderJobId):
        transition(conn, rid, S.SUBMITTED, actor="worker", provider_job_id="job-9")
    assert get_run(conn, rid).provider_job_id is None and get_run(conn, rid).state is S.STAGING
    annotate(conn, rid, expect=S.STAGING, provider_job_id="job-9")  # phase one
    with pytest.raises(ValueError, match="differs"):
        transition(conn, rid, S.SUBMITTED, actor="worker", provider_job_id="job-other")
    run = transition(conn, rid, S.SUBMITTED, actor="worker")  # phase two
    assert run.state is S.SUBMITTED and run.provider_job_id == "job-9"


def test_storage_refuses_a_provider_state_without_an_id(conn):
    import sqlite3

    rid = fresh_run(conn).id
    with pytest.raises(sqlite3.IntegrityError), tx(conn):
        conn.execute("UPDATE runs SET state = 'SUBMITTED' WHERE id = ?", (rid,))
    assert get_run(conn, rid).state is S.DRAFT


# ---------------------------------------------------------------------------- APPROVED guards


@FIXTURE_OK
@given(late_s=st.floats(min_value=0, max_value=3 * 86400, allow_nan=False))
def test_an_expired_quote_cannot_be_approved(conn, late_s):
    rid = fresh_run(conn).id
    run, q = quote_run(conn, rid, now=T0)
    assert run.quote_expires_utc == q.expires_utc == T0 + QUOTE_TTL
    with pytest.raises(QuoteExpired):
        approve(conn, rid, "human", now=q.expires_utc + timedelta(seconds=late_s))
    after = get_run(conn, rid)
    assert after.state is S.EXPIRED and after.approved_by is None and after.reserved_usd == 0
    assert S.APPROVED not in [h.to for h in after.history]
    with pytest.raises(IllegalTransition):
        approve(conn, rid, "human", now=T0)


@FIXTURE_OK
@given(early_s=st.floats(min_value=0, max_value=QUOTE_TTL.total_seconds() - 0.001, allow_nan=False))
def test_a_live_quote_is_approved(conn, early_s):
    rid = fresh_run(conn).id
    quote_run(conn, rid, now=T0)
    run = approve(conn, rid, "human", now=T0 + timedelta(seconds=early_s))
    assert run.state is S.APPROVED and run.approved_by == "human"


def test_the_raw_machine_expires_instead_of_approving(conn):
    rid = fresh_run(conn).id
    quote_run(conn, rid, now=T0)
    with pytest.raises(QuoteExpired):
        transition(conn, rid, S.APPROVED, actor="human", now=T0 + QUOTE_TTL + timedelta(seconds=1))
    assert get_run(conn, rid).state is S.EXPIRED
    with pytest.raises(QuoteExpired):
        apply(pure_run(S.QUOTED, quote=_quote(expires=T0)), S.APPROVED, now=T0, actor="human")


def test_approved_needs_an_approver(conn):
    rid = fresh_run(conn).id
    quote_run(conn, rid, now=T0)
    with pytest.raises(MissingApprover):
        transition(conn, rid, S.APPROVED, actor="system", now=T0)
    with pytest.raises(MissingApprover):
        transition(conn, rid, S.APPROVED, actor="system", approved_by="robot", now=T0)
    assert get_run(conn, rid).state is S.QUOTED
    run = transition(conn, rid, S.APPROVED, actor="mcp", approved_by="agent:env_01ABC", now=T0)
    assert run.approved_by == "agent:env_01ABC"
    with pytest.raises(TypeError):
        transition(conn, rid, S.QUEUED, actor="human", approved_by="human")


# ---------------------------------------------------------------------------- idempotency

json_scalars = st.one_of(
    st.text(max_size=12),
    st.integers(min_value=-(2**31), max_value=2**31),
    st.booleans(),
    st.none(),
    st.floats(allow_nan=False, allow_infinity=False),
)
cas_keys = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
inputs_st = st.dictionaries(st.text(min_size=1, max_size=8), st.one_of(cas_keys, json_scalars), max_size=5)
params_st = st.dictionaries(
    st.text(min_size=1, max_size=8), st.one_of(json_scalars, st.lists(json_scalars, max_size=3)), max_size=5
)


def _reordered(d: dict) -> dict:
    return dict(reversed(list(d.items())))


@settings(max_examples=200, deadline=None)
@given(inputs=inputs_st, params=params_st, attempt=st.integers(1, 9))
def test_two_staging_computations_with_equal_inputs_give_one_key(inputs, params, attempt):
    a = pure_run(S.QUEUED, provider_job_id=None, inputs=inputs, params=params, attempt=attempt)
    b = pure_run(
        S.QUEUED, provider_job_id=None, inputs=_reordered(inputs), params=_reordered(params), attempt=attempt
    )
    ka = apply(a, S.STAGING, now=T0).idempotency_key
    kb = apply(b, S.STAGING, now=T0).idempotency_key
    assert ka == kb == staging_key(a)
    assert ka == idempotency_key(input_hashes(inputs), params, route_id(ROUTE.model_dump()), attempt)
    assert re.fullmatch(r"[0-9a-f]{64}", ka)


@settings(max_examples=100, deadline=None)
@given(inputs=inputs_st, params=params_st, attempt=st.integers(1, 9))
def test_a_different_request_gets_a_different_key(inputs, params, attempt):
    base = pure_run(S.QUEUED, provider_job_id=None, inputs=inputs, params=params, attempt=attempt)
    k = staging_key(base)
    assert staging_key(base.model_copy(update={"attempt": attempt + 1})) != k
    assert staging_key(base.model_copy(update={"params": {**params, "__new": 1}})) != k
    assert staging_key(base.model_copy(update={"inputs": {**inputs, "__new": "x"}})) != k
    moved = Route(engine_id="fake", provider="fake", region="eu")
    assert staging_key(base.model_copy(update={"route": moved})) != k


def test_the_key_carries_input_hashes_not_prompt_text():
    run = pure_run(S.QUEUED, provider_job_id=None)
    hashes = input_hashes(run.inputs)
    assert hashes["plate"] == "b" * 64  # a CAS key is its own hash
    assert "late sun" not in str(hashes) and re.fullmatch(r"[0-9a-f]{64}", hashes["prompt"])


def test_restaging_the_same_run_recomputes_the_same_key_and_a_duplicate_request_is_refused(conn):
    a = fresh_run(conn, inputs={"prompt": "same"})
    for to in (S.QUOTED, S.APPROVED, S.QUEUED, S.STAGING):
        transition(conn, a.id, to, actor="human", now=T0)
    staged = get_run(conn, a.id)
    assert staged.idempotency_key == staging_key(staged)
    # A second run with the identical request (same inputs, params, route, attempt) is a duplicate.
    b = create_run(
        conn, RunCreate(project_id=PROJECT, packet_id=PACKET, inputs=a.inputs, route=ROUTE), now=T0
    )
    for to in (S.QUOTED, S.APPROVED, S.QUEUED):
        transition(conn, b.id, to, actor="human", now=T0)
    with pytest.raises(DuplicateRequest) as e:
        transition(conn, b.id, S.STAGING, actor="worker", now=T0)
    assert e.value.other_run_id == a.id and get_run(conn, b.id).state is S.QUEUED
    # A deliberate re-run is a new attempt, and a new key.
    c = create_run(
        conn, RunCreate(project_id=PROJECT, packet_id=PACKET, inputs=a.inputs, route=ROUTE, attempt=2), now=T0
    )
    for to in (S.QUOTED, S.APPROVED, S.QUEUED, S.STAGING):
        transition(conn, c.id, to, actor="human", now=T0)
    assert get_run(conn, c.id).idempotency_key != staged.idempotency_key


# ---------------------------------------------------------------------------- the Run model


def test_run_ids_are_ulids_and_the_json_shape_keeps_the_seed_names(conn):
    run = fresh_run(conn, priority="client_deadline")
    assert re.fullmatch(r"run_[0-9A-HJKMNP-TV-Z]{26}", run.id) and run.run_id == run.id
    assert ulid(T0) < ulid(T0 + timedelta(milliseconds=2))
    quote_run(conn, run.id, now=T0)
    run = approve(conn, run.id, "human", now=T0)
    doc = run.model_dump(mode="json")
    assert doc["id"] == doc["run_id"] == run.id
    assert doc["priority"] == "client_deadline" and run.priority is Priority.CLIENT_DEADLINE
    assert doc["quote"]["expires_utc"] and doc["quote_expires_utc"] and doc["cost_usd_quoted"] == 0.04
    assert [(h["from"], h["to"]) for h in doc["history"]] == [("DRAFT", "QUOTED"), ("QUOTED", "APPROVED")]
    assert doc["approved_by"] == "human" and doc["sibling_group"] is None and doc["outputs"] == []


def test_priority_parses_names_labels_and_numbers():
    assert Priority.parse("agent_batch") is Priority.AGENT_BATCH
    assert Priority.parse("CALIBRATION") is Priority.CALIBRATION
    assert Priority.parse(0) is Priority.CLIENT_DEADLINE
    with pytest.raises(ValueError):
        Priority.parse("urgent")
    with pytest.raises(ValueError):
        Priority.parse(True)
