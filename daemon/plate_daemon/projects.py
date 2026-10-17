"""Project folders: the truth the per-machine SQLite index caches (Decision 6).

A project is a folder, possibly on the NAS, holding `project.json` (the graph and the project's custody),
`assets/<h[:2]>/<h>.<ext>` (the project's CAS), `runs.jsonl` (the append-only ledger, T13),
`gates/<run_id>.<n>.json`, `review/` and `packets.json` (packets by reference). Nothing in it depends on
the machine that wrote it, so the box and a Mac can open the same project one at a time.

`project.json` is `{schema: 1, project_id, created_utc, custody: {nda, allowed_grades}, engine_pins:
{engine_id: version}, graph: {nodes: [], edges: []}, settings: {}}` and is rewritten atomically
(temp file + `os.replace` with retry, UTF-8, `\\n`). An NDA project allows only C0 and C1 routes.
One writer at a time: hold `Lock(project_dir)` (`project.lock`, portalocker) around any write; a second
holder gets `ProjectLocked(holder_host, holder_pid, since)`. `save_graph` does not take the lock itself
because the daemon holds it for as long as the project is open.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .cas import CAS, OnPut
from .config import check_project_id
from .fsutil import read_json, write_json_atomic
from .lock import ProjectLocked, project_lock
from .timeutil import iso, utcnow

SCHEMA = 1
GRADES = ("C0", "C1", "C2", "D")
NDA_GRADES = ("C0", "C1")
PROJECT_JSON = "project.json"
PACKETS_JSON = "packets.json"
RUNS_JSONL = "runs.jsonl"
ASSETS_DIR = "assets"
GATES_DIR = "gates"
REVIEW_DIR = "review"
LOCK_NAME = "project.lock"

__all__ = ["Lock", "Project", "ProjectError", "ProjectLocked", "check_custody", "check_graph"]


class ProjectError(ValueError):
    pass


def check_custody(custody: Any) -> dict[str, Any]:
    """`{nda: bool, allowed_grades: [...]}`; allowed_grades defaults to C0/C1 for NDA work, else all four.
    Unknown keys are kept (later tasks may add custody fields)."""
    if not isinstance(custody, dict):
        raise ProjectError("custody must be an object {nda, allowed_grades}")
    nda = custody.get("nda")
    if not isinstance(nda, bool):
        raise ProjectError("custody.nda must be true or false")
    grades = custody.get("allowed_grades")
    if grades is None:
        grades = list(NDA_GRADES if nda else GRADES)
    if not isinstance(grades, list) or not grades or not all(isinstance(g, str) for g in grades):
        raise ProjectError("custody.allowed_grades must be a non-empty list of grades")
    unknown = sorted(set(grades) - set(GRADES))
    if unknown:
        raise ProjectError(f"custody.allowed_grades: unknown grades {unknown}; expected {list(GRADES)}")
    if nda and set(grades) - set(NDA_GRADES):
        raise ProjectError(
            f"an NDA project allows {list(NDA_GRADES)} only, got {sorted(set(grades))}: NDA work never "
            "leaves for a C2 or D route"
        )
    out = dict(custody)
    out["nda"] = nda
    out["allowed_grades"] = [g for g in GRADES if g in set(grades)]
    return out


def check_graph(graph: Any) -> dict[str, Any]:
    if not isinstance(graph, dict):
        raise ProjectError("graph must be an object {nodes, edges}")
    for key in ("nodes", "edges"):
        items = graph.get(key)
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ProjectError(f"graph.{key} must be a list of objects")
    return graph


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Records of a JSON-lines file and the problems met. A torn last line (a crash mid-append) is
    skipped and reported; a bad line in the middle is reported too."""
    records: list[dict[str, Any]] = []
    problems: list[str] = []
    if not path.is_file():
        return records, problems
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    last = max((i for i, s in enumerate(lines) if s.strip()), default=-1)
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            kind = "torn last line" if i == last else "unreadable line"
            problems.append(f"{path.name}:{i + 1}: {kind} skipped")
            continue
        if not isinstance(rec, dict):
            problems.append(f"{path.name}:{i + 1}: not a JSON object, skipped")
            continue
        records.append(rec)
    return records, problems


@dataclass
class Project:
    root: Path
    doc: dict[str, Any] = field(repr=False)

    # ------------------------------------------------------------------ create / load

    @classmethod
    def create(
        cls, root: Path | str, project_id: str, custody: dict[str, Any], *, now: datetime | None = None
    ) -> Project:
        root = Path(root)
        check_project_id(project_id)
        if (root / PROJECT_JSON).exists():
            raise FileExistsError(f"{root / PROJECT_JSON} already exists: load the project instead")
        doc = {
            "schema": SCHEMA,
            "project_id": project_id,
            "created_utc": iso(now or utcnow()),
            "custody": check_custody(custody),
            "engine_pins": {},
            "graph": {"nodes": [], "edges": []},
            "settings": {},
        }
        (root / ASSETS_DIR).mkdir(parents=True, exist_ok=True)
        project = cls(root=root, doc=doc)
        project.save()
        return project

    @classmethod
    def load(cls, root: Path | str) -> Project:
        root = Path(root)
        path = root / PROJECT_JSON
        if not path.is_file():
            raise FileNotFoundError(f"{path}: not a PLATE project folder (no project.json)")
        try:
            doc = read_json(path)
        except ValueError as e:
            raise ProjectError(f"{path}: not valid JSON ({e})") from e
        if not isinstance(doc, dict):
            raise ProjectError(f"{path}: expected a JSON object")
        if doc.get("schema") != SCHEMA:
            raise ProjectError(f"{path}: schema {doc.get('schema')!r} is not {SCHEMA}")
        try:
            check_project_id(doc.get("project_id"))
        except ValueError as e:
            raise ProjectError(f"{path}: {e}") from e
        doc["custody"] = check_custody(doc.get("custody"))
        check_graph(doc.get("graph"))
        for key in ("engine_pins", "settings"):
            if not isinstance(doc.get(key, {}), dict):
                raise ProjectError(f"{path}: {key} must be an object")
            doc.setdefault(key, {})
        return cls(root=root, doc=doc)

    # ------------------------------------------------------------------ fields and paths

    @property
    def project_id(self) -> str:
        return self.doc["project_id"]

    @property
    def custody(self) -> dict[str, Any]:
        return self.doc["custody"]

    @property
    def nda(self) -> bool:
        return bool(self.custody["nda"])

    @property
    def allowed_grades(self) -> list[str]:
        return list(self.custody["allowed_grades"])

    @property
    def graph(self) -> dict[str, Any]:
        return self.doc["graph"]

    @property
    def engine_pins(self) -> dict[str, str]:
        return self.doc["engine_pins"]

    @property
    def project_json(self) -> Path:
        return self.root / PROJECT_JSON

    @property
    def packets_json(self) -> Path:
        return self.root / PACKETS_JSON

    @property
    def runs_jsonl(self) -> Path:
        return self.root / RUNS_JSONL

    @property
    def assets_dir(self) -> Path:
        return self.root / ASSETS_DIR

    @property
    def gates_dir(self) -> Path:
        return self.root / GATES_DIR

    @property
    def review_dir(self) -> Path:
        return self.root / REVIEW_DIR

    def cas(self, on_put: OnPut | None = None) -> CAS:
        return CAS(self.assets_dir, on_put=on_put)

    def lock(self, timeout_s: float = 0.0) -> Lock:
        return Lock(self.root, timeout_s)

    # ------------------------------------------------------------------ writes (hold the Lock)

    def save(self) -> None:
        write_json_atomic(self.project_json, self.doc, indent=1)

    def save_graph(self, graph: dict[str, Any]) -> None:
        self.doc["graph"] = copy.deepcopy(check_graph(graph))
        self.save()

    # ------------------------------------------------------------------ packets.json (by reference)

    def packets(self) -> dict[str, dict[str, Any]]:
        if not self.packets_json.is_file():
            return {}
        doc = read_json(self.packets_json)
        if not isinstance(doc, dict) or not isinstance(doc.get("packets"), dict):
            raise ProjectError(f"{self.packets_json}: expected {{schema, packets: {{packet_id: entry}}}}")
        return doc["packets"]

    def register_packet(self, packet_id: str, entry: dict[str, Any]) -> None:
        """Record a packet by reference (its folder path and manifest hash, never its pixels). The same
        id with a different manifest hash is refused: a changed packet needs a new packet_id."""
        if not entry.get("path") or not entry.get("manifest_blake3"):
            raise ProjectError("a packets.json entry needs path and manifest_blake3")
        packets = self.packets()
        old = packets.get(packet_id)
        if old is not None and old.get("manifest_blake3") != entry["manifest_blake3"]:
            raise ProjectError(f"{packet_id} is registered with another manifest hash")
        packets[packet_id] = dict(entry)
        write_json_atomic(self.packets_json, {"schema": SCHEMA, "packets": packets}, indent=1)

    # ------------------------------------------------------------------ runs.jsonl (read side)

    def ledger_records(self) -> tuple[list[dict[str, Any]], list[str]]:
        return read_jsonl(self.runs_jsonl)


class Lock:
    """The project's single-writer lock: `with Lock(project_dir): ...`.

    Raises `ProjectLocked(holder_host, holder_pid, since)` at once (or after `timeout_s`) when another
    process holds it; the OS frees it if the holder dies."""

    def __init__(self, project_dir: Path | str, timeout_s: float = 0.0):
        self.project_dir = Path(project_dir)
        self.timeout_s = timeout_s
        self._cm: Any = None

    @property
    def path(self) -> Path:
        return self.project_dir / LOCK_NAME

    def acquire(self) -> Lock:
        if self._cm is not None:
            raise RuntimeError("Lock already held by this handle")
        cm = project_lock(self.project_dir, self.timeout_s, name=LOCK_NAME)
        cm.__enter__()
        self._cm = cm
        return self

    def release(self) -> None:
        cm, self._cm = self._cm, None
        if cm is not None:
            cm.__exit__(None, None, None)

    @property
    def held(self) -> bool:
        return self._cm is not None

    def __enter__(self) -> Lock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
