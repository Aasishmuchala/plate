"""Project folders: the truth the per-machine SQLite index caches (Decision 6).

A project is a folder, possibly on the NAS, holding `project.json` (the graph and the project's custody),
`assets/<h[:2]>/<h>.<ext>` (the project's CAS), `runs.jsonl` (the append-only ledger, T13),
`gates/<run_id>.<n>.json`, `review/` and `packets.json` (packets by reference). Nothing in it depends on
the machine that wrote it, so the box and a Mac can open the same project one at a time.

`project.json` is `{schema: 1, project_id, created_utc, custody: {nda, allowed_grades}, engine_pins:
{engine_id: version}, graph: {nodes: [], edges: []}, settings: {}, graph_revision}` and is rewritten atomically
(temp file + `os.replace` with retry, UTF-8, `\\n`). An NDA project allows only C0 and C1 routes.
One writer at a time: hold `Lock(project_dir)` (`project.lock`, portalocker) around any write; a second
holder gets `ProjectLocked(holder_host, holder_pid, since)`. `save_graph` does not take the lock itself
because the daemon holds it for as long as the project is open.

`graph_revision` (decision 77) counts the graph's saves: absent (0) on a new project, one more on every
`save_graph`, so a client that read revision n and saves on top of n+1 is told (`PUT /graph` answers 409
`graph_conflict`). `custody.unset` is the server's own mark (decision 79): `check_custody` strips it from
whatever a client sends, and only a literal `true` written by the server (`mark_unset`, the packet-first
project's `UNSET_CUSTODY`) survives `Project.create` and `Project.load`. `check_graph` is what the routes hold a
client's graph to (objects with string ids and kinds, edges with string ends, at most `GRAPH_MAX_BYTES`); the
folder itself only needs lists of objects, so a graph the server stored before those checks still loads.
A node's `params` is held to `NODE_PARAMS_MAX_BYTES` of compact JSON on top of that (`NodeParamsTooLarge`,
decision 88): one node cannot take the whole graph's 4 MiB.
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
UNSET = "unset"  # the server's mark on the custody of a project a packet made first
GRAPH_MAX_BYTES = 4 * 1024 * 1024  # a client's graph, compact JSON: 4 MiB
NODE_PARAMS_MAX_BYTES = 262144  # one node's `params`, compact JSON: 256 KiB (decision 88)
GRAPH_REVISION = "graph_revision"

__all__ = [
    "GRAPH_MAX_BYTES",
    "NODE_PARAMS_MAX_BYTES",
    "GraphShapeError",
    "GraphTooLarge",
    "Lock",
    "NodeParamsTooLarge",
    "Project",
    "ProjectError",
    "ProjectLocked",
    "check_custody",
    "check_graph",
    "mark_unset",
]


class ProjectError(ValueError):
    pass


class GraphTooLarge(ProjectError):
    """A graph over `GRAPH_MAX_BYTES` (the routes answer 422 `graph_too_large`)."""

    def __init__(self, size: int):
        self.size = size
        super().__init__(f"graph is {size} bytes; the limit is 4 MiB")


class NodeParamsTooLarge(ProjectError):
    """One node's `params` over `NODE_PARAMS_MAX_BYTES` (the routes answer 422 `params_too_large`)."""

    def __init__(self, node_id: str, size: int):
        self.node_id, self.size = node_id, size
        super().__init__(
            f"node {node_id!r}: params are {size} bytes; the limit is {NODE_PARAMS_MAX_BYTES // 1024} KiB"
        )


class GraphShapeError(ProjectError):
    """A graph JSON allows and the canvas cannot hold. `code` is the canvas-rule code a node problem answers
    with (UNKNOWN_NODE), None for an edge or the outer shape."""

    def __init__(self, message: str, *, code: str | None = None, node: str | None = None):
        self.code, self.node = code, node
        super().__init__(message)


def mark_unset(custody: dict[str, Any]) -> dict[str, Any]:
    """The custody with the server's `unset` mark: a project a packet made before any human set its custody."""
    return {**custody, UNSET: True}


def _stored_custody(custody: Any) -> dict[str, Any]:
    """`check_custody` for what the server itself writes or reads back: its literal `unset: true` is kept."""
    out = check_custody(custody)
    return mark_unset(out) if custody.get(UNSET) is True else out


def check_custody(custody: Any) -> dict[str, Any]:
    """`{nda: bool, allowed_grades: [...]}`; allowed_grades defaults to C0/C1 for NDA work, else all four.
    Unknown keys are kept (later tasks may add custody fields), except `unset`: that mark is the server's
    (decision 79), so a client that sends it, as `true` or anything else, gets it dropped."""
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
    out = {k: v for k, v in custody.items() if k != UNSET}
    out["nda"] = nda
    out["allowed_grades"] = [g for g in GRADES if g in set(grades)]
    return out


def _check_graph_lists(graph: Any) -> dict[str, Any]:
    """The folder format's own floor: an object whose `nodes` and `edges` are lists of objects."""
    if not isinstance(graph, dict):
        raise ProjectError("graph must be an object {nodes, edges}")
    for key in ("nodes", "edges"):
        items = graph.get(key)
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ProjectError(f"graph.{key} must be a list of objects")
    return graph


def check_graph(graph: Any) -> dict[str, Any]:
    """A client's graph, before any rule reads it: every node an object with a non-empty string `id` and a
    string `kind` (read where the canvas rules read it: the node's `data` object when it has one, else the
    node), every edge an object with string `source` and `target`, at most `GRAPH_MAX_BYTES` as compact JSON,
    and no node whose `params` are over `NODE_PARAMS_MAX_BYTES` as compact JSON. `GraphShapeError` (UNKNOWN_NODE
    for a node), `GraphTooLarge` or `NodeParamsTooLarge`, all `ProjectError`s."""
    _check_graph_lists(graph)
    for i, node in enumerate(graph["nodes"]):
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            raise GraphShapeError(
                f"graph.nodes[{i}]: a node needs a string id, got {type(nid).__name__}", code="UNKNOWN_NODE"
            )
        fields = node["data"] if isinstance(node.get("data"), dict) else node
        if not isinstance(fields.get("kind"), str):
            raise GraphShapeError(
                f"graph.nodes[{i}] ({nid!r}): a node needs a string kind", code="UNKNOWN_NODE", node=nid
            )
    for i, edge in enumerate(graph["edges"]):
        if not isinstance(edge.get("source"), str) or not isinstance(edge.get("target"), str):
            raise GraphShapeError(f"graph.edges[{i}]: an edge needs a string source and a string target")
    size = len(json.dumps(graph, separators=(",", ":")).encode())
    if size > GRAPH_MAX_BYTES:
        raise GraphTooLarge(size)
    for node in graph["nodes"]:
        # Where the canvas rules read it: the node's `data` object when it has one, else the node (either may
        # hold `params`; both are held to the cap).
        for holder in (node, node["data"]) if isinstance(node.get("data"), dict) else (node,):
            if not holder.get("params"):
                continue
            text = json.dumps(holder["params"], separators=(",", ":"), ensure_ascii=False)
            n = len(text.encode("utf-8", "surrogatepass"))  # a lone surrogate in a string is still counted
            if n > NODE_PARAMS_MAX_BYTES:
                raise NodeParamsTooLarge(node["id"], n)
    return graph


def _revision_of(doc: dict[str, Any]) -> int:
    rev = doc.get(GRAPH_REVISION, 0)
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        raise ProjectError(f"{GRAPH_REVISION} must be a whole number of at least 0, got {rev!r}")
    return rev


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
            "custody": _stored_custody(custody),
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
        doc["custody"] = _stored_custody(doc.get("custody"))
        _check_graph_lists(doc.get("graph"))
        try:
            _revision_of(doc)
        except ProjectError as e:
            raise ProjectError(f"{path}: {e}") from e
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
    def graph_revision(self) -> int:
        """How many times the graph was saved (0 when `project.json` has no `graph_revision` yet)."""
        return _revision_of(self.doc)

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
        """Store `graph` and count the save (`graph_revision` + 1)."""
        self.doc["graph"] = copy.deepcopy(_check_graph_lists(graph))
        self.doc[GRAPH_REVISION] = self.graph_revision + 1
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
