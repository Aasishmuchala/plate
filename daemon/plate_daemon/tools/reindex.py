"""Rebuild the per-machine SQLite index from the project folder, and say whether it came out right.

The index is a cache (Decision 6): it lives in the local app-data folder, never in the project, and
`Index.rebuild_from_project` recreates it from `project.json`, `packets.json`, `assets/` and `runs.jsonl`.
`reindex(project_dir)` wraps that with what an operator needs after a disaster or a move to a new box:

* the project lock is taken first (a daemon with the project open holds it: `ProjectLocked` says who);
* an index file SQLite cannot open (damaged, half-copied) is set aside as `<name>.corrupt-<ts>` and
  the rebuild starts from nothing;
* the row counts before and after, and `lost`: tables the folder cannot restore (quotes, envelopes, run and
  packet event logs, the SSE event log), so nobody is surprised that they came back empty;
* a consistency report. For assets, packets and runs, the ids the folder holds are compared with the ids in
  the rebuilt index. Anything the folder holds that the index lacks (`missing`: a run whose packet is not in
  `packets.json`, a corrupt blob with `verify=True`), or the reverse (`extra`), and every problem the
  rebuild met, makes `ok` false. The ledger's hash chain is re-walked and reported too.
"""

from __future__ import annotations

import gc
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..cas import CAS
from ..config import app_home
from ..fsutil import replace_with_retry
from ..index import Index, fold_run_records
from ..ledger import Ledger
from ..projects import Lock, Project
from ..timeutil import stamp

TABLES = (
    "packets",
    "runs",
    "assets",
    "events",
    "project_meta",
    "quotes",
    "budget_envelopes",
    "run_events",
    "packet_events",
    "blobs",
)
FOLDER_TABLES = {"assets": "hash", "packets": "packet_id", "runs": "id"}
SIDECARS = ("-wal", "-shm")

__all__ = ["ReindexReport", "reindex"]


@dataclass
class ReindexReport:
    project_id: str
    root: str
    before: dict[str, int] | None
    after: dict[str, int]
    expected: dict[str, int]
    missing: dict[str, list[str]] = field(default_factory=dict)
    extra: dict[str, list[str]] = field(default_factory=dict)
    lost: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    ledger: dict[str, Any] = field(default_factory=dict)
    quarantined: str | None = None
    verified_assets: bool = False

    @property
    def ok(self) -> bool:
        return (
            not any(self.missing.values())
            and not any(self.extra.values())
            and not self.problems
            and bool(self.ledger.get("chain_ok", True))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_id": self.project_id,
            "root": self.root,
            "before": self.before,
            "after": self.after,
            "expected": self.expected,
            "missing": self.missing,
            "extra": self.extra,
            "lost": self.lost,
            "problems": self.problems,
            "ledger": self.ledger,
            "quarantined": self.quarantined,
            "verified_assets": self.verified_assets,
        }

    def summary_line(self) -> str:
        counts = ", ".join(f"{self.after.get(t, 0)} {t}" for t in FOLDER_TABLES)
        if self.ok:
            return f"reindex {self.project_id}: consistent ({counts})"
        n = sum(len(v) for v in self.missing.values()) + sum(len(v) for v in self.extra.values())
        return f"reindex {self.project_id}: INCONSISTENT ({counts}); {n} id mismatch(es), {len(self.problems)} problem(s)"


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in TABLES:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            continue  # a table this schema version does not have
    return out


def _quarantine(db: Path) -> Path:
    """Move an unreadable index (and its WAL sidecars) aside so the rebuild starts clean."""
    target = db.with_name(f"{db.name}.corrupt-{stamp()}")
    replace_with_retry(db, target)
    for suffix in SIDECARS:
        side = db.with_name(db.name + suffix)
        if side.exists():
            replace_with_retry(side, target.with_name(target.name + suffix))
    return target


def _counts_before(idx: Index, project_id: str) -> tuple[dict[str, int] | None, Path | None]:
    db = idx.db_path(project_id)
    if not db.is_file():
        return None, None
    counts: dict[str, int] | None = None
    try:
        with idx.project(project_id) as conn:
            counts = _table_counts(conn)
    except sqlite3.DatabaseError:
        pass
    if counts is not None:
        return counts, None
    # Outside the except block, so the half-open connection of the failed attempt is released before
    # the move: Windows will not rename a file something still has open.
    gc.collect()
    return None, _quarantine(db)


def _ids(idx: Index, project_id: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    with idx.project(project_id) as conn:
        for table, column in FOLDER_TABLES.items():
            out[table] = {r[0] for r in conn.execute(f"SELECT {column} FROM {table}")}
    return out


def reindex(
    project_dir: Path | str,
    home: Path | str | None = None,
    *,
    verify: bool = False,
    lock: Lock | None = None,
    lock_timeout_s: float = 0.0,
    index: Index | None = None,
) -> ReindexReport:
    """Drop and rebuild the index of the project in `project_dir` under `home` (default: the app home).
    `verify=True` also re-hashes every blob (slow on a big project). Pass a held `projects.Lock` as
    `lock=` when the caller already owns the project."""
    root = Path(project_dir)
    project = Project.load(root)
    idx = index if index is not None else Index(Path(home) if home is not None else app_home())
    if lock is not None and lock.held:
        return _reindex(idx, project, root, verify)
    with lock if lock is not None else Lock(root, lock_timeout_s):
        return _reindex(idx, project, root, verify)


def _reindex(idx: Index, project: Project, root: Path, verify: bool) -> ReindexReport:
    pid = project.project_id
    before, quarantined = _counts_before(idx, pid)
    result = idx.rebuild_from_project(root, verify=verify)
    with idx.project(pid) as conn:
        after = _table_counts(conn)

    records, _ = project.ledger_records()
    expected_ids = {
        "assets": {key for key, _ext, _p in CAS(project.assets_dir).iter_objects()},
        "packets": set(project.packets()),
        "runs": set(fold_run_records(records)),
    }
    indexed = _ids(idx, pid)
    missing = {t: sorted(expected_ids[t] - indexed[t]) for t in FOLDER_TABLES}
    extra = {t: sorted(indexed[t] - expected_ids[t]) for t in FOLDER_TABLES}

    lost: dict[str, int] = {}
    if before is not None:
        lost = {t: before[t] - after.get(t, 0) for t in before if before[t] > after.get(t, 0)}

    chain = Ledger(root).check()
    return ReindexReport(
        project_id=pid,
        root=str(root),
        before=before,
        after=after,
        expected={t: len(v) for t, v in expected_ids.items()},
        missing={t: v for t, v in missing.items() if v},
        extra={t: v for t, v in extra.items() if v},
        lost=lost,
        problems=list(result.get("problems", [])),
        ledger={
            "records": chain.count,
            "chain_ok": chain.ok,
            "first_bad_seq": chain.first_bad_seq,
            "torn_tail": chain.torn,
            "head_hash": chain.head_hash,
        },
        quarantined=str(quarantined) if quarantined else None,
        verified_assets=verify,
    )
