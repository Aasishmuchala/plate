"""Budgets: who may approve spend without a human click, and the spend journal (doctrine 8).

`authorize(run, policy, envelope, last_human_interaction_utc)` is pure and returns a `Decision`
`{approve, by, reason, code, unattended, alert}`. An agent (requester `agent`, or any `agent_batch` run, or
an agent-owned envelope) must pass an envelope with room for the quoted price and is approved as
`agent:<envelope_id>`. Otherwise the request is `auto`: approved only strictly below
`per_run_auto_cap_usd`, and within the envelope when one is passed. Both stay within the user's
`per_user_daily_cap_usd` (spend reserved today, net of refunds). When no human has interacted for
`unattended_after_min` minutes (or never), the run is unattended: auto and agent spend since the last
human interaction is capped at `unattended_cap_usd`, and the decision carries an `alert` for the bus
(`publish_alert`). Humans approve through `quotes.approve` without asking `authorize`.

The spend journal lives in the run's append-only `run_events` (kinds `spend.reserve`, `spend.refund`,
`spend.settle`, reason = JSON `{"usd", "by", "envelope_id"}`), written by `machine.transition` in the same
transaction as the state change: reserved at APPROVED (the envelope is debited then), and at the terminal
state refunded when the provider never had the job (no provider id: FAILED before SUBMITTED, a cancel
while queued) or settled at the quoted price otherwise (DONE, GATE_FAIL, FAILED after submit, REFUSED,
CANCELLED at the provider); the ledger's reconciliation (T13) owns the billed truth after that. The index
is per project, so the daily total here is this project's; a caller that budgets across projects passes
its own `Spend`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import SPEND_REFUND, SPEND_RESERVE, SPEND_SETTLE, BudgetEnvelope, Run, spend_body
from .states import Priority
from .timeutil import iso, utcnow

OWNER_RE = re.compile(r"^(agent(:[A-Za-z0-9_.@-]+)?|user:[A-Za-z0-9_.@-]+)$")
EPS = 1e-9
ALERT_KIND = "alert"

Requester = Literal["auto", "agent"]
DecisionCode = Literal[
    "approved",
    "no_quote",
    "quote_expired",
    "envelope_required",
    "envelope_expired",
    "envelope_exceeded",
    "auto_cap",
    "daily_cap",
    "unattended_cap",
]


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True)

    per_run_auto_cap_usd: float = Field(default=2.0, ge=0)
    per_user_daily_cap_usd: float = Field(default=50.0, ge=0)
    unattended_cap_usd: float = Field(default=20.0, ge=0)
    unattended_after_min: float = Field(default=30, gt=0)


class Envelope(BaseModel):
    """An agent's (or a user's standing) budget. `max_per_run_usd` None means only `max_usd` bounds a run."""

    envelope_id: str
    owner: str
    max_usd: float = Field(ge=0)
    spent_usd: float = Field(default=0.0, ge=0)
    expires_utc: datetime
    max_per_run_usd: float | None = Field(default=None, ge=0)

    @field_validator("owner")
    @classmethod
    def _owner(cls, v: str) -> str:
        return check_owner(v)

    @property
    def remaining_usd(self) -> float:
        return max(self.max_usd - self.spent_usd, 0.0)

    @property
    def is_agent(self) -> bool:
        return self.owner == "agent" or self.owner.startswith("agent:")

    @classmethod
    def from_db(cls, env: BudgetEnvelope) -> Envelope:
        return cls(
            envelope_id=env.id,
            owner=env.owner,
            max_usd=env.max_total_usd,
            spent_usd=env.spent_usd,
            expires_utc=env.expires_utc,
            max_per_run_usd=env.max_per_run_usd,
        )


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    approve: bool
    by: str | None = None
    reason: str
    code: DecisionCode
    unattended: bool = False
    alert: str | None = None


@dataclass(frozen=True)
class Spend:
    """What the user has already committed: today (UTC day) and, for auto/agent, since the last human."""

    today_usd: float = 0.0
    since_human_usd: float = 0.0


def check_owner(owner: str) -> str:
    if not isinstance(owner, str) or not OWNER_RE.match(owner):
        raise ValueError(f"envelope owner must be 'agent', 'agent:<name>' or 'user:<name>', got {owner!r}")
    return owner


def idle_seconds(now: datetime, last_human_interaction_utc: datetime) -> int:
    return int((now - last_human_interaction_utc).total_seconds())


def is_unattended(now: datetime, last_human_interaction_utc: datetime | None, policy: Policy) -> bool:
    if last_human_interaction_utc is None:
        return True
    # Whole seconds, so a float minute count a hair under the threshold cannot straddle the boundary.
    return idle_seconds(now, last_human_interaction_utc) >= policy.unattended_after_min * 60


def envelope_problem(env: Envelope, price: float, now: datetime) -> tuple[DecisionCode, str] | None:
    if now >= env.expires_utc:
        return "envelope_expired", f"envelope {env.envelope_id} expired at {iso(env.expires_utc)}"
    if env.max_per_run_usd is not None and price > env.max_per_run_usd + EPS:
        return "envelope_exceeded", (
            f"price {price:.4f} USD exceeds envelope {env.envelope_id}'s per-run cap {env.max_per_run_usd:.4f}"
        )
    if price > env.remaining_usd + EPS:
        return "envelope_exceeded", (
            f"price {price:.4f} USD exceeds envelope {env.envelope_id}'s remaining {env.remaining_usd:.4f}"
        )
    return None


def authorize(
    run: Run,
    policy: Policy,
    envelope: Envelope | None,
    last_human_interaction_utc: datetime | None,
    *,
    now: datetime | None = None,
    spent: Spend | None = None,
    requester: Requester = "auto",
    price_usd: float | None = None,
) -> Decision:
    """May this quoted run be approved without a human click? `price_usd` overrides the run's quoted
    price (a best-of-N group approves the sum of its siblings)."""
    now = now or utcnow()
    spent = spent or Spend()
    unattended = is_unattended(now, last_human_interaction_utc, policy)
    alert = None
    if unattended:
        idle = (
            "no human interaction recorded"
            if last_human_interaction_utc is None
            else f"no human interaction for {idle_seconds(now, last_human_interaction_utc) / 60:.0f} min"
        )
        alert = (
            f"unattended spend: {idle}; auto and agent approvals capped at {policy.unattended_cap_usd:.2f} USD "
            f"({spent.since_human_usd:.2f} USD committed since)"
        )

    def deny(code: DecisionCode, reason: str) -> Decision:
        return Decision(approve=False, reason=reason, code=code, unattended=unattended, alert=alert)

    if run.quote is None:
        return deny("no_quote", f"run {run.id} has no quote: quote before spend")
    if run.quote.expired(now):
        return deny("quote_expired", f"quote {run.quote.id} expired at {iso(run.quote.expires_utc)}")
    price = run.quote.price_usd if price_usd is None else float(price_usd)
    agent = (
        requester == "agent"
        or run.priority is Priority.AGENT_BATCH
        or (envelope is not None and envelope.is_agent)
    )
    if agent:
        if envelope is None:
            return deny("envelope_required", "agents must pass a budget envelope")
        by = f"agent:{envelope.envelope_id}"
    else:
        if price >= policy.per_run_auto_cap_usd - EPS:
            return deny(
                "auto_cap",
                f"price {price:.4f} USD is not below the auto-approve cap "
                f"{policy.per_run_auto_cap_usd:.2f} USD: a human approves this one",
            )
        by = "auto"
    if envelope is not None:
        problem = envelope_problem(envelope, price, now)
        if problem is not None:
            return deny(*problem)
    if spent.today_usd + price > policy.per_user_daily_cap_usd + EPS:
        return deny(
            "daily_cap",
            f"{spent.today_usd:.2f} + {price:.2f} USD would pass the daily cap "
            f"{policy.per_user_daily_cap_usd:.2f} USD",
        )
    if unattended and spent.since_human_usd + price > policy.unattended_cap_usd + EPS:
        return deny(
            "unattended_cap",
            f"unattended: {spent.since_human_usd:.2f} + {price:.2f} USD would pass the unattended cap "
            f"{policy.unattended_cap_usd:.2f} USD; a human must look first",
        )
    return Decision(
        approve=True,
        by=by,
        reason=f"{by} approved {price:.4f} USD" + (" (unattended)" if unattended else ""),
        code="approved",
        unattended=unattended,
        alert=alert,
    )


# ---------------------------------------------------------------------------- the spend journal


def _note(conn: sqlite3.Connection, run: Run, kind: str, body: dict[str, Any], now: datetime) -> None:
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?", (run.id,)
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO run_events (run_id, seq, kind, from_state, to_state, actor, reason, at_utc)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run.id,
            seq,
            kind,
            run.state.value,
            run.state.value,
            "budget",
            json.dumps(body, sort_keys=True, separators=(",", ":")),
            iso(now),
        ),
    )


def reserve(conn: sqlite3.Connection, run: Run, by: str, now: datetime) -> float:
    """Reserve the quoted price at APPROVED (inside the caller's transaction). Returns the amount."""
    if run.quote is None:
        return 0.0  # a run moved by hand with no stored quote has nothing priced to reserve
    usd = float(run.quote.price_usd)
    env_id = run.quote.envelope_id
    if env_id:
        conn.execute("UPDATE budget_envelopes SET spent_usd = spent_usd + ? WHERE id = ?", (usd, env_id))
    _note(conn, run, SPEND_RESERVE, {"usd": usd, "by": by, "envelope_id": env_id}, now)
    return usd


def close_out(conn: sqlite3.Connection, run: Run, now: datetime) -> tuple[str, float] | None:
    """At a terminal state: refund a reservation the provider never saw, else settle it."""
    usd = run.reserved_usd
    if usd <= EPS:
        return None
    env_id = run.quote.envelope_id if run.quote else None
    if not run.provider_job_id:
        if env_id:
            conn.execute(
                "UPDATE budget_envelopes SET spent_usd = MAX(spent_usd - ?, 0) WHERE id = ?", (usd, env_id)
            )
        _note(conn, run, SPEND_REFUND, {"usd": usd, "envelope_id": env_id, "state": run.state.value}, now)
        return SPEND_REFUND, usd
    _note(conn, run, SPEND_SETTLE, {"usd": usd, "envelope_id": env_id, "state": run.state.value}, now)
    return SPEND_SETTLE, usd


def day_start(now: datetime) -> datetime:
    now = now.astimezone(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def committed_since(conn: sqlite3.Connection, since: datetime, *, unattended_only: bool = False) -> float:
    """Spend reserved at or after `since`, net of refunds (only auto and agent approvals if asked)."""
    rows = conn.execute(
        """SELECT run_id, kind, reason FROM run_events WHERE kind IN (?, ?) AND run_id IN
             (SELECT run_id FROM run_events WHERE kind = ? AND at_utc >= ?)""",
        (SPEND_RESERVE, SPEND_REFUND, SPEND_RESERVE, iso(since)),
    ).fetchall()
    net: dict[str, float] = {}
    keep: set[str] = set()
    for r in rows:
        body = spend_body(r["reason"])
        usd = float(body.get("usd", 0.0))
        if r["kind"] == SPEND_RESERVE:
            by = str(body.get("by") or "")
            if not unattended_only or by == "auto" or by.startswith("agent:"):
                keep.add(r["run_id"])
            net[r["run_id"]] = net.get(r["run_id"], 0.0) + usd
        else:
            net[r["run_id"]] = net.get(r["run_id"], 0.0) - usd
    return round(sum(max(v, 0.0) for k, v in net.items() if k in keep), 10)


def spend_snapshot(
    conn: sqlite3.Connection, now: datetime, last_human_interaction_utc: datetime | None
) -> Spend:
    start = day_start(now)
    since = last_human_interaction_utc if last_human_interaction_utc is not None else start
    return Spend(
        today_usd=committed_since(conn, start),
        since_human_usd=committed_since(conn, since, unattended_only=True),
    )


def publish_alert(bus: Any, project_id: str, run_id: str | None, decision: Decision) -> int | None:
    """Put an unattended-spend alert on the event bus (sse.Bus); returns its seq, or None."""
    if bus is None or not decision.alert:
        return None
    return bus.publish(
        run_id,
        {
            "kind": ALERT_KIND,
            "source": "budgets",
            "level": "warning",
            "message": decision.alert,
            "approved": decision.approve,
            "code": decision.code,
        },
        project_id=project_id,
    )


AlertSink = Callable[[Decision], None]
