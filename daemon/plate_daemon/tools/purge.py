"""End-of-project purge and vendor-side deletion tracking (BUILD_PLAN decision 23).

**Purge.** `end_of_project(project_dir, keep)` deletes content-addressed blobs that nothing still points at,
and only blobs old enough (`older_than_days`, by file modification time: a blob's mtime is when it was
stored and survives a backup and restore). A blob is *referenced*, and kept, when its hash appears in:

* `project.json` (the graph) or `packets.json`: always;
* the ledger records of a run the graph names by run id: always;
* with `keep["masters"]` (default true): the ledger records of any run that was exported, overridden into
  delivery, or accepted (`data.decision == "accepted"`), including that run's inputs;
* with `keep["gate_reports"]` (default true): any `gates/*.json` report;
* with `keep["ledger"]` (default true): any ledger record at all. This is the plain reference count of
  `CAS.refcount` (graph plus runs): only true orphans go. Set it false at the end of a project to also
  reclaim the candidates nobody chose; the masters and the graph's picks stay;
* an in-flight run or in-CAS packet of the SQLite index, if a connection is passed as `index=` (those hold
  references the folder does not yet carry);
* `protect=`, hashes the caller names.

`runs.jsonl` itself is never touched: it is the audit trail and its hash chain. A chain that does not verify
refuses the purge (`PurgeError`) unless `allow_broken_ledger=True`, because references inside damaged
records cannot be seen. Deletion needs the project lock (pass a held `projects.Lock` as `lock=`, else one
is taken and `ProjectLocked` propagates when the daemon has the project open). `dry_run` (the default)
changes nothing and writes nothing. A real run writes `purge_<ts>.json` in the project folder: what was
deleted (hash, extension, bytes), what was kept and why, stale staging files removed, and the vendor-side
deletions still owed.

**Vendor-side deletions.** Every cloud run leaves files at the vendor. `vendor_deletions(ledger)` lists
runs whose job reached a vendor and whose deletion has not been ledgered (`kind: deleted_vendor_side`,
matched by run id or provider job id), with the provider and job id for the manual or API deletion;
`record_vendor_deletion` writes that record once it is done. Runs on a local route (custody grade C0, or
provider `local`) never left the box and are not listed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..cas import CAS
from ..fsutil import read_json, write_json_atomic
from ..ledger import Ledger
from ..projects import GATES_DIR, Lock, Project
from ..states import TERMINAL
from ..timeutil import iso, stamp, utcnow
from .common import hashes_in_obj, hashes_in_text, unlink_retry

KEEP_DEFAULT: Mapping[str, bool] = {"masters": True, "gate_reports": True, "ledger": True}
MASTER_KINDS = frozenset({"export", "override"})
LOCAL_PROVIDERS = frozenset({"local", "fake"})
LOCAL_GRADE = "C0"
DAY_S = 86400.0
TMP_DIR = ".tmp"
REASONS = ("graph", "packets", "masters", "gate_reports", "ledger", "index", "explicit", "young")

__all__ = [
    "KEEP_DEFAULT",
    "PendingDeletion",
    "PurgeError",
    "PurgeReport",
    "end_of_project",
    "gc",
    "index_refs",
    "record_vendor_deletion",
    "vendor_deletions",
]


class PurgeError(RuntimeError):
    pass


@dataclass
class PurgeReport:
    project_id: str
    dry_run: bool
    at: str
    older_than_days: float
    keep: dict[str, bool]
    objects_total: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[dict[str, Any]] = field(default_factory=list)
    kept: dict[str, int] = field(default_factory=dict)
    bytes_candidates: int = 0
    bytes_freed: int = 0
    tmp_removed: list[str] = field(default_factory=list)
    pending_vendor_deletions: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    report_path: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "ok": self.ok,
            "project_id": self.project_id,
            "dry_run": self.dry_run,
            "at": self.at,
            "older_than_days": self.older_than_days,
            "keep": self.keep,
            "objects_total": self.objects_total,
            "candidates": self.candidates,
            "deleted": self.deleted,
            "kept": self.kept,
            "bytes_candidates": self.bytes_candidates,
            "bytes_freed": self.bytes_freed,
            "tmp_removed": self.tmp_removed,
            "pending_vendor_deletions": self.pending_vendor_deletions,
            "errors": self.errors,
            "report_path": self.report_path,
        }


# ---------------------------------------------------------------------------- references


def _run_ids_named_in(text: str, run_ids: Iterable[str]) -> set[str]:
    return {rid for rid in run_ids if rid in text}


def _data(rec: Mapping[str, Any]) -> Mapping[str, Any]:
    d = rec.get("data")
    return d if isinstance(d, dict) else {}


def _ledger_refs(records: list[dict[str, Any]], graph_text: str) -> dict[str, set[str]]:
    """Hash references from the ledger, grouped by why they matter: `graph` (runs the graph names),
    `masters` (exported, overridden or accepted runs) and `ledger` (everything)."""
    by_run: dict[str, set[str]] = {}
    run_ids: set[str] = set()
    masters: set[str] = set()
    master_runs: set[str] = set()
    every: set[str] = set()
    for rec in records:
        body = {k: v for k, v in rec.items() if k not in ("hash", "prev_hash")}
        found = hashes_in_obj(body)
        every |= found
        rid = rec.get("run_id")
        if isinstance(rid, str) and rid:
            run_ids.add(rid)
            by_run.setdefault(rid, set()).update(found)
        is_master = rec.get("kind") in MASTER_KINDS or _data(rec).get("decision") == "accepted"
        if is_master:
            if isinstance(rid, str) and rid:
                master_runs.add(rid)
            else:
                masters |= found
    for rid in master_runs:
        masters |= by_run.get(rid, set())
    graph: set[str] = set()
    for rid in _run_ids_named_in(graph_text, run_ids):
        graph |= by_run.get(rid, set())
    return {"graph": graph, "masters": masters, "ledger": every}


def _gate_report_refs(project: Project) -> set[str]:
    out: set[str] = set()
    gates = project.root / GATES_DIR
    if gates.is_dir():
        for p in sorted(gates.rglob("*.json")):
            try:
                out |= hashes_in_text(p.read_text(encoding="utf-8-sig"))
            except OSError:
                continue
    return out


def index_refs(conn: sqlite3.Connection) -> set[str]:
    """Hashes only the SQLite index holds: payloads and results of runs still in flight, and packet blobs
    copied into the CAS (`blobs.in_cas`). Terminal runs are the ledger's business."""
    terminal = ", ".join(f"'{s.value}'" for s in sorted(TERMINAL, key=lambda s: s.value))
    out: set[str] = set()
    for row in conn.execute(
        f"""SELECT payload_key, result_key, inputs, params, normalized, gate_report, quote_json
            FROM runs WHERE state NOT IN ({terminal})"""
    ):
        for v in tuple(row):
            if isinstance(v, str):
                out |= hashes_in_text(v)
    for row in conn.execute("SELECT blake3 FROM blobs WHERE in_cas = 1"):
        if isinstance(row[0], str):
            out |= hashes_in_text(row[0])
    return out


# ---------------------------------------------------------------------------- purge


def _keep_flags(keep: Mapping[str, bool] | None) -> dict[str, bool]:
    flags = dict(KEEP_DEFAULT)
    if keep is not None:
        unknown = sorted(set(keep) - set(KEEP_DEFAULT))
        if unknown:
            raise PurgeError(f"unknown keep flag(s) {unknown}; expected {sorted(KEEP_DEFAULT)}")
        flags.update({k: bool(v) for k, v in keep.items()})
    return flags


def end_of_project(
    project_dir: Path | str,
    keep: Mapping[str, bool] | None = None,
    *,
    older_than_days: float = 30.0,
    dry_run: bool = True,
    now: datetime | None = None,
    lock: Lock | None = None,
    lock_timeout_s: float = 0.0,
    index: sqlite3.Connection | None = None,
    protect: Iterable[str] = (),
    allow_broken_ledger: bool = False,
) -> PurgeReport:
    """Delete (or, with `dry_run`, list) the unreferenced blobs older than `older_than_days`. See the
    module docstring for what counts as referenced and what `keep` changes."""
    flags = _keep_flags(keep)
    if older_than_days < 0:
        raise PurgeError("older_than_days cannot be negative")
    root = Path(project_dir)
    now = now or utcnow()
    if dry_run:
        return _purge(root, flags, older_than_days, True, now, index, protect, allow_broken_ledger)
    if lock is not None and lock.held:
        return _purge(root, flags, older_than_days, False, now, index, protect, allow_broken_ledger)
    with lock if lock is not None else Lock(root, lock_timeout_s):
        return _purge(root, flags, older_than_days, False, now, index, protect, allow_broken_ledger)


def gc(
    project_dir: Path | str,
    *,
    older_than_days: float = 0.0,
    dry_run: bool = True,
    now: datetime | None = None,
    lock: Lock | None = None,
    index: sqlite3.Connection | None = None,
) -> PurgeReport:
    """`plate gc`: only true orphans (nothing in the project folder names them), any age by default."""
    return end_of_project(
        project_dir,
        None,
        older_than_days=older_than_days,
        dry_run=dry_run,
        now=now,
        lock=lock,
        index=index,
    )


def _purge(
    root: Path,
    keep: dict[str, bool],
    older_than_days: float,
    dry_run: bool,
    now: datetime,
    index: sqlite3.Connection | None,
    protect: Iterable[str],
    allow_broken_ledger: bool,
) -> PurgeReport:
    project = Project.load(root)
    ledger = Ledger(root)
    check = ledger.check()
    if not check.ok and not allow_broken_ledger:
        raise PurgeError(
            f"the ledger's hash chain breaks at seq {check.first_bad_seq} ({check.reason}): references in "
            "damaged records cannot be seen, so nothing is purged. Restore the ledger, or pass "
            "allow_broken_ledger=True to purge on what can still be read."
        )
    records = ledger.records()

    graph_text = json.dumps(project.doc, ensure_ascii=False)
    refs = _ledger_refs(records, graph_text)
    protected: dict[str, set[str]] = {
        "graph": hashes_in_text(graph_text) | refs["graph"],
        "packets": hashes_in_obj(read_json(project.packets_json))
        if project.packets_json.is_file()
        else set(),
        "masters": refs["masters"] if keep["masters"] else set(),
        "gate_reports": _gate_report_refs(project) if keep["gate_reports"] else set(),
        "ledger": refs["ledger"] if keep["ledger"] else set(),
        "index": index_refs(index) if index is not None else set(),
        "explicit": {h for h in protect if isinstance(h, str)},
    }

    report = PurgeReport(
        project_id=project.project_id,
        dry_run=dry_run,
        at=iso(now),
        older_than_days=older_than_days,
        keep=keep,
        kept={r: 0 for r in REASONS},
    )
    cas = CAS(project.assets_dir)
    doomed: list[tuple[dict[str, Any], Path]] = []
    for key, ext, path in cas.iter_objects():
        report.objects_total += 1
        reason = next((r for r in REASONS[:-1] if key in protected[r]), None)
        try:
            st = path.stat()
        except OSError as e:
            report.errors.append(f"{path.name}: {e}")
            continue
        age_days = max((now.timestamp() - st.st_mtime) / DAY_S, 0.0)
        if reason is None and age_days < older_than_days:
            reason = "young"
        if reason is not None:
            report.kept[reason] += 1
            continue
        entry = {"hash": key, "ext": ext, "bytes": st.st_size, "age_days": round(age_days, 3)}
        report.candidates.append(entry)
        report.bytes_candidates += st.st_size
        doomed.append((entry, path))

    report.pending_vendor_deletions = [p.to_dict() for p in vendor_deletions(records)]
    stale = _stale_tmp(project, max(older_than_days, 1.0), now)

    if dry_run:
        return report

    for entry, path in doomed:
        try:
            unlink_retry(path)
        except OSError as e:
            report.errors.append(f"{path.name}: could not delete ({e})")
            continue
        report.deleted.append(entry)
        report.bytes_freed += entry["bytes"]
        _rmdir_if_empty(path.parent)
    for path in stale:
        try:
            unlink_retry(path)
            report.tmp_removed.append(path.name)
        except OSError as e:
            report.errors.append(f"{TMP_DIR}/{path.name}: could not delete ({e})")

    out = _report_path(root, stamp(now))
    report.report_path = str(out)
    write_json_atomic(out, report.to_dict(), indent=1)
    return report


def _report_path(root: Path, ts: str) -> Path:
    """`purge_<ts>.json`, or `purge_<ts>_2.json` and so on: a report is never overwritten."""
    out = root / f"purge_{ts}.json"
    n = 2
    while out.exists():
        out = root / f"purge_{ts}_{n}.json"
        n += 1
    return out


def _stale_tmp(project: Project, older_than_days: float, now: datetime) -> list[Path]:
    """CAS staging files (`assets/.tmp/*`) left by a crashed put, once they are old enough that no put
    can still be using them."""
    tmp = project.assets_dir / TMP_DIR
    out: list[Path] = []
    if tmp.is_dir():
        for p in sorted(tmp.iterdir()):
            try:
                if p.is_file() and (now.timestamp() - p.stat().st_mtime) / DAY_S >= older_than_days:
                    out.append(p)
            except OSError:
                continue
    return out


def _rmdir_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        pass  # not empty, or a reader holds it: both fine


# ---------------------------------------------------------------------------- vendor-side deletions


@dataclass(frozen=True)
class PendingDeletion:
    run_id: str
    provider_job_id: str
    provider: str | None = None
    engine_id: str | None = None
    route_id: str | None = None
    custody_grade: str | None = None
    submitted_utc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "provider_job_id": self.provider_job_id,
            "engine_id": self.engine_id,
            "route_id": self.route_id,
            "custody_grade": self.custody_grade,
            "submitted_utc": self.submitted_utc,
        }


def _provider_of(rec: Mapping[str, Any]) -> str | None:
    d = _data(rec)
    route = d.get("route") if isinstance(d.get("route"), dict) else {}
    quote = d.get("quote") if isinstance(d.get("quote"), dict) else {}
    for v in (route.get("provider"), quote.get("provider"), d.get("provider")):
        if isinstance(v, str) and v:
            return v
    return None


def vendor_deletions(
    ledger: Ledger | Iterable[dict[str, Any]], *, include_local: bool = False
) -> list[PendingDeletion]:
    """One entry per (run, provider job) that reached a vendor and has no `deleted_vendor_side` record.

    A deletion record names the run (clearing all of that run's jobs), the job id (clearing that job), or
    both. Local routes (custody grade C0, provider `local` or `fake`) are skipped unless `include_local`."""
    records = ledger.records() if isinstance(ledger, Ledger) else list(ledger)
    cleared_runs: set[str] = set()
    cleared_jobs: set[str] = set()
    runs: dict[str, dict[str, Any]] = {}
    for rec in records:
        rid = rec.get("run_id") if isinstance(rec.get("run_id"), str) else None
        job = rec.get("provider_job_id") if isinstance(rec.get("provider_job_id"), str) else None
        if rec.get("kind") == "deleted_vendor_side":
            if job:
                cleared_jobs.add(job)
            if rid and not job:
                cleared_runs.add(rid)
            continue
        if not rid:
            continue
        info = runs.setdefault(
            rid,
            {"jobs": [], "provider": None, "engine_id": None, "route_id": None, "grade": None, "at": None},
        )
        if job and job not in info["jobs"]:
            info["jobs"].append(job)
        info["provider"] = info["provider"] or _provider_of(rec)
        for src, dst in (("engine_id", "engine_id"), ("route_id", "route_id"), ("custody_grade", "grade")):
            v = rec.get(src)
            if isinstance(v, str) and v:
                info[dst] = v
        if rec.get("kind") == "submitted" and info["at"] is None and isinstance(rec.get("at"), str):
            info["at"] = rec["at"]

    out: list[PendingDeletion] = []
    for rid, info in runs.items():
        if rid in cleared_runs or not info["jobs"]:
            continue
        provider = info["provider"] or info["route_id"]
        local = info["grade"] == LOCAL_GRADE or (provider or "").lower() in LOCAL_PROVIDERS
        if local and not include_local:
            continue
        out.extend(
            PendingDeletion(
                run_id=rid,
                provider_job_id=job,
                provider=provider,
                engine_id=info["engine_id"],
                route_id=info["route_id"],
                custody_grade=info["grade"],
                submitted_utc=info["at"],
            )
            for job in info["jobs"]
            if job not in cleared_jobs
        )
    out.sort(key=lambda p: (p.submitted_utc or "", p.run_id, p.provider_job_id))
    return out


def record_vendor_deletion(
    ledger: Ledger,
    *,
    run_id: str,
    provider_job_id: str | None = None,
    by: str,
    provider: str | None = None,
    method: str = "manual",
    reason: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ledger that the vendor-side files of a run (or one job of it) were deleted. `method` is `manual`
    (a person used the vendor's console) or `api`; `data` may carry the vendor's confirmation reference."""
    detail: dict[str, Any] = {"method": method, **(data or {})}
    if provider:
        detail["provider"] = provider
    return ledger.append(
        kind="deleted_vendor_side",
        run_id=run_id,
        provider_job_id=provider_job_id,
        by=by,
        reason=reason,
        data=detail,
    )
