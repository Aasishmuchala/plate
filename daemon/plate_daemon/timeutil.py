"""UTC time helpers shared by the daemon's tables, models and worker.

Timestamps are stored in SQLite as ISO-8601 strings with a `Z` suffix and fixed microsecond width, so
they sort lexicographically. They are content, never file names (file names use `stamp()`, which has no
`:` because Windows forbids it). The worker and quote logic take an injectable clock returning an aware
UTC datetime so tests never sleep.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Aware or naive-UTC datetime -> `2026-10-17T09:41:07.123456Z`."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(s: str | None) -> datetime | None:
    if s is None:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def stamp(dt: datetime | None = None) -> str:
    """Windows-safe timestamp for file names: `20261017T094107Z`."""
    return (dt or utcnow()).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
