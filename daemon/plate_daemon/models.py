"""Pydantic models for runs, quotes, budget envelopes and run events, plus row converters.

These are the shapes the API returns and the worker reasons about. JSON columns (inputs, params, route,
gate_report, normalized, quote_json) are decoded here; timestamps are aware UTC datetimes. A quote expires
15 minutes after it is made (doctrine 8: quote before spend). `Route` keeps unknown keys (custody, manifest
version, route_id) because they describe the endpoint the idempotency key names.

`Run` is assembled by `machine.get_run` from three places: the `runs` row, the run's quote (`quote_json`
holds the full quote terms, the `quotes` row its approval) and the run's append-only `run_events`.
`history` is the transition events; `cost_usd_billed` and `approved_by` come from the spend journal
(`spend.*` events written by `budgets`, in the same transaction as the state change they belong to);
`sibling_group` (best-of-N) rides on the quote the siblings share. `priority` is a `Priority` (lower runs
first) that the index stores and JSON carries as its label.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationError,
    computed_field,
    field_validator,
)

from .db import loads
from .idempotency import new_id
from .states import ERROR_STATES, Priority, RunState
from .timeutil import parse_iso

QUOTE_TTL = timedelta(minutes=15)

CustodyGrade = Literal["C0", "C1", "C2", "D"]
Actor = Literal["human", "auto", "agent"]
APPROVED_BY_RE = re.compile(r"^(human|auto|agent:[A-Za-z0-9_.:-]+)$")

# The spend journal: run_events rows with these kinds carry `{"usd", "by", "envelope_id", "owner"}` JSON.
SPEND_RESERVE = "spend.reserve"
SPEND_REFUND = "spend.refund"
SPEND_SETTLE = "spend.settle"
SPEND_KINDS = (SPEND_RESERVE, SPEND_REFUND, SPEND_SETTLE)

PriorityField = Annotated[
    Priority,
    BeforeValidator(Priority.parse),
    PlainSerializer(lambda p: Priority(p).label, return_type=str, when_used="json"),
]


class Route(BaseModel):
    model_config = ConfigDict(extra="allow")

    engine_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    region: str = Field(min_length=1)


class Quote(BaseModel):
    """The terms a run is approved against. `siblings` > 1 when one quote covers a best-of-N group."""

    id: str = Field(default_factory=lambda: new_id("qt"))
    run_id: str | None = None
    provider: str
    model: str
    model_version: str = "unknown"
    region: str
    custody_grade: CustodyGrade
    price_usd: float = Field(ge=0)
    eta_s: float = Field(ge=0)
    created_utc: datetime
    expires_utc: datetime
    size: dict[str, Any] | None = None
    route_id: str | None = None
    price_status: str = "unconfirmed"
    sibling_group: str | None = None
    sibling_index: int | None = Field(default=None, ge=0)
    siblings: int = Field(default=1, ge=1)
    envelope_id: str | None = None
    approved_by: str | None = None
    approved_utc: datetime | None = None

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_utc

    def terms_json(self) -> dict[str, Any]:
        """What `runs.quote_json` stores: the terms, not the approval (that lives on the quotes row)."""
        return self.model_dump(mode="json", exclude={"envelope_id", "approved_by", "approved_utc"})

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any], terms: dict[str, Any] | None = None) -> Quote:
        d = dict(terms or {})
        d.update({k: v for k, v in dict(row).items() if k != "quote_json"})
        for k in ("created_utc", "expires_utc", "approved_utc"):
            d[k] = parse_iso(d[k]) if isinstance(d.get(k), str) else d.get(k)
        return cls(**d)


class HistoryEntry(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)

    from_: RunState = Field(alias="from")
    to: RunState
    at: datetime
    reason: str | None = None
    actor: str | None = None


class Run(BaseModel):
    id: str
    project_id: str
    packet_id: str
    state: RunState
    priority: PriorityField
    inputs: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    route: Route
    attempt: int = Field(ge=1)
    node_id: str | None = None
    idempotency_key: str | None = None
    provider_job_id: str | None = None
    quote_id: str | None = None
    result_key: str | None = None
    gate_report: dict[str, Any] | None = None
    reason_code: str | None = None
    created_utc: datetime
    updated_utc: datetime
    quote: Quote | None = None
    approved_by: str | None = None
    history: list[HistoryEntry] = Field(default_factory=list)
    cost_usd_billed: float | None = None
    reserved_usd: float = 0.0
    error: str | None = None
    # Worker bookkeeping (not in the plan's field list; exposed for audit).
    payload_key: str | None = None
    submit_started_utc: datetime | None = None
    submitted_utc: datetime | None = None
    result_url: str | None = None
    normalized: dict[str, Any] | None = None

    @computed_field
    @property
    def run_id(self) -> str:
        return self.id

    @computed_field
    @property
    def quote_expires_utc(self) -> datetime | None:
        return self.quote.expires_utc if self.quote else None

    @computed_field
    @property
    def cost_usd_quoted(self) -> float | None:
        return self.quote.price_usd if self.quote else None

    @computed_field
    @property
    def sibling_group(self) -> str | None:
        return self.quote.sibling_group if self.quote else None

    @computed_field
    @property
    def sibling_index(self) -> int | None:
        return self.quote.sibling_index if self.quote and self.quote.sibling_group else None

    @computed_field
    @property
    def outputs(self) -> list[str]:
        outs = (self.normalized or {}).get("outputs")
        if isinstance(outs, list):
            return [str(o) for o in outs]
        return [self.result_key] if self.result_key else []

    @computed_field
    @property
    def sizemap(self) -> dict[str, Any] | None:
        sm = (self.normalized or {}).get("sizemap")
        return sm if isinstance(sm, dict) else None

    @computed_field
    @property
    def gate_report_path(self) -> str | None:
        p = (self.gate_report or {}).get("report_path")
        return str(p) if p else None

    @computed_field
    @property
    def gate_score(self) -> float | None:
        s = (self.gate_report or {}).get("score")
        return float(s) if isinstance(s, (int, float)) and not isinstance(s, bool) else None

    @property
    def route_dict(self) -> dict[str, Any]:
        return self.route.model_dump()

    @classmethod
    def from_row(
        cls,
        row: sqlite3.Row | dict[str, Any],
        *,
        quote: Quote | None = None,
        events: Iterable[sqlite3.Row | dict[str, Any]] = (),
    ) -> Run:
        d = dict(row)
        d.pop("run_id", None)  # generated alias column of `id`
        d.pop("quote_json", None)
        for k in ("inputs", "params", "route", "gate_report", "normalized"):
            d[k] = loads(d[k])
        for k in ("created_utc", "updated_utc", "submit_started_utc", "submitted_utc"):
            d[k] = parse_iso(d[k])
        history: list[HistoryEntry] = []
        billed: float | None = None
        reserved = 0.0
        approved_by = quote.approved_by if quote else None
        last_reason: str | None = None
        for ev in events:
            e = dict(ev)
            kind = e.get("kind", "transition")
            if kind == "transition":
                history.append(
                    HistoryEntry(
                        from_=e["from_state"],
                        to=e["to_state"],
                        at=parse_iso(e["at_utc"]),
                        reason=e.get("reason"),
                        actor=e.get("actor"),
                    )
                )
                last_reason = e.get("reason")
            elif kind in SPEND_KINDS:
                body = spend_body(e.get("reason"))
                usd = float(body.get("usd", 0.0))
                if kind == SPEND_RESERVE:
                    reserved += usd
                    approved_by = approved_by or body.get("by")
                elif kind == SPEND_REFUND:
                    reserved -= usd
                else:
                    billed = (billed or 0.0) + usd
                    reserved -= usd
        error = None
        state = RunState(d["state"])
        if state in ERROR_STATES:
            code = d.get("reason_code")
            error = ": ".join(x for x in (code, last_reason) if x) or state.value
        return cls(
            **d,
            quote=quote,
            approved_by=approved_by,
            history=history,
            cost_usd_billed=billed,
            reserved_usd=round(max(reserved, 0.0), 10),
            error=error,
        )


def spend_body(reason: str | None) -> dict[str, Any]:
    try:
        body = json.loads(reason or "{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def quote_from_terms(terms: Any) -> Quote | None:
    """A quote from `runs.quote_json` alone (an index rebuilt from the ledger), or None if it is not one."""
    if not isinstance(terms, dict):
        return None
    try:
        return Quote.from_row({}, terms)
    except (ValidationError, KeyError, TypeError, ValueError):
        return None


class BudgetEnvelope(BaseModel):
    id: str
    owner: str
    max_total_usd: float = Field(ge=0)
    max_per_run_usd: float = Field(ge=0)
    spent_usd: float = Field(default=0.0, ge=0)
    created_utc: datetime
    expires_utc: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> BudgetEnvelope:
        d = dict(row)
        for k in ("created_utc", "expires_utc"):
            d[k] = parse_iso(d[k])
        return cls(**d)


class RunEvent(BaseModel):
    run_id: str
    seq: int
    from_state: RunState
    to_state: RunState
    actor: str
    reason: str | None = None
    at_utc: datetime
    kind: str = "transition"
    event_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> RunEvent:
        d = dict(row)
        d["event_id"] = d.pop("id", None)
        d["at_utc"] = parse_iso(d["at_utc"])
        return cls(**d)


class RunCreate(BaseModel):
    """Body of POST /api/v0/runs."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    packet_id: str
    node_id: str | None = None
    priority: PriorityField = Priority.INTERACTIVE
    inputs: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    route: Route
    attempt: int = Field(default=1, ge=1)

    @field_validator("project_id")
    @classmethod
    def _slug(cls, v: str) -> str:
        from .config import check_project_id

        return check_project_id(v)
