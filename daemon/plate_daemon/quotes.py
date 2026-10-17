"""Quote before spend (doctrine 8): quotes, approval rules and agent budget envelopes.

`make_quote` prices a run (a fake 0.04 USD until real engine manifests price it; provider and region from
the run's route, custody grade from the route or "D"); a quote expires 15 minutes after it is made.
`approve` moves QUOTED -> APPROVED only before expiry, otherwise commits QUOTED -> EXPIRED and raises
`QuoteExpired`. A `human` may approve without an envelope; an `agent` always needs a valid envelope with
room for the price; `auto` approval needs the session envelope and a price within its per-run cap.
Approving against an envelope debits `spent_usd` in the same transaction as the state change.
Envelopes are stored in the project's index, so an envelope budgets one owner on one project.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .db import tx
from .machine import IllegalTransition, get_run, new_id, transition
from .models import QUOTE_TTL, BudgetEnvelope, Quote, Run
from .states import RunState
from .timeutil import iso, utcnow

FAKE_PRICE_USD = 0.04
FAKE_ETA_S = 60.0
CUSTODY_GRADES = ("C0", "C1", "C2", "D")
APPROVERS = ("human", "auto", "agent")


class QuoteExpired(Exception):
    pass


class EnvelopeRequired(Exception):
    """An agent (or auto) approval has no valid budget envelope for this price."""


class EnvelopeExceeded(EnvelopeRequired):
    """The envelope exists but the price breaks its per-run cap or its remaining total."""


class QuoteNotFound(KeyError):
    pass


def make_quote(run: Run, manifests: dict | None = None, *, now: datetime | None = None) -> Quote:
    """Price a run. `manifests` (plate_engines manifests by id) is accepted for the real pricer; the
    fake price ignores it. Custody comes from the route, else D (unknown is treated as public)."""
    now = now or utcnow()
    route = run.route_dict
    grade = route.get("custody_grade") if route.get("custody_grade") in CUSTODY_GRADES else "D"
    if route.get("custody_status") not in (None, "confirmed"):
        grade = "D"  # pending written terms counts as D (decision 4 of the scaffold plan)
    return Quote(
        id=new_id("qt"),
        run_id=run.id,
        provider=route["provider"],
        model=route["engine_id"],
        region=route["region"],
        custody_grade=grade,
        price_usd=FAKE_PRICE_USD,
        eta_s=FAKE_ETA_S,
        created_utc=now,
        expires_utc=now + QUOTE_TTL,
    )


def get_quote(conn: sqlite3.Connection, quote_id: str) -> Quote:
    row = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if row is None:
        raise QuoteNotFound(quote_id)
    return Quote.from_row(row)


def quote_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    actor: str = "system",
    now: datetime | None = None,
    manifests: dict | None = None,
) -> tuple[Run, Quote]:
    """DRAFT -> QUOTED with a stored quote, atomically."""
    with tx(conn):
        run = get_run(conn, run_id)
        if run.state is not RunState.DRAFT:
            raise IllegalTransition(run.state, RunState.QUOTED)
        q = make_quote(run, manifests, now=now)
        conn.execute(
            """INSERT INTO quotes (id, run_id, provider, model, region, custody_grade, price_usd, eta_s,
                                   created_utc, expires_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                q.id,
                q.run_id,
                q.provider,
                q.model,
                q.region,
                q.custody_grade,
                q.price_usd,
                q.eta_s,
                iso(q.created_utc),
                iso(q.expires_utc),
            ),
        )
        run = transition(
            conn,
            run_id,
            RunState.QUOTED,
            actor=actor,
            reason=f"{q.provider}/{q.model} {q.region} {q.custody_grade} {q.price_usd:.4f} USD",
            quote_id=q.id,
        )
    return run, q


def create_envelope(
    conn: sqlite3.Connection,
    *,
    owner: str,
    max_total_usd: float,
    max_per_run_usd: float,
    ttl_s: float,
    now: datetime | None = None,
) -> BudgetEnvelope:
    if max_total_usd < 0 or max_per_run_usd < 0 or ttl_s <= 0:
        raise ValueError("envelope limits must be >= 0 and ttl_s > 0")
    now = now or utcnow()
    env = BudgetEnvelope(
        id=new_id("env"),
        owner=owner,
        max_total_usd=max_total_usd,
        max_per_run_usd=max_per_run_usd,
        spent_usd=0.0,
        created_utc=now,
        expires_utc=now + timedelta(seconds=ttl_s),
    )
    with tx(conn):
        conn.execute(
            """INSERT INTO budget_envelopes (id, owner, max_total_usd, max_per_run_usd, spent_usd,
                                             created_utc, expires_utc) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                env.id,
                env.owner,
                env.max_total_usd,
                env.max_per_run_usd,
                0.0,
                iso(env.created_utc),
                iso(env.expires_utc),
            ),
        )
    return env


def get_envelope(conn: sqlite3.Connection, envelope_id: str) -> BudgetEnvelope | None:
    row = conn.execute("SELECT * FROM budget_envelopes WHERE id = ?", (envelope_id,)).fetchone()
    return None if row is None else BudgetEnvelope.from_row(row)


def _check_envelope(env: BudgetEnvelope | None, price: float, now: datetime, actor: str) -> BudgetEnvelope:
    if env is None:
        raise EnvelopeRequired(f"{actor} approval needs a budget envelope")
    if now >= env.expires_utc:
        raise EnvelopeRequired(f"envelope {env.id} expired at {iso(env.expires_utc)}")
    if price > env.max_per_run_usd + 1e-12:
        raise EnvelopeExceeded(
            f"price {price:.4f} USD exceeds the envelope's per-run cap {env.max_per_run_usd:.4f}"
        )
    if env.spent_usd + price > env.max_total_usd + 1e-12:
        raise EnvelopeExceeded(
            f"price {price:.4f} USD exceeds the envelope's remaining {env.max_total_usd - env.spent_usd:.4f}"
        )
    return env


def approve(
    conn: sqlite3.Connection,
    run_id: str,
    actor: str,
    envelope_id: str | None = None,
    now: datetime | None = None,
) -> Run:
    if actor not in APPROVERS:
        raise ValueError(f"actor must be one of {APPROVERS}")
    now = now or utcnow()
    expired = False
    with tx(conn):
        run = get_run(conn, run_id)
        if run.state is not RunState.QUOTED:
            raise IllegalTransition(run.state, RunState.APPROVED)
        if run.quote_id is None:
            raise QuoteNotFound(f"run {run_id} is QUOTED without a stored quote")
        q = get_quote(conn, run.quote_id)
        if not now < q.expires_utc:
            run = transition(
                conn,
                run_id,
                RunState.EXPIRED,
                actor="system",
                reason=f"quote {q.id} expired at {iso(q.expires_utc)}",
            )
            expired = True
        else:
            env = get_envelope(conn, envelope_id) if envelope_id else None
            if actor in ("agent", "auto") or envelope_id:
                env = _check_envelope(env, q.price_usd, now, actor)
            if env is not None:
                conn.execute(
                    "UPDATE budget_envelopes SET spent_usd = spent_usd + ? WHERE id = ?",
                    (q.price_usd, env.id),
                )
            conn.execute(
                "UPDATE quotes SET envelope_id = ?, approved_by = ?, approved_utc = ? WHERE id = ?",
                (env.id if env else None, actor, iso(now), q.id),
            )
            run = transition(
                conn,
                run_id,
                RunState.APPROVED,
                actor=actor,
                reason=f"approved {q.price_usd:.4f} USD" + (f" from envelope {env.id}" if env else ""),
            )
    if expired:
        raise QuoteExpired(f"quote for run {run_id} expired; re-quote before approving")
    return run


def expire_if_due(conn: sqlite3.Connection, run_id: str, now: datetime, actor: str = "recovery") -> bool:
    """QUOTED past expiry -> EXPIRED. Returns True if it expired the run."""
    with tx(conn):
        run = get_run(conn, run_id)
        if run.state is not RunState.QUOTED or run.quote_id is None:
            return False
        q = get_quote(conn, run.quote_id)
        if now < q.expires_utc:
            return False
        transition(
            conn,
            run_id,
            RunState.EXPIRED,
            actor=actor,
            reason=f"quote {q.id} expired at {iso(q.expires_utc)}",
        )
        return True
