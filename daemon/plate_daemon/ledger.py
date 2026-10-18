"""The run ledger: `<project>/runs.jsonl`, append-only and hash-chained (Decision 6, BUILD_PLAN decision 23).

One JSON object per line, ASCII only, `\\n` line ends on every OS. Each record carries `seq` (1-based, no
gaps), `prev_hash` and `hash`, where

    hash = blake3(prev_hash + canonical_json(record_without_hash))

and the first record's `prev_hash` is `"0" * 64`. `canonical_json` is sorted keys, compact separators,
`ensure_ascii`, no NaN, so the hash is the same on Windows and macOS. Editing, deleting or reordering a
middle line breaks the chain at that position and `verify()` names the `seq` that should hold it.

A hash chain cannot see its own tail being cut off. To catch that, keep `head()` somewhere else (the
review bundle, an export) and compare it later.

Writers hold the project lock (`project.lock`, the same file as `projects.Lock`) for the length of one
append, with a thread lock in front so threads of one process queue instead of timing out on the OS lock.
A caller that already holds the project lock through `projects.Lock` passes it as `lock=` and the ledger
does not take it a second time (the OS would refuse: it is one process locking one file twice).

A crash mid-append leaves a last line with no `\\n` that does not parse (an append is one `write`, so only
that line can be torn). Readers skip it and report it; the next `append` moves those bytes to
`runs.jsonl.torn` and truncates them away so a new record never glues onto them. A middle line that does
not parse, or a last line that ends in `\\n` but does not parse, is damage and fails `verify()`.

`kind` is a closed set (`KINDS`); later tasks extend it here (T38 added `verdict`, `llm` and `scrub`: a human
Ship/Fix/Render-it call, a Claude composer or judge call, and a logged scrub of a take). Fields outside the
shape go under `data`.

Reading is memoised per `Ledger` instance on the file's `(st_size, st_mtime_ns)`: `records()` re-reads only
when the file is not exactly what this instance last saw, and `append` extends the memo with the record it
sealed. Records that must not be lost while the file cannot be written (a read-only mount, the project lock
held for longer than `lock_timeout_s`) go to the `ledger_outbox` table of the project index with
`enqueue_outbox`; `drain_outbox` appends them, in order, once the ledger is writable again (Decision 73).
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from blake3 import blake3

from .db import tx
from .lock import ProjectLocked, project_lock
from .timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

RUNS_JSONL = "runs.jsonl"
TORN_SUFFIX = ".torn"
# Same name as projects.LOCK_NAME (a test pins them together); not imported so the ledger stays light.
LOCK_NAME = "project.lock"
VERSION = 1
GENESIS = "0" * 64
BOM = b"\xef\xbb\xbf"

KINDS = frozenset(
    {
        "quoted",
        "approved",
        "submitted",
        "fetched",
        "gated",
        "done",
        "failed",
        "refused",
        "cancelled",
        "expired",
        "billed",
        "deleted_vendor_side",
        "human_minutes",
        "export",
        "override",
        "verdict",
        "llm",
        "scrub",
    }
)

# Field order on disk (the hash does not depend on it: canonical JSON sorts keys).
FIELD_ORDER = (
    "v",
    "seq",
    "prev_hash",
    "hash",
    "at",
    "kind",
    "run_id",
    "node_id",
    "engine_id",
    "route_id",
    "custody_grade",
    "cost_usd_quoted",
    "cost_usd_billed",
    "provider_job_id",
    "opaque_id",
    "gate_report",
    "by",
    "reason",
    "data",
)
RESERVED = frozenset({"v", "seq", "prev_hash", "hash"})
STR_FIELDS = (
    "run_id",
    "node_id",
    "engine_id",
    "route_id",
    "custody_grade",
    "provider_job_id",
    "opaque_id",
    "by",
    "reason",
)
DECISIONS = ("accepted", "rerolled")
# A run with one of these records, or a provider job id, was submitted even if its `submitted` record is
# missing (Decision 73).
SUBMITTED_EVIDENCE_KINDS = frozenset({"fetched", "gated", "done"})
# Engines that never call a vendor: an import, a carried-over layer, a numeric or plugin node, a local
# recomposite. Their `done` records carry a made-up job id and cost nothing, so the evidence rule above
# does not apply to them; only an explicit `submitted` record would count (none is ever written).
LOCAL_ENGINE_PREFIXES = ("import:", "carry-over", "plate-numeric", "plate-plugin", "local-recomposite")
HEX = frozenset("0123456789abcdef")


class LedgerError(ValueError):
    """A record that does not fit the shape, or a ledger that cannot be written safely."""


# ---------------------------------------------------------------------------- hashing


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def seal(prev_hash: str, body: Mapping[str, Any]) -> str:
    """The `hash` of a record: `body` is the record without its `hash` field."""
    return blake3((prev_hash + canonical_json(body)).encode("utf-8")).hexdigest()


def _is_hash(h: Any) -> bool:
    return isinstance(h, str) and len(h) == 64 and set(h) <= HEX


def _is_chained(rec: Mapping[str, Any]) -> bool:
    seq = rec.get("seq")
    return isinstance(seq, int) and not isinstance(seq, bool) and _is_hash(rec.get("hash"))


def _check_record(rec: Mapping[str, Any], seq: int, prev_hash: str) -> str | None:
    """Why `rec` is not the record that belongs at `seq` after `prev_hash`; None when it is."""
    if rec.get("v") != VERSION:
        return f"v is {rec.get('v')!r}, not {VERSION}"
    if rec.get("seq") != seq or isinstance(rec.get("seq"), bool):
        return f"seq is {rec.get('seq')!r}, expected {seq}"
    if rec.get("prev_hash") != prev_hash:
        return "prev_hash does not match the previous record"
    if not _is_hash(rec.get("hash")):
        return "hash is missing or malformed"
    try:
        want = seal(prev_hash, {k: v for k, v in rec.items() if k != "hash"})
    except (TypeError, ValueError) as e:
        return f"not canonical JSON ({e})"
    if want != rec["hash"]:
        return "hash does not match the record's content"
    return None


# ---------------------------------------------------------------------------- reading


@dataclass
class _Line:
    no: int  # 1-based line number in the file
    start: int  # byte offset of the line's first byte
    end: int  # byte offset after its "\n" (or the end of the file)
    terminated: bool
    rec: dict[str, Any] | None
    error: str | None = None

    @property
    def torn(self) -> bool:
        return self.rec is None and not self.terminated


def _scan(data: bytes) -> list[_Line]:
    """Every non-blank line of the file, parsed. Blank lines carry no information and are ignored."""
    lines: list[_Line] = []
    pos = len(BOM) if data.startswith(BOM) else 0
    no = 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        raw, end, terminated = (data[pos:], len(data), False) if nl < 0 else (data[pos:nl], nl + 1, True)
        no += 1
        if raw.strip():
            rec: dict[str, Any] | None = None
            error: str | None = None
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                error = "unreadable line"
            else:
                if isinstance(obj, dict):
                    rec = obj
                else:
                    error = "not a JSON object"
            lines.append(_Line(no, pos, end, terminated, rec, error))
        pos = end
    return lines


def _read_bytes(path: Path) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return b""


@dataclass
class Verification:
    ok: bool
    first_bad_seq: int | None  # the seq that should hold the first bad record (1-based)
    reason: str | None
    count: int  # records that verified before the first problem (all of them when ok)
    torn: bool  # the last line is a torn append: skipped, not a failure
    head_hash: str  # hash of the last verified record (GENESIS when none)
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- the ledger


@dataclass
class _Tail:
    size: int
    mtime_ns: int
    seq: int
    hash: str


@dataclass
class _Memo:
    """What `read()` returned for the file as it was at `(size, mtime_ns)`."""

    size: int
    mtime_ns: int
    records: list[dict[str, Any]]
    problems: list[str]


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _jsonable(value: Any, what: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as e:
        raise LedgerError(f"{what} is not plain JSON: {e}") from e


def _cost(value: Any, name: str, *, allow_negative: bool) -> float:
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise LedgerError(f"{name} must be a finite number, got {value!r}")
    if value < 0 and not allow_negative:
        raise LedgerError(f"{name} cannot be negative")
    return float(value)


def normalise_record(record: Mapping[str, Any], clock: Callable[[], datetime] = utcnow) -> dict[str, Any]:
    """The caller-supplied fields of a record, checked and in plain JSON form (what `append` seals)."""
    unknown = sorted(set(record) - set(FIELD_ORDER))
    if unknown:
        raise LedgerError(f"unknown ledger field(s) {unknown}: put extra detail under 'data'")
    reserved = sorted(set(record) & RESERVED)
    if reserved:
        raise LedgerError(f"{reserved} are set by the ledger, not the caller")
    kind = record.get("kind")
    if kind not in KINDS:
        raise LedgerError(f"kind {kind!r} is not one of {sorted(KINDS)}")
    out: dict[str, Any] = {}
    at = record.get("at")
    if at is None:
        at = clock()
    if isinstance(at, str):
        try:
            at = parse_iso(at)
        except ValueError as e:
            raise LedgerError(f"at {record['at']!r} is not an ISO-8601 time") from e
    if not isinstance(at, datetime):
        raise LedgerError("at must be a datetime or an ISO-8601 string")
    out["at"] = iso(at)
    out["kind"] = kind
    for name in STR_FIELDS:
        v = record.get(name)
        if v is None:
            continue
        if not isinstance(v, str) or not v:
            raise LedgerError(f"{name} must be a non-empty string")
        out[name] = v
    if record.get("cost_usd_quoted") is not None:
        out["cost_usd_quoted"] = _cost(record["cost_usd_quoted"], "cost_usd_quoted", allow_negative=False)
    if record.get("cost_usd_billed") is not None:  # a refund or credit is a negative bill
        out["cost_usd_billed"] = _cost(record["cost_usd_billed"], "cost_usd_billed", allow_negative=True)
    gate_report = record.get("gate_report")
    if gate_report is not None:
        if not isinstance(gate_report, str | dict):
            raise LedgerError("gate_report must be a path or id string, or a summary object")
        out["gate_report"] = _jsonable(gate_report, "gate_report")
    data = record.get("data")
    if data is not None:
        if not isinstance(data, dict):
            raise LedgerError("data must be an object")
        out["data"] = _jsonable(data, "data")
    return out


class Ledger:
    """`Ledger(project_dir)`: the append-only, hash-chained `runs.jsonl` of one project folder."""

    def __init__(
        self,
        project_dir: Path | str,
        *,
        lock: Any | None = None,
        lock_timeout_s: float = 5.0,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.project_dir = Path(project_dir)
        self.path = self.project_dir / RUNS_JSONL
        self.lock_timeout_s = lock_timeout_s
        self._lock = lock  # a held projects.Lock (anything with `.held`), if the caller has one
        self._clock = clock
        self._tail: _Tail | None = None
        self._memo: _Memo | None = None
        self.repairs: list[str] = []  # torn tails this instance moved aside

    # ------------------------------------------------------------------ write side

    @contextmanager
    def _writer(self) -> Iterator[None]:
        """The project lock for one append. `project_lock` fails at once when the lock is taken, so wait
        here (the holder is another writer mid-append: milliseconds) up to `lock_timeout_s`."""
        if self._lock is not None and self._lock.held:
            yield
            return
        deadline = time.monotonic() + self.lock_timeout_s
        delay = 0.005
        with ExitStack() as stack:
            while True:
                try:
                    stack.enter_context(project_lock(self.project_dir, 0, name=LOCK_NAME))
                    break
                except ProjectLocked as e:
                    if time.monotonic() >= deadline:
                        if e.holder_pid == os.getpid():
                            raise LedgerError(
                                "the project lock is held by this process through another handle: "
                                "pass that projects.Lock as Ledger(..., lock=lock)"
                            ) from e
                        raise
                    time.sleep(delay)
                    delay = min(delay * 2, 0.1)
            yield

    def _normalise(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return normalise_record(record, self._clock)

    def _tail_state(self) -> tuple[_Tail, bool]:
        """(last chained record's seq and hash, whether the file lacks its final newline). Re-reads the
        file only when it is not exactly what this instance last wrote; moves a torn tail aside first."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._tail = _Tail(0, 0, 0, GENESIS)
            return self._tail, False
        if self._tail and (self._tail.size, self._tail.mtime_ns) == (st.st_size, st.st_mtime_ns):
            return self._tail, False
        data = _read_bytes(self.path)
        lines = _scan(data)
        if lines and lines[-1].torn:
            torn = lines[-1]
            keep = lines[-2].end if len(lines) > 1 else (len(BOM) if data.startswith(BOM) else 0)
            with open(self.path.with_name(self.path.name + TORN_SUFFIX), "ab") as fh:
                fh.write(data[torn.start :].rstrip(b"\r\n") + b"\n")
                fh.flush()
                os.fsync(fh.fileno())
            with open(self.path, "r+b") as fh:
                fh.truncate(keep)
                fh.flush()
                os.fsync(fh.fileno())
            self.repairs.append(f"{RUNS_JSONL}:{torn.no}: torn last line moved to {RUNS_JSONL}{TORN_SUFFIX}")
            data = data[:keep]
            lines.pop()
            st = self.path.stat()
        tail = _Tail(st.st_size, st.st_mtime_ns, 0, GENESIS)
        for ln in reversed(lines):
            if ln.rec is not None and _is_chained(ln.rec):
                tail.seq, tail.hash = ln.rec["seq"], ln.rec["hash"]
                break
        return tail, bool(data) and not data.endswith(b"\n")

    def append(self, record: Mapping[str, Any] | None = None, /, **fields: Any) -> dict[str, Any]:
        """Seal and append one record; returns it as written. `ledger.append({...})` or `append(kind=...)`."""
        body_in = {**(record or {}), **fields}
        norm = self._normalise(body_in)
        with _thread_lock(self.path), self._writer():
            memo_current = self._memo_matches_disk()
            repairs = len(self.repairs)
            tail, needs_newline = self._tail_state()
            seq = tail.seq + 1
            body = {"v": VERSION, "seq": seq, "prev_hash": tail.hash, **norm}
            ordered = {k: body[k] for k in FIELD_ORDER if k in body}
            sealed = seal(tail.hash, ordered)
            rec = {
                k: (sealed if k == "hash" else ordered[k]) for k in FIELD_ORDER if k == "hash" or k in ordered
            }
            line = json.dumps(rec, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            self.project_dir.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as fh:
                fh.write((b"\n" if needs_newline else b"") + line.encode("ascii") + b"\n")
                fh.flush()
                os.fsync(fh.fileno())
            st = self.path.stat()
            self._tail = _Tail(st.st_size, st.st_mtime_ns, seq, sealed)
            if memo_current and len(self.repairs) == repairs:  # nothing else changed: extend, do not re-read
                memo = self._memo or _Memo(0, 0, [], [])
                memo.records.append(json.loads(line))
                memo.size, memo.mtime_ns = st.st_size, st.st_mtime_ns
                self._memo = memo
            else:
                self._memo = None
            return rec

    # ------------------------------------------------------------------ read side

    def _memo_matches_disk(self) -> bool:
        """The memo describes the file exactly as it is now (a missing file and no memo count as empty)."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return self._memo is None
        return self._memo is not None and (self._memo.size, self._memo.mtime_ns) == (
            st.st_size,
            st.st_mtime_ns,
        )

    def _snapshot(self) -> _Memo:
        """The parsed file, re-read only when `(st_size, st_mtime_ns)` is not what the memo was taken at.
        The stat comes from the open handle before the read, so a file that grows meanwhile only makes
        the next call re-read; it can never pair old content with a new key."""
        with _thread_lock(self.path):
            try:
                with open(self.path, "rb") as fh:
                    st = os.fstat(fh.fileno())
                    memo = self._memo
                    if memo is not None and (memo.size, memo.mtime_ns) == (st.st_size, st.st_mtime_ns):
                        return memo
                    data = fh.read()
            except FileNotFoundError:
                self._memo = None
                return _Memo(0, 0, [], [])
            records: list[dict[str, Any]] = []
            problems: list[str] = []
            for ln in _scan(data):
                if ln.rec is not None:
                    records.append(ln.rec)
                elif ln.torn:
                    problems.append(f"{RUNS_JSONL}:{ln.no}: torn last line skipped")
                else:
                    problems.append(f"{RUNS_JSONL}:{ln.no}: {ln.error or 'unreadable line'} skipped")
            self._memo = _Memo(st.st_size, st.st_mtime_ns, records, problems)
            return self._memo

    def read(self) -> tuple[list[dict[str, Any]], list[str]]:
        """(records, problems). A torn last line is skipped and reported; so is any unreadable line.

        Memoised (see the module docstring). The list and each record's top level are copies; the nested
        `data` objects are shared with the memo, so treat them as read-only."""
        with _thread_lock(self.path):  # an `append` on another thread extends the memo's list in place
            memo = self._snapshot()
            return [dict(r) for r in memo.records], list(memo.problems)

    def records(self) -> list[dict[str, Any]]:
        return self.read()[0]

    def head(self) -> tuple[int, str]:
        """(seq, hash) of the last record: the value to store elsewhere to detect a cut-off tail."""
        for ln in reversed(_scan(_read_bytes(self.path))):
            if ln.rec is not None and _is_chained(ln.rec):
                return ln.rec["seq"], ln.rec["hash"]
        return 0, GENESIS

    def check(self) -> Verification:
        """Walk the chain from the first line. The first record that is not the one that belongs at its
        position ends the walk; the seq reported is that position, not what the record claims."""
        lines = _scan(_read_bytes(self.path))
        torn = bool(lines) and lines[-1].torn
        if torn:
            lines = lines[:-1]
        prev = GENESIS
        problems = [f"{RUNS_JSONL}: torn last line skipped"] if torn else []
        for i, ln in enumerate(lines):
            seq = i + 1
            why = f"line {ln.no}: {ln.error}" if ln.rec is None else _check_record(ln.rec, seq, prev)
            if why is not None:
                problems.append(f"{RUNS_JSONL}:{ln.no}: seq {seq}: {why}")
                return Verification(False, seq, why, i, torn, prev, problems)
            prev = ln.rec["hash"] if ln.rec else prev
        return Verification(True, None, None, len(lines), torn, prev, problems)

    def verify(self) -> tuple[bool, int | None]:
        """(ok, first_bad_seq). A torn last line is not a failure; `check()` reports it."""
        v = self.check()
        return v.ok, v.first_bad_seq

    def runs(self) -> dict[str, RunView]:
        return fold_runs(self.records())

    def summarize(self, since: datetime | str | None = None) -> dict[str, Any]:
        return summarize_records(self.records(), since)


# ---------------------------------------------------------------------------- folding records into runs


def _money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


def usd_out(d: Decimal | None) -> float:
    return float(round(d or Decimal(0), 6))


@dataclass
class RunView:
    """What the ledger says about one run_id, folded from all of its records."""

    run_id: str
    node_id: str | None = None
    engine_id: str | None = None
    route_id: str | None = None
    custody_grade: str | None = None
    provider_job_ids: list[str] = field(default_factory=list)
    kinds: set[str] = field(default_factory=set)
    first_at: dict[str, datetime] = field(default_factory=dict)  # kind -> time of its first record
    models: list[str] = field(default_factory=list)  # data.model / data.provider_model strings
    decision: str | None = None  # data.decision: accepted | rerolled (the last one wins)
    quoted_by_kind: dict[str, Decimal] = field(default_factory=dict)
    billed_sum: Decimal | None = None  # sum over `billed` records
    billed_other: Decimal | None = None  # last cost_usd_billed seen on any other kind

    @property
    def submitted(self) -> bool:
        """The run reached the vendor. A lost `submitted` record (the ledger was unwritable at that moment)
        must not erase the spend, so a provider job id or any later kind is evidence enough, except on an
        engine that never calls a vendor (`LOCAL_ENGINE_PREFIXES`)."""
        if "submitted" in self.kinds:
            return True
        if self.engine_id and self.engine_id.startswith(LOCAL_ENGINE_PREFIXES):
            return False
        return bool(self.provider_job_ids) or not self.kinds.isdisjoint(SUBMITTED_EVIDENCE_KINDS)

    @property
    def provider_job_id(self) -> str | None:
        return self.provider_job_ids[-1] if self.provider_job_ids else None

    @property
    def quoted_usd(self) -> Decimal | None:
        """The price the human approved; else the quote; else the last quote-like figure seen."""
        for kind in ("approved", "quoted"):
            if kind in self.quoted_by_kind:
                return self.quoted_by_kind[kind]
        return next(reversed(self.quoted_by_kind.values()), None) if self.quoted_by_kind else None

    @property
    def billed_usd(self) -> Decimal | None:
        return self.billed_sum if self.billed_sum is not None else self.billed_other


def fold_runs(records: list[dict[str, Any]]) -> dict[str, RunView]:
    runs: dict[str, RunView] = {}
    for rec in records:
        rid = rec.get("run_id")
        if not isinstance(rid, str) or not rid:
            continue
        rv = runs.setdefault(rid, RunView(rid))
        kind = rec.get("kind") if isinstance(rec.get("kind"), str) else ""
        for name in ("node_id", "engine_id", "route_id", "custody_grade"):
            v = rec.get(name)
            if isinstance(v, str) and v:
                setattr(rv, name, v)
        pj = rec.get("provider_job_id")
        if isinstance(pj, str) and pj and pj not in rv.provider_job_ids:
            rv.provider_job_ids.append(pj)
        rv.kinds.add(kind)
        t = _at(rec.get("at"))
        if t is not None:
            rv.first_at.setdefault(kind, t)
        q = _money(rec.get("cost_usd_quoted"))
        if q is not None:
            rv.quoted_by_kind.pop(kind, None)  # keep insertion order meaning "most recent kind last"
            rv.quoted_by_kind[kind] = q
        b = _money(rec.get("cost_usd_billed"))
        if b is not None:
            if kind == "billed":
                rv.billed_sum = (rv.billed_sum or Decimal(0)) + b
            else:
                rv.billed_other = b
        data = rec.get("data")
        if isinstance(data, dict):
            if data.get("decision") in DECISIONS:
                rv.decision = data["decision"]
            for key in ("model", "provider_model"):
                m = data.get(key)
                if isinstance(m, str) and m and m not in rv.models:
                    rv.models.append(m)
    return runs


def summarize_records(records: list[dict[str, Any]], since: datetime | str | None = None) -> dict[str, Any]:
    """Spend and outcome totals over the records with `at >= since`.

    A *generation* is a run that reached `submitted`; `spent_usd_quoted` is the approved price of those
    runs (a run cancelled or failed before it was submitted spent nothing). `spent_usd_billed` is the
    vendor-billed money (`billed` records; else the last cost_usd_billed a run carried). `accepted` and
    `rerolled` count runs whose last `data.decision` says so (T36/T38 write it)."""
    if isinstance(since, str):
        since = parse_iso(since)
    if since is not None:
        records = [r for r in records if (t := _at(r.get("at"))) is not None and t >= since]
    runs = fold_runs(records)
    by_engine: dict[str, dict[str, Any]] = {}
    by_custody: dict[str, dict[str, Any]] = {}

    def bucket(table: dict[str, dict[str, Any]], key: str | None) -> dict[str, Any]:
        return table.setdefault(
            key or "unknown",
            {
                "generations": 0,
                "spent_usd_quoted": Decimal(0),
                "spent_usd_billed": Decimal(0),
                "accepted": 0,
                "rerolled": 0,
            },
        )

    totals = {"quoted": Decimal(0), "billed": Decimal(0), "generations": 0, "accepted": 0, "rerolled": 0}
    for rv in runs.values():
        billed = rv.billed_usd or Decimal(0)
        if not (rv.submitted or billed or rv.decision in DECISIONS):
            continue  # quoted, approved, cancelled, expired: nothing was spent or decided
        be, bc = bucket(by_engine, rv.engine_id), bucket(by_custody, rv.custody_grade)
        totals["billed"] += billed
        for b in (be, bc):
            b["spent_usd_billed"] += billed
        if rv.submitted:
            quoted = rv.quoted_usd or Decimal(0)
            totals["quoted"] += quoted
            totals["generations"] += 1
            for b in (be, bc):
                b["generations"] += 1
                b["spent_usd_quoted"] += quoted
        if rv.decision in DECISIONS:
            totals[rv.decision] += 1
            for b in (be, bc):
                b[rv.decision] += 1
    for rec in records:  # bills that name no run (an unmatched vendor line) are still money
        if not isinstance(rec.get("run_id"), str) and rec.get("kind") == "billed":
            billed = _money(rec.get("cost_usd_billed")) or Decimal(0)
            totals["billed"] += billed
            eng = rec.get("engine_id") if isinstance(rec.get("engine_id"), str) else None
            cus = rec.get("custody_grade") if isinstance(rec.get("custody_grade"), str) else None
            bucket(by_engine, eng or "unattributed")["spent_usd_billed"] += billed
            bucket(by_custody, cus or "unattributed")["spent_usd_billed"] += billed

    def out(table: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            k: {
                **v,
                "spent_usd_quoted": usd_out(v["spent_usd_quoted"]),
                "spent_usd_billed": usd_out(v["spent_usd_billed"]),
            }
            for k, v in sorted(table.items())
        }

    return {
        "spent_usd_quoted": usd_out(totals["quoted"]),
        "spent_usd_billed": usd_out(totals["billed"]),
        "generations": totals["generations"],
        "accepted": totals["accepted"],
        "rerolled": totals["rerolled"],
        "by_engine": out(by_engine),
        "by_custody": out(by_custody),
    }


# ---------------------------------------------------------------------------- the outbox


def enqueue_outbox(
    conn: sqlite3.Connection, record: Mapping[str, Any], *, clock: Callable[[], datetime] = utcnow
) -> int:
    """Park a record the ledger could not take (read-only file, lock held too long) in the project index.

    The record is checked now, exactly as `Ledger.append` would check it, and gets its `at` now if it has
    none, so a bad record fails at the call site and a late one still carries the time it happened. Returns
    the outbox row id. The row survives a daemon restart (it is in the index, not in memory)."""
    norm = normalise_record(record, clock)
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO ledger_outbox (run_id, kind, record, created_utc) VALUES (?, ?, ?, ?)",
            (norm.get("run_id"), norm["kind"], canonical_json(norm), iso(clock())),
        )
        return int(cur.lastrowid or 0)


def outbox_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM ledger_outbox").fetchone()[0])


def drain_outbox(conn: sqlite3.Connection, ledger: Ledger) -> int:
    """Append parked records to `ledger` in row order; returns how many were appended.

    Each row is appended and deleted inside one `tx`, so a row leaves the outbox only with its record in
    the ledger. The first failure bumps that row's `attempts`, stores `last_error` and stops, so later rows
    never overtake it. If the append succeeded and only the delete failed, the delete is retried once
    outside the transaction: a second drain must not append the same record twice (a `billed` line would
    count twice). A hard kill between the two steps can still do that; it needs a power cut inside one
    millisecond."""
    appended = 0
    while True:
        row = conn.execute("SELECT id, record FROM ledger_outbox ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return appended
        row_id, wrote = row["id"], False
        try:
            rec = json.loads(row["record"])
            with tx(conn):
                ledger.append(rec)
                wrote = True
                conn.execute("DELETE FROM ledger_outbox WHERE id = ?", (row_id,))
        except Exception as e:  # any failure parks the row again; SimulatedCrash is a BaseException
            if wrote:
                try:
                    with tx(conn):
                        conn.execute("DELETE FROM ledger_outbox WHERE id = ?", (row_id,))
                    appended += 1
                    continue
                except sqlite3.Error:
                    log.warning("ledger outbox row %s was appended but could not be deleted", row_id)
            _note_outbox_failure(conn, row_id, e)
            return appended
        appended += 1


def _note_outbox_failure(conn: sqlite3.Connection, row_id: int, error: Exception) -> None:
    detail = f"{type(error).__name__}: {error}"[:500]
    try:
        with tx(conn):
            conn.execute(
                "UPDATE ledger_outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (detail, row_id),
            )
    except sqlite3.Error:  # the index is busy too; the row stays as it was and the next drain tries again
        log.warning("could not record the failure of ledger outbox row %s: %s", row_id, detail)
