"""One SQLite index per project under `<home>/index`, opened per request and found by id.

The index is a per-machine cache of the project folder (Decision 6): it lives in local app data, never in
the project folder (WAL is unsafe on SMB), and `rebuild_from_project` recreates it from the folder alone.
The API speaks in packet and run ids; `Index` maps an id to its project's index file (remembered after the
first hit, otherwise found by scanning `index/*.sqlite`), opens a fresh WAL connection for each unit of
work (`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, see `db.connect`) and migrates each
file once per process. Fresh connections keep the thread-pool routes, the background ingest and the
worker from ever sharing one `sqlite3.Connection` concurrently.

Opening an index never creates one by accident (Wave 12, T144): `connect` / `project` raise `UnknownProject`
when `index/<id>.sqlite` does not exist, and `api.create_app` answers that 404 `not_found`. Only the calls that
make a project real pass `create=True`: `set_project_root` (`POST /projects`, the CLI, `Pipeline.ensure_project`),
`rebuild_from_project`, and the packet-first registration in `POST /packets`. So a request naming a project
that does not exist leaves no `index/<id>.sqlite` behind, and `project_ids()` (every `index/*.sqlite` whose stem
is a whole project id) only counts real projects. A new index file gets `project_meta.index_epoch`, a ULID that
`rebuild_from_project` replaces: the SSE ids are `<epoch>.<seq>`, so a cursor from before a rebuild (whose seq
restarts at 1) is recognised as stale and replayed from 0 (`sse.Bus`, `routes/events.cursor`). An index made
before the epoch existed gets one on first read (`index_epoch`).

Rebuild drops every table in one transaction, re-applies the migrations and replays, in order:
`project.json` (root and custody into `project_meta`), `packets.json` (packets by reference), `assets/`
(one `assets` row per CAS object) and `runs.jsonl`. Ledger records fold per `run_id`: a record's `state`
(a RunState value) wins, else its `kind` maps through `KIND_STATES`; the run's state is the one of the record
with the highest `data.event_seq` (the index `run_events` seq of the transition, Decision 73), so two appends
that raced into the file out of order still fold to the state the live index had. Records without a seq, and
ties, go in file order, and a terminal state is never overwritten by a non-terminal one (terminal states have
no exits). A record repeating an earlier `(run_id, kind, state)` is folded once. Run fields are read from the
record or its `data`, in file order. A run whose packet is not registered, or that claims a provider state with
no `provider_job_id`, is skipped and reported. What the folder does not hold (quotes, envelopes, run and
packet event logs, packet blob hashes, the SSE event log) is not recovered: write it to the folder first.

The two human decisions that unlock export are in the folder, so the rebuild replays them into `run_events`
(`replay_ledger_notes`): a ledger `override` record becomes the `override` note and a `done` record with
`data.decision == "accepted"` the `accept` note, in the shapes `routes/runs.py` writes, so `run_view` shows
the same `override`, `accepted` and `exportable` as before the rebuild. Their `at` is the rebuild time
(`run_events` is append-only and `append_note` stamps the clock). Every stored png, jpg, tif or tiff then
gets its perceptual hash in `assets.phash` (drop-to-resolve works on a fresh machine); unreadable files
are skipped and counted, never reported as problems.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cas import CAS, hash_file
from .config import PROJECT_ID_RE, check_project_id, index_db_path, index_dir
from .db import connect, dumps, loads, migrate, tx
from .idempotency import ulid
from .machine import RunNotFound, append_note
from .projects import Project
from .states import PROVIDER_STATES, TERMINAL, RunState
from .timeutil import iso, utcnow

PACKET_COLUMNS = (
    "project_id",
    "shot_id",
    "kind",
    "tier",
    "packet_version",
    "scene_hash",
    "view_hash",
    "path",
    "manifest_blake3",
    "status",
    "sender_name",
    "sender_version",
    "received_utc",
    "ingested_utc",
    "error",
)
PACKET_REQUIRED = ("shot_id", "kind", "tier", "packet_version", "path", "manifest_blake3")

# Ledger kinds that move a run's state on their own. Any record may carry `state` instead (preferred).
KIND_STATES: dict[str, RunState] = {
    "created": RunState.DRAFT,
    "quoted": RunState.QUOTED,
    "approved": RunState.APPROVED,
    "queued": RunState.QUEUED,
    "submitted": RunState.SUBMITTED,
    "done": RunState.DONE,
    "gate_fail": RunState.GATE_FAIL,
    "failed": RunState.FAILED,
    "refused": RunState.REFUSED,
    "cancelled": RunState.CANCELLED,
    "expired": RunState.EXPIRED,
}
RUN_FIELDS = (
    "node_id",
    "packet_id",
    "priority",
    "inputs",
    "params",
    "route",
    "attempt",
    "idempotency_key",
    "provider_job_id",
    "result_key",
    "reason_code",
    "gate_report",
    "engine_id",
    "provider",
    "region",
)
QUOTE_KEYS = (
    "engine_id",
    "route_id",
    "custody_grade",
    "cost_usd_quoted",
    "price_usd",
    "eta_s",
    "expires_utc",
)
PRIORITY_NAMES = ("client_deadline", "interactive", "agent_batch", "calibration")
COUNTED_TABLES = ("assets", "packets", "runs", "events", "project_meta")
OVERRIDE_PREFIX = "override: "  # the reason prefix routes/runs.py puts on an override note
PHASH_EXTS = frozenset({"png", "jpg", "jpeg", "tif", "tiff"})
EPOCH_KEY = "index_epoch"  # project_meta key: this index file's generation, a ULID


class RebuildError(RuntimeError):
    pass


class UnknownProject(LookupError):
    """No index for this project (and the caller did not ask for one to be made); the API answers 404.

    `project_id` is None when the project could not even be named (an event for a run no index holds, a
    subscription with several projects indexed and none chosen); the message then says why."""

    def __init__(self, project_id: str | None = None, detail: str | None = None):
        self.project_id = project_id
        super().__init__(detail or f"project {project_id} is not indexed")


# ---------------------------------------------------------------------------- packets


def packet_entry(conn: sqlite3.Connection, packet_id: str) -> dict[str, Any]:
    """A packet's index row as a `packets.json` entry (`Project.register_packet`)."""
    row = conn.execute(
        f"SELECT {', '.join(PACKET_COLUMNS)} FROM packets WHERE packet_id = ?", (packet_id,)
    ).fetchone()
    if row is None:
        raise KeyError(packet_id)
    return {k: row[k] for k in PACKET_COLUMNS if row[k] is not None}


def apply_packet_entry(
    conn: sqlite3.Connection, project_id: str, packet_id: str, entry: dict[str, Any]
) -> None:
    missing = [k for k in PACKET_REQUIRED if not entry.get(k)]
    if missing:
        raise RebuildError(f"packets.json {packet_id}: missing {missing}")
    vals = {k: entry.get(k) for k in PACKET_COLUMNS}
    vals["project_id"] = project_id
    vals["status"] = entry.get("status") or "ingested"
    vals["received_utc"] = entry.get("received_utc") or iso(utcnow())
    cols = ("packet_id", *PACKET_COLUMNS)
    updates = ", ".join(f"{k} = excluded.{k}" for k in PACKET_COLUMNS)
    conn.execute(
        f"INSERT INTO packets ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(packet_id) DO UPDATE SET {updates}",
        (packet_id, *(vals[k] for k in PACKET_COLUMNS)),
    )


# ---------------------------------------------------------------------------- runs


def _state_of(rec: dict[str, Any], data: dict[str, Any]) -> RunState | None:
    raw = rec.get("state", data.get("state"))
    if raw is not None:
        try:
            return RunState(str(raw).upper())
        except ValueError:
            return None
    return KIND_STATES.get(str(rec.get("kind", "")).lower())


def _event_seq(rec: dict[str, Any], data: dict[str, Any]) -> int | None:
    """The `run_events` seq the record was written at (`data.event_seq`), None when it carries none."""
    raw = data.get("event_seq", rec.get("event_seq"))
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _supersedes(state: RunState, seq: int | None, held: RunState | None, held_seq: int | None) -> bool:
    """Whether a record's state replaces the one folded so far. A terminal state is never undone by a
    non-terminal one; between two seqs the higher wins; ties and records without a seq go in file order."""
    if held is None:
        return True
    if held in TERMINAL and state not in TERMINAL:
        return False
    if seq is not None and held_seq is not None:
        return seq >= held_seq
    return True


def fold_run_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Ledger records -> one dict per run_id with its state and latest fields.

    The state is the one of the record with the highest `data.event_seq` (ties and records without a seq: file
    order; a terminal state is never overwritten by a non-terminal one). Fields follow file order. A record
    that repeats an earlier `(run_id, kind, state)` is folded once (the race that appended `cancelled` twice)."""
    runs: dict[str, dict[str, Any]] = {}
    seqs: dict[str, int | None] = {}
    seen: set[tuple[str, str, str | None]] = set()
    for rec in records:
        rid = rec.get("run_id")
        if not isinstance(rid, str) or not rid:
            continue
        data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        state = _state_of(rec, data)
        kind = str(rec.get("kind", "")).lower()
        key = (rid, kind, state.value if state is not None else None)
        if key in seen:
            continue
        seen.add(key)
        at = rec.get("at") if isinstance(rec.get("at"), str) else iso(utcnow())
        run = runs.setdefault(rid, {"run_id": rid, "created_utc": at})
        for f in RUN_FIELDS:
            v = rec.get(f, data.get(f))
            if v is not None:
                run[f] = v
        if state is not None:
            seq = _event_seq(rec, data)
            if _supersedes(state, seq, run.get("state"), seqs.get(rid)):
                run["state"] = state
                seqs[rid] = seq if seq is not None else seqs.get(rid)
        if kind == "quoted":
            quote = data.get("quote") if isinstance(data.get("quote"), dict) else None
            run["quote_json"] = quote or {k: rec[k] for k in QUOTE_KEYS if k in rec}
        run["updated_utc"] = at
    return runs


def apply_run(conn: sqlite3.Connection, project_id: str, run: dict[str, Any]) -> None:
    """Insert one folded run into the index. Raises RebuildError when the folder cannot back it."""
    rid = run["run_id"]
    state = RunState(run.get("state", RunState.DRAFT))
    packet_id = run.get("packet_id")
    if (
        not packet_id
        or not conn.execute("SELECT 1 FROM packets WHERE packet_id = ?", (packet_id,)).fetchone()
    ):
        raise RebuildError(f"run {rid}: packet {packet_id!r} is not in packets.json")
    if state in PROVIDER_STATES and not run.get("provider_job_id"):
        raise RebuildError(f"run {rid}: state {state.value} without a provider_job_id")
    priority = run.get("priority", "interactive")
    if isinstance(priority, int) and 0 <= priority < len(PRIORITY_NAMES):
        priority = PRIORITY_NAMES[priority]
    if priority not in PRIORITY_NAMES:
        priority = "interactive"
    route = run.get("route")
    if not (isinstance(route, dict) and all(route.get(k) for k in ("engine_id", "provider", "region"))):
        route = {k: str(run.get(k) or "unknown") for k in ("engine_id", "provider", "region")}
    attempt = run.get("attempt")
    attempt = attempt if isinstance(attempt, int) and attempt >= 1 else 1
    conn.execute(
        """INSERT INTO runs (id, project_id, packet_id, state, priority, inputs, params, route, attempt,
                             idempotency_key, provider_job_id, result_key, gate_report, reason_code, node_id,
                             quote_json, created_utc, updated_utc)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rid,
            project_id,
            packet_id,
            state.value,
            priority,
            dumps(run.get("inputs") if isinstance(run.get("inputs"), dict) else {}),
            dumps(run.get("params") if isinstance(run.get("params"), dict) else {}),
            dumps(route),
            attempt,
            run.get("idempotency_key"),
            run.get("provider_job_id"),
            run.get("result_key"),
            dumps(run["gate_report"]) if isinstance(run.get("gate_report"), dict) else None,
            run.get("reason_code"),
            run.get("node_id"),
            dumps(run["quote_json"]) if run.get("quote_json") else None,
            run["created_utc"],
            run["updated_utc"],
        ),
    )


def _note_exists(conn: sqlite3.Connection, run_id: str, kind: str, reason: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM run_events WHERE run_id = ? AND kind = ? AND reason = ?", (run_id, kind, reason)
        ).fetchone()
        is not None
    )


def replay_ledger_notes(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Replay the human decisions in the ledger into `run_events`; returns (notes added, problems).

    `override` record -> note `override` (actor `by`, reason `override: <reason>`; a custody edit, which
    has `data.what == "custody"`, is not a run override); `done` record with `data.decision == "accepted"`
    -> note `accept` (reason: JSON of `scrub_log_id` and `decision`, as `accept_route` writes it). A note
    already in the log (same run, kind and reason) is not added again, so replaying twice, or over an
    index that kept its notes, changes nothing. Records for runs the index does not hold are skipped."""
    added = 0
    problems: list[str] = []
    for rec in records:
        rid = rec.get("run_id")
        if not isinstance(rid, str) or not rid:
            continue
        data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        kind = rec.get("kind")
        if kind == "override":
            if data.get("what") == "custody":
                continue
            note_kind = "override"
            reason = OVERRIDE_PREFIX + str(rec.get("reason") or data.get("reason") or "")
        elif kind == "done" and data.get("decision") == "accepted":
            scrub_log_id = data.get("scrub_log_id")  # None stays None: `find_scrub` decides what it unlocks
            note_kind = "accept"
            reason = json.dumps({"scrub_log_id": scrub_log_id, "decision": "accepted"}, sort_keys=True)
        else:
            continue
        actor = rec.get("by") if isinstance(rec.get("by"), str) and rec.get("by") else "human"
        if not conn.execute("SELECT 1 FROM runs WHERE id = ?", (rid,)).fetchone():
            continue
        if _note_exists(conn, rid, note_kind, reason):
            continue
        try:
            append_note(conn, rid, kind=note_kind, actor=actor, reason=reason)
        except (RunNotFound, ValueError, sqlite3.IntegrityError) as e:
            problems.append(f"run {rid}: {note_kind} note not replayed ({e})")
            continue
        added += 1
    return added, problems


# ---------------------------------------------------------------------------- the index


class Index:
    def __init__(self, home: Path):
        self.home = Path(home)
        self._migrated: set[str] = set()
        self._owner: dict[str, str] = {}
        self._lock = threading.Lock()

    def db_path(self, project_id: str) -> Path:
        return index_db_path(project_id, self.home)

    def exists(self, project_id: str) -> bool:
        return self.db_path(check_project_id(project_id)).is_file()

    def connect(self, project_id: str, *, create: bool = False) -> sqlite3.Connection:
        """A fresh connection to the project's index, migrated. Raises `UnknownProject` when the file does
        not exist and `create` is false: only the calls that make a project real create its index."""
        check_project_id(project_id)
        path = self.db_path(project_id)
        fresh = not path.is_file()
        if fresh and not create:
            raise UnknownProject(project_id)
        conn = connect(path)
        try:
            # A file deleted under a running daemon is new again: migrate it even if this process did before.
            if fresh or project_id not in self._migrated:
                migrate(conn)
                with self._lock:
                    self._migrated.add(project_id)
            if fresh:
                index_epoch(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def project(self, project_id: str, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect(project_id, create=create)
        try:
            yield conn
        finally:
            conn.close()

    def project_ids(self) -> list[str]:
        d = index_dir(self.home)
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.sqlite") if PROJECT_ID_RE.fullmatch(p.stem))

    def epoch(self, project_id: str) -> str:
        """This index file's generation (`project_meta.index_epoch`); a rebuild gives it a new one."""
        with self.project(project_id) as conn:
            return index_epoch(conn)

    def remember(self, obj_id: str, project_id: str) -> None:
        with self._lock:
            self._owner[obj_id] = project_id

    def _locate(self, obj_id: str, sql: str) -> str | None:
        with self._lock:
            hit = self._owner.get(obj_id)
        if hit is not None:
            return hit
        for pid in self.project_ids():
            with self.project(pid) as conn:
                if conn.execute(sql, (obj_id,)).fetchone():
                    self.remember(obj_id, pid)
                    return pid
        return None

    def locate_run(self, run_id: str) -> str | None:
        return self._locate(run_id, "SELECT 1 FROM runs WHERE id = ?")

    def locate_packet(self, packet_id: str) -> str | None:
        return self._locate(packet_id, "SELECT 1 FROM packets WHERE packet_id = ?")

    def locate_envelope(self, envelope_id: str) -> str | None:
        return self._locate(envelope_id, "SELECT 1 FROM budget_envelopes WHERE id = ?")

    # ------------------------------------------------------------------ project folder cache

    def record_asset(
        self,
        project_id: str,
        key: str,
        ext: str,
        nbytes: int,
        *,
        phash: str | None = None,
        added_utc: str | None = None,
    ) -> None:
        with self.project(project_id) as conn, tx(conn):
            _insert_asset(conn, key, ext, nbytes, phash, added_utc)

    def project_root(self, project_id: str) -> Path | None:
        """The folder registered for this project (by rebuild or `set_project_root`), or None."""
        try:
            with self.project(project_id) as conn:
                row = conn.execute("SELECT value FROM project_meta WHERE key = 'root'").fetchone()
        except UnknownProject:  # no index: the caller falls back to projects_dir/<id>
            return None
        return Path(loads(row["value"])) if row else None

    def set_project_root(self, project_id: str, root: Path, custody: dict[str, Any] | None = None) -> None:
        """Register the project's folder (and custody): this is what makes a project's index exist."""
        with self.project(project_id, create=True) as conn, tx(conn):
            _set_meta(conn, "root", str(Path(root).absolute()))
            if custody is not None:
                _set_meta(conn, "custody", custody)

    def counts(self, project_id: str) -> dict[str, int]:
        with self.project(project_id) as conn:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNTED_TABLES}

    def rebuild_from_project(self, project_dir: Path | str, *, verify: bool = False) -> dict[str, Any]:
        """Drop this project's index and replay the folder (creating the index when there is none). Returns
        counts and the problems met. `verify=True` re-hashes every asset and skips (and reports) objects whose
        bytes do not match. The index gets a new `index_epoch`: its event seqs restart, and SSE cursors from
        before are recognised as stale. Take `projects.Lock(project_dir)` first when a daemon may be writing
        the folder."""
        project = Project.load(project_dir)
        pid = project.project_id
        problems: list[str] = []
        stored_images: list[tuple[str, Path]] = []
        notes = 0
        path = self.db_path(pid)
        existed = path.is_file()
        committed = False
        conn = connect(path)
        try:
            # Foreign keys off for the drop (no table order to get right); the replay checks references
            # itself and foreign_key_check below must come back empty before anything commits.
            conn.execute("PRAGMA foreign_keys=OFF")
            with tx(conn):
                tables = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                ]
                for name in tables:
                    conn.execute(f'DROP TABLE IF EXISTS "{name}"')
                migrate(conn)
                _set_meta(conn, "root", str(project.root.absolute()))
                _set_meta(conn, "custody", project.custody)
                _set_meta(conn, "created_utc", project.doc.get("created_utc"))
                _set_meta(conn, EPOCH_KEY, ulid())
                for packet_id, entry in sorted(project.packets().items()):
                    try:
                        apply_packet_entry(conn, pid, packet_id, entry)
                    except RebuildError as e:
                        problems.append(str(e))
                for key, ext, path in CAS(project.assets_dir).iter_objects():
                    size = path.stat().st_size
                    if verify and hash_file(path)[0] != key:
                        problems.append(f"assets/{key[:2]}/{path.name}: bytes do not match the hash, skipped")
                        continue
                    _insert_asset(conn, key, ext, size, None, None, mtime=path.stat().st_mtime)
                    if ext.lower().lstrip(".") in PHASH_EXTS:
                        stored_images.append((key, path))
                records, ledger_problems = project.ledger_records()
                problems.extend(ledger_problems)
                for run in fold_run_records(records).values():
                    try:
                        apply_run(conn, pid, run)
                    except (RebuildError, ValueError, sqlite3.IntegrityError) as e:
                        problems.append(str(e))
                notes, note_problems = replay_ledger_notes(conn, records)
                problems.extend(note_problems)
                dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
                if dangling:
                    raise RebuildError(
                        f"rebuild left dangling references: {[tuple(r) for r in dangling][:5]}"
                    )
            committed = True
            conn.execute("PRAGMA foreign_keys=ON")
            # After the commit: decoding images is slow and must not hold the write lock.
            phashed, phash_skipped = _phash_assets(conn, pid, stored_images)
        finally:
            conn.close()
            if not committed and not existed:  # a rebuild that failed must not leave an empty index behind
                _discard_index(path)
        with self._lock:
            self._migrated.add(pid)
            self._owner = {k: v for k, v in self._owner.items() if v != pid}
        return {
            "project_id": pid,
            **self.counts(pid),
            "problems": problems,
            "notes_replayed": notes,
            "phashed": phashed,
            "phash_skipped": phash_skipped,
        }


def _phash_assets(
    conn: sqlite3.Connection, project_id: str, images: list[tuple[str, Path]]
) -> tuple[int, int]:
    """Fill `assets.phash` for stored images. Returns (hashed, skipped); a file that is not a readable
    image is skipped, not an error (a CAS object is whatever bytes were put there)."""
    if not images:
        return 0, 0
    try:
        from .phash import PhashIndex
    except ImportError:  # numpy or Pillow missing: the index is still good, drop-to-resolve is lineage-only
        return 0, len(images)
    ph = PhashIndex(conn, project_id)
    hashed = 0
    for key, path in images:
        try:
            ph.add_file(path, key)
        except Exception:  # PhashError, OSError, a decoder's own error: skip this file
            continue
        hashed += 1
    return hashed, len(images) - hashed


def _insert_asset(
    conn: sqlite3.Connection,
    key: str,
    ext: str,
    nbytes: int,
    phash: str | None,
    added_utc: str | None,
    *,
    mtime: float | None = None,
) -> None:
    if added_utc is None:
        added_utc = iso(datetime.fromtimestamp(mtime, UTC) if mtime is not None else utcnow())
    conn.execute(
        "INSERT OR IGNORE INTO assets (hash, ext, bytes, phash, added_utc) VALUES (?, ?, ?, ?, ?)",
        (key, ext, int(nbytes), phash, added_utc),
    )


def _discard_index(path: Path) -> None:
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            p.unlink(missing_ok=True)
        except OSError:  # Windows may hold the file a moment longer; an empty index is still only a cache
            pass


def index_epoch(conn: sqlite3.Connection) -> str:
    """The index's `project_meta.index_epoch`, set now (a ULID) when the file has none yet: an index made
    before epochs existed gets one on first read, and two connections racing to set it agree on one."""
    row = conn.execute("SELECT value FROM project_meta WHERE key = ?", (EPOCH_KEY,)).fetchone()
    if row is None:
        with tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO project_meta (key, value) VALUES (?, ?)",
                (EPOCH_KEY, json.dumps(ulid())),
            )
        row = conn.execute("SELECT value FROM project_meta WHERE key = ?", (EPOCH_KEY,)).fetchone()
    return str(loads(row["value"]))


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO project_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
    )
