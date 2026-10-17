"""cas.py: the per-project content-addressed store (assets/<h[:2]>/<h>.<ext>)."""

from __future__ import annotations

import json

import blake3
import pytest

from plate_daemon.cas import CAS, CASError
from plate_daemon.worker import step


def test_put_open_round_trip_and_path_shape(tmp_path):
    project = tmp_path / "proj"
    cas = CAS(project / "assets")
    data = b"\x89PNG fake bytes"
    h = cas.put(data, "png")
    assert h == blake3.blake3(data).hexdigest()
    p = cas.path_for(h, "png")
    assert p == project / "assets" / h[:2] / f"{h}.png" and p.is_file()
    rel = p.relative_to(project).as_posix()
    assert rel == f"assets/{h[:2]}/{h}.png" and len(rel) < 200 and ":" not in rel
    assert cas.exists(h) and cas.has(h)
    with cas.open(h) as fh:
        assert fh.read() == data
    assert cas.read_bytes(h) == data and cas.verify(h)
    assert [(k, e) for k, e, _ in cas.iter_objects()] == [(h, "png")]
    assert not (project / "assets" / ".tmp").exists() or not any((project / "assets" / ".tmp").iterdir())


def test_put_file_streams_and_dedupes(tmp_path):
    src = tmp_path / "plate.EXR"
    src.write_bytes(bytes(range(256)) * 5000)
    cas = CAS(tmp_path / "proj" / "assets")
    h = cas.put(src)
    assert cas.find(h).name == f"{h}.exr"
    assert cas.put(src.read_bytes(), "bin") == h  # same bytes, other ext: still one object
    assert len(list(cas.iter_objects())) == 1


@pytest.mark.parametrize("ext", ["pn:g", "a/b", "..", "toolongextension", "e x"])
def test_bad_extensions_are_refused(tmp_path, ext):
    with pytest.raises(CASError):
        CAS(tmp_path / "assets").put(b"x", ext)


def test_missing_objects_and_bad_keys(tmp_path):
    cas = CAS(tmp_path / "assets")
    with pytest.raises(CASError):
        cas.open("0" * 64)
    traversal = "../../etc/passwd"
    with pytest.raises(CASError):
        cas.open(traversal)
    with pytest.raises(CASError):
        cas.open(blake3.blake3(b"").hexdigest(), mode="wb")
    assert not cas.exists("f" * 64) and not cas.verify("f" * 64)


def test_on_put_and_refcount(tmp_path):
    seen = []
    project = tmp_path / "proj"
    cas = CAS(project / "assets", on_put=lambda h, e, n: seen.append((h, e, n)))
    a, b, c = cas.put(b"a", "png"), cas.put(b"bb", "exr"), cas.put(b"ccc", "json")
    assert seen == [(a, "png", 1), (b, "exr", 2), (c, "json", 3)]
    (project / "project.json").write_text(
        json.dumps(
            {"graph": {"nodes": [{"id": "n1", "asset": a}, {"id": "n2", "src": f"assets/{b[:2]}/{b}.exr"}]}}
        ),
        encoding="utf-8",
        newline="\n",
    )
    lines = [json.dumps({"run_id": "r1", "data": {"result": a}}), '{"torn": ']
    (project / "runs.jsonl").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    assert cas.refcount() == {a: 2, b: 1, c: 0}


def test_worker_and_export_need_a_project_cas():
    with pytest.raises(TypeError, match="no global CAS"):
        step(None, None, "run_x")
