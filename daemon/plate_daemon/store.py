"""One SQLite index per project under `<home>/index`, opened per request and found by id.

The API speaks in packet and run ids; `Index` maps an id to its project's index file (remembered after
the first hit, otherwise found by scanning `index/*.sqlite`), opens a fresh WAL connection for each unit
of work and migrates each file once per process. Fresh connections keep the thread-pool routes, the
background ingest and the worker from ever sharing one `sqlite3.Connection` concurrently.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .db import connect, migrate
from .paths import PROJECT_ID_RE, check_project_id, index_db_path, index_dir


class Index:
    def __init__(self, home: Path):
        self.home = Path(home)
        self._migrated: set[str] = set()
        self._owner: dict[str, str] = {}
        self._lock = threading.Lock()

    def db_path(self, project_id: str) -> Path:
        return index_db_path(project_id, self.home)

    def connect(self, project_id: str) -> sqlite3.Connection:
        check_project_id(project_id)
        conn = connect(self.db_path(project_id))
        if project_id not in self._migrated:
            migrate(conn)
            with self._lock:
                self._migrated.add(project_id)
        return conn

    @contextmanager
    def project(self, project_id: str) -> Iterator[sqlite3.Connection]:
        conn = self.connect(project_id)
        try:
            yield conn
        finally:
            conn.close()

    def project_ids(self) -> list[str]:
        d = index_dir(self.home)
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.sqlite") if PROJECT_ID_RE.match(p.stem))

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
