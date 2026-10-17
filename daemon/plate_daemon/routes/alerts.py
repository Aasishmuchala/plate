"""`GET /alerts`: what the daemon has to tell the artist, read from the alert files (router plan T85).

- `GET /alerts?since=<iso>&limit=100` lists the lines of every alert file, newest first, each as
  `{kind, level, message, at, source, engine_id?, project_id?}`. `since` keeps the lines stamped at or after
  that instant (an ISO-8601 time; 422 `bad_since` otherwise), `limit` is 1 to 1000 (default 100).
- The files: those of `Pipeline.alert_paths()` (`<home>/alerts.jsonl` and the nightly's `<home>/logs/alerts.jsonl`,
  engine suspensions, price changes, nightly jobs) and the `alerts.jsonl` in each project folder (the budget
  scan, `tools.alerts`). All share one line shape, so one reader (`tools.alerts.read_alerts`: every line,
  whatever its kind, a torn last line skipped) serves them; a file that is not there is simply empty.
- `level` is `error | warning | info`. Engine kinds are fixed: `engine_suspended`, `canary_drift` and
  `retired_pin` are `error`; `price_change`, `requote`, `schema_change` and `unit_mismatch` are `warning`.
  Every other kind (the budget kinds, the nightly job kinds) keeps the `severity` its line was written with
  (`info`, `warn`, `critical`; the UI reads `warn` as a warning and `critical` as an error); a line with none is `info`.
- `source` is `engine`, `budget` or `nightly`, from the kind. `engine_id` is present when the line names one;
  `project_id` when the line (`data.project_id`) or the project folder it was read from names one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..timeutil import parse_iso
from ..tools.alerts import ALERTS_JSONL, read_alerts
from ..tools.alerts import KINDS as BUDGET_KINDS
from . import API, ApiError, Ctx, ctx, require_token

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

log = logging.getLogger("plate_daemon")

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
# scheduler.NIGHTLY_ALERT_KINDS, spelled out here: this module must import even when the scheduler does not
# (api.default_nightly imports it lazily for the same reason); a test holds the two equal.
NIGHTLY_KINDS = frozenset({"job_error", "contract_test_failed", "reconcile_failed", "backup_failed"})
ERROR_KINDS = frozenset({"engine_suspended", "canary_drift", "retired_pin"})
WARNING_KINDS = frozenset({"price_change", "requote", "schema_change", "unit_mismatch"})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def level_of(rec: dict[str, Any]) -> str:
    """`error | warning | info` for one alert line (see the module docstring)."""
    kind = rec.get("kind")
    if kind in ERROR_KINDS:
        return "error"
    if kind in WARNING_KINDS:
        return "warning"
    severity = rec.get("severity")
    return severity if isinstance(severity, str) and severity else "info"


def source_of(kind: Any) -> str:
    if kind in BUDGET_KINDS:
        return "budget"
    if kind in NIGHTLY_KINDS:
        return "nightly"
    return "engine"


def _at(rec: dict[str, Any]) -> datetime | None:
    raw = rec.get("at")
    if not isinstance(raw, str):
        return None
    try:
        return parse_iso(raw)
    except ValueError:
        return None


def alert_files(c: Ctx) -> Iterator[tuple[Path, str | None]]:
    """Every alert file once, with the project it belongs to (None for the app-wide ones)."""
    seen: set[Path] = set()

    def fresh(path: Path) -> bool:
        key = path.absolute()
        if key in seen:
            return False
        seen.add(key)
        return True

    for path in c.pipeline.alert_paths():
        if fresh(path):
            yield path, None
    roots: dict[str, Path] = {}
    for pid in c.index.project_ids():
        roots[pid] = c.pipeline.project_dir(pid)
    projects_dir = c.settings.projects_dir
    if projects_dir.is_dir():
        for child in sorted(projects_dir.iterdir()):
            if child.is_dir() and child.name not in roots:
                roots[child.name] = child
    for pid, root in sorted(roots.items()):
        path = root / ALERTS_JSONL
        if fresh(path):
            yield path, pid


def collect(c: Ctx, since: datetime | None = None) -> list[dict[str, Any]]:
    """All alert records (newest first), optionally only those stamped at or after `since`."""
    found: list[tuple[datetime, int, dict[str, Any]]] = []
    seq = 0
    for path, project_id in alert_files(c):
        try:
            lines = read_alerts(path)
        except OSError as e:  # an unreadable file hides its own alerts, not everyone's
            log.warning("alerts in %s not listed: %s", path, e)
            continue
        for rec in lines:
            seq += 1
            kind = rec.get("kind")
            if not isinstance(kind, str):
                continue
            at = _at(rec)
            if since is not None and (at is None or at < since):
                continue
            out: dict[str, Any] = {
                "kind": kind,
                "level": level_of(rec),
                "message": str(rec.get("message", "")),
                "at": rec.get("at") if isinstance(rec.get("at"), str) else "",
                "source": source_of(kind),
            }
            if rec.get("engine_id"):
                out["engine_id"] = rec["engine_id"]
            data = rec.get("data")
            pid = data.get("project_id") if isinstance(data, dict) else None
            pid = pid if isinstance(pid, str) and pid else project_id
            if pid:
                out["project_id"] = pid
            found.append((at or _EPOCH, seq, out))
    found.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [rec for _at_, _seq, rec in found]


@router.get("/alerts")
def list_alerts(
    request: Request,
    since: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[dict[str, Any]]:
    since_dt: datetime | None = None
    if since is not None:
        try:
            since_dt = parse_iso(since.strip())
        except ValueError:
            raise ApiError(
                422, {"error": "bad_since", "detail": f"since {since!r} is not an ISO-8601 time"}
            ) from None
    return collect(ctx(request), since_dt)[:limit]
