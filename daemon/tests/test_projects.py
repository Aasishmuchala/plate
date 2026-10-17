"""projects.py: the project folder format and the single-writer lock (contention from a second process)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys

import pytest

from plate_daemon.projects import Lock, Project, ProjectError, ProjectLocked

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
