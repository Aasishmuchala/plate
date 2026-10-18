"""projects.py: the project folder format and the single-writer lock (contention from a second process)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys

import pytest

from plate_daemon.projects import (
    GRAPH_MAX_BYTES,
    GraphShapeError,
    GraphTooLarge,
    Lock,
    Project,
    ProjectError,
    ProjectLocked,
    check_custody,
    check_graph,
    mark_unset,
)

# The child reports its own pid: on Windows a venv's python.exe is a launcher whose child is the real
# interpreter, so Popen.pid is not the pid that takes the lock.
HOLD_LOCK = """
import os, sys
from plate_daemon.projects import Lock
with Lock(sys.argv[1]):
    print("locked", os.getpid(), flush=True)
    sys.stdin.readline()
"""


def test_create_writes_the_project_json_schema(tmp_path):
    proj = Project.create(tmp_path / "p", "tula-wot", {"nda": True})
    doc = json.loads((tmp_path / "p" / "project.json").read_text(encoding="utf-8"))
    assert set(doc) == {"schema", "project_id", "created_utc", "custody", "engine_pins", "graph", "settings"}
    assert doc["schema"] == 1 and doc["project_id"] == "tula-wot"
    assert doc["custody"] == {"nda": True, "allowed_grades": ["C0", "C1"]}
    assert doc["graph"] == {"nodes": [], "edges": []} and doc["engine_pins"] == {} and doc["settings"] == {}
    assert doc["created_utc"].endswith("Z")
    assert (tmp_path / "p" / "assets").is_dir()
    assert proj.nda and proj.allowed_grades == ["C0", "C1"]
    with pytest.raises(FileExistsError):
        Project.create(tmp_path / "p", "tula-wot", {"nda": True})


def test_custody_rules(tmp_path):
    assert Project.create(tmp_path / "a", "open", {"nda": False}).allowed_grades == ["C0", "C1", "C2", "D"]
    with pytest.raises(ProjectError, match="NDA"):
        Project.create(tmp_path / "b", "nda", {"nda": True, "allowed_grades": ["C0", "D"]})
    with pytest.raises(ProjectError):
        Project.create(tmp_path / "c", "x", {"nda": "yes"})
    with pytest.raises(ProjectError):
        Project.create(tmp_path / "d", "x", {"nda": False, "allowed_grades": ["C9"]})
    with pytest.raises(ValueError):
        Project.create(tmp_path / "e", "Bad:Id", {"nda": False})


def test_save_graph_is_atomic_and_reloads(tmp_path):
    proj = Project.create(tmp_path / "p", "proj", {"nda": False})
    graph = {"nodes": [{"id": "plate", "type": "Plate"}], "edges": [{"from": "plate", "to": "edit"}]}
    proj.save_graph(graph)
    graph["nodes"].append({"id": "mutated-after-save"})
    again = Project.load(tmp_path / "p")
    assert again.graph["nodes"] == [{"id": "plate", "type": "Plate"}]
    raw = (tmp_path / "p" / "project.json").read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    assert [p.name for p in (tmp_path / "p").iterdir() if p.name.endswith(".tmp")] == []
    with pytest.raises(ProjectError):
        proj.save_graph({"nodes": "nope", "edges": []})


def test_save_graph_counts_the_revision(tmp_path):
    proj = Project.create(tmp_path / "p", "proj", {"nda": False})
    assert proj.graph_revision == 0 and "graph_revision" not in proj.doc  # absent on a new project
    proj.save_graph({"nodes": [], "edges": []})
    proj.save_graph({"nodes": [{"id": "e", "kind": "Edit"}], "edges": []})
    again = Project.load(tmp_path / "p")
    assert again.graph_revision == 2 and "revision" not in again.graph
    doc = json.loads((tmp_path / "p" / "project.json").read_text(encoding="utf-8"))
    assert doc["graph_revision"] == 2
    for bad in (-1, "2", True, 1.5):
        doc["graph_revision"] = bad
        (tmp_path / "p" / "project.json").write_text(json.dumps(doc), encoding="utf-8", newline="\n")
        with pytest.raises(ProjectError, match="graph_revision"):
            Project.load(tmp_path / "p")


def test_unset_is_the_servers_mark_only(tmp_path):
    for mark in (True, 1, "yes", False):
        assert "unset" not in check_custody({"nda": False, "unset": mark})
    assert check_custody({"nda": False, "region": "eu"})["region"] == "eu"  # other keys still pass
    assert mark_unset({"nda": False, "allowed_grades": ["C0"]})["unset"] is True

    # The packet-first path (pipeline.UNSET_CUSTODY): the server's literal true survives create and load ...
    proj = Project.create(tmp_path / "a", "early", {"nda": False, "allowed_grades": ["C0"], "unset": True})
    assert proj.custody["unset"] is True and Project.load(tmp_path / "a").custody["unset"] is True
    # ... anything else is not the server's mark.
    proj = Project.create(tmp_path / "b", "other", {"nda": False, "allowed_grades": ["C0"], "unset": 1})
    assert "unset" not in proj.custody and "unset" not in Project.load(tmp_path / "b").custody


def test_check_graph_holds_a_clients_graph_to_its_shape_and_size():
    ok = {"nodes": [{"id": "p", "kind": "Plate"}, {"id": "e", "data": {"kind": "Edit"}}], "edges": []}
    assert check_graph(ok) is ok
    for nodes in (
        [{"id": {}, "kind": "Plate"}],
        [{"id": [], "kind": "Plate"}],
        [{"id": "", "kind": "Plate"}],
        [{"kind": "Plate"}],
        [{"id": "p"}],
        [{"id": "p", "data": {"kind": 3}}],
    ):
        with pytest.raises(GraphShapeError) as exc:
            check_graph({"nodes": nodes, "edges": []})
        assert exc.value.code == "UNKNOWN_NODE"
    for edge in ({"source": "p"}, {"source": 1, "target": "e"}, {"from": "p", "to": "e"}):
        with pytest.raises(GraphShapeError) as exc:
            check_graph({"nodes": [], "edges": [edge]})
        assert exc.value.code is None and isinstance(exc.value, ProjectError)
    with pytest.raises(ProjectError):
        check_graph({"nodes": "nope", "edges": []})
    big = {"nodes": [{"id": "c", "kind": "Colour", "label": "x" * GRAPH_MAX_BYTES}], "edges": []}
    with pytest.raises(GraphTooLarge, match="the limit is 4 MiB") as exc:
        check_graph(big)
    assert exc.value.size > GRAPH_MAX_BYTES and isinstance(exc.value, ProjectError)


def test_load_rejects_bad_folders(tmp_path):
    with pytest.raises(FileNotFoundError):
        Project.load(tmp_path)
    (tmp_path / "project.json").write_text('{"schema": 2}', encoding="utf-8", newline="\n")
    with pytest.raises(ProjectError, match="schema"):
        Project.load(tmp_path)


def test_register_packet_by_reference(tmp_path):
    proj = Project.create(tmp_path / "p", "proj", {"nda": False})
    entry = {"path": str(tmp_path / "pk"), "manifest_blake3": "a" * 64, "shot_id": "s1"}
    proj.register_packet("pkt_1", entry)
    assert Project.load(tmp_path / "p").packets() == {"pkt_1": entry}
    with pytest.raises(ProjectError, match="another manifest"):
        proj.register_packet("pkt_1", {**entry, "manifest_blake3": "b" * 64})


def test_lock_contention_from_a_second_process(tmp_path, child_env):
    proj = tmp_path / "p"
    child = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(proj)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env(),
        text=True,
        encoding="utf-8",
    )
    try:
        word, _, pid = child.stdout.readline().strip().partition(" ")
        assert word == "locked", child.stderr.read()
        with pytest.raises(ProjectLocked) as exc:
            with Lock(proj):
                pass
        e = exc.value
        assert e.holder_pid == int(pid)
        assert e.holder_host == socket.gethostname()
        assert e.since and e.since.endswith("Z")
        assert pid in str(e)
    finally:
        child.stdin.close()
        child.wait(timeout=10)
    with Lock(proj) as lk:  # the holder exited: the lock is free again
        assert lk.held
    assert not lk.held
