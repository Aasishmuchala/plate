"""Pydantic models for runs, quotes, budget envelopes and run events, plus row converters.

These are the shapes the API returns and the worker reasons about. JSON columns (inputs, params, route,
gate_report, normalized) are decoded here; timestamps are aware UTC datetimes. A quote expires 15 minutes
after it is made (doctrine 8: quote before spend). `Route` keeps unknown keys (custody, manifest version)
because they feed the idempotency key verbatim.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import loads
from .states import RunState
from .timeutil import parse_iso

QUOTE_TTL = timedelta(minutes=15)

Priority = Literal["client_deadline", "interactive", "agent_batch", "calibration"]
CustodyGrade = Literal["C0", "C1", "C2", "D"]
Actor = Literal["human", "auto", "agent"]


class Route(BaseModel):
    model_config = ConfigDict(extra="allow")

    engine_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    region: str = Field(min_length=1)


class Run(BaseModel):
    id: str
    project_id: str
    packet_id: str
    state: RunState
    priority: Priority
    inputs: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    route: Route
    attempt: int = Field(ge=1)
    idempotency_key: str | None = None
    provider_job_id: str | None = None
    quote_id: str | None = None
    result_key: str | None = None
    gate_report: dict[str, Any] | None = None
    reason_code: str | None = None
    created_utc: datetime
    updated_utc: datetime
    # Worker bookkeeping (not in the plan's field list; exposed for audit).
    payload_key: str | None = None
    submit_started_utc: datetime | None = None
    submitted_utc: datetime | None = None
    result_url: str | None = None
    normalized: dict[str, Any] | None = None

    @property
    def route_dict(self) -> dict[str, Any]:
        return self.route.model_dump()

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Run:
        d = dict(row)
        for k in ("inputs", "params", "route", "gate_report", "normalized"):
            d[k] = loads(d[k])
        for k in ("created_utc", "updated_utc", "submit_started_utc", "submitted_utc"):
            d[k] = parse_iso(d[k])
        return cls(**d)


class Quote(BaseModel):
    id: str
    run_id: str
    provider: str
    model: str
    region: str
    custody_grade: CustodyGrade
    price_usd: float = Field(ge=0)
    eta_s: float = Field(ge=0)
    created_utc: datetime
    expires_utc: datetime
    envelope_id: str | None = None
    approved_by: str | None = None
    approved_utc: datetime | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Quote:
        d = dict(row)
        for k in ("created_utc", "expires_utc", "approved_utc"):
            d[k] = parse_iso(d[k])
        return cls(**d)


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
    priority: Priority = "interactive"
    inputs: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    route: Route
    attempt: int = Field(default=1, ge=1)

    @field_validator("project_id")
    @classmethod
    def _slug(cls, v: str) -> str:
        from .paths import check_project_id

        return check_project_id(v)
