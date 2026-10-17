"""`plate review|import|deliver|timer|spend` and the retired CLI-only fake engine (router plan T78).

One module-scoped world: the committed Blender packet `fixtures/packets/room_a`, one DONE run made by
`plate run` on the daemon's own `FakeEngine(echo=True)` (`PLATE_FAKE_ENGINE=1`) under a throw-away PLATE_HOME.
A GATE_FAIL run is made when a test needs one by importing a copy of the plate moved 12 px (no engine, so it
costs milliseconds). Every call is `cli.main(argv, env=...)` in process with an explicit environment, so the
tests never read the developer's shell. `timer` talks to a real uvicorn daemon on an ephemeral loopback port
(`live_server`) with an injected clock. No client data is read.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import sqlite3
import struct
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import pytest

from plate_daemon import cli
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.index import Index
from plate_daemon.ledger import Ledger
from plate_daemon.projects import Project

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
NIGHT = "night_relight_changelist"
PROJECT_ID = "plate-fixture"  # room_a's manifest project_id
REASON = "client signed off the drift"
needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)

# Switches that would change what a test means if a developer's shell had them exported.
SCRUBBED = (
    "PLATE_LIVE",
    "PLATE_FAKE_ENGINE",
    "PLATE_ROUTER_MODE",
    "PLATE_DEBUG",
    "PLATE_DAEMON_URL",
    "PLATE_PORT",
)


def env_with(**extra: str) -> dict[str, str]:
    return {**{k: v for k, v in os.environ.items() if k not in SCRUBBED}, **extra}


def run_cli(env: dict[str, str], *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), env=env, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@dataclass
class World:
    env: dict[str, str]
    home: Path
    project: Path
    packet_id: str
    done_run: str  # a DONE run of the fake engine, quoted at 0.04 USD

    def cli(self, *argv: str) -> tuple[int, str, str]:
        return run_cli(self.env, *argv)

    def runs(self) -> dict[str, dict]:
        with Index(self.home).project(PROJECT_ID) as conn:
            return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM runs")}


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> World:
    if not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file():
        pytest.skip("fixtures/packets/room_a not built yet (T2)")
    base = tmp_path_factory.mktemp("cli_wave8")
    home = base / "home"
    mp = pytest.MonkeyPatch()
    for name in SCRUBBED:
        mp.delenv(name, raising=False)
    mp.setenv("PLATE_HOME", str(home))
    try:
        env = env_with(PLATE_HOME=str(home), PLATE_FAKE_ENGINE="1", PYTHONUTF8="1")
        code, out, err = run_cli(
            env, "run", NIGHT, "--project", str(base / "proj"), "--packet", str(ROOM_A), "--approve", "auto"
        )
        assert code == cli.EXIT_OK, (out, err)
        packet_id = re.search(r"packet (pkt_\w+) ingested", out).group(1)
        done = re.search(r"^run (run_\w+) DONE\b", out, re.M).group(1)
    finally:
        mp.undo()
    return World(env, home, base / "proj", packet_id, done)


def plate_png(tmp_path: Path, shift_px: int = 0, name: str = "take.png") -> Path:
    """room_a's display master as a 16-bit PNG, moved `shift_px` to the right (np.roll wraps; fine for drift)."""
    img = cv2.imread(str(ROOM_A / "display.tif"), cv2.IMREAD_UNCHANGED)
    assert img is not None and img.dtype.name == "uint16"
    if shift_px:
        import numpy as np

        img = np.roll(img, shift_px, axis=1)
    path = tmp_path / name
    assert cv2.imwrite(str(path), img)
    return path


def import_run(
    world: World, tmp_path: Path, *extra: str, shift_px: int = 0
) -> tuple[int, str, str, str | None]:
    code, out, err = world.cli(
        "import",
        str(plate_png(tmp_path, shift_px, f"take{shift_px}.png")),
        "--project",
        str(world.project),
        "--packet",
        world.packet_id,
        *extra,
    )
    m = re.search(r"^run (run_\w+) ", out, re.M)
    return code, out, err, (m.group(1) if m else None)


def png_widths(zip_path: Path) -> dict[str, int]:
    with zipfile.ZipFile(zip_path) as zf:
        return {n: struct.unpack(">I", zf.read(n)[16:20])[0] for n in zf.namelist() if n.endswith(".png")}


def set_params(world: World, run_id: str, params: dict) -> None:
    """Mark a run as previs the way a previs run is marked (`params.previs`)."""
    db = Index(world.home).db_path(PROJECT_ID)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE runs SET params = ? WHERE id = ?", (json.dumps(params), run_id))


# ---------------------------------------------------------------------------- the CLI-only fake is gone


def test_plate_fake_engine_is_the_daemons_fake_engine():
    assert not hasattr(cli, "CliFakeEngine")
    on = cli.fake_engine_from_env({"PLATE_FAKE_ENGINE": "1"})
    assert type(on) is FakeEngine and on.echo is True and on.shift_px == 0
    assert cli.fake_engine_from_env({"PLATE_FAKE_ENGINE": " TRUE "}).echo is True
    for off in ("", "0", "false", "No"):
        assert cli.fake_engine_from_env({"PLATE_FAKE_ENGINE": off}) is None
    drift = cli.fake_engine_from_env({"PLATE_FAKE_ENGINE": '{"shift_px": 6}'})
    assert type(drift) is FakeEngine and drift.echo is True and drift.shift_px == 6
    noise = cli.fake_engine_from_env({"PLATE_FAKE_ENGINE": '{"echo": false, "size": [8, 8]}'})
    assert noise.echo is False and noise.size == (8, 8)


def test_the_echo_comes_from_engine_fake_and_shifts_with_edge_replication():
    import base64

    import numpy as np

    eng = cli.fake_engine_from_env({"PLATE_FAKE_ENGINE": '{"shift_px": 2}'})
    row = np.arange(1, 9, dtype=np.uint8)
    img = np.repeat(row[None, :, None], 3, axis=2)
    png = bytes(cv2.imencode(".png", img)[1])
    job = eng.submit({"inputs": {"image": {"b64": base64.b64encode(png).decode()}}}, "k")
    got = cv2.imdecode(np.frombuffer(eng.fetch(f"fake://results/{job}"), np.uint8), cv2.IMREAD_UNCHANGED)
    assert got[0, :, 0].tolist() == [1, 1, 1, 2, 3, 4, 5, 6]  # right by 2, the left edge repeated (no wrap)


def test_deliver_formats_are_the_routes_formats():
    from plate_daemon.routes import delivery

    assert cli.DELIVER_FORMATS == delivery.FORMATS


def test_the_parser_knows_the_new_commands():
    p = cli.build_parser()
    for argv in (
        ["review", "proj", "--runs", "a,b"],
        ["import", "f.png", "--project", "p", "--packet", "pkt"],
        ["deliver", "run_1", "--format", "hevc", "--to", "out"],
        ["timer", "start", "d1"],
        ["timer", "today", "--project", "p"],
        ["spend", "proj", "--since", "2026-10-01"],
    ):
        assert p.parse_args(argv).cmd == argv[0]
    for bad in (["timer"], ["deliver", "run_1", "--format", "mp3", "--to", "out"], ["import", "f.png"]):
        with pytest.raises(cli.CliExit) as e:
            p.parse_args(bad)
        assert e.value.code == cli.EXIT_USAGE


@needs_room_a
def test_smoke_still_passes_on_one_adapter(world):
    code, out, err = world.cli("smoke", "--engines", "flux-3-image")
    assert code == cli.EXIT_OK, (out, err)
    assert out.strip().splitlines()[-1] == "fixture smoke: 1 adapters ok"


# ---------------------------------------------------------------------------- plate review


@needs_room_a
def test_review_writes_a_bundle_with_the_page_and_no_proxy_wider_than_2048(world, tmp_path):
    out_dir = tmp_path / "bundle"
    code, out, err = world.cli("review", str(world.project), "--runs", world.done_run, "--out", str(out_dir))
    assert code == cli.EXIT_OK, (out, err)
    (zip_path,) = out_dir.glob("*.zip")
    assert str(zip_path) in out and "1 take(s)" in out and "watermark REVIEW" in out
    with zipfile.ZipFile(zip_path) as zf:
        assert any(n.endswith("index.html") for n in zf.namelist())
    widths = png_widths(zip_path)
    assert widths and max(widths.values()) <= 2048


@needs_room_a
def test_review_defaults_to_the_projects_review_folder_and_takes_a_watermark_and_width(world):
    code, out, err = world.cli(
        "review",
        PROJECT_ID,
        "--runs",
        f" {world.done_run} ,{world.done_run}",
        "--watermark",
        "DRAFT",
        "--max-width",
        "400",
    )
    assert code == cli.EXIT_OK, (out, err)
    zips = sorted((world.project / "review").glob("*.zip"))
    assert zips and "1 take(s)" in out and "watermark DRAFT" in out  # the repeated id is one take
    assert max(png_widths(zips[-1]).values()) <= 400


@needs_room_a
def test_review_refuses_a_gate_fail_take_and_previs_and_writes_nothing(world, tmp_path):
    code, _out, _err, failed = import_run(world, tmp_path, shift_px=12)
    assert code == cli.EXIT_GATE_FAIL and failed
    out_dir = tmp_path / "refused"
    code, out, err = world.cli("review", str(world.project), "--runs", failed, "--out", str(out_dir))
    assert code == cli.EXIT_REFUSED and "GATE_FAIL" in err and failed in err, (out, err)
    assert not out_dir.exists() or not list(out_dir.glob("*.zip"))

    code, _out, _err, previs = import_run(world, tmp_path)
    assert code == cli.EXIT_OK and previs
    set_params(world, previs, {"previs": True})
    code, out, err = world.cli(
        "review", str(world.project), "--runs", f"{world.done_run},{previs}", "--out", str(out_dir)
    )
    assert code == cli.EXIT_REFUSED and "PREVIS_NOT_DELIVERABLE" in err, (out, err)
    assert not out_dir.exists() or not list(out_dir.glob("*.zip"))


@needs_room_a
def test_review_usage_errors(world, tmp_path):
    code, _out, err = world.cli("review", str(world.project), "--runs", "run_nope")
    assert code == cli.EXIT_USAGE and "run_nope" in err
    code, _out, err = world.cli("review", str(world.project), "--runs", " , ")
    assert code == cli.EXIT_USAGE
    code, _out, err = world.cli("review", str(tmp_path / "no-project"), "--runs", world.done_run)
    assert code == cli.EXIT_USAGE and "not a PLATE project" in err
    code, _out, err = world.cli(
        "review", str(world.project), "--runs", world.done_run, "--watermark", "x" * 200
    )
    assert code == cli.EXIT_USAGE


# ---------------------------------------------------------------------------- plate import


@needs_room_a
def test_import_of_the_plate_itself_is_done_and_ledgered(world, tmp_path):
    code, out, err, run_id = import_run(world, tmp_path, "--note", "same pixels", "--cost-usd", "0.25")
    assert code == cli.EXIT_OK, (out, err)
    assert run_id and f"as run {run_id}" in out and f"run {run_id} DONE, gates pass" in out
    row = world.runs()[run_id]
    assert row["state"] == "DONE" and row["node_id"] is None
    records = [
        json.loads(line) for line in (world.project / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    (quoted,) = [r for r in records if r["kind"] == "quoted" and r.get("run_id") == run_id]
    assert quoted["data"]["imported"] is True and quoted["cost_usd_quoted"] == 0.25
    assert quoted["engine_id"] == "import:manual"
    (done,) = [r for r in records if r["kind"] == "done" and r.get("run_id") == run_id]
    assert done["data"]["imported"] is True
    assert (world.project / "gates" / f"{run_id}.1.json").is_file()
    assert Ledger(world.project).verify() == (True, None)


@needs_room_a
def test_import_of_a_moved_plate_ends_gate_fail_with_exit_2(world, tmp_path):
    code, out, err, run_id = import_run(world, tmp_path, "--source", "manual", shift_px=12)
    assert code == cli.EXIT_GATE_FAIL, (out, err)
    assert run_id and f"run {run_id} GATE_FAIL, gates fail" in out and "--override-reason" in out
    report = json.loads((world.project / "gates" / f"{run_id}.1.json").read_text(encoding="utf-8"))
    shift = next(c for c in report["checks"] if c["metric_id"] == "registration.shift_px")
    assert shift["status"] == "fail"
    assert world.runs()[run_id]["state"] == "GATE_FAIL"


@needs_room_a
def test_import_refusals_exit_with_the_right_code_and_leave_no_run(world, tmp_path):
    before = set(world.runs())
    # a file from a vendor the project's custody does not allow (grade D): 3
    code, _out, err, run_id = import_run(world, tmp_path, "--source", "higgsfield")
    assert code == cli.EXIT_REFUSED and run_id is None and "custody" in err
    # not the plate's size: 4
    small = tmp_path / "small.png"
    assert cv2.imwrite(str(small), cv2.resize(cv2.imread(str(plate_png(tmp_path))), (100, 50)))
    code, _out, err = world.cli(
        "import", str(small), "--project", str(world.project), "--packet", world.packet_id
    )
    assert code == cli.EXIT_USAGE and "100 x 50" in err
    # not a file, not an image, an unknown packet, a negative cost, an unknown project: 4
    code, _out, err = world.cli(
        "import", str(tmp_path / "nope.png"), "--project", str(world.project), "--packet", world.packet_id
    )
    assert code == cli.EXIT_USAGE and "is not a file" in err
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not an image at all")
    code, _out, err = world.cli(
        "import", str(junk), "--project", str(world.project), "--packet", world.packet_id
    )
    assert code == cli.EXIT_USAGE and "readable" in err
    code, _out, err = world.cli(
        "import", str(plate_png(tmp_path)), "--project", str(world.project), "--packet", "pkt_nope"
    )
    assert code == cli.EXIT_USAGE
    code, _out, err = world.cli(
        "import",
        str(plate_png(tmp_path)),
        "--project",
        str(world.project),
        "--packet",
        world.packet_id,
        "--cost-usd",
        "-1",
    )
    assert code == cli.EXIT_USAGE and "cost_usd" in err
    code, _out, err = world.cli(
        "import", str(plate_png(tmp_path)), "--project", "no-such-project", "--packet", world.packet_id
    )
    assert code == cli.EXIT_USAGE
    assert set(world.runs()) == before


@needs_room_a
def test_import_can_ingest_a_packet_folder_into_an_existing_project(world, tmp_path):
    code, out, err = world.cli(
        "import", str(plate_png(tmp_path)), "--project", str(world.project), "--packet", str(ROOM_A)
    )
    assert code == cli.EXIT_OK, (out, err)
    assert re.search(r"^run run_\w+ DONE", out, re.M)


# ---------------------------------------------------------------------------- plate deliver


@needs_room_a
def test_deliver_refuses_a_gate_fail_take_without_an_override_then_ships_it_on_the_record(world, tmp_path):
    _code, _out, _err, failed = import_run(world, tmp_path, shift_px=12)
    dest = tmp_path / "deliv"
    code, out, err = world.cli("deliver", failed, "--format", "exr_sequence", "--to", str(dest))
    assert code == cli.EXIT_REFUSED and "GATE_FAIL" in err, (out, err)
    assert not dest.exists() or not list(dest.iterdir())

    code, out, err = world.cli(
        "deliver", failed, "--format", "exr_sequence", "--to", str(dest), "--override-reason", "short"
    )
    assert code == cli.EXIT_REFUSED and "OVERRIDE_REASON_TOO_SHORT" in err

    code, out, err = world.cli(
        "deliver",
        failed,
        "--format",
        "exr_sequence",
        "--to",
        str(dest),
        "--override-reason",
        REASON,
        "--name",
        "shot_01",
    )
    assert code == cli.EXIT_OK, (out, err)
    assert (dest / "shot_01.0001.exr").is_file() and (dest / "shot_01.gate.json").is_file()
    assert f"override on the record: {REASON}" in out
    records = [
        json.loads(line) for line in (world.project / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    (override,) = [r for r in records if r["kind"] == "override" and r.get("run_id") == failed]
    assert override["by"] == "human" and REASON in json.dumps(override)
    export = [r for r in records if r["kind"] == "export" and r.get("run_id") == failed][-1]
    assert export["data"]["delivery"] is True and export["data"]["format"] == "exr_sequence"
    # never overwrites
    code, _out, err = world.cli(
        "deliver",
        failed,
        "--format",
        "exr_sequence",
        "--to",
        str(dest),
        "--override-reason",
        REASON,
        "--name",
        "shot_01",
    )
    assert code in (cli.EXIT_USAGE, cli.EXIT_ERROR) and "shot_01" in err


@needs_room_a
def test_deliver_refuses_previs(world, tmp_path):
    _code, _out, _err, run_id = import_run(world, tmp_path)
    set_params(world, run_id, {"previs": True})
    code, _out, err = world.cli("deliver", run_id, "--format", "exr_sequence", "--to", str(tmp_path / "d"))
    assert code == cli.EXIT_REFUSED and "PREVIS_NOT_DELIVERABLE" in err


@needs_room_a
def test_deliver_a_done_still_as_exr_and_refuses_clip_formats_with_usage(world, tmp_path):
    dest = tmp_path / "exr"
    code, out, err = world.cli("deliver", world.done_run, "--format", "exr_sequence", "--to", str(dest))
    assert code == cli.EXIT_OK, (out, err)
    assert (dest / f"{world.done_run}.0001.exr").is_file() and "delivered" in out
    code, _out, err = world.cli("deliver", world.done_run, "--format", "hevc", "--to", str(tmp_path / "x"))
    assert code == cli.EXIT_USAGE and "still" in err
    code, _out, err = world.cli("deliver", "run_nope", "--format", "hevc", "--to", str(tmp_path / "x"))
    assert code == cli.EXIT_USAGE and "run_nope" in err
    code, _out, err = world.cli(
        "deliver", world.done_run, "--format", "exr_sequence", "--to", str(tmp_path), "--name", "a:b"
    )
    assert code == cli.EXIT_USAGE and "delivery name" in err


# ---------------------------------------------------------------------------- plate spend


@needs_room_a
def test_spend_prints_the_quoted_total_of_the_fake_run(world, tmp_path):
    import_run(world, tmp_path, "--cost-usd", "2.5")  # declared, never submitted: not in the table
    code, out, err = world.cli("spend", str(world.project))
    assert code == cli.EXIT_OK, (out, err)
    lines = out.strip().splitlines()
    assert lines[0] == f"spend for {PROJECT_ID}"
    assert lines[1].split() == ["engine", "runs", "quoted", "USD", "billed", "USD", "accepted"]
    summary = Ledger(world.project).summarize()
    fake = summary["by_engine"]["fake"]
    assert fake["generations"] == 1 and fake["spent_usd_quoted"] == 0.04
    assert lines[2].split() == ["fake", "1", "0.0400", "0.0000", "0"]
    assert lines[3].split() == ["total", "1", "0.0400", "0.0000", "0"]
    imported = [
        r["cost_usd_quoted"]
        for r in Ledger(world.project).records()
        if r["kind"] == "quoted" and r["data"].get("imported") is True
    ]
    assert 2.5 in imported
    assert (
        lines[-1] == f"not in the table: {len(imported)} imported take(s), {sum(imported):.4f} USD declared"
    )
    # by project id, and a window that holds nothing
    code, out2, _err = world.cli("spend", PROJECT_ID)
    assert code == cli.EXIT_OK and out2.splitlines()[2:4] == out.splitlines()[2:4]
    code, out3, _err = world.cli("spend", PROJECT_ID, "--since", "2099-01-01")
    assert (
        code == cli.EXIT_OK
        and "fake" not in out3
        and out3.strip().splitlines()[-1].split()[:2] == ["total", "0"]
    )
    code, _out, err = world.cli("spend", PROJECT_ID, "--since", "not-a-date")
    assert code == cli.EXIT_USAGE and "--since" in err


# ---------------------------------------------------------------------------- plate timer


class Clock:
    """Local noon today, in UTC; the tests move it."""

    def __init__(self) -> None:
        self.t = (
            datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0).astimezone(UTC)
        )

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


def test_timer_records_minutes_against_a_live_daemon(live_server, plate_env):
    clock = Clock()
    with live_server(clock=clock) as (_s, url):
        env = env_with(PLATE_HOME=str(plate_env), PLATE_DAEMON_URL=url)
        code, out, err = run_cli(env, "timer", "start", "shot-12")
        assert code == cli.EXIT_OK and "timer started: shot-12" in out, (out, err)
        code, out, _err = run_cli(env, "timer", "start", "shot-12")
        assert code == cli.EXIT_OK and "already running" in out
        code, _out, err = run_cli(env, "timer", "start", "shot-13")  # one timer at a time: the daemon says no
        assert code == cli.EXIT_ERROR and "409" in err and "timer_running" in err
        code, out, _err = run_cli(env, "timer", "today")
        assert code == cli.EXIT_OK and "running: shot-12" in out
        clock.advance(minutes=12)
        code, out, err = run_cli(env, "timer", "stop", "shot-12")
        assert code == cli.EXIT_OK and "timer stopped: shot-12, 12.0 min recorded" in out, (out, err)
        code, _out, err = run_cli(env, "timer", "stop", "shot-12")
        assert code == cli.EXIT_ERROR and "timer_not_running" in err
        code, out, err = run_cli(env, "timer", "today")
        assert code == cli.EXIT_OK, (out, err)
        assert ": 12.0 min" in out.splitlines()[0] and "  shot-12: 12.0 min" in out and "running" not in out
    (rec,) = [r for r in Ledger(plate_env / "timer").records() if r["kind"] == "human_minutes"]
    assert rec["data"]["deliverable_id"] == "shot-12" and rec["data"]["minutes"] == 12.0


def test_timer_with_a_project_books_the_minutes_to_its_ledger(live_server, plate_env, settings):
    root = settings.projects_dir / "pj-timer"
    Project.create(root, "pj-timer", {"nda": False, "allowed_grades": ["C0", "C1", "C2"]})
    clock = Clock()
    with live_server(clock=clock) as (_s, url):
        env = env_with(PLATE_HOME=str(plate_env), PLATE_DAEMON_URL=url)
        assert run_cli(env, "timer", "start", "grade-3", "--project", str(root))[0] == cli.EXIT_OK  # a folder
        clock.advance(minutes=3, seconds=30)
        code, out, err = run_cli(env, "timer", "stop", "grade-3", "--project", "pj-timer")  # or an id
        assert code == cli.EXIT_OK and "3.5 min" in out, (out, err)
        code, out, _err = run_cli(env, "timer", "today", "--project", "pj-timer")
        assert code == cli.EXIT_OK and "in pj-timer: 3.5 min" in out and "  grade-3: 3.5 min" in out
        code, out, _err = run_cli(env, "timer", "today", "--project", "other")
        assert code == cli.EXIT_ERROR  # the daemon does not know that project
    (rec,) = [r for r in Ledger(root).records() if r["kind"] == "human_minutes"]
    assert rec["data"]["project_id"] == "pj-timer"


def test_timer_finds_the_daemon_by_plate_port(live_server, plate_env):
    with live_server() as (_s, url):
        port = url.rsplit(":", 1)[1]
        code, out, err = run_cli(env_with(PLATE_HOME=str(plate_env), PLATE_PORT=port), "timer", "today")
        assert code == cli.EXIT_OK and out.startswith("human minutes "), (out, err)


def test_timer_exits_4_when_no_daemon_answers(plate_env):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here once the socket is closed
    env = env_with(PLATE_HOME=str(plate_env), PLATE_DAEMON_URL=f"http://127.0.0.1:{port}")
    code, out, err = run_cli(env, "timer", "start", "shot-1")
    assert code == cli.EXIT_USAGE == 4 and out == "" and "no daemon answered" in err and str(port) in err


def test_timer_sends_the_token_to_a_loopback_daemon_only(plate_env, monkeypatch):
    def nope(*_a, **_k):
        raise AssertionError("a request was built for a non-loopback daemon")

    monkeypatch.setattr(cli.urllib.request, "build_opener", nope)
    for url in (
        "http://example.com:4610",
        "http://10.1.2.3:4610",
        "https://127.0.0.1:4610",
        "ftp://localhost",
    ):
        code, _out, err = run_cli(env_with(PLATE_HOME=str(plate_env), PLATE_DAEMON_URL=url), "timer", "today")
        assert code == cli.EXIT_USAGE and "loopback" in err, url


def test_timer_with_the_wrong_token_is_an_error_not_a_missing_daemon(live_server, plate_env, tmp_path):
    token = tmp_path / "token.txt"
    token.write_text("x" * 40, encoding="utf-8")
    with live_server() as (_s, url):
        env = env_with(PLATE_HOME=str(plate_env), PLATE_DAEMON_URL=url, PLATE_TOKEN_FILE=str(token))
        code, _out, err = run_cli(env, "timer", "today")
    assert code == cli.EXIT_ERROR and "401" in err and "token" in err


def test_timer_ignores_a_proxy_in_the_environment(live_server, plate_env, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # nothing there: a proxied call would fail
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    with live_server() as (_s, url):
        code, out, err = run_cli(env_with(PLATE_HOME=str(plate_env), PLATE_DAEMON_URL=url), "timer", "today")
    assert code == cli.EXIT_OK, (out, err)


def test_settings_for_the_timer_are_the_installs_own(plate_env):
    """The token the CLI sends is `Settings.load`'s, the daemon's own."""
    env = env_with(PLATE_HOME=str(plate_env))
    session = cli.Session(env=env, out=io.StringIO(), err=io.StringIO(), ask=lambda _p: "")
    assert session.settings.token == Settings.load(env).token
    assert cli.daemon_base(session) == "http://127.0.0.1:4610"
