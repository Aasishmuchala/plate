"""Wave 13: in-flight markers, so a kill -9's leftovers are swept on the next export (router plan T153, decision 87),
and the delivery writers claim their names (Wave 12 gap, `docs/VERIFICATION.md`).

- `fsutil.in_flight(marker)` holds an exclusive lock on a marker file; `is_in_flight(marker)` is False for a missing
  marker, True while another holder has the lock, False for an unheld one (the file stays for the caller).
- `export.export` writes under `.<name>.<tag><suffix>` while it holds `.<name>.<tag>.lock`. `sweep_stale` keeps a
  sibling whose marker is held, removes one whose marker is free whatever its age (the retry seconds after a
  kill -9 cleans up), and keeps Wave 12's one-hour rule for a sibling with no marker. The kill -9 is real here: a
  child process takes the marker, writes the leftovers and is killed; the OS drops its lock and nothing else.
- A cutdown stages under the same shape and sweeps its own name's leftovers.
- The delivery writers (`guard.embed_and_place`, `fcpxml.write`, `video.exr_sequence`) claim the destination with
  `export.claim` instead of `exists()` then replace: of any number of deliveries racing to one name exactly one
  lands, the others raise `FileExistsError` (a route's 409 `exists`). The races are forced with a barrier in front
  of the claim.

The route tests run the real pipeline on `fixtures/packets/room_a` with an engine that echoes the plate, as
`test_stress_wave12_export.py` does. No client data is read.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plate_daemon import cutdown as cutdown_mod
from plate_daemon import export as export_mod
from plate_daemon import lineage
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.delivery import fcpxml, guard
from plate_daemon.delivery import video as video_mod
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.fsutil import in_flight, is_in_flight
from plate_daemon.lineage import Lineage

ROOT = Path(__file__).resolve().parents[2]
DAEMON_DIR = ROOT / "daemon"
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
HOUR_S = 3600.0
TAG = "0123456789ab"
OTHER_TAG = "fedcba987654"

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


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


def _setup(c: TestClient, auth: dict, nodes: tuple[str, ...]) -> None:
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


def _run(c: TestClient, auth: dict, node: str) -> str:
    """One auto-approved take of `node` run to DONE; its run id."""
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/{node}/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "DONE"
    return run_id


def _one_take(c: TestClient, auth: dict) -> str:
    _setup(c, auth, ("e",))
    return _run(c, auth, "e")


def _two_takes(c: TestClient, auth: dict) -> tuple[str, str]:
    _setup(c, auth, ("e", "e2"))
    return _run(c, auth, "e"), _run(c, auth, "e2")


def _export(c: TestClient, auth: dict, run_id: str, out: Path, name: str = "take", fmt: str = "png16"):
    body = {"out_dir": str(out), "format": fmt, "name": name}
    return c.post(f"/api/v0/runs/{run_id}/export", json=body, headers=auth)


def _listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def _aged(path: Path, age_s: float, data: bytes = b"partial") -> Path:
    path.write_bytes(data)
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


def _barrier_at_claim(monkeypatch, module, parties: int, suffixes: tuple[str, ...]) -> None:
    """Hold every caller of `module.claim` on a path with one of `suffixes` until `parties` have arrived."""
    real = export_mod.claim
    barrier = threading.Barrier(parties, timeout=60)

    def claim(path):
        if Path(path).suffix in suffixes:
            barrier.wait()
        return real(path)

    monkeypatch.setattr(module, "claim", claim)


# A child that holds an in-flight marker and writes the leftovers of an export, the way a dying export would.
_WRITER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from plate_daemon.fsutil import in_flight
    out, name, tag = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
    suffix = Path(name).suffix
    with in_flight(out / f".{name}.{tag}.lock"):
        (out / f".{name}.{tag}{suffix}.part").write_bytes(b"partial")
        (out / f".{name}.{tag}{suffix}").write_bytes(b"half an image")
        import os
        print("ready", os.getpid(), flush=True)
        time.sleep(120)
    """
)


def _spawn_writer(tmp_path: Path, out: Path, name: str, tag: str) -> subprocess.Popen:
    script = tmp_path / "writer.py"
    script.write_text(_WRITER, encoding="utf-8", newline="\n")
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-I", str(script), str(DAEMON_DIR), str(out), name, tag],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    word, pid = proc.stdout.readline().split()
    assert word == "ready", "the child writer did not start"
    proc.writer_pid = int(pid)  # type: ignore[attr-defined]  # the process holding the lock (not a launcher)
    return proc


# ---------------------------------------------------------------------------- (4) the marker helpers


def test_is_in_flight_on_missing_held_and_unheld_markers(tmp_path):
    marker = tmp_path / f".take.png.{TAG}.lock"
    assert (
        is_in_flight(marker) is False and not marker.exists()
    )  # a missing marker is not created by the probe
    with in_flight(marker) as held:
        assert held == marker and marker.is_file()
        assert is_in_flight(marker) is True
    assert not marker.exists()  # released and unlinked on the way out
    marker.write_bytes(b"")  # a marker nobody holds: the writer died
    assert is_in_flight(marker) is False
    assert marker.is_file()  # left for the caller to remove
    assert is_in_flight(marker) is False  # and the probe released it again


def test_in_flight_releases_and_unlinks_when_the_block_raises(tmp_path):
    marker = tmp_path / f".take.png.{TAG}.lock"
    with pytest.raises(RuntimeError, match="boom"), in_flight(marker):
        raise RuntimeError("boom")
    assert not marker.exists() and is_in_flight(marker) is False


def test_a_second_writer_on_one_marker_is_refused_but_a_probe_only_delays_it(tmp_path):
    marker = tmp_path / f".take.png.{TAG}.lock"
    with in_flight(marker):
        # portalocker words a held lock as "...lock..." on POSIX and "[Errno 13] Permission denied" on Windows
        with pytest.raises(Exception, match="(?i)lock|permission denied|errno 13"), in_flight(marker):
            pass
    # a sweeper's probe holds the lock for an instant while a writer is starting: the writer waits it out
    import portalocker

    probe = portalocker.Lock(marker, mode="a", flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
    probe.acquire()
    threading.Timer(0.05, probe.release).start()
    with in_flight(marker):
        assert is_in_flight(marker) is True
    assert not marker.exists()


def test_a_killed_writer_stops_being_in_flight_without_anyone_releasing_it(tmp_path):
    out = tmp_path / "client"
    proc = _spawn_writer(tmp_path, out, "take.png", TAG)
    marker = out / f".take.png.{TAG}.lock"
    try:
        assert is_in_flight(marker) is True  # alive: another process holds it
    finally:
        # Kill the process that holds the lock. On Windows proc is a venv launcher whose child does the
        # work, so proc.kill() alone would leave the lock held; os.kill on the real pid is TerminateProcess
        # there and SIGKILL on POSIX: no cleanup runs either way.
        os.kill(proc.writer_pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)  # type: ignore[attr-defined]
        proc.kill()
        proc.wait(timeout=30)
        proc.stdout.close()
    assert marker.is_file()  # the dead writer's marker is still there ...
    assert is_in_flight(marker) is False  # ... and nobody holds it


# ---------------------------------------------------------------------------- sweep_stale and the marker rule


def test_sweep_stale_follows_the_marker_not_the_age(tmp_path):
    names = ("take.png", "take.gate.json")
    # a dead writer: unheld marker, young siblings. Everything of the tag goes, the marker too.
    dead = [
        _aged(tmp_path / f".take.png.{TAG}.png.part", 5),
        _aged(tmp_path / f".take.png.{TAG}.png", 5),
        _aged(tmp_path / f"..take.png.{TAG}.png.abcdef012345.tmp", 5),
        _aged(tmp_path / f".take.png.{TAG}.lock", 5, b""),
    ]
    # a live writer: held marker, old siblings. Everything stays.
    live_marker = tmp_path / f".take.png.{OTHER_TAG}.lock"
    live = [
        _aged(tmp_path / f".take.png.{OTHER_TAG}.png.part", 2 * HOUR_S),
        _aged(tmp_path / f".take.png.{OTHER_TAG}.png", 2 * HOUR_S),
    ]
    # no marker: Wave 12's one-hour rule
    markerless_old = _aged(tmp_path / ".take.gate.json.111111111111.tmp", 2 * HOUR_S)
    markerless_young = _aged(tmp_path / ".take.gate.json.222222222222.tmp", 10)
    # a marker alone that nobody holds: a dead writer's, gone whatever its age (T162: a real kill -9 in the
    # export's first instant leaves only the marker, and the retry seconds later must clean it up)
    orphan_young = _aged(tmp_path / ".take.png.333333333333.lock", 10, b"")
    orphan_old = _aged(tmp_path / ".take.png.444444444444.lock", 2 * HOUR_S, b"")
    other_name = _aged(tmp_path / f".other.png.{TAG}.png.part", 5)
    keeper = _aged(tmp_path / "notes.txt", 2 * HOUR_S)
    with in_flight(live_marker):
        removed = export_mod.sweep_stale(tmp_path, names)
        assert live_marker.is_file() and is_in_flight(live_marker) is True
    assert sorted(removed) == sorted([*dead, markerless_old, orphan_young, orphan_old])
    assert all(p.exists() for p in [*live, markerless_young, other_name, keeper])
    assert not any(p.exists() for p in [*dead, markerless_old, orphan_young, orphan_old])


# ---------------------------------------------------------------------------- (1) (2) (3) through the export route


@needs_room_a
@pytest.mark.parametrize(("fmt", "suffix"), [("png16", ".png"), ("exr", ".exr")])
def test_a_kill_minus_nine_leftover_is_swept_by_an_export_seconds_later(api, tmp_path, fmt, suffix):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"
    out.mkdir()
    name = f"take{suffix}"
    leftovers = [
        _aged(out / f".{name}.{TAG}{suffix}.part", 5),  # the image writer's temp of the export's temp
        _aged(out / f".{name}.{TAG}{suffix}", 5),  # the export's temp image
        _aged(out / f".{name}.{TAG}.lock", 5, b""),  # the marker nobody holds
    ]
    r = _export(c, auth, a, out, fmt=fmt)
    assert r.status_code == 200, r.text
    assert not any(p.exists() for p in leftovers)
    assert _listing(out) == sorted([f"take{suffix}", "take.gate.json"])


@needs_room_a
def test_a_live_exports_marker_keeps_its_files_and_a_dead_ones_are_swept(api, tmp_path):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"
    proc = _spawn_writer(tmp_path, out, "take.png", TAG)  # a real second process, mid-export
    mine = [out / f".take.png.{TAG}.png.part", out / f".take.png.{TAG}.png", out / f".take.png.{TAG}.lock"]
    for p in mine[:2]:
        then = time.time() - 5
        os.utime(p, (then, then))
    try:
        r = _export(c, auth, a, out)
        assert r.status_code == 200, r.text
        assert all(p.exists() for p in mine)  # held by a live process: not ours to touch
        assert _listing(out) == sorted(["take.png", "take.gate.json", *(p.name for p in mine)])
    finally:
        proc.kill()
        proc.wait(timeout=30)
        proc.stdout.close()
    r = _export(c, auth, a, out)  # seconds later, the writer dead: the same name exports again (a repeat) ...
    assert r.status_code == 200 and r.json()["repeat"] is True, r.text
    assert not any(p.exists() for p in mine)  # ... and its leftovers are gone, young as they are
    assert _listing(out) == ["take.gate.json", "take.png"]


@needs_room_a
def test_a_marker_held_by_another_thread_keeps_its_young_sibling_through_an_export(api, tmp_path):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"
    out.mkdir()
    marker = out / f".take.png.{TAG}.lock"
    sibling = _aged(out / f".take.png.{TAG}.png", 5)
    held, release = threading.Event(), threading.Event()

    def writer() -> None:
        with in_flight(marker):
            held.set()
            release.wait(60)

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert held.wait(30)
        r = _export(c, auth, a, out)
        assert r.status_code == 200, r.text
        assert sibling.exists() and marker.exists()
    finally:
        release.set()
        t.join(30)
    assert not marker.exists()  # the thread's own exit removed it
    assert (out / "take.png").is_file()


@needs_room_a
def test_a_marker_less_temp_follows_the_one_hour_rule(api, tmp_path):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"
    out.mkdir()
    young = _aged(out / ".take.png.111111111111.tmp", 10)
    old = _aged(out / ".take.png.222222222222.tmp", 2 * HOUR_S)
    r = _export(c, auth, a, out)
    assert r.status_code == 200, r.text
    assert young.exists() and not old.exists()


@needs_room_a
def test_an_export_leaves_no_marker_or_temp_behind(api, tmp_path):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"
    for fmt, name in (("png16", "p"), ("tiff16", "t"), ("exr", "x")):
        assert _export(c, auth, a, out, name=name, fmt=fmt).status_code == 200
    assert _listing(out) == sorted(["p.png", "p.gate.json", "t.tif", "t.gate.json", "x.exr", "x.gate.json"])
    out2 = tmp_path / "client2"
    r = _export(c, auth, a, out2, fmt="gif")  # a refusal before the write: the folder is not even made
    assert r.status_code == 422 and not out2.exists()


@needs_room_a
def test_a_failed_export_removes_its_marker(api, tmp_path, monkeypatch):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "client"

    def broken(src, dst, *args, **kwargs):
        raise PermissionError(13, "held by a scanner", str(dst))

    monkeypatch.setattr(export_mod, "replace_with_retry", broken)
    with c.app.state.index.project(PROJECT) as conn:
        from plate_daemon.machine import get_run

        run = get_run(conn, a)
        kw = {
            "conn": conn,
            "cas": c.app.state.cas_for(PROJECT),
            "project_dir": c.app.state.pipeline.project_dir(PROJECT),
        }
        with pytest.raises(PermissionError):
            export_mod.export(run, "png16", out, name="take", **kw)
    assert _listing(out) == []  # no claim, no sidecar, no temp, no marker


# ---------------------------------------------------------------------------- cutdowns


@needs_room_a
def test_a_cutdown_sweeps_a_dead_cutdowns_leftovers_and_keeps_a_live_ones(api, tmp_path):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "social"
    out.mkdir()
    dead = [
        _aged(out / f".square.png.{TAG}.png", 5),  # a cutdown's staged file
        _aged(out / f".square.png.{TAG}.lock", 5, b""),  # its marker, unheld
    ]
    live = [
        _aged(out / f".square.png.{OTHER_TAG}.png", 5),
        _aged(out / f".square.png.{OTHER_TAG}.lock", 5, b""),
    ]
    body = {"preset": "1:1", "out_dir": str(out), "name": "square"}
    with in_flight(live[1]):
        r = c.post(f"/api/v0/runs/{a}/cutdown", json=body, headers=auth)
        assert r.status_code == 200, r.text
        assert not any(p.exists() for p in dead)
        assert all(p.exists() for p in live)
    assert (out / "square.png").is_file() and lineage.read(out / "square.png").run_id == a


@needs_room_a
def test_a_cutdown_leaves_only_its_file_and_sidecar(api, tmp_path):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "social"
    body = {"preset": "1:1", "out_dir": str(out), "name": "square"}
    assert c.post(f"/api/v0/runs/{a}/cutdown", json=body, headers=auth).status_code == 200
    assert _listing(out) == ["square.cutdown.json", "square.png"]
    r = c.post(f"/api/v0/runs/{a}/cutdown", json=body, headers=auth)
    assert r.status_code == 409  # never overwrites
    assert _listing(out) == ["square.cutdown.json", "square.png"]


# ---------------------------------------------------------------------------- deliveries claim their names


def _lineage(run_id: str) -> Lineage:
    return Lineage(
        project_id="proj-1",
        node_id="node-7",
        run_id=run_id,
        packet_id="room_a",
        engine_id="nano-banana-2.1",
        engine_version="2.1.0",
        gate_report="gates/run-42.1.json",
    )


def _png(path: Path) -> Path:
    import numpy as np

    from plate_pixels import imageio

    imageio.write_png8(path, np.full((8, 8, 3), 90, np.uint8))
    return path


def test_embed_and_place_has_exactly_one_winner_among_eight_racing_deliveries(tmp_path, monkeypatch):
    final = tmp_path / "delivered.png"
    staged = [_png(tmp_path / f".stage{i}.png") for i in range(8)]
    _barrier_at_claim(monkeypatch, guard, 8, (".png",))

    def deliver(i: int) -> str:
        try:
            guard.embed_and_place(staged[i], final, _lineage(f"run-{i}"))
        except FileExistsError as e:
            assert "never overwrites" in str(e)
            return "lost"
        return f"run-{i}"

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(deliver, range(8)))
    winners = [r for r in results if r != "lost"]
    assert len(winners) == 1 and results.count("lost") == 7, results
    assert final.stat().st_size > 0
    assert lineage.read(final).run_id == winners[0]  # the bytes are the winner's own, not a mix
    assert not staged[int(winners[0].split("-")[1])].exists()  # the winner's staged file was moved into place
    assert sum(p.exists() for p in staged) == 7  # a loser's staged file is the caller's to remove


def test_embed_and_place_gives_the_name_back_when_the_move_fails(tmp_path, monkeypatch):
    final = tmp_path / "delivered.png"
    staged = _png(tmp_path / ".stage.png")

    def broken(src, dst, *args, **kwargs):
        raise PermissionError(13, "held by a scanner", str(dst))

    monkeypatch.setattr(guard, "replace_with_retry", broken)
    with pytest.raises(PermissionError):
        guard.embed_and_place(staged, final, _lineage("run-1"))
    assert not final.exists()  # no empty claim left to block the name


def _timeline(tmp_path: Path) -> dict:
    d = tmp_path / "media"
    return {
        "name": "reel",
        "fps": Fraction(24000, 1001),
        "width": 3840,
        "height": 2160,
        "clips": [{"path": d / "a.mov", "start_s": 0, "duration_s": 1}],
    }


def test_two_fcpxml_deliveries_racing_to_one_name_have_one_winner(tmp_path, monkeypatch):
    out = tmp_path / "reel.fcpxml"
    _barrier_at_claim(monkeypatch, fcpxml, 2, (".fcpxml",))
    timelines = [{**_timeline(tmp_path), "name": f"reel {i}"} for i in range(2)]

    def deliver(i: int) -> str:
        try:
            fcpxml.write(timelines[i], out)
        except FileExistsError:
            return "lost"
        return f"reel {i}"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(deliver, range(2)))
    assert sorted(results) == sorted(["lost", next(r for r in results if r != "lost")]), results
    (winner,) = [r for r in results if r != "lost"]
    assert f'name="{winner}"' in out.read_text(
        encoding="utf-8"
    )  # one document, not an overwrite by the other
    assert _listing(tmp_path) == ["reel.fcpxml"]


def test_a_failed_fcpxml_write_gives_the_name_back(tmp_path, monkeypatch):
    out = tmp_path / "reel.fcpxml"

    def broken(path, data):
        raise OSError(28, "no space left", str(path))

    monkeypatch.setattr(fcpxml, "write_bytes_atomic", broken)
    with pytest.raises(OSError, match="no space"):
        fcpxml.write(_timeline(tmp_path), out)
    assert not out.exists()


@needs_room_a
def test_two_deliveries_racing_to_one_name_over_the_route_have_one_winner(api, tmp_path, monkeypatch):
    c, auth, _ = api
    a, b = _two_takes(c, auth)
    out = tmp_path / "delivery"
    _barrier_at_claim(monkeypatch, video_mod, 2, (".exr",))
    body = {"format": "exr_sequence", "out_dir": str(out), "name": "still"}
    with ThreadPoolExecutor(2) as pool:
        answers = list(
            pool.map(lambda rid: c.post(f"/api/v0/runs/{rid}/deliver", json=body, headers=auth), [a, b])
        )
    assert sorted(r.status_code for r in answers) == [200, 409], [r.text for r in answers]
    (lost,) = [r.json() for r in answers if r.status_code == 409]
    assert lost["error"] == "exists" and "never overwrites" in lost["detail"], lost
    (won,) = [r.json() for r in answers if r.status_code == 200]
    assert won["path"] == str(out / "still.0001.exr")
    winner_run = a if answers[0].status_code == 200 else b
    assert lineage.read(out / "still.0001.exr").run_id == winner_run
    sidecar = json.loads((out / "still.gate.json").read_text(encoding="utf-8"))
    assert sidecar["lineage"]["run_id"] == winner_run  # the loser never reached the sidecar
    assert _listing(out) == ["still.0001.exr", "still.gate.json"]  # no claim or staged file of the loser


@needs_room_a
def test_a_delivery_onto_a_taken_name_is_still_409_exists(api, tmp_path):
    c, auth, _ = api
    a = _one_take(c, auth)
    out = tmp_path / "delivery"
    body = {"format": "exr_sequence", "out_dir": str(out), "name": "still"}
    assert c.post(f"/api/v0/runs/{a}/deliver", json=body, headers=auth).status_code == 200
    before = (out / "still.0001.exr").read_bytes()
    r = c.post(f"/api/v0/runs/{a}/deliver", json=body, headers=auth)
    assert r.status_code == 409 and r.json()["error"] == "exists", r.text
    assert (out / "still.0001.exr").read_bytes() == before
    assert _listing(out) == ["still.0001.exr", "still.gate.json"]


# ---------------------------------------------------------------------------- the seed's byte copy is gone


def test_the_dead_seed_copy_is_gone_and_the_claim_stays_shared():
    assert not hasattr(export_mod, "export_run")  # nothing called it; every export goes through `export`
    assert cutdown_mod.claim is export_mod.claim and guard.claim is export_mod.claim
    assert fcpxml.claim is export_mod.claim and video_mod.claim is export_mod.claim
