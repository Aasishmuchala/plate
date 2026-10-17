"""Provider and engine health for the router, and the alert log (`alerts.jsonl`).

New for the PLATE monorepo (router plan T27; BUILD_PLAN section 4 "Routing"). Two small pieces that the router,
the canaries, the retirement check and the schema diff all share, so they live apart from any one of them.

Health. `choose()` takes a mapping of health entries. An entry is a `HealthEntry` (status ok / degraded / down /
suspended) or the older `router.Health(up, latency_ms)`; `status_of` reads both. Keys are looked up most specific
first: "<engine_id>/<route_id>", then the route's provider. An engine-level key "<engine_id>" is an extra gate
that must also be ok when present: that is where a canary suspension lands. No provider or route entry at all
means down: no signal, no spend (the same rule as the seed `route()`). Only status "ok" routes; "degraded" is
shown to people but never routed to.

Alerts. `AlertLog(path)` appends one JSON object per line to `alerts.jsonl` (ASCII only, `\\n` line ends on every
OS, one write per line so a crash tears at most the last line, which readers skip). Kinds are a closed set:
schema_change, price_change, requote, unit_mismatch, retired_pin, canary_drift, engine_suspended,
engine_resumed. `suspended_engines()` folds engine_suspended / engine_resumed in file order, so the log alone
says which engines the router must skip. Nothing here touches the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

Status = Literal["ok", "degraded", "down", "suspended"]
STATUSES: tuple[str, ...] = ("ok", "degraded", "down", "suspended")

ALERT_KINDS = frozenset(
    {
        "schema_change",
        "price_change",
        "requote",
        "unit_mismatch",
        "retired_pin",
        "canary_drift",
        "engine_suspended",
        "engine_resumed",
    }
)
SEVERITIES: tuple[str, ...] = ("info", "warn", "critical")
ALERTS_JSONL = "alerts.jsonl"


# ---------------------------------------------------------------------------- health


@dataclass(frozen=True)
class HealthEntry:
    """One health reading. `since` is an ISO UTC string when known; `reason` says why it is not ok."""

    status: Status = "ok"
    latency_ms: float = 0.0
    reason: str = ""
    since: str | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown health status {self.status!r}; expected one of {STATUSES}")


def status_of(entry: Any) -> str:
    """The status string of a HealthEntry, a router.Health (up / down), a bare status string or a mapping."""
    if entry is None:
        return "down"
    if isinstance(entry, str):
        return entry if entry in STATUSES else "down"
    if isinstance(entry, Mapping):
        status = entry.get("status")
        if isinstance(status, str):
            return status if status in STATUSES else "down"
        return "ok" if entry.get("up") else "down"
    status = getattr(entry, "status", None)
    if isinstance(status, str):
        return status if status in STATUSES else "down"
    return "ok" if getattr(entry, "up", False) else "down"


def latency_of(entry: Any) -> float:
    if isinstance(entry, Mapping):
        value = entry.get("latency_ms", 0.0)
    else:
        value = getattr(entry, "latency_ms", 0.0)
    return float(value or 0.0)


def reason_of(entry: Any) -> str:
    if isinstance(entry, Mapping):
        return str(entry.get("reason", "") or "")
    return str(getattr(entry, "reason", "") or "")


def route_health(
    health: Mapping[str, Any], engine_id: str, route_id: str, provider: str
) -> tuple[str, float, str]:
    """(status, latency_ms, why) for one route.

    The reading comes from "<engine_id>/<route_id>" if present, else from the provider key; with neither the route
    is down. A non-ok engine-level entry ("<engine_id>") overrides an ok route reading (suspended engines).
    """
    engine_entry = health.get(engine_id)
    if engine_entry is not None and status_of(engine_entry) != "ok":
        reason = reason_of(engine_entry)
        return status_of(engine_entry), latency_of(engine_entry), reason or f"engine {engine_id} is not ok"
    for key in (f"{engine_id}/{route_id}", provider):
        entry = health.get(key)
        if entry is not None:
            status = status_of(entry)
            return (
                status,
                latency_of(entry),
                reason_of(entry) or ("" if status == "ok" else f"{key} is {status}"),
            )
    return "down", 0.0, f"no health signal for {provider!r} (no signal, no spend)"


class HealthBoard:
    """A small mutable health table; `as_mapping()` is what `choose()` takes."""

    def __init__(
        self, entries: Mapping[str, Any] | None = None, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._entries: dict[str, Any] = dict(entries or {})

    def _now(self) -> str:
        return _iso(self._clock())

    def set(self, key: str, status: Status, latency_ms: float = 0.0, reason: str = "") -> HealthEntry:
        entry = HealthEntry(status=status, latency_ms=latency_ms, reason=reason, since=self._now())
        self._entries[key] = entry
        return entry

    def mark_ok(self, key: str, latency_ms: float = 0.0) -> HealthEntry:
        return self.set(key, "ok", latency_ms)

    def mark_down(self, key: str, reason: str = "") -> HealthEntry:
        return self.set(key, "down", 0.0, reason)

    def suspend(self, engine_id: str, reason: str) -> HealthEntry:
        return self.set(engine_id, "suspended", 0.0, reason)

    def resume(self, engine_id: str) -> None:
        self._entries.pop(engine_id, None)

    def apply_alerts(self, alerts: AlertLog) -> HealthBoard:
        """Suspend every engine the alert log says is suspended (and not resumed since)."""
        for engine_id, reason in alerts.suspended().items():
            self.suspend(engine_id, reason)
        return self

    def as_mapping(self) -> dict[str, Any]:
        return dict(self._entries)


# ---------------------------------------------------------------------------- alerts


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Alert:
    kind: str
    message: str
    severity: str = "warn"
    engine_id: str | None = None
    route_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    at: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ALERT_KINDS:
            raise ValueError(f"unknown alert kind {self.kind!r}; expected one of {sorted(ALERT_KINDS)}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}; expected one of {SEVERITIES}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "at": self.at,
            "kind": self.kind,
            "severity": self.severity,
            "engine_id": self.engine_id,
            "route_id": self.route_id,
            "message": self.message,
            "data": dict(self.data),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Alert:
        return cls(
            kind=raw["kind"],
            message=str(raw.get("message", "")),
            severity=raw.get("severity", "warn"),
            engine_id=raw.get("engine_id"),
            route_id=raw.get("route_id"),
            data=dict(raw.get("data") or {}),
            at=str(raw.get("at", "")),
        )


class AlertLog:
    """Append-only `alerts.jsonl`. The clock is injectable so tests never depend on the wall time."""

    def __init__(self, path: str | Path, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC))

    def append(self, alert: Alert) -> Alert:
        stamped = alert if alert.at else replace(alert, at=_iso(self._clock()))
        line = json.dumps(stamped.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        return stamped

    def write(
        self,
        kind: str,
        message: str,
        *,
        severity: str = "warn",
        engine_id: str | None = None,
        route_id: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> Alert:
        return self.append(
            Alert(
                kind=kind,
                message=message,
                severity=severity,
                engine_id=engine_id,
                route_id=route_id,
                data=dict(data or {}),
            )
        )

    def read(self) -> list[Alert]:
        """Every alert in file order. A line that does not parse (a torn last write) is skipped."""
        if not self.path.exists():
            return []
        out: list[Alert] = []
        with open(self.path, encoding="utf-8", newline="") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(Alert.from_dict(json.loads(line)))
                except (ValueError, KeyError, TypeError):
                    continue
        return out

    def suspended(self) -> dict[str, str]:
        """{engine_id: reason} for engines whose latest suspension has not been followed by a resume."""
        state: dict[str, str] = {}
        for alert in self.read():
            if alert.engine_id is None:
                continue
            if alert.kind == "engine_suspended":
                state[alert.engine_id] = alert.message
            elif alert.kind == "engine_resumed":
                state.pop(alert.engine_id, None)
        return state

    def suspended_engines(self) -> set[str]:
        return set(self.suspended())

    def resume(self, engine_id: str, reason: str = "resumed by a person") -> Alert:
        return self.write("engine_resumed", reason, severity="info", engine_id=engine_id)


def as_alert_log(alerts: AlertLog | str | Path | None) -> AlertLog | None:
    """None stays None (return alerts, write nothing); a path becomes an AlertLog."""
    if alerts is None or isinstance(alerts, AlertLog):
        return alerts
    return AlertLog(alerts)


def kinds_of(alerts: Iterable[Alert]) -> list[str]:
    return [a.kind for a in alerts]
