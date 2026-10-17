"""The outbound log: one durable line before every request leaves the box, one after it comes back.

`<project>/outbound.jsonl` (UTF-8, `\\n`, append-only; BUILD_PLAN section 7, `docs/custody.md` section 6) is
the evidence behind the custody claims: what went where, how many bytes, at which grade, under which opaque
ids, with the BLAKE3 of the exact body. `egress.Egress` writes it and nothing else should.

Each request is two lines sharing an `id` (a ULID):

    {"kind": "request", "id", "at", "run_id", "route_id", "host", "method", "path", "bytes",
     "custody_grade", "opaque_ids", "body_hash"}             # fsync'd BEFORE the request leaves
    {"kind": "response", "id", "at", "status", "bytes_in"}   # after the response is read
    {"kind": "error", "id", "at", "error"}                   # or this, when the transport raised

`entries()` merges them into one record per request; a request line with neither follow-up means the
process died while the request was in flight. The URL query is never logged (keys can ride in it). Appends
are serialised by a thread lock plus a portalocker lock on `outbound.jsonl.lock`, because another process
(the MCP server) may log to the same project; readers never need the lock.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import portalocker

from .timeutil import iso, utcnow

OUTBOUND_JSONL = "outbound.jsonl"
LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT_S = 10.0
KINDS = ("request", "response", "error")

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve()))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


class OutboundLog:
    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.path = Path(path)
        self._clock = clock

    @classmethod
    def for_project(cls, project_dir: Path | str, **kwargs: Any) -> OutboundLog:
        return cls(Path(project_dir) / OUTBOUND_JSONL, **kwargs)

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + LOCK_SUFFIX)

    # ------------------------------------------------------------------ writes

    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (
            _thread_lock(self.path),
            portalocker.Lock(
                self.lock_path, mode="a", timeout=LOCK_TIMEOUT_S, fail_when_locked=False, encoding="utf-8"
            ),
        ):
            prefix = ""
            if self.path.exists() and self.path.stat().st_size > 0:
                with open(self.path, "rb") as fh:  # a crash mid-append left a torn line: start a fresh one
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        prefix = "\n"
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(prefix + line)
                fh.flush()
                os.fsync(fh.fileno())

    def request(
        self,
        *,
        entry_id: str,
        run_id: str | None,
        route_id: str,
        host: str,
        method: str,
        path: str,
        bytes_out: int,
        custody_grade: str,
        opaque_ids: Iterable[str],
        body_hash: str,
    ) -> str:
        """Durably record a request that is about to leave. Returns its id. Raises if it cannot be written,
        so the caller sends nothing."""
        self._append(
            {
                "kind": "request",
                "id": entry_id,
                "at": iso(self._clock()),
                "run_id": run_id,
                "route_id": route_id,
                "host": host,
                "method": method.upper(),
                "path": path,
                "bytes": int(bytes_out),
                "custody_grade": str(custody_grade),
                "opaque_ids": list(opaque_ids),
                "body_hash": body_hash,
            }
        )
        return entry_id

    def response(self, entry_id: str, *, status: int, bytes_in: int) -> None:
        self._append(
            {
                "kind": "response",
                "id": entry_id,
                "at": iso(self._clock()),
                "status": int(status),
                "bytes_in": int(bytes_in),
            }
        )

    def error(self, entry_id: str, *, error: str) -> None:
        self._append({"kind": "error", "id": entry_id, "at": iso(self._clock()), "error": error[:500]})

    # ------------------------------------------------------------------ reads

    def records(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Every line as written, plus the problems met (a torn or unreadable line is skipped and reported)."""
        records: list[dict[str, Any]] = []
        problems: list[str] = []
        if not self.path.is_file():
            return records, problems
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                problems.append(f"{self.path.name}:{i + 1}: unreadable line skipped")
                continue
            if not isinstance(rec, dict) or rec.get("kind") not in KINDS or not rec.get("id"):
                problems.append(f"{self.path.name}:{i + 1}: not an outbound record, skipped")
                continue
            records.append(rec)
        return records, problems

    def entries(self) -> list[dict[str, Any]]:
        """One record per request, in the order they left: the request fields plus `status` / `bytes_in`
        (None while in flight or after a crash) and `error` (None unless the transport raised)."""
        out: dict[str, dict[str, Any]] = {}
        records, _ = self.records()
        for rec in records:
            if rec["kind"] == "request":
                entry = {k: v for k, v in rec.items() if k != "kind"}
                entry.update(status=None, bytes_in=None, error=None)
                out[rec["id"]] = entry
            elif rec["id"] in out:
                entry = out[rec["id"]]
                if rec["kind"] == "response":
                    entry["status"] = rec.get("status")
                    entry["bytes_in"] = rec.get("bytes_in")
                else:
                    entry["error"] = rec.get("error")
        return list(out.values())

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        """The last n requests (the custody panel's `GET /projects/{id}/outbound?tail=`)."""
        if n <= 0:
            return []
        return self.entries()[-n:]
