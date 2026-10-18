"""Wave 12 stress fixes in export, cutdown and batch (router plan T127, decision 74), each test an attacker's repro.

- `export.export` claims its destination with `O_CREAT | O_EXCL`: of eight concurrent exports of two runs to one
  name exactly one is written, and the image and its `.gate.json` sidecar always name the same run. The same
  run exported again to the name it already has, byte for byte, is a 200 `repeat: true` that writes nothing
  (the ledger `export` record says `data.repeat: true`); another run onto that name is 409.
- A project whose `gates/` cannot take the shipped mark refuses with 503 `not_exportable` before a client file
  is written (single export, batch, cutdown, delivery).
- `name_pattern` is parsed before it is formatted: a format spec, a conversion, an attribute or an index is 422
  `bad_name_pattern` at once (a `{n:>900000000}` never builds its 900 MB string).
- Stale `.part`/temp siblings (a kill -9's leftovers) of the name, older than an hour, go with the next export.
- Cutdowns claim their name the same way: six concurrent requests for one crop name, one file.
- Names are checked with `fullmatch`: `"nlx\\n"` is a 422, never a file name ending in a newline.

The route tests run the real pipeline on `fixtures/packets/room_a` with an engine that echoes the plate, as
`test_export_wave9.py` does. The races are forced: a barrier in front of the claim holds every request until
all of them have their file ready, the worst interleaving the attacker could hope for. No client data is read.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plate_daemon import cutdown as cutdown_mod
from plate_daemon import export as export_mod
from plate_daemon import lineage
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.machine import get_run

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
HOUR_S = 3600.0

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent, so the take passes its gates."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        return self.sent[result_url[len(RESULT_SCHEME) :]]


@pytest.fixture
def api(plate_env, monkeypatch):
    """(client, auth headers, settings) on a daemon whose packets ingest inline."""
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    with TestClient(create_app(settings, EchoEngine()), base_url=BASE) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        yield c, {"Authorization": f"Bearer {settings.token}"}, settings


def _setup(c: TestClient, auth: dict, nodes: tuple[str, ...] = ("e", "e2")) -> str:
    """Project + room_a ingested + Plate -> an Edit per name in `nodes` on the fake engine. The packet id."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    graph = [{"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}]
    graph += [{"id": n, "kind": "Edit", "label": n, "params": {"engine": "fake"}} for n in nodes]
    edges = [{"id": f"p->{n}", "source": "p", "target": n} for n in nodes]
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": graph, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _run(c: TestClient, auth: dict, node: str) -> str:
    """One auto-approved take of `node` run to DONE; its run id."""
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/{node}/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "DONE"
    return run_id


def _two_takes(c: TestClient, auth: dict) -> tuple[str, str]:
    _setup(c, auth)
    return _run(c, auth, "e"), _run(c, auth, "e2")


def _one_take(c: TestClient, auth: dict) -> str:
    _setup(c, auth, ("e",))
    return _run(c, auth, "e")


def _exports(settings: Settings) -> list[dict]:
    led = Ledger(settings.projects_dir / PROJECT)
    assert led.verify() == (True, None)
    return [r for r in led.records() if r["kind"] == "export"]


def _export(c: TestClient, auth: dict, run_id: str, out: Path, name: str = "take", fmt: str = "png16"):
    body = {"out_dir": str(out), "format": fmt, "name": name}
    return c.post(f"/api/v0/runs/{run_id}/export", json=body, headers=auth)


def _listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def _barrier_at_claim(monkeypatch, module, parties: int, suffixes: tuple[str, ...]) -> None:
    """Hold every caller of `module.claim` on a path with one of `suffixes` until `parties` have arrived: they
    all race the claim with their file ready."""
    real = export_mod.claim
    barrier = threading.Barrier(parties, timeout=60)

    def claim(path):
        if Path(path).suffix in suffixes:
            barrier.wait()
        return real(path)

    monkeypatch.setattr(module, "claim", claim)


def _old(path: Path, age_s: float = 2 * HOUR_S) -> Path:
    path.write_bytes(b"partial")
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


# ---------------------------------------------------------------------------- (1) the claim race


@needs_room_a
def test_eight_concurrent_exports_of_two_runs_to_one_name_have_exactly_one_winner(api, tmp_path, monkeypatch):
    c, auth, settings = api
    a, b = _two_takes(c, auth)
    out = tmp_path / "client"
    _barrier_at_claim(monkeypatch, export_mod, 8, (".png",))
    with ThreadPoolExecutor(8) as pool:
        answers = list(pool.map(lambda rid: _export(c, auth, rid, out), [a, b] * 4))
    codes = sorted(r.status_code for r in answers)
    assert codes == [200] + [409] * 7, [r.text for r in answers]
    losers = [r.json() for r in answers if r.status_code == 409]
    assert all(x["error"] == "not_exportable" and "never overwrite" in x["detail"] for x in losers), losers
    (won,) = [r.json() for r in answers if r.status_code == 200]
    assert "repeat" not in won
    assert _listing(out) == ["take.gate.json", "take.png"]  # no claim, temp or .part left by a loser
    sidecar = json.loads((out / "take.gate.json").read_text(encoding="utf-8"))["export"]
    assert lineage.read(out / "take.png").run_id == sidecar["run_id"] == won["run_id"]
    assert sidecar["files"] == ["take.png"]
    (rec,) = _exports(settings)
    assert rec["run_id"] == won["run_id"] and rec["data"]["dest_name"] == "take.png"


@needs_room_a
def test_an_unforced_race_keeps_one_file_paired_with_its_sidecar(api, tmp_path):
    """Without the barrier the interleaving is whatever the scheduler makes; whatever it is, one export wins,
    a late export of the winner's own run may be a byte-identical repeat, and nothing else lands."""
    c, auth, settings = api
    a, b = _two_takes(c, auth)
    out = tmp_path / "client"
    with ThreadPoolExecutor(8) as pool:
        answers = list(pool.map(lambda rid: _export(c, auth, rid, out), [a, b] * 4))
    assert {r.status_code for r in answers} <= {200, 409}, [r.text for r in answers]
    firsts = [r.json() for r in answers if r.status_code == 200 and not r.json().get("repeat")]
    repeats = [r.json() for r in answers if r.status_code == 200 and r.json().get("repeat")]
    assert len(firsts) == 1, [r.text for r in answers]
    winner = firsts[0]["run_id"]
    assert all(x["run_id"] == winner for x in repeats)
    assert all(r.json()["error"] == "not_exportable" for r in answers if r.status_code == 409)
    assert _listing(out) == ["take.gate.json", "take.png"]
    sidecar = json.loads((out / "take.gate.json").read_text(encoding="utf-8"))["export"]
    assert lineage.read(out / "take.png").run_id == sidecar["run_id"] == winner
    recs = _exports(settings)
    assert [r["run_id"] for r in recs if not r["data"].get("repeat")] == [winner]
    assert len(recs) == 1 + len(repeats)


@needs_room_a
def test_two_runs_racing_one_name_in_two_formats_never_share_a_sidecar(api, tmp_path, monkeypatch):
    """A png16 of one run and a tiff16 of another under one name: each wins its own image claim, and the
    sidecar claim decides; the loser gives its image name back."""
    c, auth, _ = api
    a, b = _two_takes(c, auth)
    out = tmp_path / "client"
    _barrier_at_claim(monkeypatch, export_mod, 2, (".png", ".tif"))
    jobs = [(a, "png16"), (b, "tiff16")]
    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(lambda job: _export(c, auth, job[0], out, fmt=job[1]), jobs))
    assert sorted(r.status_code for r in answers) == [200, 409], [r.text for r in answers]
    (won,) = [r.json() for r in answers if r.status_code == 200]
    image = Path(won["path"])
    assert _listing(out) == sorted(["take.gate.json", image.name])
    sidecar = json.loads((out / "take.gate.json").read_text(encoding="utf-8"))["export"]
    assert lineage.read(image).run_id == sidecar["run_id"] == won["run_id"]
    assert sidecar["files"] == [image.name]


# ---------------------------------------------------------------------------- (2) repeats


@needs_room_a
def test_the_same_run_exported_twice_to_one_name_is_a_repeat_and_another_run_is_refused(api, tmp_path):
    c, auth, settings = api
    a, b = _two_takes(c, auth)
    out = tmp_path / "client"
    r = _export(c, auth, a, out)
    assert r.status_code == 200 and "repeat" not in r.json(), r.text
    png = out / "take.png"
    before = (_listing(out), png.read_bytes(), png.stat().st_mtime_ns, (out / "take.gate.json").read_bytes())
    r = _export(c, auth, a, out)
    assert r.status_code == 200, r.text
    assert r.json()["repeat"] is True and Path(r.json()["path"]) == png
    after = (_listing(out), png.read_bytes(), png.stat().st_mtime_ns, (out / "take.gate.json").read_bytes())
    assert after == before  # no new file, nothing rewritten, the sidecar untouched
    recs = [x for x in _exports(settings) if x["run_id"] == a]
    assert len(recs) == 2
    assert "repeat" not in recs[0]["data"] and recs[1]["data"]["repeat"] is True
    r = _export(c, auth, b, out)
    assert r.status_code == 409 and r.json()["error"] == "not_exportable", r.text
    assert _listing(out) == before[0] and png.read_bytes() == before[1]


@needs_room_a
def test_a_file_whose_sidecar_is_gone_is_not_adopted_as_a_repeat(api, tmp_path):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"
    assert _export(c, auth, a, out).status_code == 200
    (out / "take.gate.json").unlink()
    r = _export(c, auth, a, out)
    assert r.status_code == 409 and "already exists" in r.json()["detail"], r.text
    assert _listing(out) == ["take.png"]


# ---------------------------------------------------------------------------- (3) the writable precheck


@posix_only
@needs_room_a
def test_a_project_whose_gates_folder_is_read_only_refuses_every_export_with_503(api, tmp_path):
    c, auth, _ = api
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores the permission bits")
    a = _one_take(c, auth)
    gates = c.app.state.pipeline.project_dir(PROJECT) / "gates"
    assert gates.is_dir()
    out = tmp_path / "client"
    mode = gates.stat().st_mode
    os.chmod(gates, 0o500)
    try:
        r = _export(c, auth, a, out)
        assert r.status_code == 503, r.text
        assert r.json()["error"] == "not_exportable" and "not writable" in r.json()["detail"]
        r = c.post(
            f"/api/v0/projects/{PROJECT}/export",
            json={"run_ids": [a], "out_dir": str(out), "format": "png16"},
            headers=auth,
        )
        assert r.status_code == 503 and r.json()["error"] == "not_exportable", r.text
        r = c.post(f"/api/v0/runs/{a}/cutdown", json={"preset": "1:1", "out_dir": str(out)}, headers=auth)
        assert r.status_code == 503 and "not writable" in r.json()["detail"], r.text
        body = {"format": "exr_sequence", "out_dir": str(out)}
        r = c.post(f"/api/v0/runs/{a}/deliver", json=body, headers=auth)
        assert r.status_code == 503 and r.json()["error"] == "not_exportable", r.text
        assert _listing(out) == []  # nothing reached the client folder
    finally:
        os.chmod(gates, mode)
    assert _export(c, auth, a, out).status_code == 200  # writable again: it goes


def test_not_exportable_carries_its_status_and_the_precheck_names_the_folder(tmp_path):
    assert export_mod.NotExportable.status == 409 and export_mod.ProjectUnwritable.status == 503
    assert issubclass(export_mod.ProjectUnwritable, export_mod.NotExportable)
    export_mod.check_project_writable(tmp_path / "fresh")  # a folder that can be made is fine
    assert (tmp_path / "fresh" / "gates").is_dir() and _listing(tmp_path / "fresh" / "gates") == []
    blocker = tmp_path / "blocker.txt"  # a file where the project folder should be: nothing below it opens
    blocker.write_text("not a folder\n", encoding="utf-8", newline="\n")
    with pytest.raises(export_mod.ProjectUnwritable, match="gates is not writable: nothing was exported"):
        export_mod.check_project_writable(blocker)


# ---------------------------------------------------------------------------- (4) name patterns


@needs_room_a
@pytest.mark.parametrize(
    "pattern", ["{n:>900000000}", "{n!r}", "{n.real}", "{n[0]}", "{}", "{n:{run}}", "{n"]
)
def test_a_name_pattern_with_a_spec_conversion_or_lookup_is_refused_at_once(api, tmp_path, pattern):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"
    body = {"run_ids": [a], "out_dir": str(out), "format": "png16", "name_pattern": pattern}
    t0 = time.perf_counter()
    r = c.post(f"/api/v0/projects/{PROJECT}/export", json=body, headers=auth)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 422 and r.json()["error"] == "bad_name_pattern", r.text
    assert elapsed < 0.2, elapsed
    assert not out.exists()


@needs_room_a
def test_a_plain_name_pattern_still_names_the_file(api, tmp_path):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"
    body = {"run_ids": [a], "out_dir": str(out), "format": "png16", "name_pattern": "{node}-{n}"}
    r = c.post(f"/api/v0/projects/{PROJECT}/export", json=body, headers=auth)
    assert r.status_code == 200 and r.json()["refused"] == [], r.text
    assert [Path(e["path"]).name for e in r.json()["exported"]] == ["e-001.png"]
    # the batch answer and its ledger record say so when a run is exported again byte for byte
    r = c.post(f"/api/v0/projects/{PROJECT}/export", json=body, headers=auth)
    assert r.status_code == 200 and r.json()["exported"][0]["repeat"] is True, r.text


# ---------------------------------------------------------------------------- (5) kill -9 leftovers


@needs_room_a
def test_stale_part_and_temp_siblings_of_the_name_go_with_the_next_export(api, tmp_path):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"
    out.mkdir()
    stale = [
        _old(out / ".take.png.0123456789ab.png.part"),  # the image writer's own temp of the export's temp
        _old(out / ".take.png.0123456789ab.png"),  # the export's temp image
        _old(out / "..take.png.0123456789ab.png.abcdef012345.tmp"),  # lineage.embed's temp of it
        _old(out / ".take.gate.json.0123456789ab.tmp"),  # the sidecar's temp
    ]
    young = out / ".take.png.fedcba987654.png.part"  # another export of the name may be writing it now
    young.write_bytes(b"in flight")
    other = _old(out / ".other.png.0123456789ab.png.part")  # another name's leftover is not ours to remove
    keeper = _old(out / "notes.txt")
    r = _export(c, auth, a, out)
    assert r.status_code == 200, r.text
    assert not any(p.exists() for p in stale)
    assert young.exists() and other.exists() and keeper.exists()
    assert _listing(out) == sorted(
        [".other.png.0123456789ab.png.part", young.name, "notes.txt", "take.gate.json", "take.png"]
    )


def test_sweep_stale_matches_only_hidden_temp_siblings_of_the_names(tmp_path):
    names = ("shot.tif", "shot.gate.json")
    gone = [
        _old(tmp_path / ".shot.tif.0123456789ab.tif"),
        _old(tmp_path / ".shot.tif.0123456789ab.tif.part"),
        _old(tmp_path / ".shot.gate.json.0123456789ab.tmp"),
    ]
    kept = [
        _old(tmp_path / "shot.tif"),  # the export itself
        _old(tmp_path / ".shot.tif.notahexstring.tif.part"),
        _old(tmp_path / ".shot.tif.0123456789ab.txt"),
        _old(tmp_path / ".shot.tiff.0123456789ab.tif.part"),
    ]
    (tmp_path / ".shot.tif.0123456789ab.dir.part").mkdir()  # a folder is never removed
    removed = export_mod.sweep_stale(tmp_path, names)
    assert sorted(removed) == sorted(gone)
    assert not any(p.exists() for p in gone) and all(p.exists() for p in kept)
    assert export_mod.sweep_stale(tmp_path / "absent", names) == []


# ---------------------------------------------------------------------------- failures after the claim


@needs_room_a
def test_a_failure_after_the_claim_gives_the_name_back_and_keeps_a_shared_sidecar(api, tmp_path, monkeypatch):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"
    pl = c.app.state.pipeline
    real = export_mod.replace_with_retry

    def broken(src, dst, *args, **kwargs):
        raise PermissionError(13, "held by a scanner", str(dst))

    with c.app.state.index.project(PROJECT) as conn:
        run = get_run(conn, a)
        kw = {"conn": conn, "cas": c.app.state.cas_for(PROJECT), "project_dir": pl.project_dir(PROJECT)}
        monkeypatch.setattr(export_mod, "replace_with_retry", broken)
        with pytest.raises(PermissionError):
            export_mod.export(run, "png16", out, name="take", **kw)
        assert _listing(out) == []  # no empty claim, no sidecar, no temp
        monkeypatch.setattr(export_mod, "replace_with_retry", real)
        export_mod.export(run, "png16", out, name="take", **kw)
        shared = (out / "take.gate.json").read_bytes()
        monkeypatch.setattr(export_mod, "replace_with_retry", broken)
        with pytest.raises(PermissionError):
            export_mod.export(run, "tiff16", out, name="take", **kw)
        assert _listing(out) == ["take.gate.json", "take.png"]
        assert (out / "take.gate.json").read_bytes() == shared  # it does not name a tif that never landed


# ---------------------------------------------------------------------------- (6) cutdowns


@needs_room_a
def test_six_concurrent_cutdowns_to_one_name_write_one_file(api, tmp_path, monkeypatch):
    c, auth, settings = api
    a = _one_take(c, auth)
    out = tmp_path / "social"
    _barrier_at_claim(monkeypatch, cutdown_mod, 6, (".png",))
    body = {"preset": "1:1", "out_dir": str(out), "name": "square"}
    with ThreadPoolExecutor(6) as pool:
        answers = list(
            pool.map(lambda _i: c.post(f"/api/v0/runs/{a}/cutdown", json=body, headers=auth), range(6))
        )
    assert sorted(r.status_code for r in answers) == [200] + [409] * 5, [r.text for r in answers]
    assert all(r.json()["error"] == "exists" for r in answers if r.status_code == 409)
    assert _listing(out) == ["square.cutdown.json", "square.png"]
    assert lineage.read(out / "square.png").run_id == a
    assert len([r for r in _exports(settings) if r["data"].get("cutdown")]) == 1


# ---------------------------------------------------------------------------- (7) fullmatch names


@needs_room_a
def test_a_name_ending_in_a_newline_is_refused_everywhere(api, tmp_path):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"
    r = _export(c, auth, a, out, name="nlx\n")
    assert r.status_code == 422 and r.json()["error"] == "bad_name", r.text
    body = {"format": "exr_sequence", "out_dir": str(out), "name": "nlx\n"}
    r = c.post(f"/api/v0/runs/{a}/deliver", json=body, headers=auth)
    assert r.status_code == 422 and r.json()["error"] == "bad_name", r.text
    r = c.post(
        f"/api/v0/runs/{a}/cutdown",
        json={"preset": "1:1", "out_dir": str(out), "name": "nlx\n"},
        headers=auth,
    )
    assert r.status_code == 422 and r.json()["error"] == "bad_name", r.text
    # the batch names files with a pattern, and a pattern that names no file is refused before the batch runs
    body = {"run_ids": [a], "out_dir": str(out), "format": "png16", "name_pattern": "nlx\n"}
    r = c.post(f"/api/v0/projects/{PROJECT}/export", json=body, headers=auth)
    assert r.status_code == 422 and r.json()["error"] == "bad_name_pattern", r.text
    assert not out.exists()
